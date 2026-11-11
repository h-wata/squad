#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Worker / Dispatcher のコンテキスト使用量 (ctx) を取得元つきで state に残す (#105, stdlib only).

取得元は 3 つで、上ほど確か:
  1. statusline: Claude Code が status line コマンドに渡す JSON の context_window (scripts/statusline.py)。
     used_percentage は使用済みの割合 (0-100 の整数)。応答前と /clear 直後は null
  2. transcript: hook が受け取る transcript_path の、最後の assistant メッセージの usage の合計
     (input + cache read + cache creation)。トークン数しか分からないので、使用率は statusline が
     残した context_window_size があるときだけ出す
  3. pane: tmux の画面最下行を正規表現で読む (squad.detect_status)。state には残さず毎回読む

state/<worker>.json の ctx_sources に取得元ごとの値を {session, at, pct, tokens, ...} で持ち、上の順で
最初にあるものを使う。値が null でも上位の取得元があれば下位で埋めない (/clear 前の値を出さないため)。
state/<worker>.json は tmux セッションをまたいで同じファイルなので、別セッションが書いた値は使わない。
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from datetime import datetime
from datetime import timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SOURCES = ('statusline', 'transcript')  # state に残す取得元 (優先順)。pane はこの後
USAGE_KEYS = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')
TRANSCRIPT_CHUNK = 1 << 20


def state_dir() -> Path:
    """SQUAD_STATE_DIR があればそちら (watchd と同じ差し替え口)."""
    return Path(os.environ.get('SQUAD_STATE_DIR') or (ROOT / 'state'))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def as_int(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def usage_tokens(usage: object) -> int | None:
    """Usage の input + cache read + cache creation. 1 つも無ければ None."""
    if not isinstance(usage, dict):
        return None
    vals = [as_int(usage.get(k)) for k in USAGE_KEYS]
    if all(v is None for v in vals):
        return None
    return sum(v or 0 for v in vals)


def _last_usage(lines: list[bytes]) -> int | None:
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'assistant' or entry.get('isSidechain'):
            continue
        msg = entry.get('message')
        if not isinstance(msg, dict) or msg.get('model') == '<synthetic>':
            continue
        tokens = usage_tokens(msg.get('usage'))
        if tokens is not None:
            return tokens
    return None


def transcript_context_tokens(path: str | os.PathLike | None, chunk: int = TRANSCRIPT_CHUNK) -> int | None:
    """Transcript (JSON Lines) の最後の assistant メッセージの usage 合計。読めなければ None.

    transcript は長いので末尾から chunk ずつ広げて読む。subagent (isSidechain) と合成メッセージは除く。
    """
    if not path:
        return None
    try:
        with open(path, 'rb') as f:
            size = f.seek(0, os.SEEK_END)
            span = chunk
            while True:
                start = max(0, size - span)
                f.seek(start)
                lines = f.read(size - start).splitlines()
                if start > 0:
                    lines = lines[1:]  # 途中から読んだ先頭行は欠けている
                found = _last_usage(lines)
                if found is not None or start == 0:
                    return found
                span *= 4
    except OSError:
        return None


def set_slot(state: dict, source: str, *, session: str, **values: Any) -> None:  # noqa: ANN401
    slots = state.get('ctx_sources')
    if not isinstance(slots, dict):
        slots = state['ctx_sources'] = {}
    slots[source] = {'session': session, 'at': now_iso(), **values}


def _slot(state: dict, source: str, session: str | None) -> dict | None:
    slots = state.get('ctx_sources')
    slot = slots.get(source) if isinstance(slots, dict) else None
    if not isinstance(slot, dict):
        return None
    if session and slot.get('session') != session:  # session 無しで書かれた値も別セッションのものとして扱う
        return None
    return slot


def transcript_slot(tokens: int | None, state: dict, session: str | None) -> dict:
    """Transcript 由来の値。使用率は statusline が残した window の大きさがあるときだけ出す."""
    window = as_int((_slot(state, 'statusline', session) or {}).get('window'))
    pct = round(tokens * 100 / window) if tokens is not None and window else None
    return {'pct': pct, 'tokens': tokens, 'window': window}


def resolve(state: dict | None, session: str | None, pane: dict | None = None, now: datetime | None = None) -> dict:
    """優先順 (statusline > transcript > pane) で ctx と model を決める.

    pane は squad.detect_status の結果 (context_pct / model)。どれも無ければ context_source は None。
    """
    state = state if isinstance(state, dict) else {}
    out: dict[str, Any] = {'context_pct': None, 'context_tokens': None, 'context_source': None, 'context_at': None}
    model = None
    for src in SOURCES:
        slot = _slot(state, src, session)
        if slot is None:
            continue
        model = model or slot.get('model')
        if out['context_source'] is None:
            out.update(
                context_pct=as_int(slot.get('pct')),
                context_tokens=as_int(slot.get('tokens')),
                context_source=src,
                context_at=slot.get('at'),
            )
    if pane is not None:
        model = model or pane.get('model')
        pane_pct = as_int(pane.get('context_pct'))
        # 画面から何も読めなければ取得元にしない (取得元と時刻は「値を取れた」ことを表す)
        if out['context_source'] is None and pane_pct is not None:
            at = (now or datetime.now(timezone.utc)).isoformat(timespec='seconds')
            out.update(context_pct=pane_pct, context_source='pane', context_at=at)
    out['model'] = model
    return out


def apply(state: dict, session: str | None, pane: dict | None = None) -> dict:
    """State の top-level (context_pct / model 等) を resolve の結果で揃える (dashboard 等の読み手用)."""
    state.update(resolve(state, session, pane))
    return state


def update_state(path: Path, fn: Callable[[dict], None]) -> dict:
    """State ファイルをロックして読み、fn で書き換え、atomic に書き戻す.

    status line・hook・squad ls が同じファイルを読み書きするので、互いの値を消さないようロックする。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + '.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data: dict = {}
        with contextlib.suppress(OSError, ValueError):
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict):
                data = loaded
        fn(data)
        tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        os.replace(tmp, path)
    return data


def age_label(at: str | None, now: datetime | None = None) -> str:
    """取得時刻からの経過 ('30秒前' / '3分前' / '2時間前')。時刻が無ければ空文字."""
    if not at:
        return ''
    try:
        t = datetime.fromisoformat(at)
    except ValueError:
        return ''
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    sec = max(0, int(((now or datetime.now(timezone.utc)) - t).total_seconds()))
    if sec < 60:
        return f'{sec}秒前'
    if sec < 3600:
        return f'{sec // 60}分前'
    if sec < 86400:
        return f'{sec // 3600}時間前'
    return f'{sec // 86400}日前'
