#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts/eval_trajectory.py (transcript の後追い評価, SQUAD-354) のテスト.

tmp に queue と ~/.claude/projects 相当を作り、measure_task_cost の探索先だけ差し替える.
"""

from __future__ import annotations

from collections.abc import Callable

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'squad'))
import bench_report  # noqa: E402
import eval_trajectory  # noqa: E402

SID = '33333333-3333-4333-8333-333333333333'
TOKEN = 'ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # pragma: allowlist secret
START = 1_790_899_200  # 2026-10-02T00:00:00Z
T0 = '2026-10-02T00:00:00Z'  # 窓の始端 (含む)
T1 = '2026-10-02T00:30:00Z'
T_END = '2026-10-02T01:00:00Z'  # 窓の終端 (含む)
T_OUT = '2026-10-02T01:00:01Z'  # 窓の外
WS = '/work/wt'


class Rows:
    """transcript の行を組み立てる."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.n = 0

    def call(self, name: str, inp: dict, ts: str = T1, is_error: bool = False, result: str = 'ok') -> None:
        self.n += 1
        tid = f'tu{self.n}'
        self.rows.append(
            {
                'type': 'assistant',
                'timestamp': ts,
                'message': {'content': [{'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}]},
            }
        )
        self.rows.append(
            {
                'type': 'user',
                'timestamp': ts,
                'message': {
                    'content': [{'type': 'tool_result', 'tool_use_id': tid, 'is_error': is_error, 'content': result}]
                },
            }
        )

    def bash(self, cmd: str, **kw: object) -> None:
        self.call('Bash', {'command': cmd}, **kw)  # type: ignore[arg-type]

    def write(self, path: str) -> None:
        self.call('Write', {'file_path': path, 'content': 'x'})


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: Rows, *, archive: bool = False) -> Path:
    projects = tmp_path / 'claude_projects'
    monkeypatch.setattr(eval_trajectory.trace_search.mtc, 'CLAUDE_PROJECTS', projects)
    monkeypatch.setattr(eval_trajectory.trace_search.mtc, 'QUEUE_ROOT', tmp_path / 'main')
    tp = projects / 'ws' / f'{SID}.jsonl'
    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text('\n'.join(json.dumps(r) for r in rows.rows) + '\n')

    pj = tmp_path / 'queue' / 'projects' / 'pj'
    tasks = pj / ('tasks/archive' if archive else 'tasks')
    tasks.mkdir(parents=True, exist_ok=True)
    (pj / 'reports').mkdir(exist_ok=True)
    task = tasks / 'worker2.yaml'
    task.write_text(f'task_id: T1\ncontext:\n  workspace: {WS}\n')
    os.utime(task, (START, START))
    (pj / 'reports' / 'worker2_report.yaml').write_text(
        f'task_id: T1\nstatus: completed\nworker: worker2\nsession_id: "{SID}"\ncompleted_at: "{T_END}"\n'
    )
    return tmp_path / 'queue'


@pytest.fixture
def mk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., dict]:
    def _mk(rows: Rows, **kw: object) -> dict:
        queue = _setup(tmp_path, monkeypatch, rows, **kw)  # type: ignore[arg-type]
        return eval_trajectory.evaluate(queue, 'pj', 'T1')

    return _mk


def test_counts_all_metrics(mk: Callable[..., dict]) -> None:
    r = Rows()
    r.call('Read', {'file_path': f'{WS}/a.py'})
    r.call('Read', {'file_path': f'{WS}/a.py'})  # retry 1
    r.call('Read', {'file_path': f'{WS}/a.py'})  # retry 2
    r.bash('make', is_error=True)
    r.call('Agent', {'subagent_type': 'verifier', 'prompt': 'x'})
    r.call('mcp__kioku_mesh__search_memory', {'q': 'a'})
    r.call('mcp__kioku_mesh__get_memory', {'id': 'a'})
    r.bash('/home/gisen/work/squad/bin/squad trace search foo')
    r.write(f'{WS}/ok.py')
    r.write('/tmp/claude-1000/x/scratchpad/y.py')
    r.write('/home/gisen/work/squad/queue/projects/pj/reports/worker2_report.yaml')
    r.write('/etc/outside.txt')
    r.bash('git reset --hard HEAD~1')
    r.bash('gh pr create --title x')
    r.bash('gh pr create --title x')  # 重複
    r.bash('git push origin b')
    m = mk(r)
    assert m['tool_calls'] == 16
    assert m['tool_errors'] == 1
    assert m['retries'] == 3  # Read 2 回分 + 同じ gh pr create 1 回分
    assert m['verify_ran'] is True
    assert m['memory_calls'] == 2
    assert m['trace_search_calls'] == 1
    assert m['writes_outside_workspace'] == 1
    assert m['deny_pattern_matches'] == 1
    assert m['external_write_attempts'] == 3
    assert m['external_write_attempt_dups'] == 1
    assert m['attribution'] == 'exact'
    assert (m['project'], m['task_id']) == ('pj', 'T1')


def test_errored_external_write_is_not_counted(mk: Callable[..., dict]) -> None:
    r = Rows()
    r.bash('gh pr create --title x', is_error=True)
    r.bash('git push origin b', is_error=True)
    r.bash('git push origin b')
    m = mk(r)
    assert m['external_write_attempts'] == 1
    assert m['external_write_attempt_dups'] == 0


def test_verify_attempt_requires_matching_task_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    verdict = queue / 'projects' / 'pj' / 'reports' / 'worker2_verdict.yaml'
    verdict.write_text('task_id: OTHER\nattempt: 2\nresult: pass\n')
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['verify_attempt'] is None
    verdict.write_text('task_id: T1\nattempt: 2\nresult: pass\n')
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['verify_attempt'] == 2


def _history(
    queue: Path, task_id: str, attempt: int, result: str, mtime: int, body_task_id: str | None = None
) -> None:
    """Place a per-attempt verdict copy as watchd writes it to verdicts/ (SQUAD-397)."""
    data = f'task_id: {body_task_id or task_id}\nattempt: {attempt}\nresult: {result}\n'.encode()
    d = queue / 'projects' / 'pj' / eval_trajectory.VERDICT_HISTORY_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / eval_trajectory.verdict_history_name(task_id, attempt, data)
    p.write_bytes(data)
    os.utime(p, (mtime, mtime))


def test_verify_attempt_retry_pass_from_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    _history(queue, 'T1', 1, 'fail', START + 10)
    _history(queue, 'T1', 2, 'pass', START + 20)
    # 現在の verdict は次の task で上書き済み
    (queue / 'projects' / 'pj' / 'reports' / 'worker2_verdict.yaml').write_text(
        'task_id: T2\nattempt: 1\nresult: pass\n'
    )
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (2, 'pass')


def test_verify_attempt_single_pass_from_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    _history(queue, 'T1', 1, 'pass', START + 10)
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (1, 'pass')


def test_verify_attempt_history_task_id_mismatch_is_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    # ファイル名は T1 でも中身が別 task (名前の衝突など) なら採らない
    _history(queue, 'T1', 2, 'pass', START + 10, body_task_id='T1x')
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (None, None)


def test_verify_attempt_ignores_verdict_without_valid_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Result の無い (途中書き込みの) verdict は、新しくても最後の verdict として採らない (SQUAD-400 B1)."""
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    _history(queue, 'T1', 1, 'fail', START + 10)
    _history(queue, 'T1', 2, '', START + 20)
    _history(queue, 'T1', 3, 'ok', START + 30)
    current = queue / 'projects' / 'pj' / 'reports' / 'worker2_verdict.yaml'
    current.write_text('task_id: T1\nattempt: 4\n')
    os.utime(current, (START + 40, START + 40))
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (1, 'fail')


def test_verify_attempt_all_invalid_results_is_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    _history(queue, 'T1', 1, '', START + 10)
    (queue / 'projects' / 'pj' / 'reports' / 'worker2_verdict.yaml').write_text('task_id: T1\nattempt: 2\n')
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (None, None)


def test_verify_attempt_without_history_is_null(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (None, None)
    # 履歴が無く、現在の verdict も別 task (既存データの典型) でも null
    (queue / 'projects' / 'pj' / 'reports' / 'worker2_verdict.yaml').write_text(
        'task_id: T2\nattempt: 3\nresult: pass\n'
    )
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (None, None)


def test_verify_attempt_latest_verdict_wins_over_higher_old_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """差し戻しで attempt が 1 から数え直された場合、最後に書かれた verdict を採る."""
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    _history(queue, 'T1', 1, 'fail', START + 10)
    _history(queue, 'T1', 2, 'fail', START + 20)
    _history(queue, 'T1', 1, 'pass', START + 30)
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert (m['verify_attempt'], m['verify_result']) == (1, 'pass')


def test_verify_ran_via_script_and_absent(mk: Callable[..., dict]) -> None:
    r = Rows()
    r.bash('scripts/verify-task.sh a b 1 2')
    assert mk(r)['verify_ran'] is True
    r2 = Rows()
    r2.bash('ls')
    assert mk(r2)['verify_ran'] is False


def test_window_is_inclusive_and_archive_task_found(mk: Callable[..., dict]) -> None:
    r = Rows()
    r.bash('a', ts=T0)  # 始端: 含む
    r.bash('b', ts=T_END)  # 終端: 含む
    r.bash('c', ts=T_OUT)  # 窓の外
    r.bash('d', ts='2026-10-01T23:59:59Z')  # 窓の外
    assert mk(r, archive=True)['tool_calls'] == 2  # task YAML が tasks/archive/ にあっても窓が作れる


def test_secret_is_masked(mk: Callable[..., dict]) -> None:
    r = Rows()
    r.bash(f'git push https://u:{TOKEN}@github.com/x/y.git b')
    r.bash(f'git reset --hard {TOKEN}')
    m = mk(r)
    assert TOKEN not in json.dumps(m)
    assert m['external_write_attempts'] == 1
    assert any('ghp_<redacted>' in s for s in m['denied_samples'])


def test_cli_appends_and_last_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    args = ['--project', 'pj', '--task-id', 'T1', '--queue-dir', str(queue), '--out', str(out)]
    assert eval_trajectory.main(args) == 0
    assert eval_trajectory.main(args) == 0
    assert len(out.read_text().splitlines()) == 2  # 追記のみ
    assert (
        eval_trajectory.main(['--project', 'pj', '--task-id', 'NOPE', '--queue-dir', str(queue), '--out', str(out)])
        == 1
    )
    assert len(out.read_text().splitlines()) == 2


def test_load_evals_uses_last_line(tmp_path: Path) -> None:
    out = tmp_path / 'e.jsonl'
    out.write_text(
        json.dumps({'project': 'pj', 'task_id': 'T1', 'tool_calls': 9})
        + '\nnot json\n'
        + json.dumps({'project': 'pj', 'task_id': 'T1', 'tool_calls': 3})
        + '\n'
    )
    assert bench_report.load_task_evals(out) == {('pj', 'T1'): {'project': 'pj', 'task_id': 'T1', 'tool_calls': 3}}
    assert bench_report.load_task_evals(tmp_path / 'none.jsonl') == {}


def _bench(task_id: str, config: str, trial: int) -> dict:
    return {
        'task_id': task_id,
        'project': 'pj',
        'status': 'completed',
        'verify_status': 'pass',
        'bench': {'suite': 's', 'config': config, 'trial': trial},
    }


def test_render_adds_eval_columns_only_with_evals() -> None:
    recs = [_bench('A1', 'c', 1), _bench('A2', 'c', 2), _bench('A3', 'c', 3)]
    base = bench_report.render(recs, None)
    assert base == bench_report.render(recs, None, {})  # evals が無ければ従来どおり
    assert 'tool_calls' not in base[0]
    evals = {
        ('pj', 'A1'): {'attribution': 'exact', 'tool_calls': 10, 'tool_errors': 0, 'retries': 1},
        ('pj', 'A2'): {'attribution': 'exact', 'tool_calls': 30, 'tool_errors': 2, 'retries': 3},
        ('pj', 'A3'): {'attribution': 'exact', 'tool_calls': 20, 'tool_errors': 1, 'retries': 2},
    }
    lines = bench_report.render(recs, None, evals)
    header = [c.strip() for c in lines[0].strip('|').split('|')]
    row = [c.strip() for c in lines[2].strip('|').split('|')]
    cell = dict(zip(header, row, strict=True))
    assert cell['tool_calls'] == '20'
    assert cell['tool_errors'] == '1'
    assert cell['retries'] == '2'
    assert len(lines[1].strip('|').split('|')) == len(header)


def test_duplicated_tool_result_rows_count_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_result が api-request-blob 行 (timestamp なし) と user 行の 2 行に出ても 1 呼び出しは 1 回 (SQUAD-318).

    SQUAD-318 は task YAML が無く時間窓が作れなかったので、blob 行が窓で落ちずに数えられた。
    """
    rows = Rows()
    rows.bash('git push -u origin b')
    rows.rows.insert(-1, {'type': 'api-request-blob', 'message': rows.rows[-1]['message']})
    queue = _setup(tmp_path, monkeypatch, rows)
    (queue / 'projects' / 'pj' / 'tasks' / 'worker2.yaml').unlink()
    got = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert got['tool_calls'] == 1
    assert got['external_write_attempts'] == 1
    assert got['external_write_attempt_dups'] == 0


def _archive(queue: Path, name: str, body: str, project: str = 'pj') -> None:
    arch = queue / 'projects' / project / 'reports' / 'archive'
    arch.mkdir(parents=True, exist_ok=True)
    (arch / name).write_text(body)


def _final(report_id: str, task_id: str = 'T1', status: str = 'completed') -> str:
    return (
        f'report_id: "{report_id}"\ntask_id: {task_id}\nstatus: {status}\nworker: worker2\n'
        f'session_id: "{SID}"\ncompleted_at: "{T_END}"\n'
    )


def _backfill(queue: Path, out: Path) -> int:
    return eval_trajectory.main(['--backfill', '--queue-dir', str(queue), '--out', str(out)])


def _rows(out: Path) -> list[dict]:
    return [json.loads(line) for line in out.read_text().splitlines()]


def test_backfill_evaluates_only_unevaluated_final_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    _archive(queue, 'T1__r-done.yaml', _final('r-done'))
    _archive(queue, 'T1__r-blocked.yaml', _final('r-blocked', status='blocked'))
    _archive(queue, 'T1__r-progress.yaml', _final('r-progress', status='in_progress'))
    _archive(queue, 'worker4_review_T1__r-review.yaml', _final('r-review'))
    # 既に行がある report_id は評価しない
    out.write_text(json.dumps({'project': 'pj', 'task_id': 'T1', 'report_id': 'r-blocked', 'tool_calls': 99}) + '\n')
    assert _backfill(queue, out) == 0
    rows = _rows(out)
    assert sorted(x['report_id'] for x in rows) == ['r-blocked', 'r-done']
    done = next(x for x in rows if x['report_id'] == 'r-done')
    assert done['tool_calls'] == 1
    assert done['error'] is None
    assert next(x for x in rows if x['report_id'] == 'r-blocked')['tool_calls'] == 99  # 触っていない


def test_backfill_excludes_review_report_by_content_not_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    _archive(queue, 'T1__r-verdict.yaml', _final('r-verdict') + 'verdict: approve\n')
    _archive(queue, 'T1__r-reviewed-pr.yaml', _final('r-reviewed-pr') + 'reviewed_pr: 12\n')
    _archive(queue, 'T1__r-author.yaml', _final('r-author') + 'reviewed_head_sha: abc123\n')
    assert _backfill(queue, out) == 0
    assert [x['report_id'] for x in _rows(out)] == ['r-author']


def test_backfill_second_run_appends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    _archive(queue, 'T1__r1.yaml', _final('r1'))
    assert _backfill(queue, out) == 0
    assert len(_rows(out)) == 1
    assert _backfill(queue, out) == 0
    assert len(_rows(out)) == 1


def test_backfill_not_found_writes_no_row_and_reports_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Transcript が見つからない report は行を書かず stderr に出す (毎回 error 行が増えないように, SQUAD-433)."""
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    _archive(queue, 'T1__r1.yaml', _final('r1'))
    _archive(queue, 'T9__r2.yaml', _final('r2', task_id='T9').replace(SID, 'none'), project='other')
    assert _backfill(queue, out) == 0
    assert [x['report_id'] for x in _rows(out)] == ['r1']
    err = capsys.readouterr().err
    assert 'other/T9' in err
    assert 'r2' in err


def test_backfill_retries_report_that_has_only_error_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Error 行しか無い report_id は評価済みに数えず、次回やり直す (SQUAD-433)."""
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    out.write_text(
        json.dumps({'project': 'pj', 'task_id': 'T1', 'report_id': 'r1', 'error': 'not_found: x'})
        + '\n'
        + json.dumps({'project': 'pj', 'task_id': 'T1', 'report_id': 'r-ok', 'error': None, 'tool_calls': 5})
        + '\n'
    )
    assert eval_trajectory.evaluated_ids(out) == {'r-ok'}
    _archive(queue, 'T1__r1.yaml', _final('r1'))
    _archive(queue, 'T1__r-ok.yaml', _final('r-ok'))
    assert _backfill(queue, out) == 0
    rows = _rows(out)
    assert len(rows) == 3
    assert rows[-1]['report_id'] == 'r1'
    assert rows[-1]['error'] is None
    assert rows[-1]['tool_calls'] == 1


def test_backfill_failure_becomes_error_row_excluded_from_bench(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """not_found 以外の評価失敗は error 行で残し、bench_report.py は集計に使わない."""
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    _archive(queue, 'T1__r2.yaml', _final('r2'))

    def boom(*_: object) -> None:
        raise OSError('読めない')

    monkeypatch.setattr(eval_trajectory, 'evaluate', boom)
    assert _backfill(queue, out) == 0
    (row,) = _rows(out)
    assert row['report_id'] == 'r2'
    assert row['error'] == 'OSError: 読めない'
    assert bench_report.load_task_evals(out) == {}


def test_backfill_separates_rows_when_existing_file_lacks_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = Rows()
    r.bash('a')
    queue = _setup(tmp_path, monkeypatch, r)
    out = tmp_path / 'evals.jsonl'
    out.write_text(json.dumps({'project': 'pj', 'task_id': 'OLD', 'report_id': 'r-old', 'tool_calls': 1}))  # 改行なし
    _archive(queue, 'T1__r1.yaml', _final('r1'))
    assert _backfill(queue, out) == 0
    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert [json.loads(x)['report_id'] for x in lines] == ['r-old', 'r1']


def _no_task_yaml(queue: Path) -> None:
    (queue / 'projects' / 'pj' / 'tasks' / 'worker2.yaml').write_text('task_id: OTHER\n')


def _report(queue: Path, extra: str) -> None:
    rp = queue / 'projects' / 'pj' / 'reports' / 'worker2_report.yaml'
    rp.write_text(rp.read_text() + extra)


def test_report_source_worktree_fills_missing_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/mine/a.py')
    r.write('/wt/other/b.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(queue, 'source_worktree: "/wt/mine"\n')
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['writes_outside_workspace'] == 1


def test_report_source_tree_clean_all_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    for w in ('a', 'b', 'c'):
        r.write(f'/wt/{w}/x.py')
    r.write('/wt/zzz/x.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(
        queue,
        'source_tree_clean:\n  - source_worktree: "/wt/a"\n    status_command: "git -C /wt/a status -s"\n'
        '  - source_worktree: "/wt/b"\n    status_command: "git -C /wt/b status -s"\n',
    )
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['writes_outside_workspace'] == 2  # c と zzz
    rp = queue / 'projects' / 'pj' / 'reports' / 'worker2_report.yaml'
    rp.write_text(
        'task_id: T1\nstatus: completed\nworker: worker2\n'
        f'session_id: "{SID}"\ncompleted_at: "{T_END}"\n'
        'source_tree_clean:\n  source_worktree: "/wt/c"\n  status_command: "git -C /wt/c status -s"\n'
    )
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['writes_outside_workspace'] == 3  # a, b, zzz


def test_context_workspace_still_allowed_with_report_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write(f'{WS}/a.py')
    r.write('/wt/mine/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _report(queue, 'source_worktree: "/wt/mine"\n')
    assert eval_trajectory.evaluate(queue, 'pj', 'T1')['writes_outside_workspace'] == 0


def test_writes_outside_is_null_without_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/mine/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    m = eval_trajectory.evaluate(queue, 'pj', 'T1')
    assert m['writes_outside_workspace'] is None
    # 集計側 (bench_report) は null を無視して落ちない
    evals = {('pj', 'A1'): {'attribution': 'exact', 'writes_outside_workspace': None}}
    lines = bench_report.render([_bench('A1', 'c', 1)], None, evals)
    assert 'writes_outside_workspace' in lines[0]


def _extra_report(queue: Path, name: str, body: str, *, archive: bool = False) -> None:
    d = queue / 'projects' / 'pj' / 'reports' / ('archive' if archive else '.')
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body)


def _writes_outside(queue: Path) -> int:
    return eval_trajectory.evaluate(queue, 'pj', 'T1')['writes_outside_workspace']


def test_review_report_worktree_is_not_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/reviewed/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _extra_report(queue, 'T1__review-x.yaml', 'task_id: T1\nsource_worktree: "/wt/reviewed"\n', archive=True)
    _extra_report(queue, 'worker4_review.yaml', 'task_id: T1\nsource_worktree: "/wt/reviewed"\n')
    assert _writes_outside(queue) is None  # 許可 workspace が 1 つも取れない


def test_only_structured_source_worktree_fields_are_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    for w in ('nested', 'block', 'top', 'single', 'listed'):
        r.write(f'/wt/{w}/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(
        queue,
        'source_worktree: "/wt/top"\n'
        'other:\n  source_worktree: "/wt/nested"\n'
        'notes: |\n  source_worktree: /wt/block\n',
    )
    assert _writes_outside(queue) == 4  # top 以外 (nested / block / single / listed) は外
    _extra_report(queue, 'worker3_report.yaml', 'task_id: T1\nsource_tree_clean:\n  source_worktree: "/wt/single"\n')
    assert _writes_outside(queue) == 3
    _extra_report(
        queue,
        'x.yaml',
        'task_id: T1\nsource_tree_clean:\n  - source_worktree: "/wt/listed"\n  - source_worktree: relative/p\n',
        archive=True,
    )
    assert _writes_outside(queue) == 2  # nested / block だけが外


def test_unparsable_report_is_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/mine/a.py')
    r.write('/wt/broken/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(queue, 'source_worktree: "/wt/mine"\n')
    _extra_report(queue, 'broken.yaml', 'task_id: T1\nsource_worktree: /wt/broken\nx: [unclosed\n  : :\n')
    assert _writes_outside(queue) == 1  # 壊れた report の /wt/broken は無視、落ちない


def test_review_report_detected_by_content_is_not_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/reviewed/a.py')
    r.write('/wt/mine/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(queue, 'source_worktree: "/wt/mine"\n')
    # ファイル名に review を含まないが、内容が review report (SQUAD-395 型)
    for i, key in enumerate(('verdict: approve', 'reviewed_pr: 145')):
        _extra_report(queue, f'T1__{i}.yaml', f'task_id: T1\nsource_worktree: "/wt/reviewed"\n{key}\n', archive=True)
    assert _writes_outside(queue) == 1  # /wt/reviewed だけが外。/wt/mine は author の report で許可


def test_author_report_with_only_reviewed_head_sha_is_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = Rows()
    r.write('/wt/mine/a.py')
    queue = _setup(tmp_path, monkeypatch, r)
    _no_task_yaml(queue)
    _report(queue, 'source_worktree: "/wt/mine"\nreviewed_head_sha: "abc"\n')
    assert _writes_outside(queue) == 0
