#!/usr/bin/env python3
# ruff: noqa: CPY001
"""watchd の Dispatcher の区切り (ADR 0011 の 4.) のテスト.

区切る条件 3 種、割り込まない条件 5 種、BRIEF-REQUEST から RESUME までの流れ、watchd_sent の記録、
report_bridge() との順序を見る。tmux は FakeTmux、state / queue / dashboards は tmp_path、時刻は差し替える。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_watchd import write_report  # noqa: E402
from test_watchd_breaks import BUSY  # noqa: E402
from test_watchd_breaks import Env  # noqa: E402
from test_watchd_breaks import IDLE  # noqa: E402
from test_watchd_breaks import iso  # noqa: E402
from test_watchd_breaks import SESSION  # noqa: E402
from test_watchd_breaks import T0  # noqa: E402
from watchd import Config  # noqa: E402
from watchd import dispatcher_brief_request_text  # noqa: E402
from watchd import dispatcher_resume_text  # noqa: E402

DISPATCHER_ENV = (
    'WATCH_DISPATCHER_BREAK_CTX_PCT',
    'WATCH_DISPATCHER_BREAK_REPORTS',
    'WATCH_DISPATCHER_BREAK_HOURS',
    'WATCH_DISPATCHER_HUMAN_QUIET_MIN',
)
PANE = f'{SESSION}:dispatcher'


class DEnv(Env):
    """Env に Dispatcher の state / brief / pane の操作を足したもの."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for k in DISPATCHER_ENV:
            monkeypatch.delenv(k, raising=False)
        super().__init__(tmp_path, monkeypatch)
        self.dash = tmp_path / 'dashboards'
        self.dash.mkdir()
        self.w.cfg.dashboards_dir = self.dash
        self.w.cfg.notify_queue_enabled = False  # report 通知を pane 直送にして順序を見る

    def write_disp(
        self,
        *,
        pct: int | None = 52,
        ctx_at: float | None = None,
        last_clear: float | None = None,
        human_at: float | None = None,
        reports: list[float] | None = None,
        transcript: Path | None = None,
    ) -> None:
        path = self.state / 'dispatcher.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        data['worker'] = 'dispatcher'
        data['ctx_sources'] = {}
        if pct is not None:
            data['ctx_sources']['statusline'] = {
                'session': SESSION,
                'at': iso(self.wall if ctx_at is None else ctx_at),
                'pct': pct,
            }
        data['last_clear_at'] = iso(T0 - 600 if last_clear is None else last_clear)
        if human_at is not None:
            data['last_human_prompt_at'] = iso(human_at)
        if reports is not None:
            data['report_notified_at'] = [iso(t) for t in reports]
        if transcript is not None:
            data['transcript_path'] = str(transcript)
        path.write_text(json.dumps(data))

    def disp_state(self) -> dict:
        return json.loads((self.state / 'dispatcher.json').read_text())

    def disp_screen(self, text: str) -> None:
        self.tmux.captures[PANE] = [text]

    def disp_tick(self, times: int = 1) -> None:
        for _ in range(times):
            self.w._sent_to_dispatcher = False  # noqa: SLF001 (cycle() の先頭と同じ)
            self.w.check_context_breaks(dispatcher=True)

    def disp_settle(self, text: str = IDLE) -> None:
        self.disp_screen(text)
        self.disp_tick(3)

    def disp_sent(self) -> list[str]:
        return [k for p, k in self.tmux.sent if p == PANE and k != 'Enter']

    def disp_requests(self) -> list[str]:
        return [k for k in self.disp_sent() if k.startswith('[BRIEF-REQUEST dispatcher')]

    def brief_path(self) -> Path:
        return self.dash / f'{SESSION}-brief.md'

    def write_brief(self, question: str = '- なし') -> None:
        self.brief_path().write_text(
            f'# dispatcher brief: {SESSION}\n- updated_at: {iso(self.wall)}\n\n'
            f'## 1. 人間への確認待ち\n{question}\n## 2. 約束した次の行動\n- W1 の report を待つ\n'
            '## 3. 進行中のやりとり\n- なし\n## 4. 再開の手がかり\n- dashboards/pj.md\n'
        )
        os.utime(self.brief_path(), (self.wall, self.wall))

    def ready(self, word: str = 'BRIEF-READY', brief_written: bool = True) -> None:
        """Dispatcher が brief を書き直し、返事をして待機中に戻った状態にする."""
        self.advance(30)
        if brief_written:
            self.write_brief()
        req = self.disp_requests()[-1]
        self.disp_settle(f'> {req}\n\n● {word}\n\n> ')


@pytest.fixture
def denv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DEnv:
    return DEnv(tmp_path, monkeypatch)


def transcript(tmp_path: Path, *entries: dict) -> Path:
    p = tmp_path / 'transcript.jsonl'
    p.write_text(''.join(json.dumps(e) + '\n' for e in entries))
    return p


def ask_user(tool_id: str = 'toolu_1') -> dict:
    return {
        'type': 'assistant',
        'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': tool_id, 'name': 'AskUserQuestion'}]},
    }


def tool_result(tool_id: str = 'toolu_1') -> dict:
    return {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': tool_id}]}}


def text_reply(text: str = '了解です') -> dict:
    return {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}}


# ---- 文面と既定値 ----


def test_texts_match_adr() -> None:
    assert dispatcher_brief_request_text('ctx:52%', 'sq') == (
        '[BRIEF-REQUEST dispatcher reason=ctx:52%] コンテキストを区切ります。dashboards/sq-brief.md を 4 項目で'
        '書き直し、dashboards/<pj>.md も最新にしてから BRIEF-READY とだけ返してください。人間とのやりとりの途中など'
        '今は区切れないときは BRIEF-DEFER <理由> と返してください。'
    )
    assert dispatcher_resume_text('sq') == (
        '[RESUME dispatcher] /clear 後の再開です。dashboards/sq-brief.md を読み、instructions/dispatcher.md の'
        '「セッション開始時（状態復元）」の 0-5 を行ってから、brief の 2. と 3. を続けてください。'
    )


def test_defaults_match_adr(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in DISPATCHER_ENV:
        monkeypatch.delenv(k, raising=False)
    c = Config()
    assert (
        c.dispatcher_break_ctx_pct,
        c.dispatcher_break_reports,
        c.dispatcher_break_hours,
        c.dispatcher_human_quiet_min,
    ) == (50, 10, 4, 20)


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in zip(DISPATCHER_ENV, ('60', '3', '2', '5'), strict=True):
        monkeypatch.setenv(k, v)
    c = Config()
    assert (
        c.dispatcher_break_ctx_pct,
        c.dispatcher_break_reports,
        c.dispatcher_break_hours,
        c.dispatcher_human_quiet_min,
    ) == (60, 3, 2, 5)


# ---- 区切る条件 ----


class TestThresholds:
    def test_ctx_at_threshold_requests(self, denv: DEnv) -> None:
        denv.write_disp(pct=50)
        denv.disp_settle()
        assert denv.disp_requests() == [dispatcher_brief_request_text('ctx:50%', SESSION)]

    def test_ctx_below_threshold_does_not_request(self, denv: DEnv) -> None:
        denv.write_disp(pct=49)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_ctx_threshold_env(self, denv: DEnv) -> None:
        denv.w.cfg.dispatcher_break_ctx_pct = 30
        denv.write_disp(pct=30)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_known_ctx_ignores_reports_and_hours(self, denv: DEnv) -> None:
        denv.write_disp(pct=10, last_clear=T0 - 10 * 3600, reports=[T0 - 60] * 20)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_reports_since_last_clear_request_when_ctx_unknown(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, last_clear=T0 - 600, reports=[T0 - 300] * 10)
        denv.disp_settle()
        assert denv.disp_requests() == [dispatcher_brief_request_text('reports:10', SESSION)]

    def test_reports_below_threshold_do_not_request(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, last_clear=T0 - 600, reports=[T0 - 300] * 9)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_reports_before_last_clear_are_not_counted(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, last_clear=T0 - 600, reports=[T0 - 900] * 5 + [T0 - 300] * 9)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_reports_threshold_env(self, denv: DEnv) -> None:
        denv.w.cfg.dispatcher_break_reports = 3
        denv.write_disp(pct=None, last_clear=T0 - 600, reports=[T0 - 300] * 3)
        denv.disp_settle()
        assert denv.disp_requests() == [dispatcher_brief_request_text('reports:3', SESSION)]

    def test_hours_since_last_clear_request_when_ctx_unknown(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, last_clear=T0 - 4 * 3600)
        denv.disp_settle()
        assert denv.disp_requests() == [dispatcher_brief_request_text('hours:4', SESSION)]

    def test_hours_below_threshold_do_not_request(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, last_clear=T0 - 4 * 3600 + 60)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_hours_threshold_env(self, denv: DEnv) -> None:
        denv.w.cfg.dispatcher_break_hours = 1
        denv.write_disp(pct=None, last_clear=T0 - 3600)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_stale_ctx_counts_as_unknown(self, denv: DEnv) -> None:
        denv.write_disp(pct=10, ctx_at=T0 - 601, last_clear=T0 - 5 * 3600)
        denv.disp_settle()
        assert denv.disp_requests() == [dispatcher_brief_request_text('hours:5', SESSION)]

    def test_no_last_clear_and_unknown_ctx_does_not_request(self, denv: DEnv) -> None:
        denv.write_disp(pct=None, reports=[T0 - 300] * 20)
        st = denv.disp_state()
        del st['last_clear_at']
        (denv.state / 'dispatcher.json').write_text(json.dumps(st))
        denv.disp_settle()
        assert denv.disp_requests() == []


# ---- 割り込まない条件 (1 条件 1 ケース) ----


class TestDoNotInterrupt:
    def test_baseline_requests(self, denv: DEnv) -> None:
        """下のケースは、この状態から 1 つだけ変える."""
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_busy_pane(self, denv: DEnv) -> None:
        denv.write_disp(pct=52)
        denv.disp_settle(BUSY)
        assert denv.disp_requests() == []

    def test_pane_changing(self, denv: DEnv) -> None:
        denv.write_disp(pct=52)
        for i in range(4):
            denv.disp_screen(f'idle screen {i}\n> ')
            denv.disp_tick()
        assert denv.disp_requests() == []

    def test_already_sent_this_cycle(self, denv: DEnv) -> None:
        denv.write_disp(pct=52)
        denv.disp_screen(IDLE)
        denv.disp_tick(2)
        denv.w._sent_to_dispatcher = True  # noqa: SLF001
        denv.w.check_context_breaks(dispatcher=True)
        assert denv.disp_requests() == []

    def test_undelivered_web_instruction(self, denv: DEnv) -> None:
        d = denv.queue / 'instructions' / SESSION
        d.mkdir(parents=True)
        (d / '20270115-000000-a.json').write_text(json.dumps({'body': 'テストを足して', 'project': 'pj'}))
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_delivered_web_instruction_does_not_block(self, denv: DEnv) -> None:
        d = denv.queue / 'instructions' / SESSION
        d.mkdir(parents=True)
        (d / '20270115-000000-a.json').write_text(json.dumps({'body': 'x', 'delivered_at': iso(T0)}))
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_human_input_within_quiet_minutes(self, denv: DEnv) -> None:
        denv.write_disp(pct=52, human_at=T0 - 20 * 60 + 1)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_human_input_at_quiet_minutes_boundary_requests(self, denv: DEnv) -> None:
        denv.write_disp(pct=52, human_at=T0 - 20 * 60)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_human_quiet_env(self, denv: DEnv) -> None:
        denv.w.cfg.dispatcher_human_quiet_min = 5
        denv.write_disp(pct=52, human_at=T0 - 5 * 60)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_brief_has_pending_question(self, denv: DEnv) -> None:
        denv.write_brief('- 10:00 PR #120 を merge してよいか聞いた。yes なら W1 に cleanup')
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_brief_without_question_requests(self, denv: DEnv) -> None:
        denv.write_brief('- なし')
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_brief_without_section_1_does_not_request(self, denv: DEnv) -> None:
        denv.brief_path().write_text('# dispatcher brief: x\n壊れた brief\n')
        denv.write_disp(pct=52)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_ask_user_question_unanswered(self, denv: DEnv, tmp_path: Path) -> None:
        t = transcript(tmp_path, text_reply(), ask_user())
        denv.write_disp(pct=52, transcript=t)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_ask_user_question_answered_requests(self, denv: DEnv, tmp_path: Path) -> None:
        t = transcript(tmp_path, ask_user(), tool_result(), text_reply())
        denv.write_disp(pct=52, transcript=t)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1

    def test_answered_but_last_assistant_is_ask_user_requests(self, denv: DEnv, tmp_path: Path) -> None:
        """Tool_result が付いていれば、最後の assistant が AskUserQuestion でも答えは来ている."""
        t = transcript(tmp_path, ask_user(), tool_result())
        denv.write_disp(pct=52, transcript=t)
        denv.disp_settle()
        assert len(denv.disp_requests()) == 1


class TestEligible:
    def test_no_agents_file_does_not_touch_dispatcher(self, denv: DEnv) -> None:
        (denv.state / f'agents-{SESSION}.json').unlink()
        denv.write_disp(pct=90)
        denv.disp_settle()
        assert denv.disp_requests() == []

    def test_unsafe_session_name_is_skipped(self, denv: DEnv) -> None:
        denv.w.cfg.session = 'a/b'
        (denv.state / 'agents-a/b.json').parent.mkdir(parents=True, exist_ok=True)
        (denv.state / 'agents-a/b.json').write_text(json.dumps({'w1': 'claude'}))
        denv.write_disp(pct=90)
        denv.disp_settle()
        assert denv.disp_requests() == []


# ---- 流れ ----


def request(denv: DEnv) -> None:
    denv.write_disp(pct=52)
    denv.disp_settle()
    assert len(denv.disp_requests()) == 1


class TestFlow:
    def test_ready_clears_and_resumes(self, denv: DEnv) -> None:
        request(denv)
        denv.ready()
        sent = denv.disp_sent()
        assert sent[-2:] == ['/clear', dispatcher_resume_text(SESSION)]
        st = denv.disp_state()
        assert st['break']['break_count'] == 1
        assert st['break']['last_break_at'] == iso(denv.wall)
        assert 1.5 in denv.sleeps

    def test_ready_without_brief_update_does_not_clear(self, denv: DEnv) -> None:
        request(denv)
        denv.ready(brief_written=False)
        assert '/clear' not in denv.disp_sent()

    def test_defer_waits_30_minutes(self, denv: DEnv) -> None:
        request(denv)
        denv.ready('BRIEF-DEFER 人間と相談中')
        assert '/clear' not in denv.disp_sent()
        denv.advance(29 * 60)
        denv.write_disp(pct=52)
        denv.disp_settle(IDLE + ' ')
        assert len(denv.disp_requests()) == 1
        denv.advance(60)
        denv.write_disp(pct=52)
        denv.disp_settle(IDLE)
        assert len(denv.disp_requests()) == 2

    def test_no_reply_in_10_minutes_retries_after_30(self, denv: DEnv) -> None:
        request(denv)
        denv.advance(600)
        denv.disp_settle(BUSY)
        assert '/clear' not in denv.disp_sent()
        assert denv.w._disp_req is None  # noqa: SLF001
        denv.advance(1800 - 30)
        denv.write_disp(pct=52)
        denv.disp_settle(IDLE)
        assert len(denv.disp_requests()) == 1
        denv.advance(30)
        denv.write_disp(pct=52)
        denv.disp_settle(IDLE + ' ')
        assert len(denv.disp_requests()) == 2

    def test_send_after_ready_cancels_clear(self, denv: DEnv) -> None:
        """BRIEF-READY を見てから /clear までに watchd が送ったら、その回は /clear しない."""
        request(denv)
        denv.advance(30)
        denv.write_brief()
        req = denv.disp_requests()[-1]
        denv.disp_screen(f'> {req}\n\n● BRIEF-READY\n\n> ')
        denv.disp_tick()  # READY を見た (画面が変わったばかりなので待つ)
        assert denv.w.notify_dispatcher('[REPORT project=pj worker=1] x')
        denv.disp_tick()
        assert '/clear' not in denv.disp_sent()
        assert denv.w._disp_req is None  # noqa: SLF001 (やり直す)
        denv.disp_tick(3)
        assert '/clear' not in denv.disp_sent()

    def test_redo_requests_again(self, denv: DEnv) -> None:
        request(denv)
        denv.advance(30)
        denv.write_brief()
        req = denv.disp_requests()[-1]
        denv.disp_screen(f'> {req}\n\n● BRIEF-READY\n\n> ')
        denv.disp_tick()
        denv.w.notify_dispatcher('[REPORT project=pj worker=1] x')
        denv.disp_tick(3)
        denv.disp_settle(IDLE)
        assert len(denv.disp_requests()) == 2

    def test_send_before_ready_does_not_cancel(self, denv: DEnv) -> None:
        request(denv)
        denv.w.notify_dispatcher('[REPORT project=pj worker=1] x')
        denv.disp_screen(BUSY)
        denv.disp_tick()  # READY がまだ無い画面を見た後なので、それより前の送信は brief に入りうる
        denv.ready()
        assert denv.disp_sent()[-2:] == ['/clear', dispatcher_resume_text(SESSION)]

    def test_cooldown_after_break(self, denv: DEnv) -> None:
        request(denv)
        denv.ready()
        denv.advance(29 * 60)
        denv.write_disp(pct=60, human_at=None)
        denv.disp_settle(IDLE + 'x')
        assert len(denv.disp_requests()) == 1
        denv.advance(60)
        denv.write_disp(pct=60)
        denv.disp_settle(IDLE)
        assert len(denv.disp_requests()) == 2

    def test_human_input_after_request_holds_clear(self, denv: DEnv) -> None:
        request(denv)
        denv.write_disp(pct=52, human_at=denv.wall + 10)
        denv.ready()
        assert '/clear' not in denv.disp_sent()


# ---- watchd_sent と report の配達記録 ----


class TestRecords:
    def test_notify_records_watchd_sent_as_not_human(self, denv: DEnv) -> None:
        msg = 'Worker1 report: x を確認してください。'
        denv.w.notify_dispatcher(msg)
        rec = denv.disp_state()['watchd_sent'][-1]
        assert rec['crc32'] == zlib.crc32(msg.encode('utf-8'))
        assert rec['human_origin'] is False
        assert isinstance(rec['at'], str)

    def test_web_instruction_is_human_origin(self, denv: DEnv) -> None:
        d = denv.queue / 'instructions' / SESSION
        d.mkdir(parents=True)
        (d / '20270115-000000-a.json').write_text(json.dumps({'body': 'テストを足して', 'project': 'pj'}))
        denv.disp_screen(IDLE)
        denv.w.deliver_instructions()
        sent = denv.disp_sent()
        assert sent[-1].startswith('[squad-web 指示]')
        rec = denv.disp_state()['watchd_sent'][-1]
        assert rec == {**rec, 'crc32': zlib.crc32(sent[-1].encode('utf-8')), 'human_origin': True}

    def test_watchd_sent_keeps_last_20(self, denv: DEnv) -> None:
        for i in range(25):
            denv.w.notify_dispatcher(f'msg {i}')
        sent = denv.disp_state()['watchd_sent']
        assert len(sent) == 20
        assert sent[-1]['crc32'] == zlib.crc32(b'msg 24')
        assert sent[0]['crc32'] == zlib.crc32(b'msg 5')

    def test_watchd_sent_is_written_before_send(self, denv: DEnv) -> None:
        """Hook は送った本文の受付時に照合するので、送る前に記録が要る."""
        seen: list[int] = []
        orig = denv.tmux.send_keys

        def spy(pane: str, keys: str) -> bool:
            if keys != 'Enter':
                seen.extend(r['crc32'] for r in denv.disp_state().get('watchd_sent', []))
            return orig(pane, keys)

        denv.tmux.send_keys = spy
        denv.w.notify_dispatcher('hello')
        assert zlib.crc32(b'hello') in seen

    def test_brief_request_clear_and_resume_are_recorded(self, denv: DEnv) -> None:
        request(denv)
        denv.ready()
        crcs = {r['crc32'] for r in denv.disp_state()['watchd_sent']}
        for text in (denv.disp_requests()[-1], '/clear', dispatcher_resume_text(SESSION)):
            assert zlib.crc32(text.encode('utf-8')) in crcs

    def test_report_delivery_is_counted(self, denv: DEnv) -> None:
        write_report(denv.pj)
        denv.w.refresh_owned_projects()
        denv.w.report_bridge()
        assert len(denv.disp_state()['report_notified_at']) == 1


# ---- cycle の順序 ----


def test_dispatcher_break_runs_before_report_bridge(denv: DEnv) -> None:
    """/clear と再開文を送ったサイクルの report 通知は、その後に届く."""
    request(denv)
    denv.advance(30)
    denv.write_brief()
    req = denv.disp_requests()[-1]
    denv.disp_screen(f'> {req}\n\n● BRIEF-READY\n\n> ')
    denv.disp_tick(2)
    write_report(denv.pj)
    denv.w.cycle()
    sent = denv.disp_sent()
    i_clear = sent.index('/clear')
    i_report = next(i for i, k in enumerate(sent) if '[REPORT ' in k)
    assert i_clear < sent.index(dispatcher_resume_text(SESSION)) < i_report


def test_cycle_survives_dispatcher_break_errors(denv: DEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: object) -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(denv.w, '_check_dispatcher_break', boom)
    denv.w.cycle()  # 例外で watcher を止めない


def test_dispatcher_md_uses_the_same_text_as_watchd() -> None:
    """依頼と再開の文面は watchd と dispatcher.md で一字一句揃える (ADR 0011 の Consequences)."""
    text = (Path(__file__).resolve().parent.parent / 'instructions' / 'dispatcher.md').read_text()
    assert dispatcher_brief_request_text('<ctx:52% | reports:10 | hours:4>', '<session>') in text
    assert dispatcher_resume_text('<session>') in text
    assert '「セッション開始時（状態復元）」' in text
    headings = ('## 1. 人間への確認待ち', '## 2. 約束した次の行動', '## 3. 進行中のやりとり', '## 4. 再開の手がかり')
    for heading in headings:
        assert heading in text
    assert '20% を切ったら' not in text
    assert '80% を超えていたら' not in text
