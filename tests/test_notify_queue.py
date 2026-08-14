#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.notify_queue の単体テスト (SQUAD-220).

session-local durable queue の永続化・dedupe・ack・critical fallback backoff・
session namespace 分離を検証する。
"""

from __future__ import annotations

from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from notify_queue import fallback_backoff_seconds  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402


def make_nq(tmp_path: Path, session: str = 'testsess') -> NotificationQueue:
    return NotificationQueue(session, notify_dir_for(tmp_path / 'queue', session))


class TestPersistence:
    def test_enqueue_survives_process_restart(self, tmp_path: Path) -> None:
        """プロセス再起動後 (= 新しい NotificationQueue インスタンス) も event は残る."""
        nq1 = make_nq(tmp_path)
        nq1.enqueue(project='pj', source='report', priority='normal', message='m1', dedupe_key='k1')

        nq2 = make_nq(tmp_path)  # 別インスタンス = 再起動を模擬
        assert len(nq2.unacked()) == 1
        assert nq2.unacked()[0]['message'] == 'm1'

    def test_write_is_atomic_rename(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='report', priority='normal', message='m1', dedupe_key='k1')
        assert nq.events_path.exists()
        assert not any(p.name.startswith('.events.json.tmp') for p in nq.dir.iterdir())


class TestDedupe:
    def test_same_dedupe_key_merges_while_unacked(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        e1 = nq.enqueue(project='pj', source='stall', priority='low', message='first', dedupe_key='k')
        e2 = nq.enqueue(project='pj', source='stall', priority='low', message='second', dedupe_key='k')
        assert e1['event_id'] == e2['event_id']
        assert len(nq.unacked()) == 1
        assert nq.unacked()[0]['message'] == 'second'
        assert nq.unacked()[0]['attempts'] == 2

    def test_different_dedupe_key_creates_separate_events(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='report', priority='normal', message='a', dedupe_key='k1')
        nq.enqueue(project='pj', source='report', priority='normal', message='b', dedupe_key='k2')
        assert len(nq.unacked()) == 2

    def test_acked_dedupe_key_gets_fresh_event_on_recurrence(self, tmp_path: Path) -> None:
        """Ack 済みの原因が再発したら、ack 済み event に merge されず新しい event になる.

        Merge してしまうと ack 済みのまま新しい通知が黙って消える (沈黙) ため。
        """
        nq = make_nq(tmp_path)
        e1 = nq.enqueue(project='pj', source='stall', priority='low', message='first', dedupe_key='k')
        nq.ack(e1['event_id'])
        assert nq.unacked() == []
        e2 = nq.enqueue(project='pj', source='stall', priority='low', message='second', dedupe_key='k')
        assert e2['event_id'] != e1['event_id']
        assert len(nq.unacked()) == 1


class TestAck:
    def test_ack_is_per_event_id_and_idempotent(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        e1 = nq.enqueue(project='pj', source='report', priority='normal', message='a', dedupe_key='k1')
        e2 = nq.enqueue(project='pj', source='report', priority='normal', message='b', dedupe_key='k2')
        assert nq.ack(e1['event_id'])
        assert [e['event_id'] for e in nq.unacked()] == [e2['event_id']]
        assert nq.ack(e1['event_id'])  # 二重 ack は no-op で成功扱い
        assert [e['event_id'] for e in nq.unacked()] == [e2['event_id']]

    def test_ack_does_not_lose_other_unacked_events_on_reorder(self, tmp_path: Path) -> None:
        """Ack は event_id を追記する集合であり、最後の1件だけを覚える方式ではない."""
        nq = make_nq(tmp_path)
        events = [
            nq.enqueue(project='pj', source='report', priority='normal', message=str(i), dedupe_key=f'k{i}')
            for i in range(3)
        ]
        nq.ack(events[1]['event_id'])
        remaining = {e['event_id'] for e in nq.unacked()}
        assert remaining == {events[0]['event_id'], events[2]['event_id']}


class TestCriticalFallback:
    def test_no_fallback_before_threshold(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')
        due = nq.due_critical_fallback(now=100, threshold_seconds=300)
        assert due is None

    def test_fallback_due_after_threshold_and_then_backs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')

        due = nq.due_critical_fallback(now=base + 301, threshold_seconds=300)
        assert due is not None
        assert due['count'] == 1
        nq.mark_fallback_sent(now=base + 301)

        # backoff 中 (300s cap の前段 = 300s) はまだ次を送らない
        assert nq.due_critical_fallback(now=base + 301 + 100, threshold_seconds=300) is None
        assert nq.due_critical_fallback(now=base + 301 + 300 + 1, threshold_seconds=300) is not None

    def test_fallback_state_resets_when_all_criticals_acked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        e = nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')
        nq.due_critical_fallback(now=base + 301, threshold_seconds=300)
        nq.mark_fallback_sent(now=base + 301)
        nq.ack(e['event_id'])
        assert nq.due_critical_fallback(now=base + 302, threshold_seconds=300) is None

        # 新しい critical が来たら、また閾値から数え直す (前回の backoff を引きずらない)
        monkeypatch.setattr(time, 'time', lambda: base + 302)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m2', dedupe_key='k2')
        assert nq.due_critical_fallback(now=base + 302 + 100, threshold_seconds=300) is None
        assert nq.due_critical_fallback(now=base + 302 + 301, threshold_seconds=300) is not None

    def test_normal_and_low_priority_never_trigger_fallback(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k1')
        nq.enqueue(project='pj', source='stall', priority='low', message='m', dedupe_key='k2')
        assert nq.due_critical_fallback(now=10_000, threshold_seconds=300) is None

    def test_fallback_backoff_seconds_caps(self) -> None:
        assert fallback_backoff_seconds(1) == 300
        assert fallback_backoff_seconds(5) == 3600
        assert fallback_backoff_seconds(99) == 3600  # 上限を超えても cap で鳴り続ける


class TestSessionNamespace:
    def test_different_sessions_do_not_see_each_others_events(self, tmp_path: Path) -> None:
        nq_a = make_nq(tmp_path, session='a')
        nq_b = make_nq(tmp_path, session='b')
        nq_a.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k')
        assert nq_a.unacked() != []
        assert nq_b.unacked() == []


class TestHealth:
    def test_write_health_reports_unacked_and_write_status(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')
        nq.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k2')
        nq.write_health(owned_projects=2, write_ok=True)
        health = nq.read_health()
        assert health['owned_projects'] == 2
        assert health['queue_write_ok'] is True
        assert health['unacked_total'] == 2
        assert health['unacked_critical'] == 1
        assert health['oldest_unacked_critical_age_seconds'] is not None

    def test_health_survives_no_events(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.write_health(owned_projects=0, write_ok=True)
        health = nq.read_health()
        assert health['unacked_total'] == 0
        assert health['oldest_unacked_critical_age_seconds'] is None
