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
import sys
import threading
import time
import zlib

import pytest

REPO = Path(__file__).resolve().parent.parent
ON_EVENT = REPO / 'scripts' / 'hooks' / 'on-event.sh'
sys.path.insert(0, str(REPO / 'squad'))
import ctx  # noqa: E402

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


# ---------- state 更新の原子性と並行更新 (cross-review SQUAD-315) ----------


class _LockFreeReader(threading.Thread):
    """Watchd / squad ls と同じくロックを取らずに state を読み続け、読めなかった中身を集める."""

    def __init__(self, path: Path, deadline: float) -> None:
        super().__init__(daemon=True)
        self.path, self.deadline = path, deadline
        self.stop = threading.Event()
        self.reads = 0
        self.bad: list[str] = []

    def run(self) -> None:
        while not self.stop.is_set() and time.monotonic() < self.deadline:
            try:
                text = self.path.read_text()
            except FileNotFoundError:
                continue
            self.reads += 1
            try:
                json.loads(text)
            except ValueError:
                self.bad.append(text[:80])


def test_update_state_replaces_target_only_when_new_json_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """書き換えは別ファイルに全部書いてから os.replace で差し替える。それまで元のファイルは前の中身のまま.

    ロックを取らない読み手 (watchd など) が書きかけの JSON を読まないための条件。決定的に確かめる。
    """
    path = tmp_path / 'state' / 'w1.json'
    path.parent.mkdir()
    old = {'stop_count': 1, 'status': 'busy'}
    path.write_text(json.dumps(old))
    calls: list[tuple[dict, dict]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike, dst: str | os.PathLike) -> None:
        assert Path(dst) == path
        calls.append((json.loads(Path(dst).read_text()), json.loads(Path(src).read_text())))
        real_replace(src, dst)

    monkeypatch.setattr(ctx.os, 'replace', spy)

    def bump(st: dict) -> None:
        st['stop_count'] += 1
        st['pad'] = 'x' * 100_000

    ctx.update_state(path, bump)

    assert len(calls) == 1, 'state を os.replace で差し替えていない (書き込み途中の JSON が読み手に見える)'
    before, written = calls[0]
    assert before == old  # 差し替えの直前まで、元のファイルは前の完全な JSON
    assert written['stop_count'] == 2
    assert json.loads(path.read_text()) == written
    assert sorted(p.name for p in path.parent.iterdir()) == ['w1.json', 'w1.json.lock']  # 一時ファイルを残さない


def test_concurrent_update_state_keeps_every_update(tmp_path: Path) -> None:
    """20 スレッド x 25 回を同時に始めても、加算が 1 つも消えず、ロック無しの読み手も常に完全な JSON を読む."""
    threads, rounds, timeout = 20, 25, 10.0
    path = tmp_path / 'state' / 'dispatcher.json'
    barrier = threading.Barrier(threads, timeout=timeout)
    errors: list[BaseException] = []
    reader = _LockFreeReader(path, time.monotonic() + timeout)

    def bump(st: dict) -> None:
        st['stop_count'] = st.get('stop_count', 0) + 1
        st['pad'] = 'x' * 20_000  # 書き込みに時間がかかる大きさにして、途中を読む機会を作る

    def work() -> None:
        try:
            barrier.wait()
            for _ in range(rounds):
                ctx.update_state(path, bump)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    workers = [threading.Thread(target=work, daemon=True) for _ in range(threads)]
    reader.start()
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout)
    reader.stop.set()
    reader.join(timeout)

    assert not any(t.is_alive() for t in workers), f'{timeout} 秒で終わらなかった'
    assert errors == []
    assert json.loads(path.read_text())['stop_count'] == threads * rounds
    assert reader.bad == [], f'{len(reader.bad)}/{reader.reads} 回、書きかけの JSON を読んだ'


def test_parallel_stop_hooks_keep_every_increment(tmp_path: Path) -> None:
    """別プロセスの hook 20 本を同時に走らせても stop_count は 20 になり、state は常に読める JSON."""
    hooks, timeout = 20, 60.0
    state = tmp_path / 'state'
    path = state / 'w1.json'
    env = _env(state, worker='w1')
    reader = _LockFreeReader(path, time.monotonic() + timeout)
    reader.start()
    # hook は stdin を読み切るまで待つので、全部起動してから stdin を閉じて同時に走らせる
    procs = [
        subprocess.Popen(
            ['bash', str(ON_EVENT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        for _ in range(hooks)
    ]
    for p in procs:
        p.stdin.write(json.dumps({'hook_event_name': 'Stop'}))
    for p in procs:
        p.stdin.close()
    try:
        results = [(p.wait(timeout), p.stderr.read()) for p in procs]
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        reader.stop.set()
        reader.join(timeout)

    assert [rc for rc, _ in results] == [0] * hooks, [err for _, err in results if err]
    st = json.loads(path.read_text())
    assert st['stop_count'] == hooks
    assert st['last_event'] == 'Stop'
    assert reader.bad == [], f'{len(reader.bad)}/{reader.reads} 回、書きかけの JSON を読んだ'


# ---------- crc32 の計算法 (watchd_sent との取り決め) ----------


def test_prompt_crc32_is_utf8_bytes_without_normalizing() -> None:
    """Hook と watchd_sent を書く側がそろえる計算法。UTF-8 のバイト列をそのまま zlib.crc32 にかける."""
    assert ctx.prompt_crc32('hello') == 907060870
    text = f'{WEB} テストを足して\n'
    assert ctx.prompt_crc32(text) == zlib.crc32(text.encode('utf-8'))
    assert ctx.prompt_crc32(' a ') != ctx.prompt_crc32('a')  # 前後の空白を落とさない
    assert ctx.prompt_crc32('a\n') != ctx.prompt_crc32('a')
