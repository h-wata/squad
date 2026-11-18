#!/usr/bin/env python3
# ruff: noqa: CPY001
"""watchd のタスク途中の区切り (ADR 0011 の 2. と 5.) のテスト.

tmux は FakeTmux (実 session に send-keys しない)、state と queue は tmp_path、時刻は wall / mono /
sleep を差し替えて決める。sleep には頼らない。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_watchd import FakeTmux  # noqa: E402
from watchd import brief_reply  # noqa: E402
from watchd import brief_request_text  # noqa: E402
from watchd import Config  # noqa: E402
from watchd import mtime_str  # noqa: E402
from watchd import resume_text  # noqa: E402
from watchd import Watcher  # noqa: E402

SESSION = 'testsess'
T0 = 1_800_000_000.0  # 2027-01-15T08:00:00Z
IDLE = 'idle screen\n> '
BUSY = '✻ Pondering… (12s · esc to interrupt)'


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec='seconds')


class Env:
    """1 つの watcher と、その周り (queue, state, 時刻, tmux) をまとめたもの."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.state = tmp_path / 'state'
        self.state.mkdir()
        monkeypatch.setenv('SQUAD_STATE_DIR', str(self.state))
        for k in ('WATCH_BREAK_CTX_PCT', 'WATCH_BREAK_STOPS', 'WATCH_BREAK_MINUTES'):
            monkeypatch.delenv(k, raising=False)
        self.queue = tmp_path / 'queue'
        self.pj = self.queue / 'projects' / 'pj'
        for sub in ('tasks', 'reports'):
            (self.pj / sub).mkdir(parents=True)
        (self.pj / '.squad_session').write_text(SESSION + '\n')
        cfg = Config(
            session=SESSION,
            default_owner='other',
            queue_dir=self.queue,
            interval=15,
            stall_cycles=1000,  # 停止通報はこのテストでは見ない
            notify_queue_enabled=True,
        )
        self.tmux = FakeTmux(SESSION)
        self.w = Watcher(cfg=cfg, tmux=self.tmux)
        self.wall = T0
        self.mono = 5000.0
        self.sleeps: list[float] = []
        self.w.wall = lambda: self.wall
        self.w.mono = lambda: self.mono
        self.w.sleep = self.sleeps.append
        self.write_agents({'w1': 'claude', 'w2': 'claude', 'w3': 'claude'})
        self.write_task()
        self.w.refresh_owned_projects()

    # ---- 準備 ----

    def write_agents(self, agents: object) -> None:
        (self.state / f'agents-{SESSION}.json').write_text(json.dumps(agents))

    def write_task(self, n: int = 1, task_id: str = 'T-1', created: float | None = None) -> Path:
        created = T0 if created is None else created
        p = self.pj / 'tasks' / f'worker{n}.yaml'
        p.write_text(f'task_id: {task_id}\nproject: pj\ncreated_at: "{iso(created)}"\n')
        return p

    def write_state(
        self,
        n: int = 1,
        *,
        pct: int | None = 52,
        ctx_at: float | None = None,
        event: str = 'Stop',
        event_at: float | None = None,
        stop_count: int | None = None,
    ) -> None:
        path = self.state / f'w{n}.json'
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(last_event=event, last_event_at=iso(self.wall if event_at is None else event_at))
        if stop_count is not None or 'stop_count' not in data:
            data['stop_count'] = stop_count or 0
        data['ctx_sources'] = {}
        if pct is not None:
            data['ctx_sources']['statusline'] = {
                'session': SESSION,
                'at': iso(self.wall if ctx_at is None else ctx_at),
                'pct': pct,
            }
        path.write_text(json.dumps(data))

    def read_state(self, n: int = 1) -> dict:
        return json.loads((self.state / f'w{n}.json').read_text())

    def screen(self, text: str, n: int = 1) -> None:
        self.tmux.captures[f'{SESSION}:w{n}'] = [text]

    def tick(self, times: int = 1) -> None:
        for _ in range(times):
            self.w.check_workers()
            self.w.check_context_breaks()

    def settle(self, text: str = IDLE, n: int = 1) -> None:
        """同じ画面を 3 サイクル見せる (hash が 2 サイクル続けて変わらない状態にする)."""
        self.screen(text, n)
        self.tick(3)

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds

    # ---- 観測 ----

    def sent(self, n: int = 1) -> list[str]:
        return [k for p, k in self.tmux.sent if p == f'{SESSION}:w{n}']

    def requests(self, n: int = 1) -> list[str]:
        return [k for k in self.sent(n) if k.startswith('[BRIEF-REQUEST')]

    def in_flight(self, n: int = 1) -> bool:
        """BRIEF-REQUEST の返事を待っているか."""
        return n in self.w._break_req  # noqa: SLF001

    def brief(self, task_id: str = 'T-1') -> Path:
        return self.pj / 'briefs' / f'{task_id}.md'

    def task_yaml(self, n: int = 1) -> Path:
        return self.pj / 'tasks' / f'worker{n}.yaml'

    def reply(self, word: str, n: int = 1, brief_written: bool = True) -> None:
        """Worker が brief を書き、返事をして Stop した状態にする."""
        self.advance(30)
        if brief_written:
            b = self.brief()
            b.parent.mkdir(parents=True, exist_ok=True)
            b.write_text('# brief: T-1\n')
            os.utime(b, (self.wall, self.wall))
        self.write_state(n, pct=52, event_at=self.wall)
        req = self.requests(n)[-1]
        self.settle(f'> {req}\n\n● {word}\n\n> ', n)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


def request_and_ready(env: Env) -> None:
    env.write_state(pct=52)
    env.settle()
    assert len(env.requests()) == 1
    env.reply('BRIEF-READY')


# ---- しきい値 ----


class TestThresholds:
    def test_ctx_at_threshold_breaks(self, env: Env) -> None:
        env.write_state(pct=50)
        env.settle()
        assert env.requests() == [brief_request_text('T-1', 'ctx:50%', env.brief())]

    def test_ctx_below_threshold_does_not_break(self, env: Env) -> None:
        env.write_state(pct=49)
        env.settle()
        assert env.requests() == []

    def test_ctx_threshold_env(self, env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv('WATCH_BREAK_CTX_PCT', '30')
        env.w.cfg.break_ctx_pct = Config().break_ctx_pct
        env.write_state(pct=30)
        env.settle()
        assert len(env.requests()) == 1

    def test_known_ctx_below_threshold_ignores_stops_and_minutes(self, env: Env) -> None:
        env.write_task(created=T0 - 3 * 3600)
        env.write_state(pct=10, stop_count=50)
        env.settle()
        assert env.requests() == []

    def test_stale_ctx_counts_as_unknown_and_stops_break(self, env: Env) -> None:
        env.write_state(pct=None, stop_count=0)
        env.settle()  # タスクを初めて見た時点の stop_count (0) を stop_base に控える
        assert env.read_state()['break']['stop_base'] == 0
        env.write_state(pct=90, ctx_at=T0 - 601, stop_count=8)
        env.tick()
        assert env.requests() == [brief_request_text('T-1', 'stops:8', env.brief())]

    def test_ctx_exactly_ten_minutes_old_is_still_known(self, env: Env) -> None:
        env.write_state(pct=None, stop_count=0)
        env.settle()
        env.write_state(pct=10, ctx_at=T0 - 600, stop_count=8)
        env.tick()
        assert env.requests() == []

    def test_unknown_ctx_stops_below_threshold(self, env: Env) -> None:
        env.write_state(pct=None, stop_count=3)
        env.settle()
        env.write_state(pct=None, stop_count=10)  # base 3 からの差は 7
        env.tick()
        assert env.requests() == []
        env.write_state(pct=None, stop_count=11)
        env.tick()
        assert env.requests() == [brief_request_text('T-1', 'stops:8', env.brief())]

    def test_unknown_ctx_minutes_from_created_at(self, env: Env) -> None:
        env.write_task(created=T0 - 59 * 60 - 59)
        env.write_state(pct=None)
        env.settle()
        assert env.requests() == []
        env.write_task(created=T0 - 60 * 60)
        env.tick()
        assert env.requests() == [brief_request_text('T-1', 'minutes:60', env.brief())]


# ---- 待機中の判定 ----


class TestWaiting:
    def test_busy_pane_does_not_break(self, env: Env) -> None:
        env.write_state(pct=90)
        env.settle(BUSY)
        assert env.requests() == []

    def test_pane_must_be_unchanged_for_two_cycles(self, env: Env) -> None:
        env.write_state(pct=90)
        env.screen(IDLE)
        env.tick(2)  # 1 回目で hash を取り、2 回目で 1 サイクル変化なし
        assert env.requests() == []
        env.tick()
        assert len(env.requests()) == 1

    @pytest.mark.parametrize('event', ['Notification', 'StopFailure', 'permission_prompt'])
    def test_last_event_must_be_stop_or_idle_prompt(self, env: Env, event: str) -> None:
        env.write_state(pct=90, event=event)
        env.settle()
        assert env.requests() == []

    def test_idle_prompt_counts_as_waiting(self, env: Env) -> None:
        env.write_state(pct=90, event='idle_prompt')
        env.settle()
        assert len(env.requests()) == 1

    def test_last_event_older_than_watchd_send(self, env: Env) -> None:
        env.w._worker_sent_at[1] = T0 + 1  # noqa: SLF001 (watchd が最後に送った時刻)
        env.write_state(pct=90, event_at=T0)
        env.settle()
        assert env.requests() == []
        env.write_state(pct=90, event_at=T0 + 2)
        env.tick()
        assert len(env.requests()) == 1

    def test_held_approval_blocks(self, env: Env) -> None:
        env.w.approval_held[1] = f'T-1:{mtime_str(env.task_yaml())}:123'
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []
        env.w.approval_held[1] = 'OTHER:1.0:123'  # 前のタスクの保留は見ない
        env.tick()
        assert len(env.requests()) == 1


# ---- 対象の worker ----


class TestEligibility:
    @pytest.mark.parametrize('agent', ['claude-local', 'opencode', '', 'codex', None])
    def test_non_claude_agent_is_not_broken(self, env: Env, agent: object) -> None:
        env.write_agents({'w1': agent, 'w2': 'claude', 'w3': 'claude'})
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []

    def test_missing_agents_file(self, env: Env) -> None:
        (env.state / f'agents-{SESSION}.json').unlink()
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []

    def test_broken_agents_file(self, env: Env) -> None:
        (env.state / f'agents-{SESSION}.json').write_text('{not json')
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []

    def test_agents_file_of_other_session_is_not_used(self, env: Env) -> None:
        (env.state / f'agents-{SESSION}.json').unlink()
        (env.state / 'agents-other.json').write_text(json.dumps({'w1': 'claude'}))
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []

    def test_codex_w4_is_not_broken(self, env: Env) -> None:
        env.write_agents({'w1': 'claude', 'w2': 'claude', 'w3': 'claude', 'w4': 'claude'})
        env.write_task(n=4, task_id='T-4')
        env.write_state(4, pct=90)
        env.settle(n=4)
        assert env.requests(4) == []
        assert env.w._break_eligible(4) is False  # noqa: SLF001

    @pytest.mark.parametrize('task_id', ['a/b', 'T 1', 'T;1'])
    def test_unsafe_task_id_is_not_broken(self, env: Env, task_id: str) -> None:
        env.write_task(task_id=f'"{task_id}"')
        env.write_state(pct=90)
        env.settle()
        assert env.requests() == []

    def test_no_pending_task_is_not_broken(self, env: Env) -> None:
        env.write_state(pct=90)
        rep = env.pj / 'reports' / 'worker1_report.yaml'
        rep.write_text('status: completed\n')
        os.utime(rep, (T0 + 100, T0 + 100))
        os.utime(env.task_yaml(), (T0, T0))
        env.settle()
        assert env.requests() == []


# ---- 流れ ----


class TestFlow:
    def test_request_text_matches_adr(self, env: Env) -> None:
        brief = Path('/x/queue/projects/pj/briefs/T-1.md')
        assert brief_request_text('T-1', 'ctx:52%', brief) == (
            '[BRIEF-REQUEST task_id=T-1 reason=ctx:52%] コンテキストを区切ります。'
            '/x/queue/projects/pj/briefs/T-1.md を instructions/worker.md の 4 項目で書き直し、'
            '書けたら BRIEF-READY とだけ返してください。バックグラウンド処理の完了待ちなど'
            '今は区切れないときは BRIEF-DEFER <理由> と返してください。'
        )
        assert resume_text('T-1', Path('/x/w1.yaml'), brief) == (
            '[RESUME task_id=T-1] /clear 後の再開です。/x/w1.yaml と /x/queue/projects/pj/briefs/T-1.md を読み、'
            'brief の「今の意図と次の一手」から続けてください。'
        )

    def test_request_is_sent_with_separate_enter(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        assert env.sent() == [brief_request_text('T-1', 'ctx:52%', env.brief()), 'Enter']
        assert env.brief().parent.is_dir()

    def test_ready_with_fresh_brief_clears_and_resumes(self, env: Env) -> None:
        request_and_ready(env)
        sent = env.sent()
        i = sent.index('/clear')
        assert sent[i:] == ['/clear', 'Enter', resume_text('T-1', env.task_yaml(), env.brief()), 'Enter']
        assert 1.5 in env.sleeps
        brk = env.read_state()['break']
        assert brk['break_count'] == 1
        assert brk['last_break_at'] == iso(env.wall)
        assert brk['task'] == 'pj/T-1'

    def test_clear_waits_1_5s_before_resume(self, env: Env) -> None:
        order: list[object] = []
        env.w.sleep = order.append
        orig = env.tmux.send_keys

        def send(pane: str, keys: str) -> bool:
            order.append(keys)
            return orig(pane, keys)

        env.tmux.send_keys = send
        request_and_ready(env)
        i = order.index('/clear')
        assert order[i : i + 4] == ['/clear', 0.5, 'Enter', 1.5]
        assert str(order[i + 4]).startswith('[RESUME task_id=T-1]')

    def test_ready_without_brief_update_does_not_clear(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        env.reply('BRIEF-READY', brief_written=False)
        assert '/clear' not in env.sent()

    def test_brief_older_than_request_does_not_clear(self, env: Env) -> None:
        b = env.brief()
        b.parent.mkdir(parents=True)
        b.write_text('old')
        os.utime(b, (T0 - 10, T0 - 10))
        env.write_state(pct=52)
        env.settle()
        env.reply('BRIEF-READY', brief_written=False)
        assert '/clear' not in env.sent()

    def test_request_echo_alone_is_not_a_reply(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        env.advance(30)
        b = env.brief()
        b.write_text('x')
        os.utime(b, (env.wall, env.wall))
        env.write_state(pct=52, event_at=env.wall)
        req = env.requests()[-1]
        # 依頼文は折り返されて pane に写る。その中の BRIEF-READY / BRIEF-DEFER は返事ではない
        env.settle('> ' + req[:40] + '\n  ' + req[40:] + '\n\n> ')
        assert '/clear' not in env.sent()
        assert env.in_flight()

    def test_ready_but_not_waiting_does_not_clear(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        env.advance(30)
        b = env.brief()
        b.write_text('x')
        os.utime(b, (env.wall, env.wall))
        env.write_state(pct=52, event_at=env.wall)
        env.settle('● BRIEF-READY\n' + BUSY)
        assert '/clear' not in env.sent()

    def test_timeout_after_10_minutes_then_retry_after_30(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        env.advance(599)
        env.tick()
        assert env.in_flight()  # 10 分未満はまだ待つ
        env.advance(1)
        env.tick()
        assert not env.in_flight()
        assert '/clear' not in env.sent()
        # 返事が後から来ても /clear しない
        env.reply('BRIEF-READY')
        assert '/clear' not in env.sent()
        assert len(env.requests()) == 1
        env.advance(1800 - 30 - 1)
        env.write_state(pct=52, event_at=env.wall)
        env.tick()
        assert len(env.requests()) == 1
        env.advance(1)
        env.write_state(pct=52, event_at=env.wall)
        env.tick()
        assert len(env.requests()) == 2

    def test_defer_blocks_requests_for_30_minutes(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        env.reply('BRIEF-DEFER waiting-ci', brief_written=False)
        assert not env.in_flight()
        assert '/clear' not in env.sent()
        env.advance(1799)
        env.write_state(pct=52, event_at=env.wall)
        env.tick()
        assert len(env.requests()) == 1
        env.advance(1)
        env.write_state(pct=52, event_at=env.wall)
        env.tick()
        assert len(env.requests()) == 2

    def test_cooldown_30_minutes_after_break(self, env: Env) -> None:
        request_and_ready(env)
        env.tmux.sent.clear()
        env.advance(10)
        env.write_state(pct=90, event_at=env.wall)
        env.settle()
        env.advance(1800 - 10 - 1)
        env.write_state(pct=90, event_at=env.wall)
        env.tick()
        assert env.requests() == []
        env.advance(1)
        env.write_state(pct=90, event_at=env.wall)
        env.tick()
        assert len(env.requests()) == 1

    def test_request_echo_on_pane_is_not_read_as_ctx(self, env: Env) -> None:
        """画面に残った依頼文の reason=ctx:52% を ctx と読まない (ctx は state だけで決める)."""
        env.write_state(pct=52)
        env.settle()
        env.reply('BRIEF-DEFER ci', brief_written=False)
        env.advance(1800)
        env.write_state(pct=None, event_at=env.wall)  # ctx 不明、Stop も時間も目安未満
        env.write_task(created=env.wall - 60)
        env.settle(f'> {env.requests()[-1]}\n● BRIEF-DEFER ci\n> ')
        assert len(env.requests()) == 1

    def test_stop_base_and_minutes_restart_from_break(self, env: Env) -> None:
        env.write_state(pct=52, stop_count=4)
        env.settle()
        env.reply('BRIEF-READY')
        env.write_state(pct=None, stop_count=4, event_at=env.wall)
        brk = env.read_state()['break']
        assert brk['stop_base'] == 4
        env.tmux.sent.clear()
        env.advance(1800)
        env.write_state(pct=None, stop_count=11, event_at=env.wall)
        env.tick()
        assert env.requests() == []  # 差は 7、区切りから 30 分
        env.write_state(pct=None, stop_count=12, event_at=env.wall)
        env.tick()
        assert env.requests() == [brief_request_text('T-1', 'stops:8', env.brief())]

    def test_minutes_restart_from_last_break(self, env: Env) -> None:
        env.write_task(created=T0 - 2 * 3600)
        request_and_ready(env)
        env.tmux.sent.clear()
        env.advance(59 * 60)
        env.write_state(pct=None, event_at=env.wall)
        env.settle()
        assert env.requests() == []
        env.advance(60)
        env.tick()
        assert env.requests() == [brief_request_text('T-1', 'minutes:60', env.brief())]

    def test_limit_5_then_break_limit_once(self, env: Env) -> None:
        for i in range(5):
            env.tmux.sent.clear()
            env.write_state(pct=90, event_at=env.wall)
            env.settle()
            assert len(env.requests()) == 1, i
            env.reply('BRIEF-READY')
            assert '/clear' in env.sent(), i
            env.advance(1800)
        assert env.read_state()['break']['break_count'] == 5
        env.tmux.sent.clear()
        for _ in range(3):
            env.write_state(pct=90, event_at=env.wall)
            env.settle()
            env.advance(3600)
        assert env.requests() == []
        limits = [e for e in env.w.nq.unacked() if '[BREAK-LIMIT' in e['message']]
        assert len(limits) == 1
        assert limits[0]['priority'] == 'low'
        assert 'task_id=T-1' in limits[0]['message']
        assert env.read_state()['break']['limit_notified'] is True

    def test_task_yaml_change_discards_flow(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        p = env.write_task()  # 同じ task_id で書き直し (mtime が変わる)
        os.utime(p, (T0 + 5, T0 + 5))
        env.tick()
        assert not env.in_flight()
        env.reply('BRIEF-READY')
        assert '/clear' not in env.sent()

    def test_report_discards_flow(self, env: Env) -> None:
        env.write_state(pct=52)
        env.settle()
        os.utime(env.task_yaml(), (T0, T0))
        rep = env.pj / 'reports' / 'worker1_report.yaml'
        rep.write_text('status: completed\n')
        os.utime(rep, (T0 + 100, T0 + 100))
        env.tick()
        assert not env.in_flight()
        b = env.brief()
        b.write_text('x')
        os.utime(b, (env.wall + 100, env.wall + 100))
        env.settle('● BRIEF-READY\n> ')
        assert '/clear' not in env.sent()

    def test_new_task_resets_break_count(self, env: Env) -> None:
        request_and_ready(env)
        assert env.read_state()['break']['break_count'] == 1
        env.write_task(task_id='T-2')
        env.tick()
        brk = env.read_state()['break']
        assert brk['task'] == 'pj/T-2'
        assert brk['break_count'] == 0


def test_brief_reply_reads_only_after_request() -> None:
    req = brief_request_text('T-1', 'ctx:52%', Path('/b.md'))
    assert brief_reply(f'> {req}\n') == ''
    assert brief_reply(f'> {req}\n● BRIEF-READY\n') == 'READY'
    assert brief_reply(f'> {req}\n● BRIEF-DEFER ci\n') == 'DEFER'
    assert brief_reply('● BRIEF-READY\n') == 'READY'  # 依頼文が画面の外に流れた


def test_cycle_survives_break_errors(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(env.w, 'check_context_breaks', boom)
    env.w.cycle()  # 例外で watcher を止めない


def test_worker_md_uses_the_same_text_as_watchd() -> None:
    """依頼と再開の文面は watchd と worker.md で一字一句揃える (ADR 0011 の Consequences)."""
    text = (Path(__file__).resolve().parent.parent / 'instructions' / 'worker.md').read_text()
    brief = Path('<brief の絶対パス>')
    assert brief_request_text('<id>', '<ctx:52% | stops:8 | minutes:60>', brief) in text
    assert resume_text('<id>', Path('<task YAML の絶対パス>'), brief) in text
    assert 'queue/projects/<pj>/briefs/<task_id>.md' in text
    headings = ('## 1. タスク文の写し', '## 2. 今の意図と次の一手', '## 3. 計画と完了条件', '## 4. 再開の手がかり')
    for heading in headings:
        assert heading in text
    assert 'BRIEF-DEFER local-model' in text
