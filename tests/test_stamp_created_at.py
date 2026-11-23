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


OLD = 'created_at: "2026-11-01T09:00:00+09:00"\n'


def _existing(tmp_path: Path, text: str | bytes | None) -> Path:
    f = tmp_path / 'queue/projects/p/tasks/worker1.yaml'
    f.parent.mkdir(parents=True)
    if isinstance(text, bytes):
        f.write_bytes(text)
    elif text is not None:
        f.write_text(text)
    return f


def test_different_task_id_gets_new_created_at(tmp_path: Path) -> None:
    # worker{N}.yaml は新タスクで再利用される。前タスクの created_at を引き継がない
    f = _existing(tmp_path, 'task_id: T-0\n' + OLD)
    out = run(write(str(f), 'task_id: T-1\ncreated_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


def test_quoted_task_id_compares_equal(tmp_path: Path) -> None:
    f = _existing(tmp_path, 'task_id: "T-1"  \n' + OLD)
    out = run(write(str(f), 'task_id: T-1\ncreated_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == 'task_id: T-1\n' + OLD


def test_no_existing_file_gets_new_created_at(tmp_path: Path) -> None:
    out = run(write(str(tmp_path / 'queue/projects/p/tasks/worker1.yaml')), tmp_path)
    assert out['updatedInput']['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


def test_unreadable_or_broken_existing_gets_new_created_at(tmp_path: Path) -> None:
    # 既存ファイルの task_id が取れない (壊れた内容 / 読めない) ときは安全側で now に刷新する
    f = _existing(tmp_path, b'\xff\xfe not yaml\n' + OLD.encode())
    out = run(write(str(f)), tmp_path)
    assert out['updatedInput']['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


def test_new_content_without_task_id_gets_new_created_at(tmp_path: Path) -> None:
    f = _existing(tmp_path, 'task_id: T-1\n' + OLD)
    out = run(write(str(f), 'title: x\ncreated_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == f'title: x\ncreated_at: "{NOW}"\n'


def test_archived_existing_gets_new_created_at(tmp_path: Path) -> None:
    # 既存ファイルが tasks/archive/ へ移動済み (元パスに無い) なら既存ファイル無しと同じ
    arch = tmp_path / 'queue/projects/p/tasks/archive/worker1.yaml'
    arch.parent.mkdir(parents=True)
    arch.write_text('task_id: T-1\n' + OLD)
    out = run(write(str(tmp_path / 'queue/projects/p/tasks/worker1.yaml')), tmp_path)
    assert out['updatedInput']['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


def _keeps(tmp_path: Path, old_head: str, new_head: str = 'task_id: T-1\n') -> None:
    f = _existing(tmp_path, old_head + OLD)
    out = run(write(str(f), new_head + 'created_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == new_head + OLD


def test_trailing_comment_is_same_task_id(tmp_path: Path) -> None:
    _keeps(tmp_path, 'task_id: T-1 # prior\n')


def test_comment_on_new_content_is_same_task_id(tmp_path: Path) -> None:
    _keeps(tmp_path, 'task_id: T-1\n', 'task_id: T-1  # now\n')


def test_anchor_is_same_task_id(tmp_path: Path) -> None:
    _keeps(tmp_path, 'task_id: &current T-1\n')


def test_alias_is_same_task_id(tmp_path: Path) -> None:
    # アンカーを別キーで定義し task_id はエイリアスで参照する
    _keeps(tmp_path, 'base: &current T-1\ntask_id: *current\n')


def test_non_first_line_top_level_task_id_matches(tmp_path: Path) -> None:
    _keeps(tmp_path, 'title: x\ntask_id: T-1\n', 'title: y\ntask_id: "T-1"\n')


def test_nested_task_id_does_not_match(tmp_path: Path) -> None:
    f = _existing(tmp_path, 'meta:\n  task_id: T-1\n' + OLD)
    out = run(write(str(f), 'task_id: T-1\ncreated_at: "2099-01-01T00:00:00"\n'), tmp_path)
    assert out['updatedInput']['content'] == f'task_id: T-1\ncreated_at: "{NOW}"\n'


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
