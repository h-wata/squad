#!/usr/bin/env python3
# ruff: noqa: CPY001
"""start.sh の SQUAD_OWNED_PROJECTS マーカー自動整備テスト (SQUAD-210).

SQUAD_DRY_RUN=1 で settings/scaffold の pre-flight だけを実行させ、tmux には触れずに
.squad_session マーカーの書き込み/保護挙動を確認する。本番リポジトリの queue/ を巻き込ま
ないよう、start.sh + .claude/ を隔離した tmp ディレクトリへコピーして実行する
(tests/test_stop_sh.py と同じ手法)。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parent.parent


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / 'squad-root'
    root.mkdir()
    shutil.copy(REPO / 'start.sh', root / 'start.sh')
    shutil.copytree(REPO / '.claude', root / '.claude')
    return root


def _fake_tmux(tmp_path: Path, *, alive: bool) -> Path:
    """has-session が alive の生死を返す偽 tmux バイナリを作り、その bin dir を返す."""
    fake_bin = tmp_path / 'fake-tmux-bin'
    fake_bin.mkdir(exist_ok=True)
    fake_tmux = fake_bin / 'tmux'
    exit_code = 0 if alive else 1
    fake_tmux.write_text(f'#!/usr/bin/env bash\nexit {exit_code}\n')
    fake_tmux.chmod(0o755)
    return fake_bin


def _path_without_tmux(tmp_path: Path, path_value: str) -> str:
    """PATH 上の全実行ファイルを tmux だけ除いてシンボリックリンクした bin dir を作る.

    ディレクトリ単位で PATH から除外すると mkdir/dirname 等の必須コマンドも
    巻き添えで消えるため、ファイル単位で tmux だけを取り除く。
    """
    fake_bin = tmp_path / 'no-tmux-bin'
    fake_bin.mkdir(exist_ok=True)
    for entry in path_value.split(os.pathsep):
        d = Path(entry)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.name == 'tmux' or (fake_bin / f.name).exists():
                continue
            try:
                (fake_bin / f.name).symlink_to(f)
            except OSError:
                continue
    return str(fake_bin)


def _run_start(
    root: Path,
    workspace: Path,
    session: str,
    owned: str | None = None,
    *,
    tmux_alive: bool | None = None,
    strip_tmux_from_path: bool = False,
    tmp_path: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # 手元に ~/.config/squad/claude-oauth-token があっても拾わないよう、既定は存在しないパスにする
    env = {
        **os.environ,
        'SQUAD_SESSION': session,
        'SQUAD_DRY_RUN': '1',
        'SQUAD_CLAUDE_TOKEN_FILE': str(root / 'no-claude-token'),
    }
    env.pop('SQUAD_STATE_DIR', None)  # agents-<session>.json を本番の state に書かない
    if extra_env:
        env.update(extra_env)
    if owned is not None:
        env['SQUAD_OWNED_PROJECTS'] = owned
    else:
        env.pop('SQUAD_OWNED_PROJECTS', None)
    if tmux_alive is not None:
        assert tmp_path is not None
        fake_bin = _fake_tmux(tmp_path, alive=tmux_alive)
        env['PATH'] = f'{fake_bin}:{env["PATH"]}'
    bash_bin = shutil.which('bash') or 'bash'
    if strip_tmux_from_path:
        assert tmp_path is not None
        env['PATH'] = _path_without_tmux(tmp_path, env['PATH'])
    return subprocess.run(
        [bash_bin, str(root / 'start.sh'), str(workspace)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_no_owned_projects_env_leaves_markers_untouched(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess')

    assert r.returncode == 0, r.stderr
    assert not (pj / '.squad_session').exists()


def test_owned_projects_writes_marker_for_existing_project(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj')

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_creates_missing_project_dir(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='ghost')

    assert r.returncode == 0, r.stderr
    ghost = root / 'queue' / 'projects' / 'ghost'
    assert (ghost / 'tasks').is_dir()
    assert (ghost / 'reports').is_dir()
    assert (ghost / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_rejects_path_like_names(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='../escape, .hidden')

    assert r.returncode == 0, r.stderr
    assert not (root / 'queue' / 'escape').exists()
    assert not (root / 'queue' / 'projects' / '.hidden').exists()
    assert r.stdout.count('不正です') == 2


def test_owned_projects_does_not_overwrite_alive_session_marker(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('othersess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', tmux_alive=True, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'othersess'
    assert '既に' in r.stdout


def test_owned_projects_takes_over_stale_session_marker(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('deadsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', tmux_alive=False, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'
    assert '引き継ぎました' in r.stdout


def test_owned_projects_no_tmux_in_path_is_conservative(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('deadsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', strip_tmux_from_path=True, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'deadsess'
    assert '既に' in r.stdout


def test_owned_projects_multiple_comma_separated(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    (root / 'queue' / 'projects' / 'a').mkdir(parents=True)
    (root / 'queue' / 'projects' / 'b').mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='a, b')

    assert r.returncode == 0, r.stderr
    assert (root / 'queue' / 'projects' / 'a' / '.squad_session').read_text().strip() == 'testsess'
    assert (root / 'queue' / 'projects' / 'b' / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_same_value_marker_is_harmless(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('testsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj')

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'


# --- SQUAD_W3_AGENT (Worker 3 を Opencode で動かす試験運用) ---


def test_w3_agent_opencode_marks_dashboard_row(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_W3_AGENT': 'opencode'})

    assert r.returncode == 0, r.stderr
    row = [ln for ln in (root / 'dashboard.md').read_text().splitlines() if 'Worker 3' in ln]
    assert row and 'Opencode' in row[0], row


def test_w3_agent_defaults_to_claude(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess')

    assert r.returncode == 0, r.stderr
    row = [ln for ln in (root / 'dashboard.md').read_text().splitlines() if 'Worker 3' in ln]
    assert row and 'Claude' in row[0], row


def test_w3_agent_rejects_unknown_value(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_W3_AGENT': 'gemini'})

    assert r.returncode != 0
    assert 'SQUAD_W3_AGENT' in r.stderr


def test_every_instruction_placeholder_is_rendered_by_start_sh() -> None:
    """instructions/*.md の {PLACEHOLDER} が start.sh の render 引数に揃っているか.

    プレースホルダを足して render_prompt.py への KEY= 引数を足し忘れると、
    system prompt に "{WORKER_AGENT}" が literal で残るが誰も気付かない。
    """
    start_sh = (REPO / 'start.sh').read_text()
    # worker.md の {N} / {X} は N=1 等で個別に渡す・本文中の例示なので対象外。
    exempt = {'N', 'X'}
    for md in ('worker.md', 'dispatcher.md', 'worker-qwen.md'):
        text = (REPO / 'instructions' / md).read_text()
        keys = set(re.findall(r'\{([A-Z_][A-Z0-9_]*)\}', text)) - exempt
        for key in sorted(keys):
            assert f'{key}=' in start_sh, f'{md} の {{{key}}} を渡す render 引数が start.sh に無い'


def test_per_worker_agent_flags_are_independent(tmp_path: Path) -> None:
    """W1/W2/W3 を個別に切り替えられる (W2+W3 だけ Opencode にする等)."""
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(
        root,
        workspace,
        'testsess',
        extra_env={'SQUAD_W2_AGENT': 'opencode', 'SQUAD_W3_AGENT': 'opencode'},
    )

    assert r.returncode == 0, r.stderr
    rows = {
        n: next(ln for ln in (root / 'dashboard.md').read_text().splitlines() if f'Worker {n} ' in ln)
        for n in (1, 2, 3)
    }
    assert 'Claude' in rows[1], rows[1]
    assert 'Opencode' in rows[2], rows[2]
    assert 'Opencode' in rows[3], rows[3]


def test_w1_agent_rejects_unknown_value(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_W1_AGENT': 'gpt'})

    assert r.returncode != 0
    assert 'SQUAD_W1_AGENT' in r.stderr


def test_claude_local_agent_is_accepted(tmp_path: Path) -> None:
    """Claude Code ハーネス + ローカルモデルの worker を選べる."""
    root = _fake_root(tmp_path)
    shutil.copytree(REPO / 'config', root / 'config')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_W2_AGENT': 'claude-local'})

    assert r.returncode == 0, r.stderr
    row = next(ln for ln in (root / 'dashboard.md').read_text().splitlines() if 'Worker 2 ' in ln)
    assert 'Claude Code' in row, row


def test_claude_local_requires_worker_config_dir(tmp_path: Path) -> None:
    """config/claude-worker/settings.base.json が無ければ起動を止める.

    無いまま起動すると user 設定の ask ルールを拾い、worker が無言で止まる。
    """
    root = _fake_root(tmp_path)  # config/ をコピーしない
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_W3_AGENT': 'claude-local'})

    assert r.returncode != 0
    assert 'claude-worker' in r.stderr


def test_claude_token_file_requires_worker_config_dir(tmp_path: Path) -> None:
    """OAuth トークンを置くと claude worker も CLAUDE_CONFIG_DIR で ask を外すので、設定が無ければ止める."""
    root = _fake_root(tmp_path)  # config/ をコピーしない
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy\n')

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_CLAUDE_TOKEN_FILE': str(token)})

    assert r.returncode != 0
    assert 'claude-worker' in r.stderr


def test_claude_token_file_with_worker_config_starts(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    shutil.copytree(REPO / 'config', root / 'config')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy\n')

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_CLAUDE_TOKEN_FILE': str(token)})

    assert r.returncode == 0, r.stderr
    worker_dirs = [root / 'config/claude-worker/testsess' / f'w{n}' for n in (1, 2, 3)]
    base = json.loads((root / 'config/claude-worker/settings.base.json').read_text())
    for d in worker_dirs:
        cfg = json.loads((d / 'settings.json').read_text())
        assert cfg.pop('statusLine')['command'] == f'python3 {root / "scripts" / "statusline.py"}'
        assert cfg == base
    proj = json.loads((worker_dirs[0] / '.claude.json').read_text())['projects'][str(workspace)]
    assert proj['hasTrustDialogAccepted'] is True
    assert proj['hasClaudeMdExternalIncludesWarningShown'] is True


def test_blank_token_file_is_ignored(tmp_path: Path) -> None:
    """空白だけのトークンでは有効にしない (設定が無くても従来どおり起動する)."""
    root = _fake_root(tmp_path)  # config/ をコピーしない
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text(' \n\n')

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_CLAUDE_TOKEN_FILE': str(token)})

    assert r.returncode == 0, r.stderr


def _dry_run_settings(stdout: str, name: str) -> dict:
    line = next(ln for ln in stdout.splitlines() if ln.startswith(f'{name}='))
    return json.loads(line.split('=', 1)[1])


def test_dispatcher_and_worker_settings_get_squad_statusline(tmp_path: Path) -> None:
    """Dispatcher と worker の --settings に squad の statusLine を足す。settings.local.json 自体は変えない (#105)."""
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess')

    assert r.returncode == 0, r.stderr
    script = root / 'scripts' / 'statusline.py'
    disp = _dry_run_settings(r.stdout, 'DISPATCHER_SETTINGS_JSON')
    worker = _dry_run_settings(r.stdout, 'WORKER_SETTINGS_JSON')
    assert disp['statusLine']['type'] == 'command'
    assert disp['statusLine']['command'] == f'python3 {script} --name dispatcher'
    assert worker['statusLine']['command'] == f'python3 {script}'
    assert worker['disableRemoteControl'] is True
    assert 'disableRemoteControl' not in disp
    local = json.loads((root / '.claude' / 'settings.local.json').read_text())
    assert 'statusLine' not in local
    assert disp['hooks'] == local['hooks']


def test_settings_example_runs_on_event_for_user_prompt_submit(tmp_path: Path) -> None:
    """UserPromptSubmit も Stop と同じ形で on-event.sh を呼ぶ (ADR 0011 の 4.)."""
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess')

    assert r.returncode == 0, r.stderr
    hooks = json.loads((root / '.claude' / 'settings.local.json').read_text())['hooks']
    expected = [
        {
            'hooks': [{'command': f'{root}/scripts/hooks/on-event.sh', 'timeout': 5, 'type': 'command'}],
            'matcher': '',
        }
    ]
    assert hooks['UserPromptSubmit'] == expected
    assert hooks['Stop'] == expected


def test_worker_settings_keep_statusline_with_rc_enabled(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', extra_env={'SQUAD_ENABLE_RC': '1'})

    assert r.returncode == 0, r.stderr
    worker = _dry_run_settings(r.stdout, 'WORKER_SETTINGS_JSON')
    assert 'statusLine' in worker
    assert 'disableRemoteControl' not in worker


def test_agents_state_file_lists_worker_kinds(tmp_path: Path) -> None:
    """squad/state/agents-<session>.json に worker の種類を書く (ADR 0011 の 5.)."""
    root = _fake_root(tmp_path)
    shutil.copytree(REPO / 'config', root / 'config')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(
        root,
        workspace,
        'testsess',
        extra_env={'SQUAD_W2_AGENT': 'claude-local', 'SQUAD_W3_AGENT': 'opencode'},
    )

    assert r.returncode == 0, r.stderr
    agents = json.loads((root / 'squad' / 'state' / 'agents-testsess.json').read_text())
    assert agents == {'w1': 'claude', 'w2': 'claude-local', 'w3': 'opencode'}


def test_agents_state_file_is_rewritten_per_start(tmp_path: Path) -> None:
    """起動のたびに今の設定で書き直す。session ごとに別ファイル."""
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    _run_start(root, workspace, 'sq-a', extra_env={'SQUAD_W1_AGENT': 'opencode'})
    r = _run_start(root, workspace, 'sq-a')
    _run_start(root, workspace, 'sq-b', extra_env={'SQUAD_W3_AGENT': 'opencode'})

    assert r.returncode == 0, r.stderr
    state = root / 'squad' / 'state'
    assert json.loads((state / 'agents-sq-a.json').read_text()) == {'w1': 'claude', 'w2': 'claude', 'w3': 'claude'}
    assert json.loads((state / 'agents-sq-b.json').read_text()) == {'w1': 'claude', 'w2': 'claude', 'w3': 'opencode'}


def test_worker_config_dirs_are_per_session(tmp_path: Path) -> None:
    """Squad を複数立ち上げても、worker の CLAUDE_CONFIG_DIR は session ごとに分かれる."""
    root = _fake_root(tmp_path)
    shutil.copytree(REPO / 'config', root / 'config')
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy\n')

    for sess in ('sq-a', 'sq-b'):
        r = _run_start(root, workspace, sess, extra_env={'SQUAD_CLAUDE_TOKEN_FILE': str(token)})
        assert r.returncode == 0, r.stderr

    a = root / 'config/claude-worker/sq-a/w1/settings.json'
    b = root / 'config/claude-worker/sq-b/w1/settings.json'
    a.write_text('{"model": "opus"}')  # /model で書き換わった想定
    b_cfg = json.loads(b.read_text())
    assert b_cfg['model'] == json.loads((root / 'config/claude-worker/settings.base.json').read_text())['model']
    # statusLine は repo の statusline.py を絶対パスで指す
    assert b_cfg['statusLine'] == {'type': 'command', 'command': f'python3 {root / "scripts" / "statusline.py"}'}


# --- 起動行を短い 1 行で送る (SQUAD-325) ---
# 起動直後の pane は canonical mode で、1 行 4096 バイト目以降が捨てられる。dispatcher / w1-w3 の
# 起動行は 4KB を超えるため、send-keys では「. <起動用ファイル>」だけを送る。

_SEND_KEYS_LIMIT = 1024  # canonical mode の上限 4096 より十分小さい値で押さえる

_FAKE_TMUX_LOGGING = """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:2] == ['has-session']:
    sys.exit(1)
with open(os.environ['FAKE_TMUX_LOG'], 'a', encoding='utf-8') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
"""

# 起動用ファイルを source したときに呼ばれる偽 claude。引数と認証まわりの環境変数を書き出す。
_FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ['FAKE_CLAUDE_OUT'], 'w', encoding='utf-8') as f:
    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(),
               'config_dir': os.environ.get('CLAUDE_CONFIG_DIR'),
               'token': os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')}, f)
"""


def _live_root(tmp_path: Path) -> Path:
    """隔離 root を作る。SQUAD_DRY_RUN なしで tmux まで進める用で、watcher は何もしない stub にする."""
    root = _fake_root(tmp_path)
    for d in ('config', 'scripts', 'instructions'):
        shutil.copytree(REPO / d, root / d)
    watch = root / 'watch.sh'
    watch.write_text('#!/bin/bash\nexit 0\n')
    watch.chmod(0o755)
    return root


def _run_start_live(
    root: Path, workspace: Path, tmp_path: Path, *, token: Path | None, extra_env: dict[str, str] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    fake_bin = tmp_path / 'fake-tmux-log-bin'
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / 'tmux').write_text(_FAKE_TMUX_LOGGING)
    (fake_bin / 'tmux').chmod(0o755)
    log = tmp_path / 'tmux.log'
    env = {
        **os.environ,
        'SQUAD_SESSION': 'sq325t',
        'SQUAD_CLAUDE_TOKEN_FILE': str(token or (root / 'no-claude-token')),
        'SQUAD_OWNED_PROJECTS': 'pj',
        'FAKE_TMUX_LOG': str(log),
        'PATH': f'{fake_bin}:{os.environ["PATH"]}',
        **(extra_env or {}),
    }
    for k in ('SQUAD_DRY_RUN', 'SQUAD_STATE_DIR', 'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN'):
        env.pop(k, None)
    r = subprocess.run(
        [shutil.which('bash') or 'bash', str(root / 'start.sh'), str(workspace)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    calls = [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []
    return r, calls


def _send_keys(calls: list[list[str]]) -> dict[str, str]:
    """送り先 window 名から send-keys で送った文字列への dict を返す."""
    out = {}
    for c in calls:
        if c[0] == 'send-keys':
            out[c[c.index('-t') + 1].split(':', 1)[1]] = c[c.index('-t') + 2]
    return out


def _source_launch_file(line: str, tmp_path: Path) -> dict:
    """send-keys の 1 行を対話シェルと同じく bash で評価し、偽 claude が受け取った内容を返す."""
    fake_bin = tmp_path / 'fake-claude-bin'
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / 'claude').write_text(_FAKE_CLAUDE)
    (fake_bin / 'claude').chmod(0o755)
    out = tmp_path / 'claude-out.json'
    env = {**os.environ, 'PATH': f'{fake_bin}:{os.environ["PATH"]}', 'FAKE_CLAUDE_OUT': str(out)}
    for k in ('CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN'):
        env.pop(k, None)
    r = subprocess.run(
        [shutil.which('bash') or 'bash', '-c', line], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert r.returncode == 0, r.stderr
    return json.loads(out.read_text())


def test_launch_lines_are_short_for_every_agent_kind(tmp_path: Path) -> None:
    """どの worker の種類でも、send-keys で送る 1 行は canonical mode の上限より十分短い."""
    root = _live_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy-token\n')

    r, calls = _run_start_live(
        root,
        workspace,
        tmp_path,
        token=token,
        extra_env={'SQUAD_W2_AGENT': 'claude-local', 'SQUAD_W3_AGENT': 'opencode'},
    )

    assert r.returncode == 0, r.stderr
    sent = _send_keys(calls)
    assert set(sent) >= {'dispatcher', 'w1', 'w2', 'w3', 'w4'}
    launch_dir = root / 'squad' / 'state' / 'launch-sq325t'
    assert launch_dir.stat().st_mode & 0o777 == 0o700
    for window, line in sent.items():
        assert len(line.encode()) <= _SEND_KEYS_LIMIT, (window, len(line.encode()))
        if window in ('term', 'aux'):
            continue
        assert line == f'. {launch_dir / (window + ".sh")}', line
        f = launch_dir / f'{window}.sh'
        # claude-local の ANTHROPIC_AUTH_TOKEN が入るので本人だけが読める
        assert f.stat().st_mode & 0o777 == 0o600, window
        # 中身は元の起動行そのもの (4KB を超えるものがあるからファイルに逃がしている)
        assert f.read_text().splitlines()[1].startswith('cd '), window


def test_dispatcher_launch_with_token_uses_worker_config_dir(tmp_path: Path) -> None:
    """トークンがあれば Dispatcher も worker と同じ CLAUDE_CONFIG_DIR + OAuth トークンで起動する."""
    root = _live_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    token = tmp_path / 'token'
    token.write_text('dummy-token\n')

    r, calls = _run_start_live(root, workspace, tmp_path, token=token)

    assert r.returncode == 0, r.stderr
    line = _send_keys(calls)['dispatcher']
    launch_file = Path(line[2:])
    # トークン本体はファイルにも書かず、起動時に $(cat) で読む
    assert 'dummy-token' not in launch_file.read_text()
    got = _source_launch_file(line, tmp_path)
    assert got['config_dir'] == str(root / 'config' / 'claude-worker')
    assert got['token'] == 'dummy-token'
    assert got['cwd'] == str(root)
    assert got['argv'][got['argv'].index('--model') + 1] == 'opus'
    # system prompt (Worker 表を含む dispatcher.md) が最後まで渡っている
    prompt = got['argv'][got['argv'].index('--append-system-prompt') + 1]
    assert '| Worker 3 | w3 |' in prompt
    # 読み込んだら消える
    assert not launch_file.exists()
    # Dispatcher の config dir も初回確認を済ませてある (済んでいないとオンボーディング画面で止まる)
    state = json.loads((root / 'config' / 'claude-worker' / '.claude.json').read_text())
    assert state['hasCompletedOnboarding'] is True
    assert state['projects'][str(root)]['hasTrustDialogAccepted'] is True


def test_dispatcher_launch_without_token_keeps_default_config(tmp_path: Path) -> None:
    root = _live_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r, calls = _run_start_live(root, workspace, tmp_path, token=None)

    assert r.returncode == 0, r.stderr
    got = _source_launch_file(_send_keys(calls)['dispatcher'], tmp_path)
    assert got['config_dir'] is None
    assert got['token'] is None
    assert not (root / 'config' / 'claude-worker' / '.claude.json').exists()
