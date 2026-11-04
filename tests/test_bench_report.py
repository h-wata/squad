#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts.bench_report のテスト (#59). fixture の jsonl と queue から作った探索グラフで集計を確かめる."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'squad'))
import bench_report  # noqa: E402
import graph  # noqa: E402


def _rec(task_id: str, config: str, trial: int, **kw: object) -> dict:
    return {
        'task_id': task_id,
        'project': 'pj',
        'status': 'completed',
        'verify_status': 'pass',
        'output_tokens': 100,
        'wall_clock_sec': 60.0,
        'bench': {'suite': 's', 'config': config, 'trial': trial},
        **kw,
    }


def _write_jsonl(path: Path, records: list[dict]) -> Path:
    path.write_text(''.join(json.dumps(r) + '\n' for r in records) + 'not json\n', encoding='utf-8')
    return path


def _report(path: Path, task_id: str, report_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'task_id: {task_id}\nreport_id: "{report_id}"\nstatus: completed\n', encoding='utf-8')


def test_groups_by_suite_and_config_and_uses_last_measurement(tmp_path: Path) -> None:
    metrics = _write_jsonl(
        tmp_path / 'm.jsonl',
        [
            {'task_id': 'OLD', 'project': 'pj', 'status': 'completed'},  # bench 無しは集計しない
            _rec('A1', 'sonnet', 1, verify_status='fail'),
            _rec('A1', 'sonnet', 1, output_tokens=300),  # 測り直し: こちらを使う
            _rec('A2', 'sonnet', 2, output_tokens=500, wall_clock_sec=None),
            _rec('B1', 'qwen', 1, status='blocked'),
        ],
    )
    lines = bench_report.render(bench_report.load_bench_records(metrics), None)

    assert lines[2:] == [
        '| s | qwen | 1 | 0/1 | - | 100 | - | - | 60 | - | - |',
        '| s | sonnet | 2 | 2/2 | - | 400 | - | - | 60 | - | - |',
    ]


def test_duplicate_work_counts_tasks_with_two_or_more_reports(tmp_path: Path) -> None:
    queue = tmp_path / 'queue'
    reports = queue / 'projects' / 'pj' / 'reports'
    for task_id in ('A1', 'A2'):
        tasks = queue / 'projects' / 'pj' / 'tasks'
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / f'{task_id}.yaml').write_text(f'task_id: {task_id}\n', encoding='utf-8')
    _report(reports / 'worker1_report.yaml', 'A1', '11111111-1111-4111-8111-111111111111')
    _report(reports / 'archive' / 'worker1_report_A1.yaml', 'A1', '22222222-2222-4222-8222-222222222222')
    _report(reports / 'worker2_report.yaml', 'A2', '33333333-3333-4333-8333-333333333333')
    # cross-review の report は重複に数えない
    _report(reports / 'worker4_review_report.yaml', 'A2', '44444444-4444-4444-8444-444444444444')
    db = tmp_path / 'graph.db'
    graph.sync(queue, db)
    metrics = _write_jsonl(tmp_path / 'm.jsonl', [_rec('A1', 'sonnet', 1), _rec('A2', 'sonnet', 2)])

    assert bench_report.main(['--metrics', str(metrics), '--graph-db', str(db)]) == 0
    records = bench_report.load_bench_records(metrics)
    assert bench_report.render(records, bench_report.result_counts(db, records))[2].endswith('| 1/2 |')


def test_missing_graph_db_shows_dash_and_no_bench_records_fails(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    metrics = _write_jsonl(tmp_path / 'm.jsonl', [_rec('A1', 'sonnet', 1)])
    assert bench_report.main(['--metrics', str(metrics), '--graph-db', str(tmp_path / 'none.db')]) == 0
    out = capsys.readouterr()
    assert out.out.strip().splitlines()[2].endswith('| - |')
    assert 'squad graph sync' in out.err

    empty = _write_jsonl(tmp_path / 'e.jsonl', [{'task_id': 'OLD', 'project': 'pj'}])
    assert bench_report.main(['--metrics', str(empty)]) == 1


def test_same_trial_label_on_another_task_is_counted_once(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    metrics = _write_jsonl(tmp_path / 'm.jsonl', [_rec('A1', 'sonnet', 1), _rec('A9', 'sonnet', 1)])
    records = bench_report.load_bench_records(metrics)

    assert [r['task_id'] for r in records] == ['A9']
    assert '重複' in capsys.readouterr().err
