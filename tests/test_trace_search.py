#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/trace_search.py (worker transcript の横断検索, Issue #56) のテスト.

tmp に queue と ~/.claude/projects 相当を作り、measure_task_cost の探索先だけ差し替える.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
import trace_search  # noqa: E402

SID = '11111111-1111-4111-8111-111111111111'
SID_APPROX = '22222222-2222-4222-8222-222222222222'
SID_MISSING = '44444444-4444-4444-8444-444444444444'
TOKEN = 'ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # pragma: allowlist secret


def _jsonl(path: Path, ts: str, extra: list[dict] | None = None) -> None:
    rows = [
        {'type': 'assistant', 'timestamp': ts, 'message': {'content': [{'type': 'text', 'text': 'まず build する'}]}},
        {
            'type': 'assistant',
            'timestamp': ts,
            'message': {'content': [{'type': 'tool_use', 'id': 'tu1', 'name': 'Bash', 'input': {'command': 'make'}}]},
        },
        {
            'type': 'user',
            'timestamp': ts,
            'message': {
                'content': [
                    {'type': 'tool_result', 'tool_use_id': 'tu1', 'is_error': True, 'content': f'make failed {TOKEN}'}
                ]
            },
        },
        {
            'type': 'assistant',
            'timestamp': ts,
            'message': {'content': [{'type': 'tool_use', 'id': 'tu2', 'name': 'Read', 'input': {'file_path': 'a'}}]},
        },
        {
            'type': 'user',
            'timestamp': ts,
            'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu2', 'content': [{'text': 'make ok'}]}]},
        },
    ]
    rows += extra or []
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(r) for r in rows) + '\nnot json\n')


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """T1: session_id あり / T2: session_id 無し (近似) / T3: transcript 無し."""
    projects = tmp_path / 'claude_projects'
    main = tmp_path / 'main'
    monkeypatch.setattr(trace_search.mtc, 'CLAUDE_PROJECTS', projects)
    monkeypatch.setattr(trace_search.mtc, 'QUEUE_ROOT', main)
    _jsonl(projects / 'ws1' / f'{SID}.jsonl', '2026-10-01T00:10:00Z')
    _jsonl(projects / str(main).replace('/', '-') / f'{SID_APPROX}.jsonl', '2026-10-02T00:10:00Z')

    pj = tmp_path / 'queue' / 'projects' / 'pj'
    (pj / 'tasks').mkdir(parents=True)
    (pj / 'reports').mkdir()
    t2 = pj / 'tasks' / 'worker2.yaml'
    t2.write_text('task_id: T2\n')
    start = 1_790_899_200  # 2026-10-02T00:00:00Z
    os.utime(t2, (start, start))
    t1 = pj / 'tasks' / 'worker1.yaml'
    t1.write_text('task_id: T1\n')
    start1 = 1_790_812_800  # 2026-10-01T00:00:00Z (exact は窓の両端が要る)
    os.utime(t1, (start1, start1))
    (pj / 'reports' / 'worker1_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nworker: worker1\nsession_id: "{SID}"\ncompleted_at: "2026-10-01T01:00:00Z"\n'
    )
    (pj / 'reports' / 'worker2_report.yaml').write_text(
        'task_id: T2\nstatus: blocked\nworker: worker2\ncompleted_at: "2026-10-02T01:00:00Z"\n'
    )
    (pj / 'reports' / 'worker3_report.yaml').write_text('task_id: T3\nstatus: completed\n')
    return tmp_path / 'queue'


def test_search_exact_approx_and_missing(queue: Path) -> None:
    srcs, missing = trace_search.sources(queue)
    assert missing == ['pj/T3']
    hits = trace_search.search(srcs, 'make')
    lines = [trace_search.fmt(e, 'make') for e in hits]
    assert any(line.startswith(f'{SID}:3.0   pj/T1 worker1') for line in lines)
    approx = next(line for line in lines if line.startswith(f'{SID_APPROX}:3.0 ~ pj/T2 worker2'))  # 近似は ~ 付き
    # 出力の先頭の ID をそのまま get に渡せる (近似の transcript でも)
    assert 'make failed' in '\n'.join(trace_search.get(approx.split()[0]))
    assert 'make failed' in '\n'.join(trace_search.get(f'~{SID_APPROX}:3.0'))  # 旧形式の ~ 付きも受ける


def test_filters(queue: Path) -> None:
    srcs, _ = trace_search.sources(queue, project='pj', task='T1')
    errors = trace_search.search(srcs, '', errors_only=True)
    assert [(e.trace_id, e.tool, e.is_error) for e in errors] == [(f'{SID}:3.0', 'Bash', True)]
    assert [e.trace_id for e in trace_search.search(srcs, 'make', tool='Read')] == [f'{SID}:5.0']
    assert [e.tool for e in trace_search.search(srcs, 'BUILD')] == ['text']


def test_output_is_masked(queue: Path) -> None:
    srcs, _ = trace_search.sources(queue, task='T1')
    assert all(TOKEN not in trace_search.fmt(e) for e in trace_search.search(srcs, ''))
    assert trace_search.search(srcs, TOKEN) == []  # マスク前の secret では引けない
    detail = '\n'.join(trace_search.get(f'{SID}:3', context=2))
    assert 'make failed ghp_<redacted>' in detail
    assert TOKEN not in detail
    assert '"command": "make"' in detail  # tool_result に対応する入力も出る


def test_approx_window_excludes_outside(queue: Path) -> None:
    report = queue / 'projects' / 'pj' / 'reports' / 'worker2_report.yaml'
    report.write_text('task_id: T2\nstatus: blocked\ncompleted_at: "2026-10-02T00:05:00Z"\n')  # 窓の外
    srcs, _ = trace_search.sources(queue, task='T2')
    assert trace_search.search(srcs, '') == []


def test_get_unknown_trace_id(queue: Path) -> None:
    assert 'transcript が見つかりません' in trace_search.get('nope:1')[0]


def test_exact_session_is_cut_by_task_window(queue: Path) -> None:
    """同じ session で T0 → T1 と続けて処理した。T1 の検索に T0 の分 (task 発行前) は出さない."""
    pj = queue / 'projects' / 'pj'
    t1 = pj / 'tasks' / 'worker1.yaml'
    t1.write_text('task_id: T1\n')
    start = 1_790_813_400  # 2026-10-01T00:10:00Z (fixture の行と同時刻)
    os.utime(t1, (start, start))
    (pj / 'reports' / 'worker1_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nsession_id: "{SID}"\ncompleted_at: "2026-10-01T01:00:00Z"\n'
    )
    early = {
        'type': 'assistant',
        'timestamp': '2026-10-01T00:00:00Z',
        'message': {'content': [{'type': 'text', 'text': 'T0 の作業'}]},
    }
    projects = trace_search.mtc.CLAUDE_PROJECTS
    _jsonl(projects / 'ws1' / f'{SID}.jsonl', '2026-10-01T00:10:00Z', [early])
    srcs, _ = trace_search.sources(queue, task='T1')
    assert trace_search.search(srcs, 'T0 の作業') == []
    assert trace_search.search(srcs, 'build')


def test_multiple_tool_results_in_one_line(queue: Path) -> None:
    projects = trace_search.mtc.CLAUDE_PROJECTS
    two = [
        {
            'type': 'assistant',
            'timestamp': '2026-10-01T00:10:00Z',
            'message': {
                'content': [
                    {'type': 'tool_use', 'id': 'a', 'name': 'Bash', 'input': {'command': 'one'}},
                    {'type': 'tool_use', 'id': 'b', 'name': 'Bash', 'input': {'command': 'two'}},
                ]
            },
        },
        {
            'type': 'user',
            'timestamp': '2026-10-01T00:10:00Z',
            'message': {
                'content': [
                    {'type': 'tool_result', 'tool_use_id': 'a', 'is_error': True, 'content': 'boom one'},
                    {'type': 'tool_result', 'tool_use_id': 'b', 'is_error': True, 'content': 'boom two'},
                ]
            },
        },
    ]
    _jsonl(projects / 'ws1' / f'{SID}.jsonl', '2026-10-01T00:10:00Z', two)
    srcs, _ = trace_search.sources(queue, task='T1')
    ids = sorted(e.trace_id for e in trace_search.search(srcs, 'boom', errors_only=True))
    assert ids == [f'{SID}:7.0', f'{SID}:7.1']
    assert len(trace_search.get(f'{SID}:7')) == 2  # 行だけ指定すれば全 block
    only = trace_search.get(f'{SID}:7.1')
    assert len(only) == 1
    assert 'boom two' in only[0]


@pytest.mark.parametrize(('with_task', 'with_end'), [(True, False), (False, True), (False, False)])
def test_session_match_without_full_window_is_approximate(queue: Path, with_task: bool, with_end: bool) -> None:
    """Session が一致しても、時間窓の始端か終端が欠けるなら exact にしない."""
    pj = queue / 'projects' / 'pj'
    if with_task:
        (pj / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
    else:
        for f in (pj / 'tasks').glob('*.yaml'):
            if 'task_id: T1' in f.read_text():
                f.write_text('task_id: OTHER\n')
    end = 'completed_at: "2026-10-01T01:00:00Z"\n' if with_end else ''
    (pj / 'reports' / 'worker1_report.yaml').write_text(f'task_id: T1\nstatus: completed\nsession_id: "{SID}"\n{end}')
    _jsonl(trace_search.mtc.CLAUDE_PROJECTS / 'ws1' / f'{SID}.jsonl', '2026-10-01T00:10:00Z')
    srcs, _ = trace_search.sources(queue, task='T1')
    assert srcs
    assert all(not s.exact for s in srcs)


def test_session_match_with_full_window_is_exact(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    (pj / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
    (pj / 'reports' / 'worker1_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nsession_id: "{SID}"\ncompleted_at: "2026-10-01T01:00:00Z"\n'
    )
    _jsonl(trace_search.mtc.CLAUDE_PROJECTS / 'ws1' / f'{SID}.jsonl', '2026-10-01T00:10:00Z')
    srcs, _ = trace_search.sources(queue, task='T1')
    assert srcs
    assert all(s.exact for s in srcs)


def test_session_mismatch_does_not_pick_other_session_transcript(queue: Path) -> None:
    """Report の session_id と別 session の JSONL しか無いとき、その JSONL を exact として選ばない."""
    pj = queue / 'projects' / 'pj'
    (pj / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
    (pj / 'reports' / 'worker1_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nsession_id: "{SID_MISSING}"\ncompleted_at: "2026-10-01T01:00:00Z"\n'
    )
    # fixture には SID の JSONL が既にある。SID_MISSING の JSONL は無い
    srcs, missing = trace_search.sources(queue, task='T1')
    assert all(not s.exact for s in srcs)
    assert all(SID not in s.path.name for s in srcs)
    assert not srcs
    assert missing == ['pj/T1']


def test_session_mismatch_contrast_matching_session_is_selected(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    (pj / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
    (pj / 'reports' / 'worker1_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nsession_id: "{SID}"\ncompleted_at: "2026-10-01T01:00:00Z"\n'
    )
    srcs, _ = trace_search.sources(queue, task='T1')
    assert [s.path.name for s in srcs] == [f'{SID}.jsonl']
    assert all(s.exact for s in srcs)
