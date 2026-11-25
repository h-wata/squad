#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Task 完了時の自動評価 (squad/task_eval.py と watchd のフック, Refs #130, #136) のテスト.

watchd は実際に子プロセス (task_eval.py か、テストが置いた偽の評価スクリプト) を起動する。
transcript は tmp の HOME 下の ~/.claude/projects に置き、子プロセスに HOME を渡して読ませる。
"""

from __future__ import annotations

import json
import sqlite3
import os
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'squad'))
sys.path.insert(0, str(ROOT / 'scripts'))
import task_eval  # noqa: E402
import watchd  # noqa: E402
from watchd import Config  # noqa: E402
from watchd import Watcher  # noqa: E402

from test_watchd import FakeTmux  # noqa: E402
from test_watchd import ID1  # noqa: E402
from test_watchd import ID2  # noqa: E402
from test_watchd import make_project  # noqa: E402
from test_watchd import queued  # noqa: E402

SID = '44444444-4444-4444-8444-444444444444'
START = 1_790_899_200  # 2026-10-02T00:00:00Z
T1 = '2026-10-02T00:30:00Z'
T_END = '2026-10-02T01:00:00Z'


def _transcript(home: Path) -> None:
    rows = [
        {
            'type': 'assistant',
            'timestamp': T1,
            'message': {'content': [{'type': 'tool_use', 'id': 'tu1', 'name': 'Bash', 'input': {'command': 'ls'}}]},
        },
        {
            'type': 'user',
            'timestamp': T1,
            'message': {
                'content': [{'type': 'tool_result', 'tool_use_id': 'tu1', 'is_error': False, 'content': 'ok'}]
            },
        },
    ]
    p = home / '.claude' / 'projects' / 'ws' / f'{SID}.jsonl'
    p.parent.mkdir(parents=True)
    p.write_text('\n'.join(json.dumps(r) for r in rows) + '\n')


def _write(d: Path, status: str = 'completed', report_id: str = ID1, name: str = 'worker1_report.yaml') -> Path:
    task = d / 'tasks' / 'worker1.yaml'
    task.write_text('task_id: T1\ncontext:\n  workspace: /work/wt\n')
    os.utime(task, (START, START))
    p = d / 'reports' / name
    p.write_text(
        f'report_id: "{report_id}"\ntask_id: T1\nstatus: {status}\nworker: worker1\nagent: claude\n'
        f'session_id: "{SID}"\ncompleted_at: "{T_END}"\n'
    )
    return p


def _fake(tmp_path: Path, body: str) -> list[str]:
    """偽の評価スクリプト (引数は無視する) の argv."""
    p = tmp_path / 'fake_eval.py'
    p.write_text(body)
    return [sys.executable, str(p)]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Watcher, Path, Path]:
    home = tmp_path / 'home'
    _transcript(home)
    monkeypatch.setenv('HOME', str(home))  # 子プロセスの ~/.claude/projects を tmp にする
    queue = tmp_path / 'queue'
    out = tmp_path / 'metrics' / 'task_evals.jsonl'
    cfg = Config(
        session='testsess',
        default_owner='not-this-session',
        queue_dir=queue,
        interval=1,
        lease_seconds=60,
        notify_queue_enabled=True,
        task_eval_enabled=True,
        task_evals_file=out,
        task_eval_timeout=60,
    )
    w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
    w.sleep = lambda _s: None
    d = make_project(queue, 'pj', session='testsess')
    w.refresh_owned_projects()
    return w, d, out


def _drain(w: Watcher, limit: float = 60.0) -> None:
    """評価の子プロセスが全部終わるまで見回る."""
    deadline = time.monotonic() + limit
    while w._evals_waiting or w._evals_running:  # noqa: SLF001
        assert time.monotonic() < deadline, '評価の子プロセスが終わらない'
        w.poll_task_evals()
        time.sleep(0.05)


def _rows(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []


def _delivered(w: Watcher) -> bool:
    return bool(queued(w)) and w.ledger.is_delivered('pj', ID1)


def test_final_report_appends_one_row(env: tuple[Watcher, Path, Path]) -> None:
    w, d, out = env
    _write(d)
    w.report_bridge()
    _drain(w)
    rows = _rows(out)
    assert len(rows) == 1
    row = rows[0]
    assert row['error'] is None, row['error']
    assert {k: row[k] for k in ('project', 'task_id', 'report_id', 'worker', 'agent', 'status')} == {
        'project': 'pj',
        'task_id': 'T1',
        'report_id': ID1,
        'worker': 'worker1',
        'agent': 'claude',
        'status': 'completed',
    }
    assert row['tool_calls'] == 1
    assert row['attribution'] == 'exact'
    assert 'verify_attempt' in row and 'verify_result' in row and row['evaluated_at']


def test_blocked_report_is_evaluated(env: tuple[Watcher, Path, Path]) -> None:
    w, d, out = env
    _write(d, status='blocked')
    w.report_bridge()
    _drain(w)
    assert [r['status'] for r in _rows(out)] == ['blocked']


def test_child_crash_keeps_delivery_and_logs_error_row(env: tuple[Watcher, Path, Path], tmp_path: Path) -> None:
    w, d, out = env
    w.cfg.task_eval_cmd = _fake(tmp_path, 'import sys\nsys.stderr.write("boom from eval\\n")\nsys.exit(7)\n')
    _write(d)
    w.cycle()
    assert _delivered(w)
    _drain(w)
    rows = _rows(out)
    assert len(rows) == 1
    assert rows[0]['report_id'] == ID1
    assert rows[0]['error'].startswith('exit=7') and 'boom from eval' in rows[0]['error']


def test_spawn_exception_keeps_delivery(env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    w, d, out = env

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError('no fork today')

    monkeypatch.setattr(watchd.subprocess, 'Popen', boom)
    _write(d)
    w.cycle()
    assert _delivered(w)
    rows = _rows(out)
    assert len(rows) == 1 and 'no fork today' in rows[0]['error']


def test_schedule_exception_keeps_delivery(env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    w, d, _out = env

    def boom(_out: Path) -> set[str]:
        raise OSError('metrics unreadable')

    monkeypatch.setattr(task_eval, 'evaluated_ids', boom)
    _write(d)
    w.cycle()
    assert _delivered(w)
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001


def test_timeout_does_not_block_delivery(
    env: tuple[Watcher, Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w, d, out = env
    monkeypatch.setattr(watchd, 'TASK_EVAL_KILL_GRACE', 0)
    w.cfg.task_eval_timeout = 1
    w.cfg.task_eval_cmd = _fake(tmp_path, 'import time\ntime.sleep(60)\n')
    _write(d)
    t0 = time.monotonic()
    w.cycle()
    assert time.monotonic() - t0 < 1.0  # 子の終了を待たない
    assert _delivered(w)
    job = next(iter(w._evals_running.values()))  # noqa: SLF001
    time.sleep(1.2)
    w.poll_task_evals()  # kill する (待たない)
    assert job.killed_at is not None
    _drain(w, limit=10)  # 次の見回り以降で回収する
    assert job.proc.poll() is not None  # kill された
    rows = _rows(out)
    assert len(rows) == 1 and rows[0]['error'].startswith('timeout')


def test_same_report_id_is_not_evaluated_twice(env: tuple[Watcher, Path, Path], tmp_path: Path) -> None:
    w, d, out = env
    _write(d)
    w.report_bridge()
    _drain(w)
    assert len(_rows(out)) == 1
    # watchd の再起動 + ledger を作り直した再配達: 同じ report_id は評価しない
    cfg2 = Config(**{**w.cfg.__dict__, 'ledger_file': str(tmp_path / 'other.db')})
    w2 = Watcher(cfg=cfg2, tmux=FakeTmux('testsess'))
    w2.refresh_owned_projects()
    w2.report_bridge()
    assert w2.ledger.is_delivered('pj', ID1)  # 再配達は起きた
    assert not w2._evals_waiting  # noqa: SLF001
    _drain(w2)
    assert len(_rows(out)) == 1


def test_append_row_dedupes_under_lock(tmp_path: Path) -> None:
    out = tmp_path / 'm' / 'e.jsonl'
    row = task_eval.base_row('pj', 'T1', ID1, 'worker1', 'claude', 'completed')
    assert task_eval.append_row(out, row) is True
    assert task_eval.append_row(out, {**row, 'error': 'again'}) is False
    assert task_eval.append_row(out, {**row, 'report_id': ID2}) is True
    assert [r['report_id'] for r in _rows(out)] == [ID1, ID2]


def test_child_skips_already_evaluated(tmp_path: Path) -> None:
    out = tmp_path / 'e.jsonl'
    task_eval.append_row(out, task_eval.base_row('pj', 'T1', ID1, 'worker1', 'claude', 'completed'))
    argv = ['--project', 'pj', '--task-id', 'T1', '--report-id', ID1, '--queue-dir', str(tmp_path), '--out', str(out)]
    assert task_eval.main(argv) == task_eval.EXIT_DUPLICATE
    assert len(_rows(out)) == 1


@pytest.mark.parametrize(
    ('status', 'name'),
    [
        ('in_progress', 'worker1_report.yaml'),
        ('failed', 'worker1_report.yaml'),
        ('completed', 'worker1_review.yaml'),
    ],
)
def test_non_final_report_is_not_evaluated(env: tuple[Watcher, Path, Path], status: str, name: str) -> None:
    w, d, out = env
    _write(d, status=status, name=name)
    w.report_bridge()
    assert w.ledger.is_delivered('pj', ID1)
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001
    assert not out.exists()


def _args(tmp_path: Path, timeout: int = 0) -> object:
    return task_eval.argparse.Namespace(
        project='pj',
        task_id='T1',
        report_id=ID1,
        worker='worker1',
        agent='claude',
        status='completed',
        queue_dir=tmp_path,
        out=tmp_path / 'e.jsonl',
        timeout=timeout,
    )


def test_run_records_import_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, 'eval_trajectory', None)  # import すると ImportError
    row = task_eval.run(_args(tmp_path))
    assert 'ImportError' in row['error'] or 'ModuleNotFoundError' in row['error']
    assert row['report_id'] == ID1


def test_run_records_evaluate_exception(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import eval_trajectory  # noqa: PLC0415

    def boom(*_a: object) -> None:
        raise ValueError('bad transcript')

    monkeypatch.setattr(eval_trajectory, 'evaluate', boom)
    row = task_eval.run(_args(tmp_path))
    assert 'bad transcript' in row['error']


def test_run_records_own_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import eval_trajectory  # noqa: PLC0415

    monkeypatch.setattr(eval_trajectory, 'evaluate', lambda *_a: time.sleep(5))
    t0 = time.monotonic()
    row = task_eval.run(_args(tmp_path, timeout=1))
    assert time.monotonic() - t0 < 3
    assert row['error'].startswith('timeout')


# ---- PR #142 review F1-F3 ----

ID3 = '33333333-3333-4333-8333-333333333333'
SINCE_EARLY = '2000-01-01T00:00:00+00:00'
SLOW = 'import time\ntime.sleep(30)\n'


def _write_many(d: Path, ids: list[str]) -> None:
    """worker1..N の最終 report を 1 件ずつ書く (task はどれも T1)."""
    for n, rid in enumerate(ids, 1):
        _write(d, report_id=rid, name=f'worker{n}_report.yaml')


def _kill_all(*ws: Watcher) -> None:
    for w in ws:
        for job in w._evals_running.values():  # noqa: SLF001
            if job.proc is not None and job.proc.poll() is None:
                job.proc.kill()
                job.proc.wait()


class _StuckProc:
    """kill しても終わらない子 (回収できない状態). wait は timeout まで待って TimeoutExpired を投げる."""

    def __init__(self) -> None:
        self.killed = False

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> None:
        time.sleep(timeout or 0)
        raise watchd.subprocess.TimeoutExpired('stuck', timeout or 0)


def _stuck_job(w: Watcher) -> tuple[watchd.EvalJob, _StuckProc]:
    proc = _StuckProc()
    job = watchd.EvalJob(
        base=task_eval.base_row('pj', 'T1', ID1, 'worker1', 'claude', 'completed'),
        argv=[],
        proc=proc,  # type: ignore[arg-type]
        started=w.mono() - w.cfg.task_eval_timeout - watchd.TASK_EVAL_KILL_GRACE - 1,  # 上限を過ぎている
    )
    w._evals_running[ID1] = job  # noqa: SLF001
    w._eval_rescan_at = w.mono() + 3600  # noqa: SLF001 (再走査はこのテストの対象外)
    return job, proc


def test_running_is_capped_at_max(env: tuple[Watcher, Path, Path], tmp_path: Path) -> None:
    """同時に走る評価は 2 件まで。残りは待つ (上限を変えたら落ちる)."""
    w, d, _out = env
    w.cfg.task_eval_cmd = _fake(tmp_path, SLOW)
    _write_many(d, [ID1, ID2, ID3])
    try:
        w.report_bridge()
        w.poll_task_evals()
        assert len(w._evals_running) == 2  # noqa: SLF001
        assert list(w._evals_waiting) == [ID3]  # noqa: SLF001
        w.poll_task_evals()
        assert len(w._evals_running) == 2  # noqa: SLF001 (終わるまで 3 件目は起動しない)
    finally:
        _kill_all(w)


def test_waiting_survives_restart_via_rescan(env: tuple[Watcher, Path, Path], tmp_path: Path) -> None:
    """3 件を同時に配達し 1 件が上限で待っている間に watchd を作り直しても、各 report_id がちょうど 1 行になる (F1)."""
    w, d, out = env
    w.cfg.task_eval_since = SINCE_EARLY
    w.cfg.task_eval_cmd = _fake(tmp_path, SLOW)
    _write_many(d, [ID1, ID2, ID3])
    try:
        w.report_bridge()
        w.poll_task_evals()
        assert list(w._evals_waiting) == [ID3]  # noqa: SLF001
    finally:
        _kill_all(w)  # 再起動: 走っていた評価も待っていた評価も失われる (行は 1 つも書かれていない)
    assert not out.exists()
    cfg2 = Config(**{**w.cfg.__dict__, 'task_eval_cmd': list(watchd.Config().task_eval_cmd)})
    w2 = Watcher(cfg=cfg2, tmux=FakeTmux('testsess'))
    w2.refresh_owned_projects()
    w2.report_bridge()
    assert not w2._evals_waiting  # noqa: SLF001 (配達済みなので再配達・再予約は起きない)
    deadline = time.monotonic() + 60
    while len(_rows(out)) < 3:
        assert time.monotonic() < deadline, _rows(out)
        w2.poll_task_evals()
        w2._eval_rescan_at = 0.0  # noqa: SLF001 (次の見回りでも再走査する)
        time.sleep(0.05)
    _drain(w2)
    w2._eval_rescan_at = 0.0  # noqa: SLF001
    w2.poll_task_evals()  # もう一度再走査しても行は増えない
    _drain(w2)
    assert sorted(r['report_id'] for r in _rows(out)) == sorted([ID1, ID2, ID3])


def test_waiting_queue_is_capped_and_overflow_is_rescanned(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """待ち行列は上限まで。溢れた分は捨てず、次の再走査で拾い直す (F1)."""
    w, d, out = env
    monkeypatch.setattr(watchd, 'TASK_EVAL_MAX_WAITING', 1)
    w.cfg.task_eval_since = SINCE_EARLY
    _write_many(d, [ID1, ID2, ID3])
    w.report_bridge()
    assert len(w._evals_waiting) == 1  # noqa: SLF001 (2 件は溢れた)
    deadline = time.monotonic() + 60
    while len(_rows(out)) < 3:
        assert time.monotonic() < deadline, _rows(out)
        assert len(w._evals_waiting) <= 1  # noqa: SLF001
        w.poll_task_evals()
        w._eval_rescan_at = 0.0  # noqa: SLF001
        time.sleep(0.05)
    assert sorted(r['report_id'] for r in _rows(out)) == sorted([ID1, ID2, ID3])


def test_rescan_skips_reports_before_since(env: tuple[Watcher, Path, Path]) -> None:
    """自動評価を入れる前に配達した report は再走査で評価しない (起点は queue/.task_eval_since).

    導入前かどうかは ledger の delivered_at で決め、写しの mtime は見ない (N4)。写しを touch して新しくしても拾わない。
    """
    w, d, out = env
    w.cfg.task_eval_enabled = False  # 導入前の watchd が配達した
    _write(d)
    w.report_bridge()
    future = time.time() + 3600
    for p in (d / 'reports' / 'archive').glob('*.yaml'):
        os.utime(p, (future, future))  # cp / restore / touch で写しの mtime が新しくなった
    cfg2 = Config(**{**w.cfg.__dict__, 'task_eval_enabled': True})
    w2 = Watcher(cfg=cfg2, tmux=FakeTmux('testsess'))
    w2.refresh_owned_projects()
    w2.cycle()
    assert w2.task_eval_since_file.exists()  # 起点を記録した
    assert not w2._evals_waiting and not w2._evals_running  # noqa: SLF001
    assert not out.exists()
    # 起点を前にずらせば (WATCH_TASK_EVAL_SINCE) 拾う
    w2.cfg.task_eval_since = SINCE_EARLY
    w2._eval_rescan_at = 0.0  # noqa: SLF001
    w2.poll_task_evals()
    _drain(w2)
    assert [r['report_id'] for r in _rows(out)] == [ID1]


def test_rescan_skips_undelivered_reports(env: tuple[Watcher, Path, Path]) -> None:
    """Archive に写しがあっても ledger で配達済みでなければ評価しない."""
    w, d, out = env
    w.cfg.task_eval_since = SINCE_EARLY
    arch = d / 'reports' / 'archive'
    arch.mkdir()
    (arch / f'T1__{ID1}.yaml').write_text(f'report_id: "{ID1}"\ntask_id: T1\nstatus: completed\n')
    w.poll_task_evals()
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001
    assert not out.exists()


def test_poll_returns_immediately_after_kill(env: tuple[Watcher, Path, Path]) -> None:
    """時間切れの子を kill した後、終了を待たずにすぐ戻る (F2)."""
    w, _d, out = env
    job, proc = _stuck_job(w)
    t0 = time.monotonic()
    w.poll_task_evals()
    assert time.monotonic() - t0 < 0.5
    assert proc.killed and job.killed_at is not None
    assert ID1 in w._evals_running  # noqa: SLF001 (次の見回りで回収する)
    assert not out.exists()
    t0 = time.monotonic()
    w.poll_task_evals()  # 回収を待つ見回りもすぐ戻る
    assert time.monotonic() - t0 < 0.5


def test_unreapable_child_writes_error_row_and_warns(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill 後も回収できない状態が TASK_EVAL_REAP_GRACE を過ぎたら error 行 + WARN を残して見放す (F2)."""
    w, _d, out = env
    logs: list[str] = []
    monkeypatch.setattr(w, 'log', logs.append)
    monkeypatch.setattr(watchd, 'TASK_EVAL_REAP_GRACE', 0)
    _stuck_job(w)
    w.poll_task_evals()  # kill
    assert not out.exists()
    w.poll_task_evals()  # 回収できないまま猶予を過ぎた
    assert ID1 not in w._evals_running  # noqa: SLF001
    rows = _rows(out)
    assert len(rows) == 1 and rows[0]['error'].startswith('timeout: kill 後')
    assert any(m.startswith('[WARN] task 評価に失敗') for m in logs)


def test_commit_failure_does_not_schedule_eval(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ledger.commit() に失敗したら、評価の予約・子の起動・行の追記は起きず、通知の再送状態が残る (F3)."""
    w, d, out = env
    w.cfg.task_eval_since = SINCE_EARLY
    spawned: list[object] = []
    real_popen = watchd.subprocess.Popen

    def popen(*a: object, **k: object) -> object:
        spawned.append(a)
        return real_popen(*a, **k)

    monkeypatch.setattr(watchd.subprocess, 'Popen', popen)
    monkeypatch.setattr(w.ledger, 'commit', lambda *_a, **_k: False)
    _write(d)
    w.cycle()
    assert queued(w)  # 通知は queue に積まれた
    assert not w.ledger.is_delivered('pj', ID1)
    conn = sqlite3.connect(w.cfg.ledger_path)
    try:
        state, attempts, next_at = conn.execute(
            'SELECT state, attempt_count, next_attempt_at FROM deliveries WHERE project = ? AND report_id = ?',
            ('pj', ID1),
        ).fetchone()
    finally:
        conn.close()
    assert state == 'pending' and attempts >= 1 and next_at > 0  # backoff 後に再送される
    w.poll_task_evals()
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001
    assert spawned == []
    assert not out.exists()


# ---- PR #142 re-review N1-N4 ----

LEASE_EVAL = """import sys, time
from pathlib import Path
sys.path.insert(0, {squad!r})
sys.path.insert(0, {scripts!r})
import eval_trajectory
import task_eval

def fake(queue_dir, project, task_id):
    with open({counter!r}, 'a') as f:
        f.write(task_id + '\\n')
    time.sleep(1.0)  # 重い評価の代わり (この間にもう一方の watcher の子が lock を取りに来る)
    return {{'project': project, 'task_id': task_id, 'worker': 'worker1', 'tool_calls': 0}}

eval_trajectory.evaluate = fake
sys.exit(task_eval.main(sys.argv[1:]))
"""


def _watcher_like(w: Watcher, **over: object) -> Watcher:
    """同じ queue / ledger / 出力先を見る別の watcher (再起動か、同じ project を見るもう 1 つの watchd)."""
    w2 = Watcher(cfg=Config(**{**w.cfg.__dict__, **over}), tmux=FakeTmux('testsess'))
    w2.sleep = lambda _s: None
    w2.refresh_owned_projects()
    return w2


def test_since_is_recorded_before_first_delivery(env: tuple[Watcher, Path, Path]) -> None:
    """起点 queue/.task_eval_since は最初の report_bridge() より前に作る (N1)."""
    w, d, _out = env
    _write(d)
    seen: list[bool] = []
    real = w.report_bridge

    def bridge() -> None:
        seen.append(w.task_eval_since_file.exists())
        real()

    w.report_bridge = bridge  # type: ignore[method-assign]
    w.cycle()
    assert seen == [True]
    since = watchd._parse_iso(w.task_eval_since_file.read_text().strip())  # noqa: SLF001
    conn = sqlite3.connect(w.cfg.ledger_path)
    try:
        (at,) = conn.execute('SELECT delivered_at FROM deliveries WHERE report_id = ?', (ID1,)).fetchone()
    finally:
        conn.close()
    assert since is not None and at >= since


def test_since_file_is_not_overwritten(env: tuple[Watcher, Path, Path]) -> None:
    """既にある起点はそのまま使う (O_EXCL で作る。別の watchd や再起動で書き換えない)."""
    w, _d, _out = env
    w.task_eval_since_file.parent.mkdir(parents=True, exist_ok=True)
    w.task_eval_since_file.write_text('2026-01-01T00:00:00+09:00\n')
    w.cycle()
    assert w.task_eval_since_file.read_text() == '2026-01-01T00:00:00+09:00\n'
    assert w.task_eval_since() == watchd._parse_iso('2026-01-01T00:00:00+09:00')  # noqa: SLF001


def test_failed_schedule_is_recovered_by_rescan_after_restart(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6: 配達は確定したが予約に失敗し、そのまま watchd が止まった report も、再起動後の再走査で評価される (N1).

    report は起点より前に書かれている (写しの mtime < 起点) が、配達は起点の後なので拾う。
    """
    w, d, out = env
    p = _write(d)
    os.utime(p, (START, START))  # watchd を入れる前に書かれた report
    real_ids = task_eval.evaluated_ids

    def boom(_out: Path) -> set[str]:
        raise OSError('metrics unreadable')

    monkeypatch.setattr(task_eval, 'evaluated_ids', boom)
    w._eval_rescan_at = time.monotonic() + 3600  # noqa: SLF001 (このサイクルでは再走査しない)
    w.cycle()
    assert w.ledger.is_delivered('pj', ID1)
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001 (予約に失敗した)
    monkeypatch.setattr(task_eval, 'evaluated_ids', real_ids)
    time.sleep(1.2)
    w2 = _watcher_like(w)  # 再起動
    w2.cycle()
    assert ID1 in w2._evals_waiting or ID1 in w2._evals_running  # noqa: SLF001
    _drain(w2)
    assert [r['report_id'] for r in _rows(out)] == [ID1]


def test_ledger_rows_without_delivered_at_are_not_rescanned(env: tuple[Watcher, Path, Path]) -> None:
    """delivered_at 列を足す前の ledger で配達済みの行 (0) は、起点を前にずらしても拾わない (N4)."""
    w, d, out = env
    db = w.cfg.ledger_path
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            'CREATE TABLE deliveries (project TEXT NOT NULL, report_id TEXT NOT NULL, path TEXT NOT NULL, '
            'content_sha256 TEXT NOT NULL, state TEXT NOT NULL, lease TEXT NOT NULL, attempt_count INTEGER NOT NULL, '
            'next_attempt_at INTEGER NOT NULL, PRIMARY KEY (project, report_id))'
        )
        conn.execute("INSERT INTO deliveries VALUES ('pj', ?, 'x', 'sha', 'delivered', '', 1, 0)", (ID1,))
        conn.commit()
    finally:
        conn.close()
    arch = d / 'reports' / 'archive'
    arch.mkdir()
    (arch / f'T1__{ID1}.yaml').write_text(f'report_id: "{ID1}"\ntask_id: T1\nstatus: completed\n')
    w.cfg.task_eval_since = '1970-01-01T00:00:00+00:00'
    w.poll_task_evals()
    assert w.ledger.is_delivered('pj', ID1)
    assert w.ledger.delivered_since(['pj'], 0.0) == []  # 列は足され、値は 0 (配達時刻が分からない)
    assert not w._evals_waiting and not w._evals_running  # noqa: SLF001
    assert not out.exists()


class _LateProc:
    """kill しても猶予の間は終わらず、後で終わる子."""

    def __init__(self) -> None:
        self.done = False
        self.pid = 4242

    def poll(self) -> int | None:
        return -9 if self.done else None

    def kill(self) -> None:
        pass


def test_abandoned_child_is_reaped_later(env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """見放した子は見放し中の一覧に残り、遅れて終わったら次の見回りで回収される (N2)."""
    w, _d, out = env
    monkeypatch.setattr(watchd, 'TASK_EVAL_REAP_GRACE', 0)
    job, _ = _stuck_job(w)
    late = _LateProc()
    job.proc = late  # type: ignore[assignment]
    w.poll_task_evals()  # kill
    w.poll_task_evals()  # 猶予を過ぎた -> error 行を書いて見放す
    assert ID1 not in w._evals_running  # noqa: SLF001
    assert w._evals_abandoned == [job]  # noqa: SLF001
    assert len(_rows(out)) == 1
    t0 = time.monotonic()
    w.poll_task_evals()  # まだ終わっていない: 待たずに戻り、一覧に残す
    assert time.monotonic() - t0 < 0.5
    assert w._evals_abandoned == [job]  # noqa: SLF001
    late.done = True
    w.poll_task_evals()
    assert w._evals_abandoned == []  # noqa: SLF001
    assert len(_rows(out)) == 1  # 回収で行は増えない


def test_abandoned_real_child_leaves_no_zombie(env: tuple[Watcher, Path, Path]) -> None:
    """実際の子プロセスを見放し中の一覧に入れ、終わった後の見回りで回収されて zombie が残らない (N2)."""
    w, _d, _out = env
    proc = watchd.subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.3)'])
    w._evals_abandoned.append(watchd.EvalJob(base={}, argv=[], proc=proc))  # noqa: SLF001
    w._eval_rescan_at = time.monotonic() + 3600  # noqa: SLF001
    deadline = time.monotonic() + 10
    while w._evals_abandoned:  # noqa: SLF001
        assert time.monotonic() < deadline
        w.poll_task_evals()
        time.sleep(0.05)
    assert proc.returncode == 0
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, os.WNOHANG)  # 回収済み (zombie なら (pid, status) が返る)


def test_full_abandoned_list_blocks_new_spawns_but_keeps_reaping(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """見放した子が上限まで溜まったら、追跡は外さず新しい評価を起動しない。回収されたら起動を再開する (R2)."""
    w, d, _out = env
    logs: list[str] = []
    monkeypatch.setattr(w, 'log', logs.append)
    monkeypatch.setattr(watchd, 'TASK_EVAL_MAX_ABANDONED', 2)
    spawned: list[object] = []
    monkeypatch.setattr(watchd.subprocess, 'Popen', lambda *a, **_k: spawned.append(a) or _LateProc())
    jobs = [watchd.EvalJob(base={}, argv=[], proc=_LateProc()) for _ in range(3)]  # type: ignore[arg-type]
    for job in jobs:
        w._abandon_task_eval(job)  # noqa: SLF001
    assert w._evals_abandoned == jobs  # noqa: SLF001 (上限を超えても追跡は外さない)
    w._eval_rescan_at = time.monotonic() + 3600  # noqa: SLF001
    _write(d)
    w.report_bridge()
    assert ID1 in w._evals_waiting  # noqa: SLF001
    w.poll_task_evals()
    w.poll_task_evals()
    assert spawned == []  # 満杯の間は起動しない
    assert ID1 in w._evals_waiting  # noqa: SLF001 (待ちは残す)
    assert sum(m.startswith('[WARN] 終わらない task 評価の子が') for m in logs) == 1
    jobs[0].proc.done = True  # type: ignore[attr-defined]  # 一番古い子が遅れて終わった
    w.poll_task_evals()
    assert w._evals_abandoned == jobs[1:]  # noqa: SLF001 (回収された)
    assert spawned == []  # まだ 2 件 (上限) 残っている
    jobs[1].proc.done = True  # type: ignore[attr-defined]
    w.poll_task_evals()
    assert w._evals_abandoned == jobs[2:]  # noqa: SLF001
    assert len(spawned) == 1 and ID1 in w._evals_running  # noqa: SLF001 (上限を下回ったので起動を再開)


def test_lease_allows_one_holder(tmp_path: Path) -> None:
    first = task_eval.acquire_lease(tmp_path, ID1)
    assert first is not None
    assert task_eval.acquire_lease(tmp_path, ID1) is None  # 同じ report_id は取れない
    other = task_eval.acquire_lease(tmp_path, ID2)
    assert other is not None
    first.close()  # type: ignore[attr-defined]
    again = task_eval.acquire_lease(tmp_path, ID1)  # 持ち主が終われば取れる (ファイルは残っていてよい)
    assert again is not None
    assert task_eval.lock_path(tmp_path, ID1).exists()
    again.close()  # type: ignore[attr-defined]
    other.close()  # type: ignore[attr-defined]


def test_two_watchers_rescan_same_archive_evaluate_once(env: tuple[Watcher, Path, Path], tmp_path: Path) -> None:
    """同じ project を見る watcher 2 つが同じ archive を同時に再走査しても、evaluate は 1 回だけ走る (N3)."""
    w, d, out = env
    counter = tmp_path / 'evaluate_calls.txt'
    script = tmp_path / 'lease_eval.py'
    script.write_text(
        LEASE_EVAL.format(squad=str(ROOT / 'squad'), scripts=str(ROOT / 'scripts'), counter=str(counter))
    )
    w.cfg.task_eval_enabled = False  # 予約させずに配達だけ済ませる
    _write(d)
    w.report_bridge()
    cmd = [sys.executable, str(script)]
    wa = _watcher_like(w, task_eval_enabled=True, task_eval_since=SINCE_EARLY, task_eval_cmd=cmd)
    wb = _watcher_like(w, task_eval_enabled=True, task_eval_since=SINCE_EARLY, task_eval_cmd=cmd)
    wa.poll_task_evals()
    wb.poll_task_evals()
    assert ID1 in wa._evals_running and ID1 in wb._evals_running  # noqa: SLF001 (両方が子を起動した)
    _drain(wa)
    _drain(wb)
    assert counter.read_text().splitlines() == ['T1']  # evaluate は 1 回
    rows = _rows(out)
    assert [r['report_id'] for r in rows] == [ID1] and rows[0]['error'] is None


# ---- PR #142 3rd review R1-R3 ----


def test_since_value_is_watchd_start_even_if_creation_fails_first(
    env: tuple[Watcher, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1: 起点ファイルの作成と予約が同じサイクルで一度ずつ失敗しても、起点 > 配達時刻 にならず再起動後に評価される.

    起点の値は起点ファイルを作れた時刻ではなく watchd の起動時刻。作れない間の再走査はメモリ上の起動時刻を使う。
    """
    w, d, out = env
    w.cfg.task_eval_cmd = _fake(tmp_path_of(out), SLOW)  # 子は終わらないうちに watchd が止まる
    since_file = str(w.task_eval_since_file)
    real_open = watchd.os.open
    fails = {'open': 1}

    def flaky_open(path: object, *a: object, **k: object) -> int:
        if str(path) == since_file and fails['open']:
            fails['open'] -= 1
            raise OSError('queue is read-only for a moment')
        return real_open(path, *a, **k)

    real_ids = task_eval.evaluated_ids
    fails['ids'] = 1

    def flaky_ids(path: Path) -> set[str]:
        if fails['ids']:
            fails['ids'] -= 1
            raise OSError('metrics unreadable')
        return real_ids(path)

    monkeypatch.setattr(watchd.os, 'open', flaky_open)
    monkeypatch.setattr(task_eval, 'evaluated_ids', flaky_ids)
    _write(d)
    time.sleep(0.05)  # 起動時刻と配達時刻の間を空ける
    try:
        w.cycle()  # 起点の作成に失敗 -> 配達 -> 予約に失敗 -> 再走査 (起動時刻を起点に使う)
        assert not w.task_eval_since_file.exists()
        assert w.ledger.is_delivered('pj', ID1)
        assert ID1 in w._evals_running  # noqa: SLF001 (同じサイクルの再走査で拾った)
        time.sleep(0.05)
        w.cycle()  # 次のサイクルで起点ファイルを作る。値は起動時刻
        since = watchd._parse_iso(w.task_eval_since_file.read_text().strip())  # noqa: SLF001
        (at,) = w.ledger.delivered_since(['pj'], 0.0)[0][2:]
        assert since is not None and since <= at  # 起点 > 配達時刻 にならない
    finally:
        _kill_all(w)  # 評価が終わる前に watchd が止まった
    assert not out.exists()
    w2 = _watcher_like(w, task_eval_cmd=list(watchd.Config().task_eval_cmd))  # 再起動
    w2.cycle()
    _drain(w2)
    assert [r['report_id'] for r in _rows(out)] == [ID1]


def tmp_path_of(out: Path) -> Path:
    return out.parent.parent


def _lock(queue: Path, rid: str, age: float) -> Path:
    p = task_eval.lock_path(queue, rid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def test_gc_removes_old_evaluated_locks_only(env: tuple[Watcher, Path, Path]) -> None:
    """R3: 評価済みで古い lock は消え、評価中 (flock 保持)・新しい・未評価の lock は残る."""
    w, _d, out = env
    queue = w.cfg.queue_dir
    id4 = '44444444-4444-4444-8444-444444444444'
    day = watchd.TASK_EVAL_LOCK_TTL
    for rid in (ID1, ID2, id4):
        task_eval.append_row(out, task_eval.base_row('pj', 'T1', rid, 'worker1', 'claude', 'completed'))
    old_done = _lock(queue, ID1, day + 60)
    held = _lock(queue, ID2, day + 60)
    fresh = _lock(queue, id4, 60)
    not_done = _lock(queue, ID3, day + 60)
    lease = task_eval.acquire_lease(queue, ID2)  # 評価中の子が持っている
    os.utime(held, (time.time() - day - 60,) * 2)
    try:
        w.cfg.task_eval_since = SINCE_EARLY
        w.poll_task_evals()  # 再走査のついでに消す
    finally:
        lease.close()  # type: ignore[union-attr]
    assert not old_done.exists()
    assert held.exists() and fresh.exists() and not_done.exists()


def test_lease_refreshes_lock_mtime(tmp_path: Path) -> None:
    p = _lock(tmp_path, ID1, 10 * 86400)
    lease = task_eval.acquire_lease(tmp_path, ID1)
    assert lease is not None
    assert time.time() - p.stat().st_mtime < 60  # 評価を始めた時刻になる (GC の対象から外れる)
    lease.close()  # type: ignore[attr-defined]
