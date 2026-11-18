#!/usr/bin/env python3
# ruff: noqa: CPY001
"""on-event.sh が state に残す値 (ADR 0011 の分割案 1).

- Stop ごとに stop_count を 1 足す
- UserPromptSubmit で、watchd_sent と照合して人間の入力なら last_human_prompt_at を書く (4 段の順序規則)
- window 名 dispatcher を worker dispatcher に解決する

hook は本物の bash で実走し、state は tmp_path に置く (本番の squad/state は触らない)。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import subprocess
import zlib

import pytest

REPO = Path(__file__).resolve().parent.parent
ON_EVENT = REPO / 'scripts' / 'hooks' / 'on-event.sh'
WEB = '[squad-web 指示]'


def _env(state: Path, worker: str | None = 'dispatcher', **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith('SQUAD_') and k != 'TMUX_PANE'}
    env.update({'SQUAD_STATE_DIR': str(state), 'SQUAD_SESSION': 'sq1', **extra})
    if worker:
        env['SQUAD_WORKER_ID'] = worker
    return env


def _run(env: dict[str, str], hook: dict | str) -> subprocess.CompletedProcess[str]:
    stdin = hook if isinstance(hook, str) else json.dumps(hook, ensure_ascii=False)
    return subprocess.run(
        ['bash', str(ON_EVENT)], input=stdin, env=env, capture_output=True, text=True, check=False, timeout=30
    )


def _state(state: Path, name: str = 'dispatcher') -> dict:
    return json.loads((state / f'{name}.json').read_text())


def _crc(text: str) -> int:
    return zlib.crc32(text.encode('utf-8'))


def _prompt(text: object) -> dict:
    return {'hook_event_name': 'UserPromptSubmit', 'prompt': text, 'session_id': 's'}


def _submit(tmp_path: Path, text: object, sent: object = None, *, has_sent: bool = True) -> dict:
    """Watchd_sent を置いた state に UserPromptSubmit を 1 回送り、書き終えた state を返す."""
    state = tmp_path / 'state'
    state.mkdir(exist_ok=True)
    init: dict = {'worker': 'dispatcher'}
    if has_sent:
        init['watchd_sent'] = sent
    (state / 'dispatcher.json').write_text(json.dumps(init, ensure_ascii=False))

    r = _run(_env(state), _prompt(text))

    assert r.returncode == 0, r.stderr
    assert r.stdout == ''  # UserPromptSubmit の stdout はプロンプトに足されるので何も出さない
    return _state(state)


def _wrote(st: dict) -> bool:
    if 'last_human_prompt_at' not in st:
        return False
    at = datetime.fromisoformat(st['last_human_prompt_at'])
    assert at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - at).total_seconds()) < 60
    return True


def _rec(text: str, human: object, **extra: object) -> dict:
    return {'crc32': _crc(text), 'at': '2026-11-18T00:00:00+00:00', 'human_origin': human, **extra}


# ---------- Stop の回数 ----------


def test_stop_increments_stop_count(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    env = _env(state, worker='w1')

    for _ in range(2):
        r = _run(env, {'hook_event_name': 'Stop', 'session_id': 's'})
        assert r.returncode == 0, r.stderr

    st = _state(state, 'w1')
    assert st['stop_count'] == 2
    assert st['last_event'] == 'Stop'


def test_stop_count_continues_from_existing_value(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'w1.json').write_text(json.dumps({'stop_count': 7, 'status': 'busy'}))

    r = _run(_env(state, worker='w1'), {'hook_event_name': 'Stop'})

    assert r.returncode == 0, r.stderr
    assert _state(state, 'w1')['stop_count'] == 8


@pytest.mark.parametrize('bad', ['3', None, True, -1, 1.5])
def test_stop_count_restarts_from_zero_when_broken(tmp_path: Path, bad: object) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'w1.json').write_text(json.dumps({'stop_count': bad}))

    r = _run(_env(state, worker='w1'), {'hook_event_name': 'Stop'})

    assert r.returncode == 0, r.stderr
    assert _state(state, 'w1')['stop_count'] == 1


@pytest.mark.parametrize(
    'hook',
    [
        {'hook_event_name': 'StopFailure', 'error_type': 'rate_limit'},
        {'hook_event_name': 'Notification', 'type': 'idle_prompt'},
        _prompt('hello'),
    ],
)
def test_other_events_do_not_count_stops(tmp_path: Path, hook: dict) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'w1.json').write_text(json.dumps({'stop_count': 3}))

    r = _run(_env(state, worker='w1'), hook)

    assert r.returncode == 0, r.stderr
    assert _state(state, 'w1')['stop_count'] == 3


# ---------- UserPromptSubmit と window 名 dispatcher ----------


def _fake_tmux(tmp_path: Path, answer: str) -> Path:
    fake_bin = tmp_path / 'fake-bin'
    fake_bin.mkdir()
    tmux = fake_bin / 'tmux'
    tmux.write_text(f'#!/usr/bin/env bash\necho "{answer}"\n')
    tmux.chmod(0o755)
    return fake_bin


@pytest.mark.parametrize('answer', ['dispatcher 0.0', 'bash 0.0'])  # 2 つ目は window 0 を 7 pane に分けていた旧構成
def test_dispatcher_window_resolves_to_dispatcher(tmp_path: Path, answer: str) -> None:
    state = tmp_path / 'state'
    fake_bin = _fake_tmux(tmp_path, answer)
    env = _env(state, worker=None, TMUX_PANE='%1')
    env['PATH'] = f'{fake_bin}:{env["PATH"]}'

    r = _run(env, _prompt('hello'))

    assert r.returncode == 0, r.stderr
    assert r.stdout == ''
    st = _state(state)
    assert st['worker'] == 'dispatcher'
    assert _wrote(st)


def test_user_prompt_submit_does_not_touch_last_event(tmp_path: Path) -> None:
    """Watchd は last_event で待機中を判定するので、入力の受付では書き換えない."""
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'w1.json').write_text(json.dumps({'status': 'completed', 'last_event': 'Stop', 'last_event_at': 'x'}))

    r = _run(_env(state, worker='w1'), _prompt('hello'))

    assert r.returncode == 0, r.stderr
    st = _state(state, 'w1')
    assert (st['status'], st['last_event'], st['last_event_at']) == ('completed', 'Stop', 'x')
    assert _wrote(st)


# ---------- human_origin 照合の 4 段 (ADR 0011 の 4.) ----------


def test_step1_web_instruction_is_human_even_if_recorded_as_watchd(tmp_path: Path) -> None:
    """1 段目: [squad-web 指示] で始まれば、crc32 が human_origin false で記録されていても人間の入力."""
    text = f'{WEB} テストを足して'
    st = _submit(tmp_path, text, [_rec(text, False)])

    assert _wrote(st)


def test_step2_recorded_human_origin_true_is_human(tmp_path: Path) -> None:
    """2 段目: crc32 が watchd_sent にあり human_origin true なら人間の入力."""
    text = 'web から来た本文'
    st = _submit(tmp_path, text, [_rec(text, True)])

    assert _wrote(st)


def test_step2_comes_before_step3_for_same_crc(tmp_path: Path) -> None:
    """同じ crc32 の記録が true と false の両方にあれば 2 段目が先に当たる."""
    text = '同じ本文'
    st = _submit(tmp_path, text, [_rec(text, False), _rec(text, True), _rec(text, False)])

    assert _wrote(st)


def test_step3_recorded_human_origin_false_is_watchd(tmp_path: Path) -> None:
    """3 段目: crc32 が watchd_sent にあり human_origin false なら watchd の送信。書かない."""
    text = '[REPORT w1 SQUAD-1] report が届きました'
    st = _submit(tmp_path, text, [_rec('別の本文', True), _rec(text, False)])

    assert not _wrote(st)


def test_step3_does_not_overwrite_previous_human_prompt_time(tmp_path: Path) -> None:
    text = '[REPORT w1 SQUAD-1]'
    state = tmp_path / 'state'
    state.mkdir()
    before = '2026-11-18T00:00:00+00:00'
    (state / 'dispatcher.json').write_text(
        json.dumps({'watchd_sent': [_rec(text, False)], 'last_human_prompt_at': before})
    )

    r = _run(_env(state), _prompt(text))

    assert r.returncode == 0, r.stderr
    assert _state(state)['last_human_prompt_at'] == before


def test_step4_crc_not_recorded_is_human(tmp_path: Path) -> None:
    """4 段目: crc32 が watchd_sent に無ければ人間の入力."""
    st = _submit(tmp_path, '人が打った文', [_rec('watchd が送った文', False)])

    assert _wrote(st)


@pytest.mark.parametrize(
    'sent',
    [
        'not a list',
        {'crc32': 1},
        [],
        ['broken', 3, None],
        'MISSING_ORIGIN',
        'STRING_ORIGIN',
        'STRING_CRC',
        'BOOL_CRC',
    ],
)
def test_step4_unreadable_watchd_sent_is_human(tmp_path: Path, sent: object) -> None:
    """4 段目: watchd_sent や human_origin が無い・壊れているときは照合できないので人間の入力 (区切らない側)."""
    text = 'watchd が送った文'
    if sent == 'MISSING_ORIGIN':
        sent = [{'crc32': _crc(text), 'at': 'x'}]
    elif sent == 'STRING_ORIGIN':
        sent = [_rec(text, 'false')]
    elif sent == 'STRING_CRC':
        sent = [{**_rec(text, False), 'crc32': str(_crc(text))}]
    elif sent == 'BOOL_CRC':
        sent = [{**_rec(text, False), 'crc32': True}]
    st = _submit(tmp_path, text, sent)

    assert _wrote(st)


def test_step4_missing_watchd_sent_is_human(tmp_path: Path) -> None:
    st = _submit(tmp_path, 'hello', has_sent=False)

    assert _wrote(st)


def test_step4_missing_state_file_is_human(tmp_path: Path) -> None:
    state = tmp_path / 'state'

    r = _run(_env(state), _prompt('hello'))

    assert r.returncode == 0, r.stderr
    assert _wrote(_state(state))


def test_step4_broken_state_file_is_human(tmp_path: Path) -> None:
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'dispatcher.json').write_text('{not json')

    r = _run(_env(state), _prompt('hello'))

    assert r.returncode == 0, r.stderr
    assert _wrote(_state(state))


@pytest.mark.parametrize('prompt', [None, 123, ['a']])
def test_step4_unreadable_prompt_is_human(tmp_path: Path, prompt: object) -> None:
    st = _submit(tmp_path, prompt, [_rec('x', False)])

    assert _wrote(st)


def test_step4_missing_prompt_field_is_human(tmp_path: Path) -> None:
    state = tmp_path / 'state'

    r = _run(_env(state), {'hook_event_name': 'UserPromptSubmit'})

    assert r.returncode == 0, r.stderr
    assert _wrote(_state(state))


def test_broken_hook_json_is_not_treated_as_prompt(tmp_path: Path) -> None:
    """Hook JSON が読めなければイベント名も分からないので、UserPromptSubmit として扱わない."""
    state = tmp_path / 'state'

    r = _run(_env(state), 'not json')

    assert r.returncode == 0, r.stderr
    assert 'last_human_prompt_at' not in _state(state)


# ---------- fail-soft ----------


@pytest.mark.skipif(os.geteuid() == 0, reason='root は権限を無視する')
def test_unwritable_state_dir_does_not_block_prompt(tmp_path: Path) -> None:
    """State を書けなくても hook は 0 で終わり、何も出さない (UserPromptSubmit の exit 2 は入力を止める)."""
    state = tmp_path / 'state'
    state.mkdir()
    os.chmod(state, 0o500)
    try:
        r = _run(_env(state), _prompt('hello'))
        r2 = _run(_env(state, worker='w1'), {'hook_event_name': 'Stop'})
    finally:
        os.chmod(state, 0o700)

    assert (r.returncode, r.stdout) == (0, '')
    assert r2.returncode == 0
    assert not (state / 'dispatcher.json').exists()
