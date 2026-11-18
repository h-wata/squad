#!/usr/bin/env python3
# ruff: noqa: CPY001
"""bin/squad が Python のサブコマンドを squad/squad.py に渡す (#99).

本物の squad.py や tmux を触らないよう、bin/squad を tmp_path にコピーし、
引数と SQUAD_SESSION を JSON で書き出すだけの偽 squad.py を隣に置いて叩く。
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_SQUAD_PY = """import json, os, sys
print(json.dumps({'argv': sys.argv[1:], 'session': os.environ.get('SQUAD_SESSION')}))
"""
DELEGATED = ['ls', 'assign', 'dashboard', 'ledger', 'notify', 'graph', 'trace', 'web']


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'bin').mkdir(parents=True)
    (root / 'squad').mkdir()
    shutil.copy2(REPO / 'bin' / 'squad', root / 'bin' / 'squad')
    (root / 'squad' / 'squad.py').write_text(FAKE_SQUAD_PY)
    return root


def _run(
    cmd: Path, *args: str, cwd: Path, session: str | None = None, piddir: Path | None = None
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != 'SQUAD_SESSION'}
    if session is not None:
        env['SQUAD_SESSION'] = session
    # 本物の tmux server を見ないよう socket の置き場所を cwd に向ける
    env['TMUX_TMPDIR'] = str(cwd)
    env.pop('TMUX', None)
    if piddir is not None:
        env['SQUAD_PIDFILE_DIR'] = str(piddir)
    return subprocess.run([str(cmd), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('sub', DELEGATED)
def test_python_subcommands_are_delegated(fake_root: Path, tmp_path: Path, sub: str) -> None:
    cwd = tmp_path / 'elsewhere'
    cwd.mkdir()
    r = _run(fake_root / 'bin' / 'squad', sub, 'a b', '--flag', cwd=cwd, session='scratch-s')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {'argv': [sub, 'a b', '--flag'], 'session': 'scratch-s'}


def test_delegation_works_through_symlink(fake_root: Path, tmp_path: Path) -> None:
    link_dir = tmp_path / 'home' / '.local' / 'bin'
    link_dir.mkdir(parents=True)
    (link_dir / 'squad').symlink_to(fake_root / 'bin' / 'squad')
    empty = tmp_path / 'nopids'
    empty.mkdir()
    r = _run(link_dir / 'squad', 'graph', 'status', cwd=tmp_path, piddir=empty)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {'argv': ['graph', 'status'], 'session': None}


def test_status_stays_in_bash(fake_root: Path, tmp_path: Path) -> None:
    r = _run(fake_root / 'bin' / 'squad', 'status', cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert '"argv"' not in r.stdout
    assert f'SQUAD_ROOT: {fake_root}' in r.stdout
    assert 'WATCHER' in r.stdout


class PidDir:
    """一意名の pidfile を置く専用ディレクトリ。生きた pid (sleep) を pidfile に書く."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.procs: list[subprocess.Popen] = []

    def make(self, name: str) -> None:
        p = subprocess.Popen(['sleep', '30'])
        self.procs.append(p)
        (self.path / f'{name}-watch.pid').write_text(str(p.pid))


@pytest.fixture
def piddir(tmp_path: Path) -> Iterator[PidDir]:
    d = tmp_path / 'pids'
    d.mkdir()
    pd = PidDir(d)
    yield pd
    for p in pd.procs:
        p.terminate()
        p.wait()
    for f in d.glob('*-watch.pid'):
        f.unlink()


def _name() -> str:
    return f'sq101test-{uuid.uuid4().hex[:8]}'


def test_single_running_session_is_auto_selected(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    name = _name()
    piddir.make(name)
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, piddir=piddir.path)
    assert json.loads(r.stdout)['session'] == name
    # log も同じセッションのログを見に行く
    r = _run(fake_root / 'bin' / 'squad', 'log', cwd=tmp_path, piddir=piddir.path)
    assert f'/tmp/{name}-watch.log' in r.stderr


def test_explicit_session_wins(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    piddir.make(_name())
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, session='explicit-s', piddir=piddir.path)
    assert json.loads(r.stdout)['session'] == 'explicit-s'


def test_two_sessions_keep_default_and_warn(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    a, b = _name(), _name()
    piddir.make(a)
    piddir.make(b)
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, piddir=piddir.path)
    assert json.loads(r.stdout)['session'] is None
    assert a in r.stderr and b in r.stderr
    assert 'SQUAD_SESSION=<name>' in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1


def _assign_dry_run(tmp_path: Path, task_body: str) -> str:
    task = tmp_path / 'worker2.yaml'
    task.write_text(task_body)
    r = _run(REPO / 'bin' / 'squad', 'assign', 'w2', str(task), '--dry-run', cwd=tmp_path, session='scratch-s')
    assert r.returncode == 0, r.stderr
    return next(ln for ln in r.stdout.splitlines() if ln.startswith('[assign] cmd:'))


def test_assign_does_not_pass_keep_context_by_default(tmp_path: Path) -> None:
    cmd = _assign_dry_run(tmp_path, 'task_id: T\nagent: claude\ncontext:\n  workspace: /x\n')
    assert '--keep-context' not in cmd


def test_assign_passes_keep_context_when_task_yaml_says_true(tmp_path: Path) -> None:
    cmd = _assign_dry_run(tmp_path, 'task_id: T\nagent: claude\ncontext:\n  keep_context: true\n')
    assert '--keep-context' in cmd


@pytest.mark.parametrize('value', ['false', 'no'])
def test_assign_ignores_keep_context_unless_true(tmp_path: Path, value: str) -> None:
    cmd = _assign_dry_run(tmp_path, f'task_id: T\nagent: claude\ncontext:\n  keep_context: {value}\n')
    assert '--keep-context' not in cmd


def test_assign_ignores_keep_context_outside_context(tmp_path: Path) -> None:
    cmd = _assign_dry_run(tmp_path, 'task_id: T\nagent: claude\nkeep_context: true\n')
    assert '--keep-context' not in cmd


def test_assign_passes_keep_context_to_codex_as_no_new(tmp_path: Path) -> None:
    cmd = _assign_dry_run(tmp_path, 'task_id: T\nagent: codex\ncontext:\n  keep_context: true\n')
    assert '--keep-context' in cmd


def _check_item17(tmp_path: Path, body: str) -> str:
    """Scripts/check_task_yaml.py の項目 17 の結果 (OK / NG)."""
    sys.path.insert(0, str(REPO / 'scripts'))
    from check_task_yaml import check_task_yaml

    task = tmp_path / 'worker2.yaml'
    task.write_text('task_id: T\nagent: claude\n' + body)
    return next(r.status for r in check_task_yaml(task, {}) if r.item == 17)


# 検査が OK にする値は、squad assign の実行時の意味と一致する (true は付く、false とキー無しは付かない)。
# 検査が NG にする値 (yes / True / "true") は実行側がどう判定してもよい: 検査が止めるので不一致は起きない。
@pytest.mark.parametrize(
    ('body', 'expect_flag'),
    [
        ('context:\n  keep_context: true\n', True),
        ('context:\n  keep_context: false\n', False),
        ('context:\n  workspace: /x\n', False),
        ('', False),
    ],
)
def test_values_accepted_by_check_match_assign(tmp_path: Path, body: str, expect_flag: bool) -> None:
    assert _check_item17(tmp_path, body) == 'OK'
    assert ('--keep-context' in _assign_dry_run(tmp_path, 'task_id: T\nagent: claude\n' + body)) is expect_flag


@pytest.mark.parametrize('raw', ['yes', 'True', '"true"', 'on'])
def test_values_rejected_by_check_are_not_silently_accepted(tmp_path: Path, raw: str) -> None:
    body = f'context:\n  keep_context: {raw}\n'
    assert _check_item17(tmp_path, body) == 'NG'
