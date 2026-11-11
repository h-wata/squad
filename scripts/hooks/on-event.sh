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
#
# 解決できなければサイレントに exit 0 (Claude のターンを止めない)。
#
# settings.local.json の hooks セクションから呼ばれる:
#   Stop / Notification / StopFailure
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
    if [ -n "$target" ] && [ -f "$CONFIG" ]; then
        # config.json の pane は session 非依存の window 名 (例 "w1") で持つ (#95)。
        # 旧構成 (window 0 を 7 pane に分けていた頃) のセッションは pane 番号 ("0.1") で照合する。
        worker=$(CONFIG="$CONFIG" TARGET="$target" python3 -c '
import json, os, sys
LEGACY = {"0.0": "dispatcher", "0.1": "w1", "0.2": "w2", "0.3": "w3", "0.4": "term", "0.5": "aux", "0.6": "w4"}
try:
    cfg = json.load(open(os.environ["CONFIG"]))
    window, index = os.environ["TARGET"].rsplit(" ", 1)
    if window not in LEGACY.values():  # 役割名の window が無い = 旧構成
        window = LEGACY.get(index, window)
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
STATE_FILE="$state_file" WORKER="$worker" SQUAD_DIR="$SQUAD_DIR" python3 - <<'PY' || true
import json
import os
import sys
from datetime import datetime
from datetime import timezone
from pathlib import Path

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


def merge(data):
    data['worker'] = worker
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
