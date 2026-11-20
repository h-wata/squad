# ruff: noqa: CPY001
"""scripts/verify-task.sh が claude-local worker の env を verifier に引き継がないこと (#69)."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify-task.sh'

# 偽の claude: 受け取った env の名前だけを記録し、prompt に書かれた出力先へ verdict を書く
FAKE_CLAUDE = """#!/usr/bin/env bash
env | cut -d= -f1 > "$FAKE_ENV_OUT"  # 値は残さない (API キー等)
# トークンは値ではなく「期待値と一致したか」だけを記録する
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN+x}" ]; then echo unset > "$FAKE_TOKEN_OUT"
elif [ "$CLAUDE_CODE_OAUTH_TOKEN" = "$FAKE_TOKEN_EXPECT" ]; then echo match > "$FAKE_TOKEN_OUT"
else echo other > "$FAKE_TOKEN_OUT"; fi
verdict="$(sed -n 's/^- verdict の出力先 (このパスに Write すること): //p')"
printf 'result: pass\\n' > "$verdict"
"""

TOKEN_FROM_FILE = 'sk-ant-oat-from-file-SECRET'
TOKEN_FROM_ENV = 'sk-ant-oat-from-env-SECRET'

LOCAL_ENV = {
    'SQUAD_WORKER_AGENT': 'claude-local',
    'ANTHROPIC_BASE_URL': 'http://lan-litellm:4000',
    'ANTHROPIC_AUTH_TOKEN': 'sk-local-dummy',
    'ANTHROPIC_MODEL': 'qwen38-flash-next',
    'ANTHROPIC_SMALL_FAST_MODEL': 'qwen38-flash-next',
    'CLAUDE_CONFIG_DIR': '/tmp/claude-worker',
    'CLAUDE_CODE_MAX_CONTEXT_TOKENS': '262144',
}


def _run(tmp_path: Path, extra_env: dict[str, str], bash_args: tuple[str, ...] = ()) -> set[str]:
    """偽の claude で verify-task.sh を走らせ、claude が受け取った env 名を返す."""
    seen, _ = _run_full(tmp_path, extra_env, bash_args)
    return seen


def _run_full(
    tmp_path: Path, extra_env: dict[str, str], bash_args: tuple[str, ...] = ()
) -> tuple[set[str], subprocess.CompletedProcess[str]]:
    """_run の本体。proc も返す."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'claude'
    fake.write_text(FAKE_CLAUDE)
    fake.chmod(0o755)
    tasks = tmp_path / 'queue' / 'projects' / 'p' / 'tasks'
    tasks.mkdir(parents=True)
    (tasks.parent / 'reports').mkdir()
    task_yaml = tasks / 'worker2.yaml'
    task_yaml.write_text('task_id: T-1\n')
    worktree = tmp_path / 'wt'
    worktree.mkdir()
    env_out = tmp_path / 'env.txt'

    base = {k: v for k, v in os.environ.items() if k not in ('CLAUDE_CODE_OAUTH_TOKEN', 'SQUAD_CLAUDE_TOKEN_FILE')}
    env = {
        **base,
        'HOME': str(tmp_path / 'home'),
        **extra_env,
        'SQUAD_VERIFIER_CMD': str(fake),
        'FAKE_ENV_OUT': str(env_out),
        'FAKE_TOKEN_OUT': str(tmp_path / 'token.txt'),
        'FAKE_TOKEN_EXPECT': TOKEN_FROM_FILE,
    }
    proc = subprocess.run(
        ['bash', *bash_args, str(SCRIPT), str(task_yaml), str(worktree)], env=env, capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stderr
    assert 'result=pass' in proc.stdout
    return set(env_out.read_text().split()), proc


def _token_seen(tmp_path: Path) -> str:
    return (tmp_path / 'token.txt').read_text().strip()


def test_claude_local_worker_env_is_dropped(tmp_path: Path) -> None:
    seen = _run(tmp_path, LOCAL_ENV)
    assert not seen & LOCAL_ENV.keys()
    assert 'FAKE_ENV_OUT' in seen  # 関係ない env まで消していない


def test_other_workers_keep_their_gateway_env(tmp_path: Path) -> None:
    """claude-local でなければ、利用者自身の ANTHROPIC_BASE_URL 等はそのまま渡す."""
    gateway = {'ANTHROPIC_BASE_URL': 'https://corp-gateway', 'ANTHROPIC_AUTH_TOKEN': 'x'}
    seen = _run(tmp_path, gateway)
    assert gateway.keys() <= seen


def _token_file(tmp_path: Path, content: str) -> dict[str, str]:
    f = tmp_path / 'tokfile'
    f.write_text(content)
    return {'SQUAD_CLAUDE_TOKEN_FILE': str(f)}


def test_token_is_filled_from_file_without_leaking(tmp_path: Path) -> None:
    """環境変数に無ければファイルから補う。値は stdout/stderr (set -x 含む) に出ない."""
    _, proc = _run_full(tmp_path, _token_file(tmp_path, TOKEN_FROM_FILE + '\n'), bash_args=('-x',))
    assert _token_seen(tmp_path) == 'match'
    assert TOKEN_FROM_FILE not in proc.stdout + proc.stderr


def test_existing_token_env_wins(tmp_path: Path) -> None:
    extra = {**_token_file(tmp_path, TOKEN_FROM_FILE), 'CLAUDE_CODE_OAUTH_TOKEN': TOKEN_FROM_ENV}
    _run(tmp_path, extra)
    assert _token_seen(tmp_path) == 'other'


def test_blank_or_missing_token_file_is_ignored(tmp_path: Path) -> None:
    _run(tmp_path, _token_file(tmp_path, ' \n'))
    assert _token_seen(tmp_path) == 'unset'


def test_claude_local_does_not_get_token(tmp_path: Path) -> None:
    _run(tmp_path, {**LOCAL_ENV, **_token_file(tmp_path, TOKEN_FROM_FILE)})
    assert _token_seen(tmp_path) == 'unset'
