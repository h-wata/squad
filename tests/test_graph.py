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
        'goal:pj#42': 'running',  # 配下の T2 が実行中 (#76 addendum: ゴールの状態は配下の task から決める)
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


@pytest.fixture
def status_queue(queue: Path) -> Path:
    """T1: done (後で verified)、T2: running、T3: blocked、T4: done (PR の review は request_changes)."""
    pj = queue / 'projects' / 'pj'
    _write(pj / 'tasks' / 'archive' / 'worker3_T3.yaml', 'task_id: T3\ntitle: "詰まった"\nassigned_to: worker3\n', 100)
    _write(
        pj / 'reports' / 'archive' / 'worker3_T3.yaml',
        'task_id: T3\nstatus: blocked\nsummary: "権限が無く投稿できない"\n',
        110,
    )
    _write(pj / 'tasks' / 'archive' / 'worker4_T4.yaml', 'task_id: T4\n', 100)
    _write(
        pj / 'reports' / 'archive' / 'worker4_T4.yaml',
        'task_id: T4\nstatus: completed\nverify_status: pass\npr_url: "https://github.com/o/r/pull/9"\n',
        110,
    )
    _write(
        pj / 'reports' / 'archive' / 'worker4_review_T4R.yaml',
        'task_id: T4R\nverdict: request_changes\npr_url: "https://github.com/o/r/pull/9/"\n',
        120,
    )
    return queue


def _status(queue: Path) -> dict:
    graph.sync(queue)
    return graph.status(graph.default_db_path(queue))['pj']


def test_status_counts_and_unfinished(status_queue: Path) -> None:
    pj = _status(status_queue)
    assert pj['goals']['goal:pj#42'] == {'running': 1, 'blocked': 0, 'done': 1, 'verified': 0, 'closed': 0}
    assert pj['goals']['-'] == {'running': 0, 'blocked': 1, 'done': 1, 'verified': 0, 'closed': 0}
    unfinished = {u['node_id']: u for u in pj['unfinished']}
    assert set(unfinished) == {'intent:pj/T2', 'intent:pj/T3'}
    blocked = unfinished['intent:pj/T3']
    assert blocked['reason'] == '権限が無く投稿できない'
    assert blocked['reason_id'].startswith('result:sha:')
    assert blocked['assigned_to'] == 'worker3'
    # 各行の node_id から why で根拠 (report) まで辿れる
    text = '\n'.join(graph.why(graph.default_db_path(status_queue), blocked['reason_id']))
    assert 'intent:pj/T3' in text
    lines = '\n'.join(graph.render_status({'pj': pj}))
    assert '[blocked 1970-01-01] intent:pj/T3 詰まった (worker3, -)' in lines
    assert '理由: 権限が無く投稿できない' in lines


@pytest.mark.parametrize(
    ('review_name', 'review'),
    [
        ('worker4_review_T1.yaml', 'task_id: T1\nverdict: approve\n'),  # 同じ task_id
        ('worker4_review_T1R.yaml', 'task_id: T1R\nverdict: approve\npr_url: https://github.com/o/r/pull/1\n'),  # PR
        ('worker4_review_T1.yaml', 'task_id: T1\nverdict: approve_continues\n'),  # 古い approve の再レビュー
    ],
)
def test_verified_needs_verify_pass_and_approve(status_queue: Path, review_name: str, review: str) -> None:
    pj_dir = status_queue / 'projects' / 'pj'
    report = pj_dir / 'reports' / 'archive' / 'worker1_T1.yaml'
    _write(report, report.read_text() + 'pr_url: "https://github.com/o/r/pull/1"\n', 200)
    _write(pj_dir / 'reports' / 'archive' / review_name, review, 210)
    pj = _status(status_queue)
    assert pj['goals']['goal:pj#42']['verified'] == 1  # T1
    assert pj['goals']['-']['done'] == 1  # T4 は request_changes なので done のまま


def test_verified_ignores_approve_older_than_latest_result(status_queue: Path) -> None:
    pj_dir = status_queue / 'projects' / 'pj'
    _write(pj_dir / 'reports' / 'archive' / 'worker4_review_T4R2.yaml', 'task_id: T4\nverdict: approve\n', 130)
    assert _status(status_queue)['goals']['-']['verified'] == 1  # T4: approve が最新 report より新しい
    report = pj_dir / 'reports' / 'archive' / 'worker4_T4.yaml'
    _write(report, report.read_text(), 140)  # やり直して新しい report が出た
    assert _status(status_queue)['goals']['-']['verified'] == 0  # 古い approve は新しい report を承認しない


# ---------- #76: web と status が共有する状態判定 (実 queue と同じ形の fixture) ----------

T0 = 1_780_000_000  # 2026-05-28 頃の epoch。created_at / completed_at はこれを基準に書く


def _at(sec: int, tz: str = '+00:00') -> str:
    from datetime import datetime  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415
    from datetime import timezone  # noqa: PLC0415

    hours = int(tz[:3]) if tz else 9  # tz='' は naive (fixture の TZ=Asia/Tokyo のローカル時刻)
    text = datetime.fromtimestamp(T0 + sec, timezone(timedelta(hours=hours))).isoformat(timespec='seconds')
    return text if tz else text[:-6]


@pytest.fixture
def real_queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """実 queue で見た形: mtime の一括更新、naive と +09:00 の混在、別 task_id の report、verdict だけの review."""
    import time  # noqa: PLC0415

    monkeypatch.setenv('TZ', 'Asia/Tokyo')
    time.tzset()
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    (pj / '.squad_session').parent.mkdir(parents=True)
    (pj / '.squad_session').write_text('sq1\n')
    # A: created_at (+09:00) の後に report が出て、その後 tasks/ が一括 touch された -> done
    _write(
        pj / 'tasks' / 'worker1.yaml',
        f'task_id: A\ntitle: "一括 touch された完了済み"\nassigned_to: worker1\ncreated_at: "{_at(0, "+09:00")}"\n'
        'description: |\n  画面を直す\n  二行目\n',
        T0 + 500,
    )
    _write(
        pj / 'reports' / 'worker1_report.yaml',
        f'report_id: "{RID1}"\ntask_id: A\nstatus: completed\nverify_status: pass\n'
        f'summary: |\n  直した\n  二行目\ncompleted_at: "{_at(100, "")}"\n',  # naive = ローカル時刻
        T0 + 600,
    )
    # B: 別 task_id の新しい report しか無い -> running (task_id で突き合わせる)
    _write(
        pj / 'tasks' / 'worker2.yaml',
        f'task_id: B\ntitle: "実行中"\nassigned_to: worker2\ncreated_at: "{_at(200)}"\n',
        T0 + 200,
    )
    _write(pj / 'reports' / 'worker2_report.yaml', 'task_id: OLD\nstatus: completed\nsummary: "前の"\n', T0 + 300)
    # C: created_at 無し。report の completed_at は task より新しいが mtime は古い -> mtime で比べて running
    _write(pj / 'tasks' / 'worker3.yaml', 'task_id: C\ntitle: "再発行"\nassigned_to: worker3\n', T0 + 400)
    _write(
        pj / 'reports' / 'worker3_report.yaml',
        f'task_id: C\nstatus: blocked\nsummary: "前回"\ncompleted_at: "{_at(900)}"\n',
        T0 + 350,
    )
    # D: cross-review。status を持たない verdict だけの review -> done
    _write(pj / 'tasks' / 'worker4.yaml', f'task_id: D\ntitle: "review"\ncreated_at: "{_at(700)}"\n', T0 + 700)
    _write(pj / 'reports' / 'worker4_review.yaml', 'task_id: D\nverdict: request_changes\n', T0 + 800)
    # E: verifier の verdict だけ (fail) -> blocked
    _write(pj / 'tasks' / 'worker5.yaml', 'task_id: E\ntitle: "検証中"\n', T0 + 100)
    _write(pj / 'reports' / 'worker5_verdict.yaml', 'task_id: E\nresult: fail\n', T0 + 150)
    # project ではないディレクトリ
    (q / 'projects' / '.ruff_cache' / 'x').mkdir(parents=True)
    (q / 'projects' / 'empty').mkdir()
    yield q
    monkeypatch.delenv('TZ')
    time.tzset()


def _intents(queue: Path) -> dict[str, tuple[str, dict]]:
    import json  # noqa: PLC0415

    graph.sync(queue)
    rows = _rows(graph.default_db_path(queue), "SELECT node_id, state, payload_json FROM nodes WHERE kind = 'intent'")
    return {r[0].split('/')[-1]: (r[1], json.loads(r[2])) for r in rows}


def test_state_uses_created_at_completed_at_task_id_and_verdicts(real_queue: Path) -> None:
    intents = _intents(real_queue)
    assert {k: v[0] for k, v in intents.items()} == {
        'A': 'done',  # verify は pass だが review が無いので verified ではない
        'B': 'running',
        'C': 'running',
        'D': 'done',
        'E': 'blocked',
    }
    a = intents['A'][1]
    assert a['description'] == '画面を直す\n二行目\n'
    assert a['current'] is True
    assert a['finished_at'] == _iso_utc(100)  # naive の completed_at を Asia/Tokyo として UTC にした値
    assert intents['B'][1]['finished_at'] == ''


def _iso_utc(sec: int) -> str:
    from datetime import datetime  # noqa: PLC0415
    from datetime import timezone  # noqa: PLC0415

    return datetime.fromtimestamp(T0 + sec, timezone.utc).isoformat(timespec='seconds')


def test_status_matches_db_states(real_queue: Path) -> None:
    pj = _status(real_queue)
    assert pj['goals']['-'] == {'running': 2, 'blocked': 1, 'done': 2, 'verified': 0, 'closed': 0}
    assert {u['node_id'] for u in pj['unfinished']} == {'intent:pj/B', 'intent:pj/C', 'intent:pj/E'}


def test_sync_stores_projects_and_synced_at(real_queue: Path) -> None:
    graph.sync(real_queue)
    db = graph.default_db_path(real_queue)
    rows = _rows(db, 'SELECT project, session, latest_result, updated_at FROM projects')
    assert [r[:2] for r in rows] == [('pj', 'sq1')]  # .ruff_cache と空ディレクトリは出ない
    # 最新の結果は completed_at (無ければ mtime) が一番新しい report (C の completed_at = +900)
    latest = _rows(db, f"SELECT title FROM nodes WHERE node_id = '{rows[0][2]}'")
    assert latest == [('前回',)]
    assert rows[0][3] == _iso_utc(900)
    result = _rows(db, f"SELECT payload_json FROM nodes WHERE node_id = 'result:{RID1}'")[0][0]
    assert '"summary": "直した\\n二行目\\n"' in result
    (synced_at,) = _rows(db, "SELECT value FROM meta WHERE key = 'synced_at'")[0]
    assert synced_at.endswith('+00:00')


# ---------- #76 addendum: ゴールの組み分けと完了 ----------


def test_goals_group_derived_tasks_by_task_id_prefix(tmp_path: Path) -> None:
    """実 queue の命名: SQUAD-284 / -fix1 / -fix2 / -rework、SQUAD-016-review、parent_issue 付きの派生、単独 task."""
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'squad'
    t = pj / 'tasks' / 'archive'
    _write(t / 'worker1_SQUAD-284.yaml', 'task_id: SQUAD-284\ntitle: "web dashboard"\n', 100)
    _write(t / 'worker1_SQUAD-284-fix1.yaml', 'task_id: SQUAD-284-fix1\ntitle: "fix1"\n', 200)
    _write(pj / 'tasks' / 'worker1.yaml', 'task_id: SQUAD-284-rework\ntitle: "作り直し"\n', 300)
    _write(t / 'worker4_SQUAD-016-review.yaml', 'task_id: SQUAD-016-review\ntitle: "review だけ残った"\n', 100)
    _write(t / 'worker1_SQUAD-100.yaml', 'task_id: SQUAD-100\ntitle: "親"\ncontext:\n  parent_issue: 9\n', 100)
    _write(t / 'worker1_SQUAD-100-fix1.yaml', 'task_id: SQUAD-100-fix1\ntitle: "親の修正"\n', 200)
    _write(t / 'worker2_issue34-bench.yaml', 'task_id: issue34-bench\ntitle: "単独"\n', 100)
    for tid, mtime in (('SQUAD-284', 110), ('SQUAD-284-fix1', 210), ('SQUAD-016-review', 110)):
        _write(pj / 'reports' / 'archive' / f'{tid}.yaml', f'task_id: {tid}\nstatus: completed\n', mtime)
    for tid in ('SQUAD-100', 'SQUAD-100-fix1', 'issue34-bench'):
        _write(pj / 'reports' / 'archive' / f'{tid}.yaml', f'task_id: {tid}\nstatus: completed\n', 150)
    graph.sync(q)
    db = graph.default_db_path(q)
    spawns: dict[str, set[str]] = {}
    for src, dst in _rows(db, "SELECT src_id, dst_id FROM edges WHERE rel = 'spawns'"):
        spawns.setdefault(src, set()).add(dst.split('/', 1)[1])
    assert spawns == {
        'goal:squad/SQUAD-284': {'SQUAD-284', 'SQUAD-284-fix1', 'SQUAD-284-rework'},
        'goal:squad/SQUAD-016': {'SQUAD-016-review'},  # 根の task が無くても頭でまとめる
        'goal:squad#9': {'SQUAD-100', 'SQUAD-100-fix1'},  # 根が Issue を持てば派生もその Issue へ
        'goal:squad/issue34-bench': {'issue34-bench'},  # 組めない task は 1 件で 1 ゴール
    }
    goals = {r[0]: r[1:] for r in _rows(db, "SELECT node_id, state, title FROM nodes WHERE kind = 'goal'")}
    assert goals['goal:squad/SQUAD-284'] == ('running', 'SQUAD-284 web dashboard')  # 題名は根の task
    assert goals['goal:squad#9'] == ('done', 'Issue 9 親')  # 配下が全部 done なら完了
    assert goals['goal:squad/SQUAD-016'][0] == 'done'
    # status は従来どおり Issue のゴールだけで集計する
    assert set(graph.status(db)['squad']['goals']) == {'goal:squad#9', '-'}


def test_issue_url_goal_merges_tasks_across_projects(tmp_path: Path) -> None:
    """同じ Issue の URL を別 project の task が指しても、ゴールの配下は全部残る."""
    q = tmp_path / 'queue'
    url = 'https://github.com/o/r/issues/1'
    for pj, state in (('a', 'completed'), ('b', 'blocked')):
        task = f'task_id: T\ncontext:\n  parent_issue: {url}\n'
        _write(q / 'projects' / pj / 'tasks' / 'archive' / 'w.yaml', task, 1)
        _write(q / 'projects' / pj / 'reports' / 'archive' / 'w.yaml', f'task_id: T\nstatus: {state}\n', 2)
    graph.sync(q)
    db = graph.default_db_path(q)
    assert set(_rows(db, "SELECT dst_id FROM edges WHERE src_id = 'goal:o/r#1'")) == {('intent:a/T',), ('intent:b/T',)}
    assert _rows(db, "SELECT state FROM nodes WHERE node_id = 'goal:o/r#1'") == [('blocked',)]


def test_archived_task_without_result_is_closed(tmp_path: Path) -> None:
    """#78: archive にあって結果が無い task は「完了 (報告なし)」。tasks/ 直下で結果が無いものは running のまま."""
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(pj / 'tasks' / 'archive' / 'a.yaml', 'task_id: A-1\ncontext:\n  parent_issue: 1\n', 100)  # 報告なし
    _write(pj / 'tasks' / 'archive' / 'b.yaml', 'task_id: B-1\ncontext:\n  parent_issue: 1\n', 100)  # done
    _write(pj / 'reports' / 'archive' / 'b.yaml', f'report_id: "{RID1}"\ntask_id: B-1\nstatus: completed\n', 200)
    _write(pj / 'tasks' / 'archive' / 'c.yaml', 'task_id: C-1\ncontext:\n  parent_issue: 2\n', 100)  # 報告なしだけ
    _write(pj / 'tasks' / 'archive' / 'd.yaml', 'task_id: D-1\ncontext:\n  parent_issue: 3\n', 100)  # 報告なし
    _write(pj / 'tasks' / 'worker1.yaml', 'task_id: D-2\ncontext:\n  parent_issue: 3\n', 300)  # 作業中
    graph.sync(q)
    db = graph.default_db_path(q)
    nodes = dict(_rows(db, 'SELECT node_id, state FROM nodes'))
    assert nodes['intent:pj/A-1'] == 'closed'
    assert nodes['intent:pj/C-1'] == 'closed'
    assert nodes['intent:pj/D-2'] == 'running'
    assert nodes['goal:pj#1'] == 'done'  # 配下が closed と done だけ
    assert nodes['goal:pj#2'] == 'closed'  # 結果が 1 件も残っていないゴールは灰色のまま
    assert nodes['goal:pj#3'] == 'running'
    goals = graph.status(db)['pj']['goals']
    assert goals['goal:pj#1'] == {'running': 0, 'blocked': 0, 'done': 1, 'verified': 0, 'closed': 1}
    assert [u['node_id'] for u in graph.status(db)['pj']['unfinished']] == ['intent:pj/D-2']
    assert '| (closed (完了・報告なし) だけの goal 1 件) |' in '\n'.join(graph.render_status(graph.status(db)))


def test_archive_copy_of_review_keeps_review_flag(tmp_path: Path) -> None:
    """Watchd の写し (<task_id>__<id>.yaml) は名前に _review を含まないので verdict で review と判定する."""
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(pj / 'tasks' / 'archive' / 'r.yaml', 'task_id: R-1\n', 100)
    _write(
        pj / 'reports' / 'archive' / f'R-1__{RID1}.yaml',
        f'report_id: "{RID1}"\ntask_id: R-1\nstatus: completed\nverdict: approve\n',
        200,
    )
    graph.sync(q)
    [(payload,)] = _rows(graph.default_db_path(q), f"SELECT payload_json FROM nodes WHERE node_id = 'result:{RID1}'")
    assert '"review": true' in payload


def test_issued_at_is_min_of_created_at_and_mtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """活動タブの発注時刻は min(created_at, task YAML の mtime)。未来の created_at は mtime で出す (#84)."""
    import time  # noqa: PLC0415

    monkeypatch.setenv('TZ', 'Asia/Tokyo')
    time.tzset()
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    _write(pj / '.squad_session', 'sq1\n', T0)
    _write(pj / 'tasks' / 'worker1.yaml', f'task_id: FUTURE\ncreated_at: "{_at(5400, "+09:00")}"\n', T0)  # 未来
    _write(pj / 'tasks' / 'worker2.yaml', f'task_id: PAST\ncreated_at: "{_at(-600, "+09:00")}"\n', T0)  # 過去
    _write(pj / 'tasks' / 'worker3.yaml', f'task_id: NAIVE\ncreated_at: "{_at(3600, "")}"\n', T0)  # naive の未来
    _write(pj / 'tasks' / 'worker4.yaml', 'task_id: NONE\n', T0)  # created_at 無し
    intents = _intents(tmp_path / 'queue')
    assert intents['FUTURE'][1]['issued_at'] == _iso_utc(0)
    assert intents['PAST'][1]['issued_at'] == _iso_utc(-600)
    assert intents['NAIVE'][1]['issued_at'] == _iso_utc(0)
    assert intents['NONE'][1]['issued_at'] == _iso_utc(0)


# --- #88: task 同士をつなぐ手がかり -----------------------------------------------------------


def _link_queue(tmp_path: Path, tasks: dict[str, str]) -> tuple[dict[str, str], set[tuple[str, str]]]:
    """task_id -> YAML 本文 (context 以降) から sync し、(task -> goal, derived_from) を返す."""
    q = tmp_path / 'queue'
    for i, (tid, body) in enumerate(tasks.items()):
        _write(q / 'projects' / 'pj' / 'tasks' / f'{tid}.yaml', f'task_id: {tid}\n{body}', 100 + i)
    db = tmp_path / 'g.db'
    graph.sync(q, db)
    goal = {r[0]: r[1] for r in _rows(db, "SELECT dst_id, src_id FROM edges WHERE rel = 'spawns'")}
    derived = set(_rows(db, "SELECT src_id, dst_id FROM edges WHERE rel = 'derived_from'"))
    return {k.split('/')[1]: v for k, v in goal.items()}, {(a.split('/')[1], b.split('/')[1]) for a, b in derived}


def test_parent_issue_spellings_share_one_goal(tmp_path: Path) -> None:
    ctx = 'context:\n  parent_issue: {}\n'
    goal, _ = _link_queue(
        tmp_path,
        {
            'A-1': ctx.format('79'),
            'A-2': ctx.format('"#79"'),
            'A-3': ctx.format('h-wata/pj#79'),
            'A-4': ctx.format('"79, 80"'),  # カンマ区切りは最初の Issue
            'A-5': ctx.format('"#80"'),
        },
    )
    assert len({goal[t] for t in ('A-1', 'A-2', 'A-3', 'A-4')}) == 1
    assert goal['A-1'] == 'goal:pj#79'
    assert goal['A-5'] == 'goal:pj#80'


def test_task_id_reference_joins_goal_and_derives(tmp_path: Path) -> None:
    goal, derived = _link_queue(
        tmp_path,
        {
            'S-1': 'context:\n  parent_issue: 5\n',
            'S-2': 'context:\n  parent_issue: S-1\n',  # parent_issue に task_id
            'S-3': 'context:\n  parent_task: "S-2 (レビュー済み)"\n',
            'S-9': 'context:\n  parent_issue: 6\n',
        },
    )
    assert goal['S-1'] == goal['S-2'] == goal['S-3'] == 'goal:pj#5'
    assert derived == {('S-2', 'S-1'), ('S-3', 'S-2')}
    assert goal['S-9'] == 'goal:pj#6'


def test_same_pr_url_shares_goal(tmp_path: Path) -> None:
    pr = 'context:\n  pr_url: https://github.com/o/r/pull/7{}\n'
    goal, _ = _link_queue(
        tmp_path,
        {
            'P-1': pr.format(''),
            'P-2': pr.format('/'),
            'P-3': 'context:\n  pr_url: https://github.com/o/r/pull/8\n',
        },
    )
    assert goal['P-1'] == goal['P-2'] != goal['P-3']


def test_review_fields_derive_from_original_task(tmp_path: Path) -> None:
    q = tmp_path / 'queue'
    _write(q / 'projects' / 'pj' / 'reports' / 'worker4_review.yaml', 'task_id: R-1\nverdict: approve\n', 50)
    goal, derived = _link_queue(
        tmp_path,
        {
            'I-1': 'context:\n  parent_issue: 3\n',
            'R-1': 'context:\n  parent_issue: 3\n  review_report: /x/queue/projects/pj/reports/worker4_review.yaml\n',
            'F-1': 'context:\n  prior_review: /x/queue/projects/pj/reports/archive/worker4_review_I-1.yaml\n',
            'R-2': 'context:\n  previous_review: /x/queue/projects/pj/reports/worker4_review.yaml\n',
        },
    )
    assert ('F-1', 'I-1') in derived  # ファイル名の task_id
    assert ('R-2', 'R-1') in derived  # report の中の task_id
    assert ('R-1', 'R-1') not in derived  # 自分自身へは張らない
    assert goal['I-1'] == goal['F-1'] == goal['R-1'] == goal['R-2']


def test_links_never_merge_different_issues(tmp_path: Path) -> None:
    goal, derived = _link_queue(
        tmp_path,
        {
            'X-1': 'context:\n  parent_issue: 1\n  pr_url: https://github.com/o/r/pull/9\n',
            'X-2': 'context:\n  parent_issue: 2\n  pr_url: https://github.com/o/r/pull/9\n  parent_task: X-1\n',
        },
    )
    assert goal['X-1'] == 'goal:pj#1'
    assert goal['X-2'] == 'goal:pj#2'
    assert derived == set()  # 別 Issue の task へは derived_from も張らない
