#!/bin/bash
# Tmux マルチエージェントシステム起動スクリプト
# Claude (W1-W3) + Codex (W4) 対応

set -e

# 引数チェック
if [ $# -lt 1 ]; then
    echo "使用方法: $0 <workspace_path>"
    echo "例: $0 ~/my_ws"
    exit 1
fi

WORKSPACE="$(cd "$1" 2>/dev/null && pwd)" || {
    echo "エラー: ワークスペース '$1' が見つかりません"
    exit 1
}

SESSION_NAME="${SQUAD_SESSION:-ros-agents}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- fresh clone 対応: settings.local.json の自動生成 ---
# .claude/settings.local.json は個人パス・MCP allow リストを含むため gitignore 対象。
# fresh clone には存在しないため、無ければ .example から生成する。
# (claude CLI は --settings に存在しないパスを渡すと即エラー終了するため必須)
SETTINGS_FILE="$SCRIPT_DIR/.claude/settings.local.json"
SETTINGS_EXAMPLE="$SCRIPT_DIR/.claude/settings.local.json.example"
if [ ! -f "$SETTINGS_FILE" ]; then
    if [ -f "$SETTINGS_EXAMPLE" ]; then
        echo "初回起動: $SETTINGS_EXAMPLE から $SETTINGS_FILE を生成します（{SQUAD_ROOT} を実パスに置換）..."
        mkdir -p "$(dirname "$SETTINGS_FILE")"
        # sed は clone 先パス ($SCRIPT_DIR) に & | " 等の特殊文字が含まれると壊れるため
        # python3/json で置換する。テンプレート自体は {SQUAD_ROOT} を含む plain な
        # 文字列のまま json.load でパースし、パース後の Python オブジェクトツリー上で
        # 文字列置換してから json.dump で書き戻す（生テキストに対する置換だと、置換後の
        # パスに " が含まれる場合 JSON の引用符と衝突して invalid JSON になるため、
        # 必ずパース後に置換すること — json.dump が改めて正しくエスケープする）
        SETTINGS_EXAMPLE_PATH="$SETTINGS_EXAMPLE" SETTINGS_FILE_PATH="$SETTINGS_FILE" SQUAD_ROOT_PATH="$SCRIPT_DIR" python3 -c "
import json
import os

src = os.environ['SETTINGS_EXAMPLE_PATH']
dst = os.environ['SETTINGS_FILE_PATH']
root = os.environ['SQUAD_ROOT_PATH']

def substitute(obj):
    if isinstance(obj, str):
        return obj.replace('{SQUAD_ROOT}', root)
    if isinstance(obj, list):
        return [substitute(x) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v) for k, v in obj.items()}
    return obj

with open(src, 'r', encoding='utf-8') as f:
    data = json.load(f)
data = substitute(data)
with open(dst, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4, ensure_ascii=False)
    f.write('\n')
" || {
            echo "エラー: settings.local.json.example の生成に失敗しました（python3/json を確認してください）"
            exit 1
        }
    else
        echo "エラー: $SETTINGS_FILE も $SETTINGS_EXAMPLE も見つかりません。"
        echo "  .claude/settings.local.json.example を確認してください。"
        exit 1
    fi
fi

# --- fresh clone 対応: queue/ dashboards/ の scaffold ---
# queue/ dashboards/ dashboard.md は .gitignore 対象のため fresh clone には存在しない。
# queue/templates 配下の中身（task.yaml, report.yaml）はここでは生成しない（別管理）。
mkdir -p "$SCRIPT_DIR/queue/projects" "$SCRIPT_DIR/queue/templates"
mkdir -p "$SCRIPT_DIR/dashboards"
if [ ! -f "$SCRIPT_DIR/dashboard.md" ]; then
    cat > "$SCRIPT_DIR/dashboard.md" <<'DASHEOF'
# マルチPJ ダッシュボード (Index)

このファイルは全プロジェクトの俯瞰用 index。各 PJ の詳細は `dashboards/<project>.md` を参照。
squad 起動時に自動生成された初期ファイルです。Dispatcher が実タスク開始時に更新します。

## Worker ステータス

| Worker | Pane | Agent | 現在のPJ | 状態 | 直近の完了タスク |
|--------|------|-------|----------|------|------------|
| Worker 1 | 1 | Claude (Sonnet) | - | 待機 | - |
| Worker 2 | 2 | Claude (Sonnet) | - | 待機 | - |
| Worker 3 | 3 | Claude (Sonnet) | - | 待機 | - |
| Worker 4 | 6 | Codex | - | 待機 | - |
DASHEOF
fi

# SQUAD_DRY_RUN=1: settings/scaffold の pre-flight のみ実行して tmux には触れずに終了
# (fresh clone 検証・CI での再利用向け。既存の tmux 起動セッションを巻き込まずに検証できる)
if [ "${SQUAD_DRY_RUN:-0}" = "1" ]; then
    echo "SQUAD_DRY_RUN=1: pre-flight (settings/scaffold) のみ実行して終了します"
    exit 0
fi

# 各エージェントのパーミッション設定
# kioku-mesh MCP (共有プロジェクト知識) を摩擦なく使えるよう allowlist に含める
KIOKU_TOOLS="mcp__kioku_mesh__search_memory mcp__kioku_mesh__get_memory mcp__kioku_mesh__save_observation"
DISPATCHER_TOOLS="Read Write Edit Bash(tmux:*) mcp__kioku_mesh__search_memory mcp__kioku_mesh__get_memory"
WORKER_TOOLS="Read Write Edit Grep Glob Bash Task $KIOKU_TOOLS"

# --- instructions/*.md のプレースホルダ展開 (--append-system-prompt 用) ---
# sed の s/// や bash の ${var//pattern/replacement} は、置換値 ($SCRIPT_DIR) に
# & | " 等が含まれると壊れる（& は「マッチ全体」として再解釈されるため、sed だけで
# なく bash のパラメータ置換でも同様に壊れる）。scripts/render_prompt.py の
# str.replace() はリテラル置換のみを行うため安全。
# コマンド文字列は「今」($SCRIPT_DIR 展開込みで) 組み立て、実際の展開処理自体は
# 各 pane 起動時に走る (元の sed 方式と同じタイミング設計)。$SCRIPT_DIR 等の値は
# printf '%q' で pane 側シェルにとって安全な形にエスケープしてから埋め込む。
RENDER_SCRIPT_Q="$(printf '%q' "$SCRIPT_DIR/scripts/render_prompt.py")"
DISPATCHER_MD_Q="$(printf '%q' "$SCRIPT_DIR/instructions/dispatcher.md")"
WORKER_MD_Q="$(printf '%q' "$SCRIPT_DIR/instructions/worker.md")"
CODEX_MD_Q="$(printf '%q' "$SCRIPT_DIR/instructions/worker-codex.md")"
SQUAD_ROOT_ARG_Q="$(printf '%q' "SQUAD_ROOT=$SCRIPT_DIR")"

# tmux send-keys が pane に送るコマンド行そのものに埋め込まれる $SCRIPT_DIR / $WORKSPACE
# / settings ファイルパスも、render_prompt.py 引数と同様に printf '%q' で
# エスケープしてから使う（raw のままだと & | " ; や改行を含むパスで pane 側シェルの
# パースが壊れる）。%q の出力はそれ自体で shell-safe な1トークンなので、
# 埋め込み側で追加の \"..\" 二重引用符化はしない。
SCRIPT_DIR_Q="$(printf '%q' "$SCRIPT_DIR")"
WORKSPACE_Q="$(printf '%q' "$WORKSPACE")"
SETTINGS_FILE_Q="$(printf '%q' "$SETTINGS_FILE")"

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
#   Pane 0: Dispatcher | Pane 1: Worker 1 | Pane 4: Terminal
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
tmux select-pane -t "$SESSION_NAME:0.4" -T "Terminal"
tmux select-pane -t "$SESSION_NAME:0.5" -T "Aux-Shell"
tmux select-pane -t "$SESSION_NAME:0.6" -T "Worker4 (Codex)"

# Terminal (Pane 4) は汎用シェル
tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE_Q && echo Terminal ready - $WORKSPACE_Q" Enter

# Aux-Shell (Pane 5) は汎用シェル
tmux send-keys -t "$SESSION_NAME:0.5" "cd $WORKSPACE_Q && echo 'Aux-Shell ready (SSH 等の汎用利用)'" Enter

# Pane 0: Dispatcher (Claude, スクリプトディレクトリで起動)
# instructions/*.md 内の {SQUAD_ROOT} プレースホルダは起動時に実パスへ展開する
tmux send-keys -t "$SESSION_NAME:0.0" "cd $SCRIPT_DIR_Q && claude --allowedTools \"$DISPATCHER_TOOLS\" --add-dir $WORKSPACE_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $DISPATCHER_MD_Q $SQUAD_ROOT_ARG_Q)\"" Enter

# Pane 1-3: Worker 1-3 (Claude, ワークスペースで起動)
# SQUAD_WORKER_ID: squad の hook script が「自分が誰か」を解決するための識別子。
# 無指定でも $TMUX_PANE → config.json 逆引きで動くが、明示する方が確実。
# --settings: worker の cwd が任意の WORKSPACE のため、project hooks が読まれない。
#   SCRIPT_DIR/.claude/settings.local.json を明示ロードして squad の hook を有効化。
tmux send-keys -t "$SESSION_NAME:0.1" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w1 claude --allowedTools \"$WORKER_TOOLS\" --add-dir $SCRIPT_DIR_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $WORKER_MD_Q N=1 $SQUAD_ROOT_ARG_Q)\"" Enter
tmux send-keys -t "$SESSION_NAME:0.2" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w2 claude --allowedTools \"$WORKER_TOOLS\" --add-dir $SCRIPT_DIR_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $WORKER_MD_Q N=2 $SQUAD_ROOT_ARG_Q)\"" Enter
tmux send-keys -t "$SESSION_NAME:0.3" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w3 claude --allowedTools \"$WORKER_TOOLS\" --add-dir $SCRIPT_DIR_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $WORKER_MD_Q N=3 $SQUAD_ROOT_ARG_Q)\"" Enter

# Pane 6: Worker 4 (Codex, ワークスペースで起動)
# Codex は --append-system-prompt 相当が無いため、初期 PROMPT として worker-codex.md を渡す。
# --dangerously-bypass-approvals-and-sandbox: tmux 内の信頼環境で完全自律実行 (承認なし)。
#   tmux send-keys / gh / git push 等が無確認で通り、毎ステップの承認待ち停止を解消する。
# SQUAD_WORKER_ID は Codex の hook 機構があれば squad と連携するための識別子 (将来用、Claude hook には未対応)。
tmux send-keys -t "$SESSION_NAME:0.6" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w4 codex --cd $WORKSPACE_Q --add-dir $SCRIPT_DIR_Q --dangerously-bypass-approvals-and-sandbox \"\$(python3 $RENDER_SCRIPT_Q $CODEX_MD_Q $SQUAD_ROOT_ARG_Q)\"" Enter

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
echo "  Pane 4: Terminal (汎用シェル)"
echo "  Pane 5: Aux-Shell (汎用 SSH 等)"
echo "  Pane 6: Worker 4 (Codex, 設計・cross-review 担当)"
echo ""
echo "接続コマンド: tmux attach -t $SESSION_NAME"
echo "終了コマンド: ./stop.sh"
echo ""

# セッションにアタッチ
tmux attach -t "$SESSION_NAME"
