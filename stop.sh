#!/bin/bash
# Tmux マルチエージェントシステム終了スクリプト

SESSION_NAME="ros-agents"

echo "マルチエージェントシステムを終了します..."

# 監視デーモン (watcher) を停止
if pkill -f "tmux-multi-agents/watch.sh" 2>/dev/null; then
    echo "watcher を停止しました。"
fi

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
