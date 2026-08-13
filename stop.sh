#!/bin/bash
# Tmux マルチエージェントシステム終了スクリプト
#
# 使い方: ./stop.sh [session_name]
#   session_name > SQUAD_SESSION env > 既定 ros-agents の順で対象を決める。
#   複数 Squad 並行運用時は必ず引数か env で対象セッションを明示すること。

SESSION_NAME="${1:-${SQUAD_SESSION:-ros-agents}}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# watcher の実体は squad/watchd.py (watch.sh は exec するだけの薄いラッパなので、
# 起動後の cmdline に watch.sh は残らない)。移行期間の旧 watch.sh プロセスも拾えるよう
# 両方を含む pgrep パターンにする。
WATCHER_PATTERN="$SCRIPT_DIR/(watch\.sh|squad/watchd\.py)"

echo "マルチエージェントシステムを終了します... (session: $SESSION_NAME)"

# 引数も env も無しで複数の squad watcher が動いている場合は誤爆防止で確認を求める
if [ -z "${1:-}" ] && [ -z "${SQUAD_SESSION:-}" ]; then
    running=$(pgrep -cf "$WATCHER_PATTERN" 2>/dev/null || echo 0)
    if [ "$running" -gt 1 ]; then
        echo "警告: watcher が ${running} 個動いています (複数 Squad 並行運用中?)。"
        echo "対象セッションを明示してください: ./stop.sh <session_name>"
        echo "  実行中セッション:"
        tmux list-sessions -F '    - #{session_name}' 2>/dev/null
        exit 1
    fi
fi

# 監視デーモン (watcher) を停止 — このセッションの watcher だけを対象にする。
# pkill -f watchd.py は他セッションの watcher まで殺すため使わない。
WATCH_PID_FILE="/tmp/${SESSION_NAME}-watch.pid"
watcher_stopped=0
if [ -f "$WATCH_PID_FILE" ]; then
    pid=$(cat "$WATCH_PID_FILE")
    if [ -n "$pid" ] && kill "$pid" 2>/dev/null; then
        watcher_stopped=1
    fi
    rm -f "$WATCH_PID_FILE"
fi
if [ "$watcher_stopped" -eq 0 ]; then
    # pidfile が無い/stale な場合のフォールバック: /proc environ で
    # SQUAD_SESSION が一致する watcher プロセスだけを殺す
    for pid in $(pgrep -f "$WATCHER_PATTERN" 2>/dev/null); do
        env_session=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
            | grep -m1 '^SQUAD_SESSION=' | cut -d= -f2)
        [ -z "$env_session" ] && env_session="ros-agents"
        if [ "$env_session" = "$SESSION_NAME" ] && kill "$pid" 2>/dev/null; then
            watcher_stopped=1
        fi
    done
fi
[ "$watcher_stopped" -eq 1 ] && echo "watcher を停止しました。"

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    # エージェント Pane (Dispatcher + Worker 1-3 + Codex Worker 4)
    for pane in 0 1 2 3 6; do
        echo "Pane $pane (Agent) を終了中..."
        tmux send-keys -t "$SESSION_NAME:0.$pane" C-c
        sleep 0.5
        tmux send-keys -t "$SESSION_NAME:0.$pane" "exit" Enter
        sleep 0.2
    done

    # Terminal / Aux-Shell
    for pane in 4 5; do
        echo "Pane $pane (Aux) を終了中..."
        tmux send-keys -t "$SESSION_NAME:0.$pane" C-c
        sleep 0.2
        tmux send-keys -t "$SESSION_NAME:0.$pane" "exit" Enter
        sleep 0.2
    done

    # セッションを強制終了
    sleep 1
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

    echo ""
    echo "セッション '$SESSION_NAME' を終了しました。"
else
    echo "セッション '$SESSION_NAME' は存在しません。"
fi
