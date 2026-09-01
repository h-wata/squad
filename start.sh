#!/bin/bash
# Tmux マルチエージェントシステム起動スクリプト
# Claude (W1-W3) + Codex (W4) 対応

set -e

usage() {
    echo "使用方法: $0 [-s session_name] [-p project1,project2] <workspace_path>"
    echo "例: $0 -s rmf -p rmf_ws ~/rmf_ws/src"
    echo "  -s: tmux セッション名 (優先順: -s > SQUAD_SESSION env > 対話入力 > ros-agents)"
    echo "  -p: このセッションが担当する project (SQUAD_OWNED_PROJECTS env と同義)"
    exit 1
}

while getopts "s:p:h" _opt; do
    case "$_opt" in
        s) SQUAD_SESSION="$OPTARG" ;;
        p) SQUAD_OWNED_PROJECTS="$OPTARG" ;;
        *) usage ;;
    esac
done
shift $((OPTIND - 1))

# 引数チェック
if [ $# -lt 1 ]; then
    usage
fi

WORKSPACE="$(cd "$1" 2>/dev/null && pwd)" || {
    echo "エラー: ワークスペース '$1' が見つかりません"
    exit 1
}

# セッション名が -s でも env でも指定されていなければ、対話端末なら聞く
# (非対話 [CI 等] は従来通り既定値 ros-agents に落とす)
if [ -z "${SQUAD_SESSION:-}" ] && [ -t 0 ]; then
    read -rp "tmux セッション名 [ros-agents]: " SQUAD_SESSION
fi
SESSION_NAME="${SQUAD_SESSION:-ros-agents}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# SQUAD_ENABLE_CODEX=0 で Pane 6 (Worker 4 / Codex) の起動を丸ごとスキップできる
# (codex CLI を使わない環境向け)。既定は 1 (従来通り Codex を起動)。
ENABLE_CODEX="${SQUAD_ENABLE_CODEX:-1}"
# SQUAD_ENABLE_OPENCODE=0 で Pane 4 (Opencode) の起動を丸ごとスキップできる
# (汎用ターミナルとして使い続けたい環境向け)。既定は 1 (Opencode を起動)。
# Pane 4 を Opencode に置換するため、純粋な汎用-shell は Pane 5 (Aux-Shell) が担当。
ENABLE_OPENCODE="${SQUAD_ENABLE_OPENCODE:-1}"
# Opencode の既定モデル。常用は Flash-Next (LAN vLLM 上の local/qwen38-flash-next)。
# 切り替えの根拠は ADR 0004。
OPENCODE_MODEL="local/qwen38-flash-next"
OPENCODE_MODEL_Q="$(printf '%q' "$OPENCODE_MODEL")"
# SQUAD_W1_AGENT / SQUAD_W2_AGENT / SQUAD_W3_AGENT で worker ごとに claude か opencode を
# 選ぶ。既定は claude。判断 (Dispatcher=Opus) と最終レビュー (Codex W4) は Claude 系に
# 残したまま、実装 worker だけをローカル LLM に倒せるようにするための口。
# 全台を opencode にすると死角が揃うので、検証は必ず別系統のモデルで行うこと
# (scripts/verify-task.sh の既定は sonnet)。
# 取りうる値:
#   claude       … Claude Code + Claude モデル (従来)
#   opencode     … Opencode + ローカル LLM
#   claude-local … Claude Code + ローカル LLM。サブエージェント / hook / Skill が
#                  そのまま効くうえ課金 0。実測ではこれが最も成果が良かった
#                  (三つ巴比較: 301 tests、受け入れ条件の矛盾を自分から報告)。
#                  ただし Anthropic 公式は非 Claude モデルへの routing を
#                  サポートしないので、更新で壊れうる前提で使うこと。
LOCAL_BASE_URL="${SQUAD_LOCAL_BASE_URL:-http://dell-server01.cs.local:4000}"
LOCAL_MODEL="${SQUAD_LOCAL_MODEL:-qwen38-flash-next}"
LOCAL_AUTH_TOKEN="${SQUAD_LOCAL_AUTH_TOKEN:-sk-local-dummy}"
LOCAL_CONTEXT_TOKENS="${SQUAD_LOCAL_CONTEXT_TOKENS:-262144}"
WORKER_AGENTS=('' claude claude claude)   # index 1-3 を使う (0 は捨て)
ANY_OPENCODE=0
ANY_CLAUDE_LOCAL=0
for n in 1 2 3; do
    _var="SQUAD_W${n}_AGENT"
    _val="${!_var:-claude}"
    case "$_val" in
        claude) ;;
        opencode) ANY_OPENCODE=1 ;;
        claude-local) ANY_CLAUDE_LOCAL=1 ;;
        *)
            echo "$_var は claude / opencode / claude-local のいずれかを指定してください (指定値: $_val)" >&2
            exit 1
            ;;
    esac
    WORKER_AGENTS[n]="$_val"
done
# claude-local worker 用の CLAUDE_CONFIG_DIR。~/.claude/settings.json の
# permissions.ask (Bash(git commit*) / Bash(rm *) 等) は project 側 allow でも
# bypassPermissions でも突破できず、tmux 越しの worker を無言で止める。
# user 設定の出所ごと差し替えて、手元の対話設定に触れずに ask を外す。
# 詳細は config/claude-worker/README.md。
WORKER_CONFIG_DIR="$SCRIPT_DIR/config/claude-worker"
if [ "$ANY_CLAUDE_LOCAL" = "1" ]; then
    if [ ! -f "$WORKER_CONFIG_DIR/settings.json" ]; then
        echo "エラー: $WORKER_CONFIG_DIR/settings.json がありません (claude-local worker に必須)" >&2
        exit 1
    fi
    # 新しい CLAUDE_CONFIG_DIR は初回オンボーディング (テーマ選択 / セキュリティ注意 /
    # フォルダ信頼 / bypass 確認) を出す。tmux の pane には答える人がいないので、
    # 起動前に「済んだこと」にしておく。workspace ごとの信頼も先に入れる。
    WORKER_CONFIG_DIR="$WORKER_CONFIG_DIR" WORKSPACE="$WORKSPACE" SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import json, os, pathlib

cfg_dir = pathlib.Path(os.environ['WORKER_CONFIG_DIR'])
state = cfg_dir / '.claude.json'
try:
    data = json.loads(state.read_text())
except (OSError, ValueError):
    data = {}
data['hasCompletedOnboarding'] = True
projects = data.setdefault('projects', {})
for d in (os.environ['WORKSPACE'], os.environ['SCRIPT_DIR']):
    projects.setdefault(d, {})['hasTrustDialogAccepted'] = True
state.write_text(json.dumps(data, indent=2))
" || {
        echo "エラー: $WORKER_CONFIG_DIR の初期化に失敗しました" >&2
        exit 1
    }
fi
# Opencode worker は Skill を ~/.claude/skills から skills.paths 経由で読む。
# 未設定だと recommended_skills が黙って無視されるだけで失敗が見えないため、警告する。
if [ "$ANY_OPENCODE" = "1" ]; then
    OC_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
    if ! grep -q '"skills"' "$OC_CONFIG" 2>/dev/null; then
        echo "警告: $OC_CONFIG に skills.paths がありません。" >&2
        echo "      Opencode worker が Skill を読めません。以下を追記してください:" >&2
        echo '      "skills": { "paths": ["'"$HOME"'/.claude/skills"] }' >&2
    fi
fi

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
if [ "$ENABLE_CODEX" = "1" ]; then
    WORKER4_ROW="| Worker 4 | 6 | Codex | - | 待機 | - |"
else
    WORKER4_ROW="| Worker 4 | 6 | Codex | - | 無効 (SQUAD_ENABLE_CODEX=0) | - |"
fi
# worker ごとの Agent 表記 (dashboard / dispatcher.md の両方で使う)
worker_agent_label() {
    case "${WORKER_AGENTS[$1]}" in
        opencode) echo "Opencode ($OPENCODE_MODEL)" ;;
        claude-local) echo "Claude Code ($LOCAL_MODEL)" ;;
        *) echo "Claude (Sonnet)" ;;
    esac
}
WORKER_ROWS=""
for n in 1 2 3; do
    WORKER_ROWS="${WORKER_ROWS}| Worker $n | $n | $(worker_agent_label "$n") | - | 待機 | - |
"
done
if [ ! -f "$SCRIPT_DIR/dashboard.md" ]; then
    cat > "$SCRIPT_DIR/dashboard.md" <<DASHEOF
# マルチPJ ダッシュボード (Index)

このファイルは全プロジェクトの俯瞰用 index。各 PJ の詳細は \`dashboards/<project>.md\` を参照。
squad 起動時に自動生成された初期ファイルです。Dispatcher が実タスク開始時に更新します。

## Worker ステータス

| Worker | Pane | Agent | 現在のPJ | 状態 | 直近の完了タスク |
|--------|------|-------|----------|------|------------|
${WORKER_ROWS}$WORKER4_ROW
DASHEOF
fi

# --- SQUAD_OWNED_PROJECTS: 起動 session が担当する project の .squad_session マーカーを自動整備 ---
# 未指定 (既定) ならマーカーには一切触れない (後方互換)。
# 使い方: カンマ区切りで複数 project を指定できる。
#   例: SQUAD_OWNED_PROJECTS=squad,note ./start.sh ~/work
# 既に別 session を指すマーカーがある project は無警告で奪わない (スキップ + 警告表示)。
# queue/projects/<pj> が無ければ tasks/ reports/ ごと新規作成する (-p は「この session が
# 担当する」という明示指定なので、存在しないだけで担当 0 件のまま idle 起動させない)。
#
# -p / env とも未指定で、この session を指すマーカーが 1 つも無い場合は対話端末なら聞く。
# 担当 0 件のまま起動すると watcher が idle になり、Dispatcher が全 project を
# claim しにいく事故につながるため (空 Enter で従来通りスキップできる)。
if [ -z "${SQUAD_OWNED_PROJECTS:-}" ] && [ -t 0 ] \
    && ! grep -qsx "$SESSION_NAME" "$SCRIPT_DIR"/queue/projects/*/.squad_session 2>/dev/null; then
    read -rp "session '$SESSION_NAME' の担当 project (カンマ区切り、空で skip): " SQUAD_OWNED_PROJECTS
fi
if [ -n "${SQUAD_OWNED_PROJECTS:-}" ]; then
    IFS=',' read -ra _OWNED_PJS <<< "$SQUAD_OWNED_PROJECTS"
    for _pj in "${_OWNED_PJS[@]}"; do
        _pj="$(echo "$_pj" | xargs)"
        [ -z "$_pj" ] && continue
        # project 名はディレクトリ名 1 つぶん。typo で queue/projects の外や隠しディレクトリを
        # 掘らないよう、パス区切りと先頭ドット (. / .. 含む) を弾く。
        case "$_pj" in
            */* | .*)
                echo "[WARN] SQUAD_OWNED_PROJECTS: project 名 '$_pj' は不正です (/ と先頭ドットは使えません)。スキップします"
                continue
                ;;
        esac
        _pj_dir="$SCRIPT_DIR/queue/projects/$_pj"
        if [ ! -d "$_pj_dir" ]; then
            mkdir -p "$_pj_dir/tasks" "$_pj_dir/reports"
            echo "project '$_pj' の queue ディレクトリを新規作成しました ($_pj_dir)"
        fi
        _marker="$_pj_dir/.squad_session"
        if [ -f "$_marker" ]; then
            _existing="$(head -n1 "$_marker" | tr -d '[:space:]')"
            if [ -n "$_existing" ] && [ "$_existing" != "$SESSION_NAME" ]; then
                if command -v tmux >/dev/null 2>&1 && ! tmux has-session -t "$_existing" 2>/dev/null; then
                    echo "project '$_pj' の .squad_session は停止済み session '$_existing' が保持していたため '$SESSION_NAME' に引き継ぎました"
                else
                    echo "警告: queue/projects/$_pj/.squad_session は既に '$_existing' が担当中です。上書きするには手動で編集してください"
                    continue
                fi
            fi
        fi
        echo "$SESSION_NAME" > "$_marker"
        echo "project '$_pj' の .squad_session を '$SESSION_NAME' に設定しました"
    done
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
# instructions 内の {SQUAD_SESSION} プレースホルダ用 + pane 内プロセスへの env 伝搬用。
# tmux 既存 server では pane に client の環境変数が継承されないため、
# コマンド行に inline env として埋め込む (DISPATCHER_MODEL と同じ理由)。
SQUAD_SESSION_ARG_Q="$(printf '%q' "SQUAD_SESSION=$SESSION_NAME")"
SESSION_NAME_Q="$(printf '%q' "$SESSION_NAME")"

# tmux send-keys が pane に送るコマンド行そのものに埋め込まれる $SCRIPT_DIR / $WORKSPACE
# / settings ファイルパスも、render_prompt.py 引数と同様に printf '%q' で
# エスケープしてから使う（raw のままだと & | " ; や改行を含むパスで pane 側シェルの
# パースが壊れる）。%q の出力はそれ自体で shell-safe な1トークンなので、
# 埋め込み側で追加の \"..\" 二重引用符化はしない。
SCRIPT_DIR_Q="$(printf '%q' "$SCRIPT_DIR")"
WORKSPACE_Q="$(printf '%q' "$WORKSPACE")"
SETTINGS_FILE_Q="$(printf '%q' "$SETTINGS_FILE")"

# Dispatcher 起動モデルも同様に client (start.sh 実行時) 側で解決してから %q で埋め込む。
# 既存 tmux server では新規 pane に client の環境変数が継承されないため、
# pane 側 shell に ${SQUAD_DISPATCHER_MODEL:-sonnet} をリテラルのまま渡すと
# SQUAD_DISPATCHER_MODEL の上書きが効かない (PR #8 cross-review F1 対応)。
DISPATCHER_MODEL="${SQUAD_DISPATCHER_MODEL:-opus}"
DISPATCHER_MODEL_Q="$(printf '%q' "$DISPATCHER_MODEL")"

# dispatcher.md の {SQUAD_ENABLE_CODEX_NOTE} プレースホルダ用。
# SQUAD_ENABLE_CODEX=0 のときのみ、Pane 6 (W4) が存在しない旨を Dispatcher の
# system prompt に埋め込む (既定=1 の場合は空文字で何も追記しない)。
if [ "$ENABLE_CODEX" = "1" ]; then
    DISPATCHER_CODEX_NOTE=""
else
    DISPATCHER_CODEX_NOTE="この環境では Codex W4 は無効です (SQUAD_ENABLE_CODEX=0)。設計レビュー / cross-review も Claude W1-W3 に振ってください。"
fi
DISPATCHER_CODEX_NOTE_ARG_Q="$(printf '%q' "SQUAD_ENABLE_CODEX_NOTE=$DISPATCHER_CODEX_NOTE")"

# worker.md の {WORKER_AGENT} / {WORKER_AGENT_NOTE} プレースホルダ用。
# Claude worker は従来通り (note は空)。Opencode worker は Skill ツールや
# verifier サブエージェントが無いため、その差分だけを note で上書きする。
WORKER_AGENT_ARG_CLAUDE_Q="$(printf '%q' 'WORKER_AGENT=claude') $(printf '%q' 'WORKER_AGENT_NOTE=')"
OPENCODE_WORKER_NOTE="あなたは Opencode (ローカル LLM $OPENCODE_MODEL) で動く worker です。\
Skill は ~/.claude/skills を skills.paths 経由で共有しているので通常どおり使えます。\
ただし verifier サブエージェントは無いため、verify: があれば委譲せず自分で \
verify.commands を実行し、コマンドと出力を report にそのまま貼ってください \
(実行していないものを pass と書かないこと)。\
手に負えない・仕様が読み切れないと判断したら、無理に進めず report に blocked \
として理由を書いて返してください。途中まででも状況を残すほうが価値があります。\
report は日本語で書くこと (中国語の字が混ざりやすいので注意: 無を无、書を书と \
書かない)。completed_at は推測で書かず、date -Iseconds を実行した結果を使うこと。"
WORKER_AGENT_ARG_OPENCODE_Q="$(printf '%q' 'WORKER_AGENT=opencode') $(printf '%q' "WORKER_AGENT_NOTE=$OPENCODE_WORKER_NOTE")"

# Opencode の --prompt は system prompt ではなく「最初のユーザ発言」として届くため、
# worker.md をそのまま渡すと起動直後に 1 ターン走る。実測では queue/ を勝手に漁って
# 過去タスクの手順 (git push / gh pr) まで読み込みにいったため、--auto と組み合わせると
# 誤発火の危険がある。そこで指示本文は事前レンダリングしたファイルに置き、--prompt には
# 「読んで待機しろ」という短い bootstrap だけを渡す。
opencode_prompt_file() { echo "$SCRIPT_DIR/squad/state/worker$1-opencode-instructions.md"; }
opencode_bootstrap() {
    local n="$1"
    echo "あなたは squad の Worker $n です。まず $(opencode_prompt_file "$n") を Read して運用ルールを頭に入れてください。\
読み終えたら「W$n 待機中」とだけ出力して停止すること。\
起動直後のこのターンでは、それ以外を一切しないこと: queue/ や他プロジェクトを探索しない、\
既存の task YAML / report を読まない、ファイルを変更しない、git / gh コマンドを打たない。\
Dispatcher が tmux 経由で task YAML の絶対パスを通知してくるまで待機します。"
}

# dispatcher.md 用。Opencode で動いている worker がいるときだけ routing の注意を足す。
OPENCODE_WORKERS=""
for n in 1 2 3; do
    case "${WORKER_AGENTS[$n]}" in
        opencode | claude-local) OPENCODE_WORKERS="${OPENCODE_WORKERS}W$n " ;;
    esac
done
if [ -n "$OPENCODE_WORKERS" ]; then
    DISPATCHER_OPENCODE_NOTE="**${OPENCODE_WORKERS%% }はローカル LLM で動いている**。\
task YAML の \`agent\` は worker 表の Agent 欄に合わせる (Opencode なら \`opencode\`、\
Claude Code + ローカルモデルなら \`claude\`)。振ってよいのは仕様が確定していて \
機械検証できる \`verify:\` があるタスク。設計判断そのものや、仕様が固まっていない調査は \
Claude worker か W4 に振ること。成果物は自己申告の pass を信用せず、必ず verify の実走結果で \
裏取りしてから merge 判断する (scripts/verify-task.sh を使う。**検証は author と別系統の \
モデルで行うこと**。ローカル LLM の成果をローカル LLM で検証すると死角を共有する)。\
recommended_skills は通常どおり書いてよい (~/.claude/skills を skills.paths で共有済み)。"
else
    DISPATCHER_OPENCODE_NOTE=""
fi
WORKER_ROWS_ARG_Q="$(printf '%q' "SQUAD_WORKER_ROWS=$WORKER_ROWS")"
OPENCODE_NOTE_ARG_Q="$(printf '%q' "SQUAD_OPENCODE_WORKER_NOTE=$DISPATCHER_OPENCODE_NOTE")"

# 既存セッションがあれば終了
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "既存のセッション '$SESSION_NAME' を終了します..."
    tmux kill-session -t "$SESSION_NAME"
fi

echo "マルチエージェントシステムを起動します..."

# 新しいセッションを作成（Pane 0: Dispatcher）
tmux new-session -d -s "$SESSION_NAME" -x 220 -y 60

# Pane を追加して 7 pane 構成を作る (Codex 無効時は 6 pane)
# 配置は tiled レイアウトが決める。ペイン番号はレイアウト上の並び順
# (左上から右へ、次の行へ) に一致する:
#   Pane 0: Dispatcher | Pane 1: Worker 1 | Pane 2: Worker 2
#   Pane 3: Worker 3   | Pane 4: Opencode/Terminal | Pane 5: Aux-Shell
#   Pane 6: Worker 4 (Codex)
#
# 重要: 分割ごとに tiled を掛け直すこと。tmux は分割のたびにペイン番号を
# レイアウト順で振り直すため、"0.1" のようなインデックス指定は狙ったペインを
# 指さず、実際には同じ列を半分ずつ割り続ける。60 行から 6 回半分にすると
# 2 行まで縮んで 6 個目の分割が "no space for new pane" で失敗する。
# 1 分割ごとに tiled で均等化すれば、どの pane も分割可能な高さを保てる。
if [ "$ENABLE_CODEX" = "1" ]; then
    SPLIT_COUNT=6
else
    SPLIT_COUNT=5
fi
for _ in $(seq 1 "$SPLIT_COUNT"); do
    tmux split-window -v -t "$SESSION_NAME:0"
    tmux select-layout -t "$SESSION_NAME:0" tiled >/dev/null
done

# Pane タイトル
tmux select-pane -t "$SESSION_NAME:0.0" -T "Dispatcher"
for n in 1 2 3; do
    case "${WORKER_AGENTS[$n]}" in
        opencode) _t="Worker$n (Opencode)" ;;
        claude-local) _t="Worker$n (CC+local)" ;;
        *) _t="Worker$n (Claude)" ;;
    esac
    tmux select-pane -t "$SESSION_NAME:0.$n" -T "$_t"
done
if [ "$ENABLE_OPENCODE" = "1" ]; then
    tmux select-pane -t "$SESSION_NAME:0.4" -T "Opencode (Flash-Next)"
else
    tmux select-pane -t "$SESSION_NAME:0.4" -T "Terminal"
fi
tmux select-pane -t "$SESSION_NAME:0.5" -T "Aux-Shell"
if [ "$ENABLE_CODEX" = "1" ]; then
    tmux select-pane -t "$SESSION_NAME:0.6" -T "Worker4 (Codex)"
fi

# Pane 4: Opencode (既定モデル Flash-Next) か汎用 Terminal のいずれか
# Opencode は --add-dir / --append-system-prompt を持たないため、project 位置引数に
# ワークスペースを渡し、カレントディレクトリもワークスペースに合わせる。system prompt
# 注入は不要 (対話 / send-keys での指示受け取り方式)。
if [ "$ENABLE_OPENCODE" = "1" ]; then
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE_Q && opencode -m $OPENCODE_MODEL_Q $WORKSPACE_Q" Enter
else
    tmux send-keys -t "$SESSION_NAME:0.4" "cd $WORKSPACE_Q && echo Terminal ready - $WORKSPACE_Q" Enter
fi

# Aux-Shell (Pane 5) は汎用シェル
tmux send-keys -t "$SESSION_NAME:0.5" "cd $WORKSPACE_Q && echo 'Aux-Shell ready (SSH 等の汎用利用)'" Enter

# Pane 0: Dispatcher (Claude, スクリプトディレクトリで起動)
# instructions/*.md 内の {SQUAD_ROOT} プレースホルダは起動時に実パスへ展開する
# PONYTAIL_DEFAULT_MODE: ponytail プラグイン導入済み環境でロール別に制御する。
#   Dispatcher は YAML 管理のみでコードを書かないため off、Worker 1-3 は実装担当のため
#   full。プラグイン未導入なら無視されるだけで無害。レベルを変えたい場合はここを編集
#   (lite/full/ultra)。導入手順は README の「Ponytail 連携 (任意)」参照。
tmux send-keys -t "$SESSION_NAME:0.0" "cd $SCRIPT_DIR_Q && SQUAD_SESSION=$SESSION_NAME_Q PONYTAIL_DEFAULT_MODE=off claude --model $DISPATCHER_MODEL_Q --allowedTools \"$DISPATCHER_TOOLS\" --add-dir $WORKSPACE_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $DISPATCHER_MD_Q $SQUAD_ROOT_ARG_Q $SQUAD_SESSION_ARG_Q $DISPATCHER_CODEX_NOTE_ARG_Q $WORKER_ROWS_ARG_Q $OPENCODE_NOTE_ARG_Q)\"" Enter

# Pane 1-3: Worker 1-3 (Claude, ワークスペースで起動)
# SQUAD_WORKER_ID: squad の hook script が「自分が誰か」を解決するための識別子。
# 無指定でも $TMUX_PANE → config.json 逆引きで動くが、明示する方が確実。
# --settings: worker の cwd が任意の WORKSPACE のため、project hooks が読まれない。
#   SCRIPT_DIR/.claude/settings.local.json を明示ロードして squad の hook を有効化。
for n in 1 2 3; do
    if [ "${WORKER_AGENTS[$n]}" = "claude-local" ]; then
        # Claude Code のまま、モデルだけローカル LLM に向ける。--append-system-prompt も
        # サブエージェントも hook もそのまま効くので、起動の形は claude worker と同じ。
        # 違いは CLAUDE_CONFIG_DIR (ask 回避) と ANTHROPIC_* (ゲートウェイ認証) だけ。
        tmux send-keys -t "$SESSION_NAME:0.$n" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w$n SQUAD_SESSION=$SESSION_NAME_Q PONYTAIL_DEFAULT_MODE=full CLAUDE_CONFIG_DIR=$(printf '%q' "$WORKER_CONFIG_DIR") ANTHROPIC_BASE_URL=$(printf '%q' "$LOCAL_BASE_URL") ANTHROPIC_AUTH_TOKEN=$(printf '%q' "$LOCAL_AUTH_TOKEN") ANTHROPIC_MODEL=$(printf '%q' "$LOCAL_MODEL") ANTHROPIC_SMALL_FAST_MODEL=$(printf '%q' "$LOCAL_MODEL") CLAUDE_CODE_MAX_CONTEXT_TOKENS=$(printf '%q' "$LOCAL_CONTEXT_TOKENS") claude --permission-mode bypassPermissions --add-dir $SCRIPT_DIR_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $WORKER_MD_Q N=$n $SQUAD_ROOT_ARG_Q $SQUAD_SESSION_ARG_Q $WORKER_AGENT_ARG_CLAUDE_Q)\"" Enter
    elif [ "${WORKER_AGENTS[$n]}" = "opencode" ]; then
        # 指示本文は先にレンダリングしてファイルへ。--prompt には bootstrap だけを渡す
        # (理由は opencode_bootstrap 定義箇所のコメント参照)。
        _pf="$(opencode_prompt_file "$n")"
        mkdir -p "$(dirname "$_pf")"
        python3 "$SCRIPT_DIR/scripts/render_prompt.py" "$SCRIPT_DIR/instructions/worker.md" \
            "N=$n" "SQUAD_ROOT=$SCRIPT_DIR" "SQUAD_SESSION=$SESSION_NAME" \
            WORKER_AGENT=opencode "WORKER_AGENT_NOTE=$OPENCODE_WORKER_NOTE" >"$_pf"
        # --auto は tmux 越しで承認プロンプトに応答できないため必須
        # (Claude worker の permission-mode 相当)。external_directory が既定 ask のため、
        # これが無いと $SQUAD_ROOT 配下の task YAML すら読めない。
        _bs_q="$(printf '%q' "$(opencode_bootstrap "$n")")"
        tmux send-keys -t "$SESSION_NAME:0.$n" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w$n SQUAD_SESSION=$SESSION_NAME_Q opencode -m $OPENCODE_MODEL_Q --auto --prompt $_bs_q $WORKSPACE_Q" Enter
    else
        tmux send-keys -t "$SESSION_NAME:0.$n" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w$n SQUAD_SESSION=$SESSION_NAME_Q PONYTAIL_DEFAULT_MODE=full claude --allowedTools \"$WORKER_TOOLS\" --add-dir $SCRIPT_DIR_Q --settings $SETTINGS_FILE_Q --append-system-prompt \"\$(python3 $RENDER_SCRIPT_Q $WORKER_MD_Q N=$n $SQUAD_ROOT_ARG_Q $SQUAD_SESSION_ARG_Q $WORKER_AGENT_ARG_CLAUDE_Q)\"" Enter
    fi
done

# Pane 6: Worker 4 (Codex, ワークスペースで起動)
# Codex は --append-system-prompt 相当が無いため、初期 PROMPT として worker-codex.md を渡す。
# --dangerously-bypass-approvals-and-sandbox: tmux 内の信頼環境で完全自律実行 (承認なし)。
#   tmux send-keys / gh / git push 等が無確認で通り、毎ステップの承認待ち停止を解消する。
# SQUAD_WORKER_ID は Codex の hook 機構があれば squad と連携するための識別子 (将来用、Claude hook には未対応)。
# SQUAD_ENABLE_CODEX=0 の場合、codex CLI を使わない環境向けに Pane 6 自体を起動しない。
if [ "$ENABLE_CODEX" = "1" ]; then
    tmux send-keys -t "$SESSION_NAME:0.6" "cd $WORKSPACE_Q && SQUAD_WORKER_ID=w4 SQUAD_SESSION=$SESSION_NAME_Q codex --cd $WORKSPACE_Q --add-dir $SCRIPT_DIR_Q --dangerously-bypass-approvals-and-sandbox \"\$(python3 $RENDER_SCRIPT_Q $CODEX_MD_Q $SQUAD_ROOT_ARG_Q $SQUAD_SESSION_ARG_Q)\"" Enter
fi

# 監視デーモン (watcher) をバックグラウンド起動
#   - worker の report YAML を検知して Dispatcher へ自動橋渡し (send-keys 抜けの保険)
#   - 残存承認プロンプトの自動受理 / 停止 worker の Dispatcher 通報
WATCH_LOG="/tmp/${SESSION_NAME}-watch.log"
WATCH_PID_FILE="/tmp/${SESSION_NAME}-watch.pid"
SQUAD_SESSION="$SESSION_NAME" nohup "$SCRIPT_DIR/watch.sh" >"$WATCH_LOG" 2>&1 &
echo "$!" > "$WATCH_PID_FILE"
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
echo "  Pane 0: Dispatcher (Claude $DISPATCHER_MODEL, タスク分配)"
for n in 1 2 3; do
    case "${WORKER_AGENTS[$n]}" in
        opencode) echo "  Pane $n: Worker $n (Opencode, $OPENCODE_MODEL) ← SQUAD_W${n}_AGENT=opencode" ;;
        claude-local) echo "  Pane $n: Worker $n (Claude Code + $LOCAL_MODEL) ← SQUAD_W${n}_AGENT=claude-local" ;;
        *) echo "  Pane $n: Worker $n (Claude)" ;;
    esac
done
if [ "$ENABLE_OPENCODE" = "1" ]; then
    echo "  Pane 4: Opencode (Flash-Next, デフォルトモデル local/qwen38-flash-next)"
else
    echo "  Pane 4: Terminal (汎用シェル)"
fi
echo "  Pane 5: Aux-Shell (汎用 SSH 等)"
if [ "$ENABLE_CODEX" = "1" ]; then
    echo "  Pane 6: Worker 4 (Codex, 設計・cross-review 担当)"
else
    echo "  Pane 6: (無効 — SQUAD_ENABLE_CODEX=0。設計・cross-review は Claude W1-W3 に振ってください)"
fi
echo ""
echo "接続コマンド: tmux attach -t $SESSION_NAME"
echo "終了コマンド: ./stop.sh"
echo ""

# セッションにアタッチ
tmux attach -t "$SESSION_NAME"
