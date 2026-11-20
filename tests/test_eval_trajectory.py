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
    assert m['denied_commands'] == 1
    assert m['external_writes'] == 3
    assert m['external_write_dups'] == 1
    assert m['attribution'] == 'exact'
    assert (m['project'], m['task_id']) == ('pj', 'T1')


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
    assert m['external_writes'] == 1
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
        ('pj', 'A1'): {'tool_calls': 10, 'tool_errors': 0, 'retries': 1},
        ('pj', 'A2'): {'tool_calls': 30, 'tool_errors': 2, 'retries': 3},
        ('pj', 'A3'): {'tool_calls': 20, 'tool_errors': 1, 'retries': 2},
    }
    lines = bench_report.render(recs, None, evals)
    header = [c.strip() for c in lines[0].strip('|').split('|')]
    row = [c.strip() for c in lines[2].strip('|').split('|')]
    cell = dict(zip(header, row, strict=True))
    assert cell['tool_calls'] == '20'
    assert cell['tool_errors'] == '1'
    assert cell['retries'] == '2'
    assert len(lines[1].strip('|').split('|')) == len(header)
