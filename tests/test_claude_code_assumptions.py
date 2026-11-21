#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad が頼る Claude Code の挙動の前提を、設定ファイルの形から検出する静的テスト (SQUAD-351).

モデルは呼ばない。前提が実機で今も成り立つかは scripts/smoke-claude-code-assumptions.sh で確かめる。

- (a) permission ルールは Write(path) では照合されず Edit(path) だけが効く。
  squad の設定に Write( ルールを書いても効かないので、書かれていたら落とす。
- (d) Dispatcher / worker 用 config dir の permissions.ask は空でなければならない
  (グローバル ~/.claude/settings.json の ask は project の allow と auto mode より優先され、
  tmux の pane にはそれを承認する人がいないため)。
- (b) worker の claude は CLAUDE_CODE_OAUTH_TOKEN を子プロセスに渡さず CLAUDE_CONFIG_DIR だけが残る。
  start.sh が起動行で CLAUDE_CONFIG_DIR を与え、トークンを argv に出さないことを確かめる。

検査対象が 0 件のときは黙って pass させず fail にする。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from test_start_sh import (
    REPO,
    _dry_run_settings,
    _fake_root,
    _live_root,
    _run_start,
    _run_start_live,
    _send_keys,
    _source_launch_file,
)

SESSION = 'cca351'
PERMISSION_LISTS = ('allow', 'ask', 'deny')


def _rules(settings: dict) -> list[str]:
    """Permissions にある全ルール文字列を集めて返す."""
    perms = settings.get('permissions', {})
    return [rule for key in PERMISSION_LISTS for rule in perms.get(key, [])]


def _write_rules(settings: dict) -> list[str]:
    return [rule for rule in _rules(settings) if rule.startswith('Write(')]


def _assert_no_write_rule(settings: dict, origin: str) -> None:
    # 対象が空だと「Write( が無い」が自明に成り立つので、ルールが 1 件以上あることを先に確かめる
    assert _rules(settings), f'{origin}: permission ルールが 0 件 (検査対象が見つからない)'
    assert _write_rules(settings) == [], f'{origin}: Write( ルールは効かない。Edit( を使うこと'


def _example() -> dict:
    return json.loads((REPO / '.claude' / 'settings.local.json.example').read_text())


def _with_token(tmp_path: Path) -> tuple[Path, Path, Path]:
    """トークンファイルを置いた隔離 root (config/ 付き)・workspace・トークンファイルを返す."""
    root = _fake_root(tmp_path)
    shutil.copytree(REPO / 'config', root / 'config')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy\n')
    return root, workspace, token


def test_settings_example_has_no_write_rule() -> None:
    _assert_no_write_rule(_example(), '.claude/settings.local.json.example')


def test_local_settings_has_no_write_rule_when_present() -> None:
    """.claude/settings.local.json は gitignore 対象で CI には無い。あるときだけ検査し、無ければ skip する.

    skip でも .example 側の検査 (対象 0 件で fail) は常に走るので、検査が丸ごと抜け落ちることはない。
    """
    path = REPO / '.claude' / 'settings.local.json'
    if not path.exists():
        pytest.skip('.claude/settings.local.json が無い (gitignore 対象)')
    _assert_no_write_rule(json.loads(path.read_text()), str(path))


def test_start_sh_generated_settings_have_no_write_rule(tmp_path: Path) -> None:
    """Start.sh が生成する settings.local.json と、Dispatcher / worker に渡す --settings に Write( が無い."""
    root = _fake_root(tmp_path)
    # 手元の settings.local.json に引きずられないよう、example から作り直させる
    (root / '.claude' / 'settings.local.json').unlink(missing_ok=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, SESSION)

    assert r.returncode == 0, r.stderr
    _assert_no_write_rule(
        json.loads((root / '.claude' / 'settings.local.json').read_text()), 'generated settings.local.json'
    )
    for name in ('DISPATCHER_SETTINGS_JSON', 'WORKER_SETTINGS_JSON'):
        _assert_no_write_rule(_dry_run_settings(r.stdout, name), name)


def test_worker_settings_base_has_no_write_rule() -> None:
    _assert_no_write_rule(
        json.loads((REPO / 'config' / 'claude-worker' / 'settings.base.json').read_text()),
        'config/claude-worker/settings.base.json',
    )


def test_dispatcher_and_worker_config_dirs_have_empty_ask(tmp_path: Path) -> None:
    """Dispatcher / worker 用 config dir の settings.json は permissions.ask が空で、Write( も無い."""
    root, workspace, token = _with_token(tmp_path)

    r = _run_start(root, workspace, SESSION, extra_env={'SQUAD_CLAUDE_TOKEN_FILE': str(token)})

    assert r.returncode == 0, r.stderr
    base = root / 'config' / 'claude-worker' / SESSION
    dirs = [base / name for name in ('dispatcher', 'w1', 'w2', 'w3')]
    for d in dirs:
        path = d / 'settings.json'
        assert path.is_file(), f'{path} が生成されていない (検査対象が見つからない)'
        settings = json.loads(path.read_text())
        # キー自体が消えていても「空」と読まず fail にする。ask が空であることを明示的に積んでおく契約
        assert settings.get('permissions', {}).get('ask') == [], f'{path}: permissions.ask が空でない'
        _assert_no_write_rule(settings, str(path))


def test_launch_gives_every_claude_window_its_config_dir_and_hides_token(tmp_path: Path) -> None:
    """Dispatcher と w1-w3 の起動行は CLAUDE_CONFIG_DIR を与え、トークンを argv に出さない."""
    root = _live_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('oauth-secret-351\n')

    r, calls = _run_start_live(root, workspace, tmp_path, token=token)

    assert r.returncode == 0, r.stderr
    sent = _send_keys(calls)
    assert {'dispatcher', 'w1', 'w2', 'w3'} <= set(sent), f'起動した window: {sorted(sent)}'
    for window in ('dispatcher', 'w1', 'w2', 'w3'):
        got = _source_launch_file(sent[window], tmp_path)
        assert got['config_dir'] == str(root / 'config' / 'claude-worker' / 'sq325t' / window)
        assert got['token'] == 'oauth-secret-351'
        assert not any('oauth-secret-351' in a for a in got['argv']), window
        assert 'oauth-secret-351' not in sent[window], window


def test_smoke_script_removes_its_temp_dir_and_hides_token_under_xtrace(tmp_path: Path) -> None:
    """スモークは終了時に mktemp -d の dir を消し、bash -x でもトークンの値を出さない (SQUAD-353 NB1)."""
    dummy = 'dummy-token-DO-NOT-LEAK-12345'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'claude'
    fake.write_text('#!/bin/sh\nexit 0\n')  # 実 claude は呼ばない。何も書かないので各項目は FAIL でよい
    fake.chmod(0o755)
    token = tmp_path / 'token'
    token.write_text(dummy + '\n')
    tmp_root = tmp_path / 'tmp'
    tmp_root.mkdir()
    env = {
        **os.environ,
        'PATH': f'{bin_dir}:{os.environ["PATH"]}',
        'SQUAD_CLAUDE_TOKEN_FILE': str(token),
        'TMPDIR': str(tmp_root),
    }

    proc = subprocess.run(
        ['bash', '-x', str(REPO / 'scripts' / 'smoke-claude-code-assumptions.sh')],
        env=env,
        capture_output=True,
        text=True,
    )

    assert 'PASS' in proc.stdout or 'FAIL' in proc.stdout, proc.stderr  # 前提確認を抜けて本体まで走った
    assert list(tmp_root.iterdir()) == [], '一時 dir が残っている'
    assert dummy not in proc.stdout + proc.stderr
