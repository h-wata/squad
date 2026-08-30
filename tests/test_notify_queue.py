#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.notify_queue の単体テスト (SQUAD-220).

session-local durable queue の永続化・dedupe・ack・critical fallback backoff・
session namespace 分離を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from notify_queue import fallback_backoff_seconds  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import QueueUnreadableError  # noqa: E402

# 既定の priority 別 fallback 閾値 (watchd.Config と同じ値)
TH = {'critical': 300, 'normal': 900, 'low': 3600}


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
        due = nq.due_fallback(now=100, thresholds=TH)
        assert due is None

    def test_fallback_due_after_threshold_and_then_backs_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')

        due = nq.due_fallback(now=base + 301, thresholds=TH)
        assert due is not None
        assert due['count'] == 1
        nq.mark_fallback_sent(now=base + 301)

        # backoff 中 (300s cap の前段 = 300s) はまだ次を送らない
        assert nq.due_fallback(now=base + 301 + 100, thresholds=TH) is None
        assert nq.due_fallback(now=base + 301 + 300 + 1, thresholds=TH) is not None

    def test_fallback_state_resets_when_all_criticals_acked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        e = nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k')
        nq.due_fallback(now=base + 301, thresholds=TH)
        nq.mark_fallback_sent(now=base + 301)
        nq.ack(e['event_id'])
        assert nq.due_fallback(now=base + 302, thresholds=TH) is None

        # 新しい critical が来たら、また閾値から数え直す (前回の backoff を引きずらない)
        monkeypatch.setattr(time, 'time', lambda: base + 302)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m2', dedupe_key='k2')
        assert nq.due_fallback(now=base + 302 + 100, thresholds=TH) is None
        assert nq.due_fallback(now=base + 302 + 301, thresholds=TH) is not None

    def test_normal_and_low_are_promoted_after_their_own_thresholds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SQUAD-226 critical 2: normal/low も age で昇格する (pull を忘れても沈黙しない)."""
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k1')
        nq.enqueue(project='pj', source='stall', priority='low', message='m', dedupe_key='k2')

        assert nq.due_fallback(now=base + 301, thresholds=TH) is None  # critical 閾値では鳴らない
        due = nq.due_fallback(now=base + 901, thresholds=TH)
        assert due is not None and due['priority'] == 'normal' and due['count'] == 1

        nq.mark_fallback_sent(now=base + 901)
        due = nq.due_fallback(now=base + 3601, thresholds=TH)
        assert due is not None and due['count'] == 2  # low も閾値超過で合流

    def test_highest_priority_is_reported_when_several_are_due(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base = 1_000_000
        monkeypatch.setattr(time, 'time', lambda: base)
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k1')
        nq.enqueue(project='pj', source='blocked', priority='critical', message='m', dedupe_key='k2')
        due = nq.due_fallback(now=base + 901, thresholds=TH)
        assert due is not None and due['priority'] == 'critical' and due['count'] == 2


class TestCorruptQueue:
    """SQUAD-226 critical 1: 破損 queue を「空」と同一視しない (fault injection)."""

    def test_enqueue_refuses_to_overwrite_corrupt_events(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        crit = nq.enqueue(project='pj', source='blocked', priority='critical', message='CRIT', dedupe_key='k1')
        broken = '{corrupt'
        nq.events_path.write_text(broken)

        with pytest.raises(QueueUnreadableError):
            nq.enqueue(project='pj', source='report', priority='normal', message='m', dedupe_key='k2')
        # 既存ファイルを壊していない = 復旧すれば critical は生きている
        assert nq.events_path.read_text() == broken
        nq.events_path.write_text(json.dumps({'events': {'k1': crit}}))
        assert [e['event_id'] for e in nq.unacked()] == [crit['event_id']]

    def test_unacked_and_fallback_raise_instead_of_reporting_empty(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='CRIT', dedupe_key='k1')
        nq.events_path.write_text('{corrupt')
        with pytest.raises(QueueUnreadableError):
            nq.unacked()
        with pytest.raises(QueueUnreadableError):
            nq.due_fallback(now=9_999_999_999, thresholds=TH)

    def test_corrupt_ack_file_does_not_silently_unack(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='CRIT', dedupe_key='k1')
        nq.ack_path.write_text('{corrupt')
        with pytest.raises(QueueUnreadableError):
            nq.unacked()

    def test_health_records_unreadable_queue(self, tmp_path: Path) -> None:
        """未 ack 件数すら読めない状況を health に必ず残す (別経路の可視化)."""
        nq = make_nq(tmp_path)
        nq.enqueue(project='pj', source='blocked', priority='critical', message='CRIT', dedupe_key='k1')
        nq.events_path.write_text('{corrupt')
        nq.write_health(owned_projects=1, write_ok=False)
        h = nq.read_health()
        assert h['queue_readable'] is False
        assert h['queue_error']
        assert h['unacked_critical'] is None

    def test_fallback_backoff_works_without_reading_events(self, tmp_path: Path) -> None:
        """Queue 破損時の alert も backoff する (毎サイクル鳴り続けない)."""
        nq = make_nq(tmp_path)
        nq.dir.mkdir(parents=True)
        nq.events_path.write_text('{corrupt')
        assert nq.fallback_due(1_000_000) is True
        nq.mark_fallback_sent(1_000_000)
        assert nq.fallback_due(1_000_100) is False
        assert nq.fallback_due(1_000_301) is True

    def test_fallback_backoff_seconds_caps(self) -> None:
        assert fallback_backoff_seconds(1) == 300
        assert fallback_backoff_seconds(5) == 3600
        assert fallback_backoff_seconds(99) == 3600  # 上限を超えても cap で鳴り続ける


class TestUnreadablePath:
    """SQUAD-272: path.exists() の bool 判定だけでは dangling symlink / ENOTDIR を「空 queue」と誤判定してしまう.

    exists() は例外を出さず False を返すため。
    """

    def test_dangling_symlink_raises_unreadable(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        nq.dir.mkdir(parents=True)
        target = tmp_path / 'does_not_exist.json'
        nq.events_path.symlink_to(target)

        with pytest.raises(QueueUnreadableError):
            nq.unacked()

    def test_enotdir_in_parent_path_raises_unreadable(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / 'not_a_dir'
        not_a_dir.write_text('')
        nq = NotificationQueue('testsess', not_a_dir / 'nested')

        with pytest.raises(QueueUnreadableError):
            nq.unacked()

    def test_missing_file_with_normal_parent_returns_empty(self, tmp_path: Path) -> None:
        nq = make_nq(tmp_path)
        assert nq.unacked() == []


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
