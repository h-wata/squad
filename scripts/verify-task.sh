#!/usr/bin/env bash
# verify-task.sh — task の独立検証 (verifier) を CLI 非依存で実行する。
#
# Claude worker は verifier サブエージェント (.claude/agents/verifier.md) を Task tool で
# 呼べるが、Codex / Opencode worker には同等の仕組みが無く、実装した本人が自分の成果を
# 採点する状態になっていた。このスクリプトは同じ verifier.md を prompt として使い、
# **author とは別のモデル** で headless に検証を走らせて verdict YAML を書かせる。
#
# 使い方:
#   scripts/verify-task.sh <task_yaml> <worktree> [attempt] [worker_num]
#
# env:
#   SQUAD_VERIFIER_MODEL  検証に使うモデル (既定: local/ornith-15-35b-a3b)
#                         author と別モデルであること。同じにすると自己採点に戻る。
#   SQUAD_VERIFIER_CMD    実行する CLI (既定: opencode)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERIFIER_MD="$REPO_ROOT/.claude/agents/verifier.md"

TASK_YAML="${1:-}"
WORKTREE="${2:-}"
ATTEMPT="${3:-1}"
WORKER_NUM="${4:-}"
MODEL="${SQUAD_VERIFIER_MODEL:-local/ornith-15-35b-a3b}"
CLI="${SQUAD_VERIFIER_CMD:-opencode}"

if [ -z "$TASK_YAML" ] || [ -z "$WORKTREE" ]; then
    echo "usage: verify-task.sh <task_yaml> <worktree> [attempt] [worker_num]" >&2
    exit 2
fi
for f in "$TASK_YAML" "$VERIFIER_MD"; do
    [ -f "$f" ] || { echo "not found: $f" >&2; exit 2; }
done
[ -d "$WORKTREE" ] || { echo "worktree not found: $WORKTREE" >&2; exit 2; }

# worker_num 未指定なら task YAML のファイル名 (worker3.yaml) から拾う
if [ -z "$WORKER_NUM" ]; then
    WORKER_NUM="$(basename "$TASK_YAML" | LC_ALL=C grep -oE '[0-9]+' | head -1 || true)"
fi
[ -n "$WORKER_NUM" ] || { echo "worker_num を決定できません。第4引数で渡してください。" >&2; exit 2; }

# verifier.md の YAML frontmatter (--- 〜 ---) を落として本文だけを prompt にする。
# frontmatter は Claude の agent 定義用メタデータで、prompt としては不要。
BODY="$(awk 'BEGIN{n=0} /^---[[:space:]]*$/{n++; next} n>=2{print}' "$VERIFIER_MD")"
if [ -z "${BODY//[[:space:]]/}" ]; then
    echo "verifier.md の本文を抽出できませんでした: $VERIFIER_MD" >&2
    exit 2
fi

REPORTS_DIR="$(cd "$(dirname "$TASK_YAML")/../reports" 2>/dev/null && pwd || true)"
[ -n "$REPORTS_DIR" ] || { echo "reports ディレクトリが見つかりません (task YAML の隣): $TASK_YAML" >&2; exit 2; }
VERDICT="$REPORTS_DIR/worker${WORKER_NUM}_verdict.yaml"

PROMPT="$BODY

---

## 今回の入力

- task_yaml: $TASK_YAML
- worktree: $WORKTREE
- attempt: $ATTEMPT
- worker_num: $WORKER_NUM
- verdict の出力先 (このパスに Write すること): $VERDICT

上記の手順に従って検証し、verdict を書いたら result と verdict のパスだけを返すこと。
実装コードや task YAML は絶対に修正しないこと。"

rm -f "$VERDICT"
echo "[verify-task] model=$MODEL worker=$WORKER_NUM attempt=$ATTEMPT"
echo "[verify-task] worktree=$WORKTREE"

cd "$WORKTREE"
"$CLI" run --auto -m "$MODEL" "$PROMPT" || true

if [ ! -f "$VERDICT" ]; then
    echo "[verify-task] verdict が書かれませんでした: $VERDICT" >&2
    echo "[verify-task] result=inconclusive (検証未実施として扱うこと)" >&2
    exit 1
fi

RESULT="$(LC_ALL=C grep -m1 '^result:' "$VERDICT" | sed 's/^result:[[:space:]]*//' | tr -d '"' || true)"
echo "[verify-task] verdict=$VERDICT"
echo "[verify-task] result=${RESULT:-unknown}"
[ "$RESULT" = "pass" ]
