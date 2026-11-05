# ruff: noqa: CPY001
"""scripts/verify-task.sh が claude-local worker の env を verifier に引き継がないこと (#69)."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify-task.sh'

# 偽の claude: 受け取った env を記録し、prompt に書かれた出力先へ verdict を書く
FAKE_CLAUDE = """#!/usr/bin/env bash
env > "$FAKE_ENV_OUT"
verdict="$(sed -n 's/^- verdict の出力先 (このパスに Write すること): //p')"
printf 'result: pass\\n' > "$verdict"
"""

LOCAL_ENV = {
    'ANTHROPIC_BASE_URL': 'http://lan-litellm:4000',
    'ANTHROPIC_AUTH_TOKEN': 'sk-local-dummy',
    'ANTHROPIC_MODEL': 'qwen38-flash-next',
    'ANTHROPIC_SMALL_FAST_MODEL': 'qwen38-flash-next',
    'CLAUDE_CONFIG_DIR': '/tmp/claude-worker',
    'CLAUDE_CODE_MAX_CONTEXT_TOKENS': '262144',
}


def test_claude_verifier_drops_local_model_env(tmp_path: Path) -> None:
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

    env = {**os.environ, **LOCAL_ENV, 'SQUAD_VERIFIER_CMD': str(fake), 'FAKE_ENV_OUT': str(env_out)}
    proc = subprocess.run(
        ['bash', str(SCRIPT), str(task_yaml), str(worktree)], env=env, capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stderr
    assert 'result=pass' in proc.stdout
    seen = {line.split('=', 1)[0] for line in env_out.read_text().splitlines() if '=' in line}
    assert not seen & LOCAL_ENV.keys()
    assert 'FAKE_ENV_OUT' in seen  # 関係ない env まで消していない
