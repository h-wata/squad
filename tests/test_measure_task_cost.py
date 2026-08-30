#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts.measure_task_cost のテスト (SQUAD-258).

実ユーザーの session transcript には依存せず、tmp dir に fixture の task/report YAML と
session JSONL を作って検証する。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
import measure_task_cost as mtc  # noqa: E402

PROJECT = 'demo'
TASK_ID = 'DEMO-001'


def _setup_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """queue/projects/<project>/{tasks,reports} を tmp 配下に用意し、module 定数を差し替える."""
    repo_root = tmp_path / 'repo'
    tasks_dir = repo_root / 'queue' / 'projects' / PROJECT / 'tasks'
    reports_dir = repo_root / 'queue' / 'projects' / PROJECT / 'reports'
    tasks_dir.mkdir(parents=True)
    reports_dir.mkdir(parents=True)
    claude_projects = tmp_path / 'claude_projects'
    claude_projects.mkdir()
    metrics_path = repo_root / 'metrics' / 'task_costs.jsonl'
    monkeypatch.setattr(mtc, 'REPO_ROOT', repo_root)
    monkeypatch.setattr(mtc, 'QUEUE_ROOT', repo_root)
    monkeypatch.setattr(mtc, 'CLAUDE_PROJECTS', claude_projects)
    monkeypatch.setattr(mtc, 'METRICS_PATH', metrics_path)
    return tasks_dir, reports_dir


def _write_task(tasks_dir: Path, *, mtime: datetime | None = None) -> Path:
    path = tasks_dir / 'worker1.yaml'
    path.write_text(
        f'task_id: {TASK_ID}\nproject: {PROJECT}\nassigned_to: worker1\nagent: claude\nmodel: "sonnet"\n',
        encoding='utf-8',
    )
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _write_report(reports_dir: Path, *, completed_at: str, verdict_path: str = '') -> Path:
    path = reports_dir / 'worker1_report.yaml'
    path.write_text(
        f'task_id: {TASK_ID}\nproject: {PROJECT}\nworker: worker1\nagent: claude\n'
        f'status: completed\nverify_status: pass\nverdict_path: "{verdict_path}"\n'
        f'completed_at: "{completed_at}"\n',
        encoding='utf-8',
    )
    return path


def _session_dir_for_repo_root(claude_projects: Path, repo_root: Path) -> Path:
    encoded = str(repo_root).replace('/', '-')
    d = claude_projects / encoded
    d.mkdir(parents=True, exist_ok=True)
    return d


def _assistant_line(ts: datetime, *, session_id: str = 'sess-1', usage: dict | None = None) -> str:
    entry = {
        'type': 'assistant',
        'timestamp': ts.isoformat().replace('+00:00', 'Z'),
        'sessionId': session_id,
        'message': {'model': 'claude-sonnet-5', 'usage': usage if usage is not None else {}},
    }
    if usage is None:
        del entry['message']['usage']
    return json.dumps(entry, ensure_ascii=False)


def test_window_extraction_only_counts_messages_inside_task_mtime_to_completed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)

    before = _assistant_line(start - timedelta(minutes=5), usage={'input_tokens': 999, 'output_tokens': 999})
    inside1 = _assistant_line(start + timedelta(minutes=1), usage={'input_tokens': 10, 'output_tokens': 20})
    inside2 = _assistant_line(start + timedelta(minutes=2), usage={'input_tokens': 5, 'output_tokens': 7})
    after = _assistant_line(end + timedelta(minutes=5), usage={'input_tokens': 999, 'output_tokens': 999})
    (session_dir / 'session.jsonl').write_text('\n'.join([before, inside1, inside2, after]) + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['input_tokens'] == 15
    assert record['output_tokens'] == 27
    assert record['wall_clock_sec'] == 1800.0


def test_usage_field_missing_is_recorded_as_unknown_not_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    line = _assistant_line(start + timedelta(minutes=1), usage=None)
    (session_dir / 'session.jsonl').write_text(line + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['input_tokens'] is None
    assert record['output_tokens'] is None
    assert 'usage フィールド欠損' in record['notes']


def test_corrupt_jsonl_line_is_skipped_and_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    good = _assistant_line(start + timedelta(minutes=1), usage={'input_tokens': 3, 'output_tokens': 4})
    lines = [good, '{not valid json', good]
    (session_dir / 'session.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['input_tokens'] == 6
    assert 'JSONL 破損行を 1 件スキップ' in record['notes']


def test_missing_session_file_recorded_as_unknown_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    # session dir を一切作らない

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['input_tokens'] is None
    assert record['status'] == 'completed'
    assert 'session transcript ディレクトリ' in record['notes']


def test_missing_task_and_report_yaml_records_unknown_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_repo(tmp_path, monkeypatch)

    record = mtc.measure(PROJECT, 'NO-SUCH-TASK')

    assert record['input_tokens'] is None
    assert record['wall_clock_sec'] is None
    assert 'task YAML が見つからず' in record['notes']
    assert 'report YAML が見つからず' in record['notes']


def test_attempts_extracted_from_verdict_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    verdict_path = tmp_path / 'worker1_verdict.yaml'
    verdict_path.write_text('task_id: DEMO-001\nattempt: 2\nresult: pass\n', encoding='utf-8')
    _write_report(reports_dir, completed_at=end.isoformat(), verdict_path=str(verdict_path))

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attempts'] == 2


def test_append_preserves_existing_lines_in_metrics_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    mtc.METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mtc.METRICS_PATH.write_text('{"task_id": "OLD-1"}\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)
    with mtc.METRICS_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    lines = mtc.METRICS_PATH.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])['task_id'] == 'OLD-1'
    assert json.loads(lines[1])['task_id'] == TASK_ID


def test_multiple_session_ids_in_window_are_noted_as_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    a = _assistant_line(start + timedelta(minutes=1), session_id='sess-a', usage={'input_tokens': 1})
    b = _assistant_line(start + timedelta(minutes=2), session_id='sess-b', usage={'input_tokens': 2})
    (session_dir / 'session.jsonl').write_text('\n'.join([a, b]) + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert '異なる sessionId' in record['notes']
