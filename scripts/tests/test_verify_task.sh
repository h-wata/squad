#!/usr/bin/env bash
# test_verify_task.sh — scripts/verify-task.sh を、フェイク CLI で検証する。
#
# 実 LLM は叩かない: SQUAD_VERIFIER_CMD に差し替えたフェイクが verdict を書く / 書かない。
# 見たいのは「verdict が無いのに pass を名乗らないか」「result を正しく読むか」。
#
# 使い方: bash scripts/tests/test_verify_task.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERIFY="$REPO_ROOT/scripts/verify-task.sh"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

PJ="$WORKDIR/queue/projects/pj"
mkdir -p "$PJ/tasks" "$PJ/reports" "$WORKDIR/wt"
TASK="$PJ/tasks/worker3.yaml"
VERDICT="$PJ/reports/worker3_verdict.yaml"
cat >"$TASK" <<'YAML'
task_id: T1
project: pj
verify:
  commands:
    - "true"
YAML

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"

# フェイク CLI: FAKE_RESULT が空なら verdict を書かない (検証不能ケース)。
# 受け取った prompt は $WORKDIR/prompt に落として中身を確認できるようにする。
cat >"$FAKE_BIN/fakecli" <<'FAKE'
#!/usr/bin/env bash
set -eu
for a in "$@"; do printf '%s\n' "$a"; done >"$WORKDIR/prompt"
if [ -n "${FAKE_RESULT:-}" ]; then
  cat >"$WORKDIR/verdict_target" <<EOF2
result: $FAKE_RESULT
EOF2
  cp "$WORKDIR/verdict_target" "$FAKE_VERDICT"
fi
FAKE
chmod +x "$FAKE_BIN/fakecli"

export WORKDIR FAKE_VERDICT="$VERDICT"
export PATH="$FAKE_BIN:$PATH"
export SQUAD_VERIFIER_CMD=fakecli

fail() { echo "FAIL: $1" >&2; exit 1; }

run() {
  set +e
  FAKE_RESULT="$1" bash "$VERIFY" "$TASK" "$WORKDIR/wt" 1 3 >"$WORKDIR/out" 2>&1
  RC=$?
  set -e
}

# --- case 1: verdict が pass → exit 0 -------------------------------------
rm -f "$VERDICT"; run pass
[ "$RC" -eq 0 ] || fail "case1: pass なのに exit $RC ($(cat "$WORKDIR/out"))"
grep -q "result=pass" "$WORKDIR/out" || fail "case1: result を読めていない"
echo "ok: case1 verdict pass なら 0 を返す"

# --- case 2: verdict が fail → 非 0 ---------------------------------------
rm -f "$VERDICT"; run fail
[ "$RC" -ne 0 ] || fail "case2: fail なのに exit 0"
grep -q "result=fail" "$WORKDIR/out" || fail "case2: result を読めていない"
echo "ok: case2 verdict fail なら非 0 を返す"

# --- case 3: verdict が書かれない → pass を名乗らない ----------------------
rm -f "$VERDICT"; run ""
[ "$RC" -ne 0 ] || fail "case3: verdict 無しで exit 0 (自己申告 pass を許してしまう)"
grep -q "inconclusive" "$WORKDIR/out" || fail "case3: inconclusive と言っていない"
echo "ok: case3 verdict が無ければ pass を名乗らない"

# --- case 4: prompt に verifier.md 本文と入力が入っている -------------------
rm -f "$VERDICT"; run pass
grep -q "独立検証者" "$WORKDIR/prompt" || fail "case4: verifier.md 本文が渡っていない"
grep -q -- "---" "$WORKDIR/prompt" && true
grep -q "^name: verifier$" "$WORKDIR/prompt" && fail "case4: frontmatter を落とせていない"
grep -q "task_yaml: $TASK" "$WORKDIR/prompt" || fail "case4: task_yaml が渡っていない"
grep -q "worker_num: 3" "$WORKDIR/prompt" || fail "case4: worker_num が渡っていない"
echo "ok: case4 frontmatter を除いた verifier.md 本文 + 入力が prompt に載る"

echo "PASS: test_verify_task.sh"
