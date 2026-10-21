#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/graph.py (探索グラフ索引, Issue #55) のテスト.

実ファイルの tmp queue を作って sync し、DB の中身を見る (mock 注入はしない)。
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
import graph  # noqa: E402

RID1 = '11111111-1111-4111-8111-111111111111'
RID2 = '22222222-2222-4222-8222-222222222222'


def _write(path: Path, text: str, mtime: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    """T1 (完了・fact あり、archive) と T2 (T1 から派生、実行中) を持つ queue."""
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(
        pj / 'tasks' / 'archive' / 'worker1_T1.yaml',
        'task_id: T1\ntitle: "最初の調査"\ncontext:\n  parent_issue: 42\n',
        100,
    )
    _write(
        pj / 'reports' / 'archive' / 'worker1_T1.yaml',
        f'report_id: "{RID1}"\ntask_id: T1\nstatus: completed\nverify_status: pass\nsummary: "調べた"\n'
        'decision_bearing_claims:\n  - claim: "重複書き込みがある"\n    status: falsified\n'
        '    evidence: "backfill copy だった"\n',
        200,
    )
    _write(
        pj / 'tasks' / 'worker2.yaml',
        f'task_id: T2\ntitle: "続き"\ncontext:\n  parent_issue: "#42"\nderived_from: ["T1", "{RID1}"]\n',
        300,
    )
    _write(pj / 'reports' / 'worker1_verdict.yaml', 'task_id: T1\nresult: pass\n', 250)  # report ではない
    return q


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def test_sync_builds_nodes_and_edges(queue: Path) -> None:
    graph.sync(queue)
    db = graph.default_db_path(queue)
    nodes = dict(_rows(db, 'SELECT node_id, state FROM nodes'))
    assert nodes == {
        'goal:pj#42': 'unknown',
        'intent:pj/T1': 'done',
        'intent:pj/T2': 'running',
        f'result:{RID1}': 'completed',
        f'fact:{RID1}#0': 'falsified',
    }
    edges = set(_rows(db, 'SELECT src_id, rel, dst_id FROM edges'))
    assert edges == {
        ('goal:pj#42', 'spawns', 'intent:pj/T1'),
        ('goal:pj#42', 'spawns', 'intent:pj/T2'),
        ('intent:pj/T1', 'yields', f'result:{RID1}'),
        (f'result:{RID1}', 'yields', f'fact:{RID1}#0'),
        ('intent:pj/T2', 'derived_from', 'intent:pj/T1'),
        ('intent:pj/T2', 'derived_from', f'result:{RID1}'),
    }


def test_why_traces_result_back_to_goal(queue: Path) -> None:
    graph.sync(queue)
    text = '\n'.join(graph.why(graph.default_db_path(queue), f'fact:{RID1}#0'))
    # fact <- result <- intent:T1 <- goal の順に辿れる
    assert text.index(f'fact:{RID1}#0') < text.index(f'result:{RID1}') < text.index('intent:pj/T1')
    assert text.index('intent:pj/T1') < text.index('goal:pj#42')


def test_sync_is_idempotent_and_rebuildable(queue: Path) -> None:
    db = graph.default_db_path(queue)
    first = graph.sync(queue)
    dump = _rows(db, 'SELECT * FROM nodes ORDER BY node_id')
    assert graph.sync(queue) == first
    db.unlink()
    assert graph.sync(queue) == first
    assert _rows(db, 'SELECT * FROM nodes ORDER BY node_id') == dump


def test_sync_does_not_modify_yaml(queue: Path) -> None:
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in queue.rglob('*.yaml')}
    graph.sync(queue)
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in queue.rglob('*.yaml')} == before


def test_broken_yaml_and_legacy_report_are_handled(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    _write(pj / 'tasks' / 'worker3.yaml', 'task_id: [unterminated\n', 400)
    legacy = 'task_id: T2\nstatus: blocked\n'  # report_id の無い古い report
    _write(pj / 'reports' / 'worker2_report.yaml', legacy, 500)
    graph.sync(queue)
    nodes = dict(_rows(graph.default_db_path(queue), 'SELECT node_id, state FROM nodes'))
    legacy_ids = [n for n in nodes if n.startswith('result:sha:')]
    assert len(legacy_ids) == 1
    assert nodes['intent:pj/T2'] == 'blocked'  # 最新の result が T2 より新しい


def test_reassigned_task_newer_than_result_is_running(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    _write(pj / 'reports' / 'worker2_report.yaml', f'report_id: "{RID2}"\ntask_id: T2\nstatus: blocked\n', 350)
    os.utime(pj / 'tasks' / 'worker2.yaml', (400, 400))  # 差し戻して再発行
    graph.sync(queue)
    nodes = dict(_rows(graph.default_db_path(queue), 'SELECT node_id, state FROM nodes'))
    assert nodes['intent:pj/T2'] == 'running'


def test_stale_schema_is_rebuilt(queue: Path) -> None:
    db = graph.default_db_path(queue)
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE nodes (x TEXT)')
    conn.execute('PRAGMA user_version = 0')
    conn.commit()
    conn.close()
    assert graph.sync(queue)[0] == 5


def test_parent_issue_url_in_other_repo_is_separate_goal(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    _write(
        pj / 'tasks' / 'worker3.yaml',
        'task_id: T3\ncontext:\n  parent_issue: "https://github.com/o/r/issues/42"\n',
        400,
    )
    graph.sync(queue)
    goals = {r[0] for r in _rows(graph.default_db_path(queue), "SELECT node_id FROM nodes WHERE kind = 'goal'")}
    assert goals == {'goal:pj#42', 'goal:o/r#42'}


def test_active_task_wins_over_newer_archive_copy(queue: Path) -> None:
    pj = queue / 'projects' / 'pj'
    _write(pj / 'reports' / 'worker2_report.yaml', f'report_id: "{RID2}"\ntask_id: T2\nstatus: completed\n', 350)
    os.utime(pj / 'tasks' / 'worker2.yaml', (400, 400))  # 差し戻して再発行
    _write(pj / 'tasks' / 'archive' / 'worker2_T2.yaml', 'task_id: T2\ntitle: "旧版"\n', 500)  # 旧版を後から cp
    graph.sync(queue)
    row = _rows(graph.default_db_path(queue), "SELECT state, title FROM nodes WHERE node_id = 'intent:pj/T2'")
    assert row == [('running', '続き')]


def test_concurrent_init_on_fresh_db(queue: Path) -> None:
    import threading

    errors: list[BaseException] = []
    for i in range(20):
        db = queue / f'g{i}.db'
        barrier = threading.Barrier(4)

        def run(db: Path = db, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            try:
                graph.connect(db).close()
            except sqlite3.Error as e:
                errors.append(e)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
