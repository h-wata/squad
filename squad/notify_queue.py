#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Session-local 永続通知キュー (stdlib only).

watchd.py が Dispatcher pane (0) へ直接 send-keys していた通知を、先に
`queue/notifications/<session>/` 配下の JSON へ永続化してから配る方式に置き換える
(SQUAD-220, SQUAD-213 設計)。

優先度:
  critical: status:blocked report / report_id 欠落など。即時 enqueue。未 ack が
            一定時間を超えたときだけ Pane 0 へ 1 行 fallback (age-based, 指数 backoff)。
  normal:   通常 report。即時 enqueue。Dispatcher の次の pull で読む。
  low:      stall / discovery / sweep。Pane 直送はせず、dedupe_key で同一原因を
            1 件に畳んで queue に留める。

配達の記録は event_id 単位の `events.json` (dedupe_key をキーに upsert) と、
event 単位で追記する `ack.json` の 2 ファイルに分ける。ack は「最後の event id」
ではなく acked な event_id の集合を持つ (並び替え・再送で ack 済み判定を失わないため)。
書込みはすべて temp file + os.replace の atomic rename。

複数 Dispatcher が同一 session を誤って起動しても、ack は追記 (存在すれば no-op) で
event を消さないため競合しない。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

PRIORITIES = ('critical', 'normal', 'low')

# critical fallback (Pane 0 への唯一の直送経路) の再送間隔。末尾が cap。
FALLBACK_BACKOFF_SECONDS = (300, 600, 1200, 2400, 3600)


def fallback_backoff_seconds(fallback_count: int) -> int:
    """Fallback_count 回目の送信のあと、次に送るまで待つ秒数 (最後の値が cap)."""
    return FALLBACK_BACKOFF_SECONDS[min(max(fallback_count, 1), len(FALLBACK_BACKOFF_SECONDS)) - 1]


def notify_dir_for(queue_dir: Path, session: str) -> Path:
    return queue_dir / 'notifications' / session


class NotificationQueue:
    """Session 単位の durable notification queue."""

    def __init__(self, session: str, notify_dir: Path) -> None:
        self.session = session
        self.dir = Path(notify_dir)
        self.events_path = self.dir / 'events.json'
        self.ack_path = self.dir / 'ack.json'
        self.health_path = self.dir / 'health.json'
        self.fallback_path = self.dir / 'fallback.json'

    # ---- 内部: atomic read/write ----

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.tmp.{os.getpid()}')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(tmp, path)

    def _events(self) -> dict[str, dict[str, Any]]:
        return self._load(self.events_path).get('events', {})

    def _acked_ids(self) -> set[str]:
        return set(self._load(self.ack_path).get('acked', {}).keys())

    # ---- enqueue ----

    def enqueue(self, *, project: str, source: str, priority: str, message: str, dedupe_key: str) -> dict[str, Any]:
        """Event を永続化する (dedupe_key が未 ack のまま既存なら debounce merge).

        既に ack 済みの dedupe_key は新しい event として扱う (ack 済み event に
        merge すると、その通知が ack 済み扱いのまま黙って消えてしまうため)。
        """
        if priority not in PRIORITIES:
            raise ValueError(f'unknown priority: {priority!r}')
        now = int(time.time())
        events = self._events()
        acked = self._acked_ids()
        existing = events.get(dedupe_key)
        if existing and existing['event_id'] not in acked:
            existing['updated_at'] = now
            existing['message'] = message
            existing['attempts'] = existing.get('attempts', 1) + 1
            event = existing
        else:
            event = {
                'event_id': str(uuid.uuid4()),
                'session': self.session,
                'project': project,
                'source': source,
                'priority': priority,
                'created_at': now,
                'updated_at': now,
                'message': message,
                'dedupe_key': dedupe_key,
                'attempts': 1,
                'fallback_count': 0,
                'last_fallback_at': 0,
            }
        events[dedupe_key] = event
        self._atomic_write(self.events_path, {'events': events})
        return event

    # ---- pull / ack ----

    def unacked(self) -> list[dict[str, Any]]:
        acked = self._acked_ids()
        events = [e for e in self._events().values() if e['event_id'] not in acked]
        return sorted(events, key=lambda e: e['created_at'])

    def ack(self, event_id: str, by: str = '') -> bool:
        """Event_id を ack 済みにする (追記。既に ack 済みなら何もしない = 冪等)."""
        acked = self._load(self.ack_path).get('acked', {})
        if event_id in acked:
            return True
        acked[event_id] = {'acked_at': int(time.time()), 'acked_by': by}
        try:
            self._atomic_write(self.ack_path, {'acked': acked})
            return True
        except OSError:
            return False

    # ---- critical fallback (age-based, backoff) ----

    def due_critical_fallback(self, now: int, threshold_seconds: int) -> dict[str, Any] | None:
        """未 ack critical が閾値超過なら {'count', 'oldest_age'} を返す (backoff 未到来なら None).

        Fallback は event 単位ではなく queue 全体で 1 本のメッセージにまとめる
        (「未確認 critical 通知 N 件」)。未 ack critical がゼロになれば backoff 状態を
        リセットする (次に新しい critical が来たとき、また 5 分から数え直すため)。
        """
        criticals = [e for e in self.unacked() if e['priority'] == 'critical']
        if not criticals:
            if self.fallback_path.exists():
                self._atomic_write(self.fallback_path, {})
            return None
        oldest = min(e['created_at'] for e in criticals)
        age = now - oldest
        if age < threshold_seconds:
            return None
        state = self._load(self.fallback_path)
        next_at = state.get('next_fallback_at', 0)
        if next_at and now < next_at:
            return None
        return {'count': len(criticals), 'oldest_age': age}

    def mark_fallback_sent(self, now: int) -> None:
        state = self._load(self.fallback_path)
        fallback_count = state.get('fallback_count', 0) + 1
        wait = fallback_backoff_seconds(fallback_count)
        self._atomic_write(
            self.fallback_path,
            {'fallback_count': fallback_count, 'next_fallback_at': now + wait, 'last_fallback_at': now},
        )

    # ---- health ----

    def write_health(self, *, owned_projects: int, write_ok: bool) -> None:
        now = int(time.time())
        unacked = self.unacked()
        criticals = [e for e in unacked if e['priority'] == 'critical']
        oldest = min((e['created_at'] for e in criticals), default=None)
        data = {
            'session': self.session,
            'updated_at': now,
            'owned_projects': owned_projects,
            'queue_write_ok': write_ok,
            'unacked_total': len(unacked),
            'unacked_critical': len(criticals),
            'oldest_unacked_critical_age_seconds': (now - oldest) if oldest is not None else None,
        }
        self._atomic_write(self.health_path, data)

    def read_health(self) -> dict[str, Any]:
        return self._load(self.health_path)
