#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts/hooks/stamp_created_at.py のテスト (#87).

Hook を実走して stdin/stdout JSON を検証する。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

HOOK = Path(__file__).resolve().parent.parent / 'scripts' / 'hooks' / 'stamp_created_at.py'
NOW = '2026-11-06T15:30:00+09:00'
BODY = 'task_id: T-1\ncreated_at: "2020-01-01T00:00:00"\n'


def run(payload: dict | None, cwd: Path, raw: str | None = None) -> dict | None:
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw if raw is not None else json.dumps({**payload, 'cwd': str(cwd)}),
        capture_output=True,
        text=True,
        env={**os.environ, 'SQUAD_STAMP_NOW': NOW},
    )
    assert r.returncode == 0
    return json.loads(r.stdout)['hookSpecificOutput'] if r.stdout else None


def write(path: str | Path, content: str = BODY, tool: str = 'Write') -> dict:
    return {'tool_name': tool, 'tool_input': {'file_path': path, 'content': content}}


def test_new_task_yaml_is_stamped(tmp_path: Path) -> None:
    out = run(write('queue/projects/p/tasks/worker1.yaml'), tmp_path)
    assert 'permissionDecision' not in out  # 通常の権限フローを変えない
    ti = out['updatedInput']
    assert ti['file_path'] == 'queue/projects/p/tasks/worker1.yaml'
    assert ti['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


def test_rewrite_keeps_existing_created_at(tmp_path: Path) -> None:
    f = tmp_path / 'queue/projects/p/tasks/worker1.yaml'
    f.parent.mkdir(parents=True)
    f.write_text('task_id: T-1\ncreated_at: "2026-11-01T09:00:00+09:00"\n')
    out = run(write(str(f), 'task_id: T-1\ntitle: x\ncreated_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == 'task_id: T-1\ntitle: x\ncreated_at: "2026-11-01T09:00:00+09:00"\n'


def test_edit_and_other_paths_untouched(tmp_path: Path) -> None:
    assert run(write('queue/projects/p/tasks/w.yaml', tool='Edit'), tmp_path) is None
    for p in (
        'queue/projects/p/reports/w.yaml',
        'queue/projects/p/tasks/archive/w.yaml',
        'notes/tasks/w.yaml',
        'queue/projects/p/tasks/w.txt',
    ):
        assert run(write(p), tmp_path) is None


def test_no_created_at_or_bad_input_passes_through(tmp_path: Path) -> None:
    assert run(write('queue/projects/p/tasks/w.yaml', 'task_id: T-1\n'), tmp_path) is None
    assert run(None, tmp_path, raw='not json') is None
    assert run(None, tmp_path, raw='{"tool_name": "Write"}') is None


def test_only_line_start_created_at_replaced(tmp_path: Path) -> None:
    body = 'description: |\n  created_at: "x"\ncreated_at: "2020-01-01"\n'
    out = run(write('queue/projects/p/tasks/w.yaml', body), tmp_path)
    assert out['updatedInput']['content'] == f'description: |\n  created_at: "x"\ncreated_at: "{NOW}"\n'
