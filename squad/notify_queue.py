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


class QueueUnreadableError(Exception):
    """events.json / ack.json が存在するのに読めない (破損・IO エラー).

    「空 queue」と区別するための例外。空と同一視すると、未 ack の通知を保持したまま
    上書きして永久に消してしまう (SQUAD-226 critical 1)。
    """


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
        """壊れていても空扱いで良いファイル (backoff 状態・health) 用の緩い読出し."""
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _load_strict(path: Path) -> dict[str, Any]:
        """通知本体 (events/ack) 用。未作成なら空、壊れていれば QueueUnreadableError.

        `path.exists()` 自体も親ディレクトリの権限次第で PermissionError を送出しうる
        (mode 000 等)。read と同じ try に入れて OSError 全般を QueueUnreadableError に
        正規化する (SQUAD-234: これが外にあったため cycle() が恒久停止していた)。

        SQUAD-272: `path.exists()` の bool 判定は dangling symlink (リンク先が無い)
        や ENOTDIR (親パスが非ディレクトリ) でも例外を出さず False を返すため、
        「未作成」と区別がつかず空 queue に化ける。os.lstat で symlink 自体の存在を
        確認してから read することで両者を区別する。
        """
        try:
            os.lstat(path)
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise QueueUnreadableError(f'{path}: {e}') from e
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise QueueUnreadableError(f'{path}: {e}') from e
        if not isinstance(data, dict):
            raise QueueUnreadableError(f'{path}: top-level object ではありません')
        return data

    def _atomic_write(self, path: Path, data: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f'.{path.name}.tmp.{os.getpid()}')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
        os.replace(tmp, path)

    def _events(self) -> dict[str, dict[str, Any]]:
        return self._load_strict(self.events_path).get('events', {})

    def _acked_ids(self) -> set[str]:
        return set(self._load_strict(self.ack_path).get('acked', {}).keys())

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
        acked = self._load_strict(self.ack_path).get('acked', {})
        if event_id in acked:
            return True
        acked[event_id] = {'acked_at': int(time.time()), 'acked_by': by}
        try:
            self._atomic_write(self.ack_path, {'acked': acked})
            return True
        except OSError:
            return False

    # ---- critical fallback (age-based, backoff) ----

    def due_fallback(self, now: int, thresholds: dict[str, int]) -> dict[str, Any] | None:
        """未 ack event が priority 別閾値を超えたら {'priority','count','oldest_age'} を返す.

        critical だけでなく normal / low も age で昇格させる。Dispatcher が pull を
        忘れても、閾値を過ぎれば Pane 0 へ「N 件未確認」の 1 行が出て永久沈黙しない
        (SQUAD-226 critical 2)。閾値超過が複数 priority にまたがるときは、より重い
        priority を代表として報告する (count は超過分の合計)。

        Fallback は event 単位ではなく queue 全体で 1 本のメッセージにまとめる。
        未 ack がゼロになれば backoff 状態をリセットする (次の通知でまた閾値から数え直す)。
        Backoff 未到来なら None。events/ack が読めない場合は QueueUnreadableError を送出する。
        """
        due = [e for e in self.unacked() if now - e['created_at'] >= thresholds.get(e['priority'], 0)]
        if not due:
            if self._load(self.fallback_path):
                self._atomic_write(self.fallback_path, {})
            return None
        state = self._load(self.fallback_path)
        next_at = state.get('next_fallback_at', 0)
        if next_at and now < next_at:
            return None
        top = min(due, key=lambda e: PRIORITIES.index(e['priority']))
        return {
            'priority': top['priority'],
            'count': len(due),
            'oldest_age': now - min(e['created_at'] for e in due),
        }

    def fallback_due(self, now: int) -> bool:
        """Backoff 的に今 fallback を送って良いか (events を読まずに判定できる)."""
        next_at = self._load(self.fallback_path).get('next_fallback_at', 0)
        return not next_at or now >= next_at

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
        """Health を更新する。queue 本体が読めない場合も (むしろその時こそ) 記録する."""
        now = int(time.time())
        data: dict[str, Any] = {
            'session': self.session,
            'updated_at': now,
            'owned_projects': owned_projects,
            'queue_write_ok': write_ok,
            'queue_readable': True,
            'queue_error': '',
        }
        try:
            unacked = self.unacked()
        except QueueUnreadableError as e:
            # 未 ack の件数すら分からない = 最も危険な状態。health は必ず残す
            # (Pane 0 への直送 alert は watchd 側が別経路で出す)。
            data.update(
                queue_readable=False,
                queue_error=str(e),
                unacked_total=None,
                unacked_critical=None,
                oldest_unacked_critical_age_seconds=None,
            )
            self._atomic_write(self.health_path, data)
            return
        criticals = [e for e in unacked if e['priority'] == 'critical']
        oldest = min((e['created_at'] for e in criticals), default=None)
        data.update(
            unacked_total=len(unacked),
            unacked_critical=len(criticals),
            oldest_unacked_critical_age_seconds=(now - oldest) if oldest is not None else None,
        )
        self._atomic_write(self.health_path, data)

    def read_health(self) -> dict[str, Any]:
        return self._load(self.health_path)
