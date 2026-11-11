#!/usr/bin/env python3
# ruff: noqa: CPY001
"""ctx の取得元 (statusline > transcript > pane) と squad 用 status line (#105).

fixture の statusline_stdin_*.json は Claude Code 2.1.322 の status line コマンドが stdin で受け取った
JSON の実測値 (パスと ID は伏せた)。before_reply は起動直後、after_reply は 1 往復後。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
STATUSLINE = REPO / 'scripts' / 'statusline.py'
ON_EVENT = REPO / 'scripts' / 'hooks' / 'on-event.sh'
sys.path.insert(0, str(REPO / 'squad'))
import ctx  # noqa: E402
import squad  # noqa: E402


def _fixture(name: str) -> str:
    return (FIXTURES / f'statusline_stdin_{name}.json').read_text()


def _run_statusline(
    tmp_path: Path, stdin: str, *args: str, worker: str | None = 'w1', session: str = 'sq1'
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith('SQUAD_')}
    env.update({'SQUAD_STATE_DIR': str(tmp_path / 'state'), 'SQUAD_QUEUE_DIR': str(tmp_path / 'queue')})
    env['SQUAD_SESSION'] = session
    if worker:
        env['SQUAD_WORKER_ID'] = worker
    return subprocess.run(
        [sys.executable, str(STATUSLINE), *args], input=stdin, env=env, capture_output=True, text=True, check=False
    )


def _write_transcript(path: Path, lines: list[object]) -> Path:
    path.write_text(''.join((x if isinstance(x, str) else json.dumps(x)) + '\n' for x in lines))
    return path


def _assistant(inp: int, read: int, create: int, **extra: object) -> dict:
    usage = {'input_tokens': inp, 'cache_read_input_tokens': read, 'cache_creation_input_tokens': create}
    return {'type': 'assistant', 'message': {'role': 'assistant', 'model': 'claude-opus-5-5', 'usage': usage}, **extra}


# ---------- status line スクリプト ----------


def test_statusline_prints_worker_model_ctx_and_task(tmp_path: Path) -> None:
    task = tmp_path / 'queue' / 'projects' / 'pj' / 'tasks' / 'worker1.yaml'
    task.parent.mkdir(parents=True)
    task.write_text('task_id: SQUAD-9\nproject: pj\n')
    (task.parent.parent / '.squad_session').write_text('sq1\n')

    r = _run_statusline(tmp_path, _fixture('after_reply'))

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'w1 | Haiku 5.5 | ctx 6% | SQUAD-9'


def test_statusline_unknown_ctx_is_not_zero(tmp_path: Path) -> None:
    """起動直後は used_percentage が null。0% にせず不明と出す."""
    r = _run_statusline(tmp_path, _fixture('before_reply'))

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == 'w1 | Haiku 5.5 | ctx 不明'
    st = json.loads((tmp_path / 'state' / 'w1.json').read_text())
    assert st['context_pct'] is None
    assert st['context_source'] == 'statusline'


def test_statusline_writes_state_and_keeps_other_keys(tmp_path: Path) -> None:
    state = tmp_path / 'state' / 'w1.json'
    state.parent.mkdir()
    state.write_text(json.dumps({'worker': 'w1', 'status': 'busy', 'last_event': 'Stop', 'last_event_at': 'x'}))

    r = _run_statusline(tmp_path, _fixture('after_reply'))

    assert r.returncode == 0, r.stderr
    st = json.loads(state.read_text())
    assert (st['status'], st['last_event'], st['last_event_at']) == ('busy', 'Stop', 'x')
    assert st['context_pct'] == 6
    assert st['context_tokens'] == 2 + 34678 + 27641
    assert st['model'] == 'Haiku 5.5'
    assert st['context_source'] == 'statusline'
    at = datetime.fromisoformat(st['context_at'])
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60
    slot = st['ctx_sources']['statusline']
    assert (slot['session'], slot['window']) == ('sq1', 1000000)


def test_statusline_task_from_other_session_is_not_shown(tmp_path: Path) -> None:
    task = tmp_path / 'queue' / 'projects' / 'pj2' / 'tasks' / 'worker1.yaml'
    task.parent.mkdir(parents=True)
    task.write_text('task_id: OTHER-1\n')
    (task.parent.parent / '.squad_session').write_text('other\n')

    r = _run_statusline(tmp_path, _fixture('after_reply'))

    assert r.stdout.strip() == 'w1 | Haiku 5.5 | ctx 6%'


def test_statusline_task_with_newer_report_is_finished(tmp_path: Path) -> None:
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    (pj / 'tasks').mkdir(parents=True)
    (pj / 'reports').mkdir()
    (pj / '.squad_session').write_text('sq1\n')
    (pj / 'tasks' / 'worker1.yaml').write_text('task_id: SQUAD-9\n')
    report = pj / 'reports' / 'worker1_report.yaml'
    report.write_text('task_id: SQUAD-9\nstatus: completed\n')
    old = (pj / 'tasks' / 'worker1.yaml').stat().st_mtime - 10
    os.utime(pj / 'tasks' / 'worker1.yaml', (old, old))

    r = _run_statusline(tmp_path, _fixture('after_reply'))

    assert r.stdout.strip() == 'w1 | Haiku 5.5 | ctx 6%'


def test_statusline_for_dispatcher_uses_name_arg(tmp_path: Path) -> None:
    r = _run_statusline(tmp_path, _fixture('after_reply'), '--name', 'dispatcher', worker=None)

    assert r.stdout.strip() == 'dispatcher | Haiku 5.5 | ctx 6%'
    assert json.loads((tmp_path / 'state' / 'dispatcher.json').read_text())['context_pct'] == 6


def test_statusline_broken_stdin_still_prints_and_does_not_write(tmp_path: Path) -> None:
    r = _run_statusline(tmp_path, 'not json')

    assert r.returncode == 0
    assert r.stdout.strip() == 'w1 | - | ctx 不明'
    assert not (tmp_path / 'state' / 'w1.json').exists()


def test_statusline_without_worker_id_does_not_write_state(tmp_path: Path) -> None:
    r = _run_statusline(tmp_path, _fixture('after_reply'), worker=None)

    assert r.stdout.strip() == 'Haiku 5.5 | ctx 6%'
    assert not (tmp_path / 'state').exists()


# ---------- transcript ----------


def test_transcript_uses_last_main_assistant_usage(tmp_path: Path) -> None:
    t = _write_transcript(
        tmp_path / 't.jsonl',
        [
            _assistant(1, 100, 10),
            {'type': 'user', 'message': {'role': 'user', 'content': 'hi'}},
            _assistant(2, 200, 20),
            _assistant(9, 9, 9, isSidechain=True),  # subagent の usage は本体の ctx ではない
            {
                'type': 'assistant',
                'message': {'model': '<synthetic>', 'usage': {'input_tokens': 0, 'cache_read_input_tokens': 0}},
            },
            {'type': 'system'},
        ],
    )

    assert ctx.transcript_context_tokens(t) == 2 + 200 + 20


@pytest.mark.parametrize('content', ['', 'not json\n{"type": "user"}\n', '{"type": "assistant", "message": {}}\n'])
def test_transcript_without_usage_is_none(tmp_path: Path, content: str) -> None:
    t = tmp_path / 't.jsonl'
    t.write_text(content)

    assert ctx.transcript_context_tokens(t) is None


def test_transcript_missing_file_is_none(tmp_path: Path) -> None:
    assert ctx.transcript_context_tokens(tmp_path / 'nope.jsonl') is None
    assert ctx.transcript_context_tokens(None) is None


def test_transcript_last_assistant_beyond_tail_chunk(tmp_path: Path) -> None:
    """末尾の読み込み範囲に assistant が無ければ範囲を広げて探す."""
    filler = [{'type': 'attachment', 'pad': 'x' * 1000}] * 300
    t = _write_transcript(tmp_path / 't.jsonl', [_assistant(1, 2, 3), *filler])

    assert ctx.transcript_context_tokens(t, chunk=4096) == 6


def test_on_event_hook_records_transcript_tokens(tmp_path: Path) -> None:
    t = _write_transcript(tmp_path / 't.jsonl', [_assistant(5, 1000, 50)])
    state = tmp_path / 'state'
    env = {k: v for k, v in os.environ.items() if not k.startswith('SQUAD_')}
    env.update({'SQUAD_STATE_DIR': str(state), 'SQUAD_WORKER_ID': 'w2', 'SQUAD_SESSION': 'sq1'})
    hook = {'hook_event_name': 'Stop', 'transcript_path': str(t), 'session_id': 's'}

    r = subprocess.run(['bash', str(ON_EVENT)], input=json.dumps(hook), env=env, capture_output=True, text=True)

    assert r.returncode == 0, r.stderr
    st = json.loads((state / 'w2.json').read_text())
    assert (st['status'], st['last_event']) == ('completed', 'Stop')
    assert st['context_tokens'] == 1055
    assert st['context_pct'] is None  # window の大きさが分からないので使用率は決め打ちしない
    assert st['context_source'] == 'transcript'


def test_transcript_pct_uses_window_from_statusline(tmp_path: Path) -> None:
    st: dict = {}
    ctx.set_slot(st, 'statusline', session='sq1', pct=None, tokens=None, window=200000, model='Opus 5.5')
    ctx.set_slot(st, 'transcript', session='sq1', **ctx.transcript_slot(50000, st, 'sq1'))

    assert st['ctx_sources']['transcript']['pct'] == 25


# ---------- 優先順位 ----------

NOW = datetime(2026, 11, 11, 3, 0, tzinfo=timezone.utc)


def _slots(**slots: dict) -> dict:
    return {'ctx_sources': {k: {'session': 'sq1', 'at': NOW.isoformat(), **v} for k, v in slots.items()}}


def test_priority_statusline_over_transcript_over_pane() -> None:
    pane = {'context_pct': 77, 'model': 'Opus 5.5'}
    both = _slots(statusline={'pct': 10, 'model': 'Sonnet 5.5'}, transcript={'pct': 20, 'tokens': 5})
    assert ctx.resolve(both, 'sq1', pane, NOW)['context_pct'] == 10
    assert ctx.resolve(both, 'sq1', pane, NOW)['context_source'] == 'statusline'
    assert ctx.resolve(both, 'sq1', pane, NOW)['model'] == 'Sonnet 5.5'
    only_t = _slots(transcript={'pct': None, 'tokens': 5})
    r = ctx.resolve(only_t, 'sq1', pane, NOW)
    assert (r['context_source'], r['context_pct'], r['context_tokens']) == ('transcript', None, 5)
    r = ctx.resolve({}, 'sq1', pane, NOW)
    assert (r['context_source'], r['context_pct'], r['context_at']) == ('pane', 77, NOW.isoformat())


def test_statusline_null_is_not_filled_by_older_sources() -> None:
    """/clear 直後の statusline は null。前の transcript の値で埋めない."""
    st = _slots(statusline={'pct': None}, transcript={'pct': 40, 'tokens': 9})
    r = ctx.resolve(st, 'sq1', {'context_pct': 40}, NOW)
    assert (r['context_source'], r['context_pct']) == ('statusline', None)


def test_slots_from_other_session_are_ignored() -> None:
    st = _slots(statusline={'pct': 10})
    r = ctx.resolve(st, 'other', {'context_pct': None, 'model': None}, NOW)
    assert (r['context_pct'], r['context_source']) == (None, 'pane')


def test_nothing_available_stays_null() -> None:
    r = ctx.resolve({'context_pct': 55}, 'sq1', {'context_pct': None, 'model': None}, NOW)
    assert r['context_pct'] is None  # 旧形式の top-level context_pct は取得元が分からないので使わない
    assert ctx.resolve({}, 'sq1', None, NOW)['context_source'] is None


def test_age_label() -> None:
    assert ctx.age_label((NOW - timedelta(seconds=30)).isoformat(), NOW) == '30秒前'
    assert ctx.age_label((NOW - timedelta(minutes=3)).isoformat(), NOW) == '3分前'
    assert ctx.age_label((NOW - timedelta(hours=2)).isoformat(), NOW) == '2時間前'
    assert ctx.age_label(None, NOW) == ''


# ---------- pane 読み ----------


@pytest.mark.parametrize(
    ('tail', 'pct'),
    [
        ('Opus 5.5 | ctx:54% | main', 54),
        ('ctx 12%', 12),
        ('w1 | Opus 5.5 | ctx 不明', None),
        ('gpt-5.6  Context 100% left  ~/x', 0),
        ('gpt-5.6  Context 30% left', 70),
    ],
)
def test_pane_ctx_patterns(tail: str, pct: int | None) -> None:
    assert squad.detect_status(tail)['context_pct'] == pct


# ---------- squad ls ----------


def test_ls_shows_unknown_and_keeps_statusline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv('SQUAD_STATE_DIR', str(tmp_path))
    monkeypatch.setenv('SQUAD_SESSION', 'sq1')
    st = {'worker': 'w1', 'last_event': 'Stop'}
    ctx.set_slot(st, 'statusline', session='sq1', pct=12, tokens=24000, window=200000, model='Opus 5.5')
    (tmp_path / 'w1.json').write_text(json.dumps(st))
    probes = {'sq1:w1': 'ctx 99%\n', 'sq1:w2': 'idle\n'}
    monkeypatch.setattr(squad, 'probe_worker', lambda pane: squad.detect_status(probes[pane]))
    cfg = {'workers': {'w1': {'agent': 'claude', 'pane': 'sq1:w1'}, 'w2': {'agent': 'claude', 'pane': 'sq1:w2'}}}

    assert squad.cmd_ls(argparse.Namespace(), cfg) == 0

    out = capsys.readouterr().out
    w1 = next(ln for ln in out.splitlines() if ' w1 ' in ln)
    w2 = next(ln for ln in out.splitlines() if ' w2 ' in ln)
    assert 'ctx=12%' in w1
    assert 'statusline' in w1
    assert 'model=Opus 5.5' in w1
    assert 'ctx=不明' in w2
    saved = json.loads((tmp_path / 'w1.json').read_text())
    assert saved['ctx_sources']['statusline']['pct'] == 12  # ls が statusline の値を消さない
    assert (saved['context_pct'], saved['context_source'], saved['last_event']) == (12, 'statusline', 'Stop')
    assert json.loads((tmp_path / 'w2.json').read_text())['context_pct'] is None
