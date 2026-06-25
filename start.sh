#!/bin/bash
# Tmux マルチエージェントシステム起動スクリプト
# ROS2 Jazzy + Claude (W1-W3) + Codex (W4) 対応

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
# kioku-mesh MCP (共有プロジェクト知識) を摩擦なく使えるよう allowlist に含める
KIOKU_TOOLS="mcp__kioku_mesh__search_memory mcp__kioku_mesh__get_memory mcp__kioku_mesh__save_observation"
DISPATCHER_TOOLS="Read Write Edit Bash(tmux:*) mcp__kioku_mesh__search_memory mcp__kioku_mesh__get_memory"
WORKER_TOOLS="Read Write Edit Grep Glob Bash Task $KIOKU_TOOLS"

# 既存セッションがあれば終了
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "既存のセッション '$SESSION_NAME' を終了します..."
    tmux kill-session -t "$SESSION_NAME"
fi

echo "マルチエージェントシステムを起動します..."

# 新しいセッションを作成（Pane 0: Dispatcher）
tmux new-session -d -s "$SESSION_NAME" -x 220 -y 60

# Pane を追加して 4 列構成を作る (合計 7 pane)
# 配置 (tiled 後にレイアウト自動調整):
#   Pane 0: Dispatcher | Pane 1: Worker 1 | Pane 4: ROS-Run
#   Pane 2: Worker 2   | Pane 3: Worker 3 | Pane 5: Aux-Shell
#                                          | Pane 6: Worker 4 (Codex)
tmux split-window -h -t "$SESSION_NAME:0"      # Pane 1
tmux split-window -v -t "$SESSION_NAME:0.0"    # Pane 2
tmux split-window -v -t "$SESSION_NAME:0.1"    # Pane 3
tmux split-window -v -t "$SESSION_NAME:0.2"    # Pane 4
tmux split-window -v -t "$SESSION_NAME:0.3"    # Pane 5
tmux split-window -v -t "$SESSION_NAME:0.4"    # Pane 6

# レイアウトを調整
tmux select-layout -t "$SESSION_NAME:0" tiled

# Pane タイトル
tmux select-pane -t "$SESSION_NAME:0.0" -T "Dispatcher"
tmux select-pane -t "$SESSION_NAME:0.1" -T "Worker1 (Claude)"
tmux select-pane -t "$SESSION_NAME:0.2" -T "Worker2 (Claude)"
tmux select-pane -t "$SESSION_NAME:0.3" -T "Worker3 (Claude)"
tmux select-pane -t "$SESSION_NAME:0.4" -T "ROS-Run"
tmux select-pane -t "$SESSION_NAME:0.5" -T "Aux-Shell"
tmux select-pane -t "$SESSION_NAME:0.6" -T "Worker4 (Codex)"

# ROS-Run (Pane 4) は ROS2 環境を read
if [ -f "$ROS_SETUP" ]; then
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE && source $ROS_SETUP && echo 'ROS2 Jazzy loaded (Run) - $WORKSPACE'" Enter
else
    echo "警告: ROS2 Jazzy が見つかりません ($ROS_SETUP)"
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE && echo 'ROS2 not found - install ROS2 Jazzy'" Enter
fi

# Aux-Shell (Pane 5) は汎用シェル
tmux send-keys -t "$SESSION_NAME:0.5" "cd $WORKSPACE && echo 'Aux-Shell ready (SSH / mesh-mem CLI 等の汎用利用)'" Enter

# Pane 0: Dispatcher (Claude, スクリプトディレクトリで起動)
tmux send-keys -t "$SESSION_NAME:0.0" "cd $SCRIPT_DIR && claude --allowedTools \"$DISPATCHER_TOOLS\" --add-dir \"$WORKSPACE\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/dispatcher.md)\"" Enter

# Pane 1-3: Worker 1-3 (Claude, ワークスペースで起動)
tmux send-keys -t "$SESSION_NAME:0.1" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/1/g')\"" Enter
tmux send-keys -t "$SESSION_NAME:0.2" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/2/g')\"" Enter
tmux send-keys -t "$SESSION_NAME:0.3" "cd $WORKSPACE && claude --allowedTools \"$WORKER_TOOLS\" --add-dir \"$SCRIPT_DIR\" --append-system-prompt \"\$(cat $SCRIPT_DIR/instructions/worker.md | sed 's/{N}/3/g')\"" Enter

# Pane 6: Worker 4 (Codex, ワークスペースで起動)
# Codex は --append-system-prompt 相当が無いため、初期 PROMPT として worker-codex.md を渡す。
# --dangerously-bypass-approvals-and-sandbox: tmux 内の信頼環境で完全自律実行 (承認なし)。
#   tmux send-keys / gh / git push 等が無確認で通り、毎ステップの承認待ち停止を解消する。
tmux send-keys -t "$SESSION_NAME:0.6" "cd $WORKSPACE && codex --cd $WORKSPACE --add-dir $SCRIPT_DIR --dangerously-bypass-approvals-and-sandbox \"\$(cat $SCRIPT_DIR/instructions/worker-codex.md)\"" Enter

# 監視デーモン (watcher) をバックグラウンド起動
#   - worker の report YAML を検知して Dispatcher へ自動橋渡し (send-keys 抜けの保険)
#   - 残存承認プロンプトの自動受理 / 停止 worker の Dispatcher 通報
WATCH_LOG="/tmp/${SESSION_NAME}-watch.log"
nohup "$SCRIPT_DIR/watch.sh" >"$WATCH_LOG" 2>&1 &
echo "watcher 起動 (PID $!, log: $WATCH_LOG)"

echo ""
echo "=========================================="
echo "マルチエージェントシステムが起動しました"
echo "=========================================="
echo ""
echo "セッション名: $SESSION_NAME"
echo "ワークスペース: $WORKSPACE"
echo ""
echo "Pane構成:"
echo "  Pane 0: Dispatcher (Claude, タスク分配)"
echo "  Pane 1: Worker 1 (Claude)"
echo "  Pane 2: Worker 2 (Claude)"
echo "  Pane 3: Worker 3 (Claude)"
echo "  Pane 4: ROS-Run (ROS2コマンド実行用)"
echo "  Pane 5: Aux-Shell (汎用 SSH / mesh-mem CLI 等)"
echo "  Pane 6: Worker 4 (Codex, 設計・実装担当)"
echo ""
echo "接続コマンド: tmux attach -t $SESSION_NAME"
echo "終了コマンド: ./stop.sh"
echo ""

# セッションにアタッチ
tmux attach -t "$SESSION_NAME"
