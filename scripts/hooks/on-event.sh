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
STATE_DIR="$SQUAD_DIR/state"
CONFIG="$SQUAD_DIR/config.json"

# stdin (hook JSON) を全部読む
HOOK_JSON="$(cat || true)"
export HOOK_JSON

# worker ID 解決
worker="${SQUAD_WORKER_ID:-}"
if [ -z "$worker" ] && [ -n "${TMUX_PANE:-}" ]; then
    # %85 → session:window.pane 形式に変換
    target=$(tmux display-message -p -t "$TMUX_PANE" \
        '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || true)
    if [ -n "$target" ] && [ -f "$CONFIG" ]; then
        # config.json の pane は session 非依存のサフィックス (例 "0.1") で
        # 持っているため、target ("session:0.1") からもサフィックス側だけを
        # 切り出して比較する (squad.py load_config() と同じ設計)。
        worker=$(CONFIG="$CONFIG" TARGET="$target" python3 -c '
import json, os, sys
try:
    cfg = json.load(open(os.environ["CONFIG"]))
    target = os.environ["TARGET"]
    target_suffix = target.split(":", 1)[-1]
    for name, meta in cfg.get("workers", {}).items():
        pane = meta.get("pane", "")
        pane_suffix = pane.split(":", 1)[-1]
        if pane_suffix == target_suffix:
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
STATE_FILE="$state_file" WORKER="$worker" python3 - <<'PY' || true
import json
import os
from datetime import datetime
from datetime import timezone

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

# 既存 state を読んで部分更新
data = {}
if os.path.exists(state_path):
    try:
        data = json.loads(open(state_path).read())
    except Exception:
        data = {}

data['worker'] = worker
if status is not None:
    data['status'] = status
data['last_event'] = last_event
data['last_event_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
if msg:
    data['last_event_message'] = msg[:200]
data['updated_at'] = data['last_event_at']

tmp = state_path + '.tmp'
with open(tmp, 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
os.replace(tmp, state_path)
PY

exit 0
