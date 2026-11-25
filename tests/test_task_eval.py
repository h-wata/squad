#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Task 完了時の自動評価 (squad/task_eval.py と watchd のフック, Refs #130, #136) のテスト.

watchd は実際に子プロセス (task_eval.py か、テストが置いた偽の評価スクリプト) を起動する。
transcript は tmp の HOME 下の ~/.claude/projects に置き、子プロセスに HOME を渡して読ませる。
"""

from __future__ import annotations

import json
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
    w.poll_task_evals()
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
