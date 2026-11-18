#!/usr/bin/env bash
# on-event.sh — Claude Code hook が呼び出すイベント受信スクリプト
#
# squad/state/<worker>.json を更新して worker の即時状態を共有する。
# stdin に hook JSON が来る (session_id, transcript_path, cwd,
# hook_event_name, permission_mode + イベント固有 fields)。
#
# worker ID 解決の優先順:
#   1. $SQUAD_WORKER_ID  (start.sh が pane 起動時に export する想定)
#   2. $TMUX_PANE        (e.g. "%85") → tmux で session:pane を得て config.json で逆引き
#                        window 名 dispatcher は config.json に無いので worker dispatcher にする (ADR 0011)
#
# 解決できなければサイレントに exit 0 (Claude のターンを止めない)。
# stdout には何も出さない (UserPromptSubmit の stdout はプロンプトに足される)。
#
# settings.local.json の hooks セクションから呼ばれる:
#   Stop / Notification / StopFailure / UserPromptSubmit
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SQUAD_DIR="$REPO_ROOT/squad"
STATE_DIR="${SQUAD_STATE_DIR:-$SQUAD_DIR/state}"  # テストでは一時ディレクトリに差し替える
CONFIG="$SQUAD_DIR/config.json"

# stdin (hook JSON) を全部読む
HOOK_JSON="$(cat || true)"
export HOOK_JSON

# worker ID 解決
worker="${SQUAD_WORKER_ID:-}"
if [ -z "$worker" ] && [ -n "${TMUX_PANE:-}" ]; then
    # %85 → window 名と window.pane 番号 (例 "w1 0.0")
    target=$(tmux display-message -p -t "$TMUX_PANE" \
        '#{window_name} #{window_index}.#{pane_index}' 2>/dev/null || true)
    if [ -n "$target" ]; then
        # config.json の pane は session 非依存の window 名 (例 "w1") で持つ (#95)。
        # 旧構成 (window 0 を 7 pane に分けていた頃) のセッションは pane 番号 ("0.1") で照合する。
        worker=$(CONFIG="$CONFIG" TARGET="$target" python3 -c '
import json, os, sys
LEGACY = {"0.0": "dispatcher", "0.1": "w1", "0.2": "w2", "0.3": "w3", "0.4": "term", "0.5": "aux", "0.6": "w4"}
try:
    window, index = os.environ["TARGET"].rsplit(" ", 1)
    if window not in LEGACY.values():  # 役割名の window が無い = 旧構成
        window = LEGACY.get(index, window)
    if window == "dispatcher":  # Dispatcher は config.json の workers に無い (ADR 0011)
        print("dispatcher"); sys.exit(0)
    cfg = json.load(open(os.environ["CONFIG"]))
    for name, meta in cfg.get("workers", {}).items():
        pane = meta.get("pane", "").rsplit(":", 1)[-1]
        if LEGACY.get(pane, pane) == window:
            print(name); sys.exit(0)
except Exception:
    pass
' 2>/dev/null || true)
    fi
fi

if [ -z "$worker" ]; then
    # worker 不明: ターン進行に影響させず無音終了
    exit 0
fi

mkdir -p "$STATE_DIR"
state_file="$STATE_DIR/${worker}.json"

# Python で JSON merge + atomic write (既存 status/model/context_pct 等は保持)
# transcript_path があれば、最後の assistant の usage を ctx の取得元 transcript として残す (#105)
# Stop で stop_count を足し、UserPromptSubmit で人間の入力なら last_human_prompt_at を書く (ADR 0011)
STATE_FILE="$state_file" WORKER="$worker" SQUAD_DIR="$SQUAD_DIR" python3 - <<'PY' || true
import json
import os
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path
import zlib

sys.path.insert(0, os.environ['SQUAD_DIR'])
import ctx

state_path = os.environ['STATE_FILE']
worker = os.environ['WORKER']
try:
    hook = json.loads(os.environ.get('HOOK_JSON', '') or '{}')
except json.JSONDecodeError:
    hook = {}

event = hook.get('hook_event_name', '')
ntype = hook.get('type', '')          # Notification.type
err = hook.get('error_type', '')      # StopFailure.error_type
msg = hook.get('message', '')         # Notification.message

# event → status マップ
status = None
last_event = event or 'unknown'
if event == 'Stop':
    status, last_event = 'completed', 'Stop'
elif event == 'StopFailure':
    last_event = f'StopFailure({err})' if err else 'StopFailure'
    status = 'stop_failure'
elif event == 'Notification':
    if ntype == 'idle_prompt':
        status, last_event = 'idle', 'idle_prompt'
    elif ntype == 'permission_prompt':
        status, last_event = 'permission_wait', 'permission_prompt'
    else:
        last_event = f'Notification({ntype})' if ntype else 'Notification'

session = os.environ.get('SQUAD_SESSION', '')
transcript = hook.get('transcript_path') or ''
tokens = ctx.transcript_context_tokens(transcript) if transcript else None  # ロックの外で読む
WEB_PREFIX = '[squad-web 指示]'


def _sent_origins(prompt, sent):
    """watchd_sent のうち crc32 が prompt と一致し、human_origin が bool の記録の human_origin 一覧."""
    if not isinstance(sent, list):
        return []
    crc = zlib.crc32(prompt.encode('utf-8'))
    out = []
    for rec in sent:
        if not isinstance(rec, dict):
            continue
        c, origin = rec.get('crc32'), rec.get('human_origin')
        if isinstance(c, bool) or not isinstance(c, int) or c != crc or not isinstance(origin, bool):
            continue
        out.append(origin)
    return out


def is_human_prompt(prompt, sent):
    """ADR 0011 の 4. の順序で判定し、最初に当てはまったもので決める."""
    if not isinstance(prompt, str):
        return True  # 4. 入力が読めない: 照合できないので人間の入力 (区切らない側)
    # 1. [squad-web 指示] で始まる: watchd_sent にあっても人間の入力
    if prompt.startswith(WEB_PREFIX):
        return True
    origins = _sent_origins(prompt, sent)
    # 2. crc32 が watchd_sent にあり human_origin が true: 人間の入力
    if True in origins:
        return True
    # 3. crc32 が watchd_sent にあり human_origin が false: watchd の送信
    if False in origins:
        return False
    # 4. それ以外 (crc32 が無い、watchd_sent や human_origin が無い・壊れている): 人間の入力
    return True


def merge(data):
    data['worker'] = worker
    if event == 'UserPromptSubmit':
        # 入力の受付では status / last_event を変えない (watchd は last_event で待機中を判定する)
        if is_human_prompt(hook.get('prompt'), data.get('watchd_sent')):
            data['last_human_prompt_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        return
    if event == 'Stop':
        # watchd の区切り判定 (ADR 0011 の 2.) が差を数える。単調増加。壊れていれば 0 から数え直す
        count = data.get('stop_count')
        valid = isinstance(count, int) and not isinstance(count, bool) and count >= 0
        data['stop_count'] = (count if valid else 0) + 1
    if status is not None:
        data['status'] = status
    data['last_event'] = last_event
    data['last_event_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if msg:
        data['last_event_message'] = msg[:200]
    data['updated_at'] = data['last_event_at']
    if transcript:
        ctx.set_slot(data, 'transcript', session=session, **ctx.transcript_slot(tokens, data, session))
        ctx.apply(data, session)


ctx.update_state(Path(state_path), merge)
PY

exit 0
