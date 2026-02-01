#!/bin/bash
# Tmux マルチエージェントシステム終了スクリプト

SESSION_NAME="ros-agents"

echo "マルチエージェントシステムを終了します..."

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    # 各エージェント Pane に終了シグナルを送信
    for pane in 0 1 2 3; do
        echo "Pane $pane を終了中..."
        # Ctrl+C を送信して実行中のプロセスを停止
        tmux send-keys -t "$SESSION_NAME:0.$pane" C-c
        sleep 0.5
        # exit を送信
        tmux send-keys -t "$SESSION_NAME:0.$pane" "exit" Enter
        sleep 0.2
    done

    # ROS用 Pane も終了
    for pane in 4 5; do
        echo "Pane $pane (ROS) を終了中..."
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
