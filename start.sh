#!/bin/bash
# Tmux マルチエージェントシステム起動スクリプト
# ROS2 Jazzy 対応

set -e

# 引数チェック
if [ $# -lt 1 ]; then
    echo "使用方法: $0 <workspace_path>"
    echo "例: $0 /home/gisen/rmf_ws"
    exit 1
fi

WORKSPACE="$(cd "$1" 2>/dev/null && pwd)" || {
    echo "エラー: ワークスペース '$1' が見つかりません"
    exit 1
}

SESSION_NAME="ros-agents"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROS_SETUP="/opt/ros/jazzy/setup.bash"

# 各エージェントのパーミッション設定
# Dispatcher: タスク管理のみ、コード調査禁止
DISPATCHER_TOOLS="Read Write Edit Bash(tmux:*)"

# Worker: フル権限（何でもやる）
WORKER_TOOLS="Read Write Edit Grep Glob Bash Task"

# 既存セッションがあれば終了
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "既存のセッション '$SESSION_NAME' を終了します..."
    tmux kill-session -t "$SESSION_NAME"
fi

echo "マルチエージェントシステムを起動します..."

# 新しいセッションを作成（Pane 0: Dispatcher）
tmux new-session -d -s "$SESSION_NAME" -x 200 -y 50

# Pane を追加して 2x3 グリッドを構成
# Pane 0 | Pane 1
# Pane 2 | Pane 3
# Pane 4 | Pane 5

tmux split-window -h -t "$SESSION_NAME:0"      # Pane 1 (右)
tmux split-window -v -t "$SESSION_NAME:0.0"    # Pane 2 (左下)
tmux split-window -v -t "$SESSION_NAME:0.1"    # Pane 3 (右下)
tmux split-window -v -t "$SESSION_NAME:0.2"    # Pane 4 (左最下)
tmux split-window -v -t "$SESSION_NAME:0.3"    # Pane 5 (右最下)

# レイアウトを調整
tmux select-layout -t "$SESSION_NAME:0" tiled

# Pane にタイトルを設定
tmux select-pane -t "$SESSION_NAME:0.0" -T "Dispatcher"
tmux select-pane -t "$SESSION_NAME:0.1" -T "Worker1"
tmux select-pane -t "$SESSION_NAME:0.2" -T "Worker2"
tmux select-pane -t "$SESSION_NAME:0.3" -T "Worker3"
tmux select-pane -t "$SESSION_NAME:0.4" -T "ROS-Run"
tmux select-pane -t "$SESSION_NAME:0.5" -T "ROS-Monitor"

# ROS2環境をセットアップ（Pane 4, 5: ROS用ターミナル、ワークスペースで起動）
if [ -f "$ROS_SETUP" ]; then
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE && source $ROS_SETUP && echo 'ROS2 Jazzy loaded (Run) - $WORKSPACE'" Enter
    tmux send-keys -t "$SESSION_NAME:0.5" "cd $WORKSPACE && source $ROS_SETUP && echo 'ROS2 Jazzy loaded (Monitor) - $WORKSPACE'" Enter
else
    echo "警告: ROS2 Jazzy が見つかりません ($ROS_SETUP)"
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE && echo 'ROS2 not found - install ROS2 Jazzy'" Enter
    tmux send-keys -t "$SESSION_NAME:0.5" "cd $WORKSPACE && echo 'ROS2 not found - install ROS2 Jazzy'" Enter
fi

# エージェント用 Pane で Claude Code を起動
# Pane 0: Dispatcher（タスク管理のみ、スクリプトディレクトリで起動）
tmux send-keys -t "$SESSION_NAME:0.0" "cd $SCRIPT_DIR && claude --allowedTools \"$DISPATCHER_TOOLS\" --add-dir \"$WORKSPACE\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/dispatcher.md)\"" Enter

# Pane 1-3: Worker（フル権限、ワークスペースで起動）
tmux send-keys -t "$SESSION_NAME:0.1" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/1/g')\"" Enter
tmux send-keys -t "$SESSION_NAME:0.2" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/2/g')\"" Enter
tmux send-keys -t "$SESSION_NAME:0.3" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/3/g')\"" Enter

echo ""
echo "=========================================="
echo "マルチエージェントシステムが起動しました"
echo "=========================================="
echo ""
echo "セッション名: $SESSION_NAME"
echo "ワークスペース: $WORKSPACE"
echo ""
echo "Pane構成:"
echo "  Pane 0: Dispatcher (タスク分配)"
echo "  Pane 1: Worker 1 (汎用ワーカー)"
echo "  Pane 2: Worker 2 (汎用ワーカー)"
echo "  Pane 3: Worker 3 (汎用ワーカー)"
echo "  Pane 4: ROS-Run (ROS2コマンド実行用)"
echo "  Pane 5: ROS-Monitor (ROS2監視用)"
echo ""
echo "接続コマンド: tmux attach -t $SESSION_NAME"
echo "終了コマンド: ./stop.sh"
echo ""

# セッションにアタッチ
tmux attach -t "$SESSION_NAME"
