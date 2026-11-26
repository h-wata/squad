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
import time

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
    # 実環境の /tmp/claude_usage_cache.json や実 tmux pane に依存しないよう既定で無効化する。
    monkeypatch.setattr(mtc, 'QUOTA_CACHE_PATH', tmp_path / 'no_such_quota_cache.json')
    monkeypatch.setattr(mtc, 'capture_own_tmux_pane', lambda: None)
    return tasks_dir, reports_dir


def _write_task(tasks_dir: Path, *, mtime: datetime | None = None, extra: str = '') -> Path:
    path = tasks_dir / 'worker1.yaml'
    path.write_text(
        f'task_id: {TASK_ID}\nproject: {PROJECT}\nassigned_to: worker1\nagent: claude\nmodel: "sonnet"\n{extra}',
        encoding='utf-8',
    )
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


def _write_report(
    reports_dir: Path,
    *,
    completed_at: str,
    verdict_path: str = '',
    session_id: str = '',
    quota_5h_before_pct: str = '',
    quota_5h_after_pct: str = '',
    quota_7d_before_pct: str = '',
    quota_7d_after_pct: str = '',
    quota_source: str = '',
) -> Path:
    path = reports_dir / 'worker1_report.yaml'
    path.write_text(
        f'task_id: {TASK_ID}\nproject: {PROJECT}\nworker: worker1\nagent: claude\n'
        f'status: completed\nverify_status: pass\nverdict_path: "{verdict_path}"\n'
        f'session_id: "{session_id}"\n'
        f'quota_5h_before_pct: "{quota_5h_before_pct}"\nquota_5h_after_pct: "{quota_5h_after_pct}"\n'
        f'quota_7d_before_pct: "{quota_7d_before_pct}"\nquota_7d_after_pct: "{quota_7d_after_pct}"\n'
        f'quota_source: "{quota_source}"\n'
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


def test_window_endpoints_are_inclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ADR 0011 Decision 6: 区間は task YAML mtime から report completed_at まで。両端ちょうどの message は数える
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)

    just_before = _assistant_line(start - timedelta(seconds=1), usage={'input_tokens': 1000, 'output_tokens': 1000})
    at_start = _assistant_line(start, usage={'input_tokens': 10, 'output_tokens': 1})
    at_end = _assistant_line(end, usage={'input_tokens': 20, 'output_tokens': 2})
    just_after = _assistant_line(end + timedelta(seconds=1), usage={'input_tokens': 1000, 'output_tokens': 1000})
    (session_dir / 'session.jsonl').write_text(
        '\n'.join([just_before, at_start, at_end, just_after]) + '\n', encoding='utf-8'
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['input_tokens'] == 30
    assert record['output_tokens'] == 3


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
    assert record['attribution'] == 'approximate'


def test_no_session_id_gives_approximate_attribution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'approximate'


def test_session_id_gives_exact_attribution_and_excludes_other_sessions_in_same_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQUAD-261 の主目的: session_id があれば同じ時間窓に重なる他セッションを混入させない."""
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    my_session_id = 'aaaaaaaa-1111-1111-1111-111111111111'
    _write_report(reports_dir, completed_at=end.isoformat(), session_id=my_session_id)
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)

    mine = _assistant_line(
        start + timedelta(minutes=1), session_id=my_session_id, usage={'input_tokens': 10, 'output_tokens': 20}
    )
    other = _assistant_line(
        start + timedelta(minutes=2),
        session_id='other-worker-session',
        usage={'input_tokens': 999, 'output_tokens': 999},
    )
    (session_dir / f'{my_session_id}.jsonl').write_text(mine + '\n', encoding='utf-8')
    (session_dir / 'other-worker-session.jsonl').write_text(other + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'exact'
    assert record['input_tokens'] == 10
    assert record['output_tokens'] == 20


def test_session_id_present_but_transcript_missing_falls_back_to_approximate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat(), session_id='does-not-exist')
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    line = _assistant_line(start + timedelta(minutes=1), usage={'input_tokens': 3, 'output_tokens': 4})
    (session_dir / 'some-other-session.jsonl').write_text(line + '\n', encoding='utf-8')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'approximate'
    assert record['input_tokens'] == 3
    assert 'approximate にフォールバック' in record['notes']


# --- quota (5h/7d) 消費計測 (SQUAD-265) ---


def test_read_quota_cache_parses_valid_file(tmp_path: Path) -> None:
    cache_path = tmp_path / 'quota_cache.json'
    cache_path.write_text(
        json.dumps({'data': {'five_hour': {'utilization': 48.0}, 'seven_day': {'utilization': 90.0}}}),
        encoding='utf-8',
    )

    result = mtc.read_quota_cache(cache_path)

    assert result == {'five_hour_pct': 48, 'seven_day_pct': 90}


def test_read_quota_cache_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert mtc.read_quota_cache(tmp_path / 'no_such_file.json') is None


def test_read_quota_cache_returns_none_for_malformed_json(tmp_path: Path) -> None:
    cache_path = tmp_path / 'quota_cache.json'
    cache_path.write_text('{not valid json', encoding='utf-8')

    assert mtc.read_quota_cache(cache_path) is None


def test_read_quota_cache_returns_none_when_utilization_missing(tmp_path: Path) -> None:
    cache_path = tmp_path / 'quota_cache.json'
    cache_path.write_text(json.dumps({'data': {'five_hour': {}, 'seven_day': {}}}), encoding='utf-8')

    assert mtc.read_quota_cache(cache_path) is None


def test_parse_tmux_status_line_extracts_percentages() -> None:
    text = '   Sonnet 5   Usage   5h:49%~18:19   7d:90%~8/24   ctx:12%\n'

    assert mtc.parse_tmux_status_line(text) == {'five_hour_pct': 49, 'seven_day_pct': 90}


def test_parse_tmux_status_line_returns_none_when_percentages_absent() -> None:
    assert mtc.parse_tmux_status_line('no usage info here') is None


def test_get_quota_snapshot_prefers_cache_file_over_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / 'quota_cache.json'
    cache_path.write_text(
        json.dumps({'data': {'five_hour': {'utilization': 10}, 'seven_day': {'utilization': 20}}}),
        encoding='utf-8',
    )
    monkeypatch.setattr(mtc, 'QUOTA_CACHE_PATH', cache_path)

    def _boom() -> str | None:
        raise AssertionError('tmux should not be consulted when the cache file is available')

    monkeypatch.setattr(mtc, 'capture_own_tmux_pane', _boom)

    result = mtc.get_quota_snapshot()

    assert result == {'five_hour_pct': 10, 'seven_day_pct': 20, 'source': 'usage_cache_file'}


def test_get_quota_snapshot_falls_back_to_tmux_when_cache_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mtc, 'QUOTA_CACHE_PATH', tmp_path / 'no_such_file.json')
    monkeypatch.setattr(mtc, 'capture_own_tmux_pane', lambda: '5h:33%~10:00   7d:77%~8/30')

    result = mtc.get_quota_snapshot()

    assert result == {'five_hour_pct': 33, 'seven_day_pct': 77, 'source': 'tmux_status_line'}


def test_get_quota_snapshot_unavailable_when_both_sources_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mtc, 'QUOTA_CACHE_PATH', tmp_path / 'no_such_file.json')
    monkeypatch.setattr(mtc, 'capture_own_tmux_pane', lambda: None)

    result = mtc.get_quota_snapshot()

    assert result == {'five_hour_pct': None, 'seven_day_pct': None, 'source': 'unavailable'}


def test_extract_quota_deltas_computes_delta_when_both_points_present() -> None:
    meta = {
        'quota_5h_before_pct': '10',
        'quota_5h_after_pct': '15',
        'quota_7d_before_pct': '80',
        'quota_7d_after_pct': '82',
    }

    result = mtc.extract_quota_deltas(meta)

    assert result == {'quota_5h_delta_pct': 5, 'quota_7d_delta_pct': 2}


def test_extract_quota_deltas_none_when_only_one_point_present_distinguishes_from_zero() -> None:
    """片方しか無い場合は 0 と区別できる None (=未取得) にする."""
    meta = {'quota_5h_before_pct': '10'}

    result = mtc.extract_quota_deltas(meta)

    assert result == {'quota_5h_delta_pct': None, 'quota_7d_delta_pct': None}


def test_measure_includes_quota_delta_when_report_has_before_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(
        reports_dir,
        completed_at=end.isoformat(),
        quota_5h_before_pct='40',
        quota_5h_after_pct='44',
        quota_7d_before_pct='88',
        quota_7d_after_pct='89',
        quota_source='usage_cache_file',
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['quota_5h_delta_pct'] == 4
    assert record['quota_7d_delta_pct'] == 1
    assert record['quota_source'] == 'usage_cache_file'


def test_measure_quota_delta_is_none_when_report_has_no_quota_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['quota_5h_delta_pct'] is None
    assert record['quota_7d_delta_pct'] is None


def test_measure_negative_quota_delta_is_noted_as_possible_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 6, 30, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(
        reports_dir,
        completed_at=end.isoformat(),
        quota_7d_before_pct='95',
        quota_7d_after_pct='3',
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['quota_7d_delta_pct'] == -92
    assert 'reset' in record['notes']


def test_bench_is_copied_only_when_task_has_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, _ = _setup_repo(tmp_path, monkeypatch)
    task = _write_task(tasks_dir)
    assert 'bench' not in mtc.measure(PROJECT, TASK_ID)  # 既存 task のレコードの形は変わらない

    task.write_text(
        task.read_text(encoding='utf-8') + 'bench:\n  suite: issue34\n  config: sonnet\n  trial: 2\n',
        encoding='utf-8',
    )
    assert mtc.measure(PROJECT, TASK_ID)['bench'] == {'suite': 'issue34', 'config': 'sonnet', 'trial': 2}


def test_claude_local_transcript_under_worker_config_dir_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude-local worker の transcript (config/claude-worker/projects) を session_id で見つける (#72)."""
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 5, 3, 10, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat(), session_id='local-1')
    worker_dir = mtc.REPO_ROOT / 'config' / 'claude-worker' / 'projects' / '-home-x'
    worker_dir.mkdir(parents=True)
    usage = {'input_tokens': 5, 'output_tokens': 7}
    (worker_dir / 'local-1.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=1), session_id='local-1', usage=usage) + '\n', encoding='utf-8'
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'exact'
    assert record['output_tokens'] == 7


def test_opencode_usage_is_unavailable_not_borrowed_from_same_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opencode worker は同じ cwd の別セッション (verifier 等) の token を拾わず unknown にする (#72)."""
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 5, 3, 10, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    report = _write_report(reports_dir, completed_at=end.isoformat())
    report.write_text(report.read_text().replace('agent: claude', 'agent: opencode'), encoding='utf-8')
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    (session_dir / 'verifier.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=1), usage={'output_tokens': 99}) + '\n', encoding='utf-8'
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'unavailable'
    assert record['output_tokens'] is None
    assert record['wall_clock_sec'] == 600


def test_naive_completed_at_is_read_as_local_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """オフセット無しの completed_at はローカル時刻として読む (UTC 扱いで 9 時間ずれない, #72)."""
    original_tz = os.environ.get('TZ')
    os.environ['TZ'] = 'Asia/Tokyo'
    time.tzset()
    try:
        tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
        start = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)  # = 12:00 JST
        _write_task(tasks_dir, mtime=start)
        report = _write_report(reports_dir, completed_at='2026-11-05T12:05:00')
        later = datetime(2026, 11, 5, 3, 30, tzinfo=timezone.utc).timestamp()
        os.utime(report, (later, later))

        record = mtc.measure(PROJECT, TASK_ID)

        assert record['wall_clock_sec'] == 300
    finally:
        if original_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = original_tz
        time.tzset()


def test_completed_at_after_report_mtime_is_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """completed_at が report の更新時刻より未来なら、更新時刻を使う (#72)."""
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    report = _write_report(reports_dir, completed_at=(start + timedelta(minutes=10)).isoformat())
    written = (start + timedelta(minutes=8)).timestamp()
    os.utime(report, (written, written))

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['wall_clock_sec'] == 480
    assert '更新時刻を使う' in record['notes']


def test_transcript_under_per_worker_config_dir_is_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker ごとの CLAUDE_CONFIG_DIR (config/claude-worker/<session>/w<n>/projects) も探す."""
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 5, 3, 10, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat(), session_id='tok-1')
    worker_dir = mtc.REPO_ROOT / 'config' / 'claude-worker' / 'squad' / 'w2' / 'projects' / '-home-x'
    worker_dir.mkdir(parents=True)
    usage = {'input_tokens': 5, 'output_tokens': 7}
    (worker_dir / 'tok-1.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=1), session_id='tok-1', usage=usage) + '\n', encoding='utf-8'
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'exact'


# --- #105 の 5: worker_dir 集計 / task_created_at / --compare ---


def _setup_worker_dir(tmp_path: Path, session: str = 'sq', worker: str = 'w1') -> Path:
    """config/claude-worker/<session>/w<n>/projects/<encoded-cwd>/ を作って返す."""
    pj_dir = mtc.QUEUE_ROOT / 'queue' / 'projects' / PROJECT
    (pj_dir / '.squad_session').write_text(session + '\n', encoding='utf-8')
    d = mtc.REPO_ROOT / 'config' / 'claude-worker' / session / worker / 'projects' / '-work-ws'
    d.mkdir(parents=True)
    return d


def test_worker_dir_sums_all_sessions_in_window_and_counts_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 8, 21, 7, 0, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=end.isoformat())
    wdir = _setup_worker_dir(tmp_path)
    brief_dir = mtc.QUEUE_ROOT / 'queue' / 'projects' / PROJECT / 'briefs'
    brief_dir.mkdir()
    (brief_dir / f'{TASK_ID}.md').write_text(f'# brief: {TASK_ID}\n- break_count: 2\n', encoding='utf-8')

    u = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 5}
    (wdir / 'a.jsonl').write_text(
        '\n'.join(
            [
                _assistant_line(start - timedelta(minutes=1), session_id='a', usage=u),
                _assistant_line(start + timedelta(minutes=1), session_id='a', usage=u),
            ]
        )
        + '\n',
        encoding='utf-8',
    )
    (wdir / 'b.jsonl').write_text(
        '\n'.join(
            [
                _assistant_line(start + timedelta(minutes=30), session_id='b', usage=u),
                _assistant_line(end + timedelta(minutes=1), session_id='b', usage=u),
            ]
        )
        + '\n',
        encoding='utf-8',
    )
    sub = wdir / 'a' / 'subagents'
    sub.mkdir(parents=True)
    (sub / 'agent-x.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=2), session_id='x', usage=u) + '\n', encoding='utf-8'
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'worker_dir'
    assert record['input_tokens'] == 20
    assert record['cache_read_tokens'] == 200
    assert record['total_tokens'] == 2 * (10 + 20 + 100 + 5)
    assert record['sessions'] == 2
    assert record['breaks'] == 2


def test_worker_dir_preferred_over_session_id_exact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=(start + timedelta(hours=1)).isoformat(), session_id='sid-1')
    wdir = _setup_worker_dir(tmp_path)
    (wdir / 'sid-1.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=1), session_id='sid-1', usage={'input_tokens': 1}) + '\n',
        encoding='utf-8',
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'worker_dir'


def test_no_worker_dir_keeps_legacy_attribution_and_zero_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    start = datetime(2026, 8, 21, 6, 0, 0, tzinfo=timezone.utc)
    _write_task(tasks_dir, mtime=start)
    _write_report(reports_dir, completed_at=(start + timedelta(hours=1)).isoformat())
    session_dir = _session_dir_for_repo_root(mtc.CLAUDE_PROJECTS, mtc.REPO_ROOT)
    (session_dir / 's.jsonl').write_text(
        _assistant_line(start + timedelta(minutes=1), usage={'input_tokens': 3, 'output_tokens': 4}) + '\n',
        encoding='utf-8',
    )

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['attribution'] == 'approximate'
    assert record['total_tokens'] == 7
    assert record['sessions'] == 1
    assert record['breaks'] == 0


def test_total_tokens_is_none_when_usage_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_repo(tmp_path, monkeypatch)

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['total_tokens'] is None
    assert record['sessions'] == 0


@pytest.mark.parametrize(
    ('line', 'expected', 'error'),
    [
        ('created_at: "2026-11-18T17:09:46+09:00"\n', '2026-11-18T08:09:46+00:00', None),
        ('created_at: "2026-11-18T08:09:46Z"\n', '2026-11-18T08:09:46+00:00', None),
        ('', None, 'missing'),
        ('created_at: ""\n', None, 'missing'),
        ('created_at: "2026-11-18T17:09:46"\n', None, 'invalid'),
        ('created_at: "yesterday"\n', None, 'invalid'),
    ],
)
def test_task_created_at_three_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, expected: str | None, error: str | None
) -> None:
    tasks_dir, reports_dir = _setup_repo(tmp_path, monkeypatch)
    _write_task(tasks_dir, mtime=datetime(2030, 1, 1, tzinfo=timezone.utc), extra=line)
    _write_report(reports_dir, completed_at='2026-11-18T10:00:00+00:00')

    record = mtc.measure(PROJECT, TASK_ID)

    assert record['task_created_at'] == expected
    assert record['task_created_at_error'] == error
    if error == 'invalid':
        assert 'created_at' in record['notes']


def _rec(
    task_id: str,
    created: str | None,
    *,
    error: str | None = None,
    attribution: str = 'worker_dir',
    model: str = 'sonnet',
    agent: str = 'claude',
    total: int | None = 100,
    cache: int | None = 60,
    with_field: bool = True,
    project: str = PROJECT,
) -> dict:
    rec = {
        'task_id': task_id,
        'project': project,
        'model': model,
        'agent': agent,
        'attribution': attribution,
        'total_tokens': total,
        'cache_read_tokens': cache,
    }
    if with_field:
        rec['task_created_at'] = created
        rec['task_created_at_error'] = error
    return rec


def test_compare_splits_before_after_and_counts_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_repo(tmp_path, monkeypatch)
    recs = [
        _rec('B1', '2026-11-18T00:00:00+00:00', total=100, cache=50),
        _rec('B2', '2026-11-18T01:00:00+00:00', total=300, cache=150),
        _rec('A1', '2026-11-18T09:00:00+00:00', total=40, cache=10),
        _rec('EQ', '2026-11-18T08:00:00+00:00', total=60, cache=30),  # cutover ちょうど = 後
        _rec('M1', None, error='missing'),
        _rec('I1', None, error='invalid'),
        _rec('I2', None, error='invalid'),
        _rec('OLD', None, with_field=False),
        _rec('ATT', '2026-11-18T00:00:00+00:00', attribution='approximate'),
        _rec('B1', '2026-11-18T00:00:00+00:00', total=100, cache=50),  # 重複は最新 1 件にまとめる
    ]
    mtc.METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mtc.METRICS_PATH.write_text('\n'.join(json.dumps(r) for r in recs) + '\n', encoding='utf-8')
    before_bytes = mtc.METRICS_PATH.read_bytes()

    out = mtc.compare('2026-11-18T08:00:00+00:00')

    assert mtc.METRICS_PATH.read_bytes() == before_bytes
    assert '| sonnet | claude | before | 2 | 200 | 200 | 100 | 100 |' in out
    assert '| sonnet | claude | after | 2 | 50 | 50 | 20 | 20 |' in out
    assert 'missing: 1' in out
    assert 'invalid: 2' in out
    assert '項目なし: 1' in out
    assert 'attribution 対象外: 1' in out


def test_compare_ignores_corrupt_jsonl_lines_without_modifying_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_repo(tmp_path, monkeypatch)
    good_before = json.dumps(_rec('B1', '2026-11-18T00:00:00+00:00', total=100, cache=50))
    good_after = json.dumps(_rec('A1', '2026-11-18T09:00:00+00:00', total=40, cache=10))
    content = (
        '\n'.join(['{"task_id": "BROKEN", "project": ', good_before, 'not json at all', '', good_after, '{']) + '\n'
    )
    mtc.METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mtc.METRICS_PATH.write_text(content, encoding='utf-8')
    before_bytes = mtc.METRICS_PATH.read_bytes()

    records = mtc.load_latest_records(mtc.METRICS_PATH)
    out = mtc.compare('2026-11-18T08:00:00+00:00')

    assert sorted(r['task_id'] for r in records) == ['A1', 'B1']
    assert '| sonnet | claude | before | 1 | 100 |' in out
    assert '| sonnet | claude | after | 1 | 40 |' in out
    assert mtc.METRICS_PATH.read_bytes() == before_bytes


def test_compare_cli_rejects_cutover_without_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'argv', ['measure_task_cost.py', '--compare', '--cutover', '2026-11-18T08:00:00'])

    with pytest.raises(SystemExit) as exc:
        mtc.main()

    assert exc.value.code != 0


def test_compare_cli_prints_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _setup_repo(tmp_path, monkeypatch)
    mtc.METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    mtc.METRICS_PATH.write_text(json.dumps(_rec('A1', '2026-11-18T09:00:00+00:00')) + '\n', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['measure_task_cost.py', '--compare', '--cutover', '2026-11-18T17:00:00+09:00'])

    mtc.main()

    assert '| sonnet | claude | after | 1 |' in capsys.readouterr().out


def test_worker_transcript_found_under_main_checkout_when_run_from_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """副 worktree から実行しても、主 checkout の config/claude-worker の transcript を見つける (SQUAD-433)."""
    _setup_repo(tmp_path, monkeypatch)
    worktree = tmp_path / 'wt'
    (worktree / 'config' / 'claude-worker').mkdir(parents=True)  # 副 worktree には追跡ファイルだけがある
    monkeypatch.setattr(mtc, 'REPO_ROOT', worktree)
    d = mtc.QUEUE_ROOT / 'config' / 'claude-worker' / 'squad' / 'w1' / 'projects' / '-home-x'
    d.mkdir(parents=True)
    (d / 'sid-main.jsonl').write_text('{}\n', encoding='utf-8')

    assert mtc.find_session_transcript('sid-main') == d / 'sid-main.jsonl'
