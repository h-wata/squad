#!/usr/bin/env bash
# test_notify_worker.sh — notify-worker.sh の send_line 再送ガードを検証する。
#
# 実 tmux は叩かない: PATH の先頭に置いたフェイク tmux で完結させる。
# フェイク tmux は「send-keys されたテキストが pane に乗るまでに N 回失敗する」
# 挙動をシミュレートする (Opencode W3 で実際に起きた取りこぼしの再現)。
#
# 使い方: bash scripts/tests/test_notify_worker.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NOTIFY="$REPO_ROOT/scripts/notify-worker.sh"
VERIFY_NOTIFY="$NOTIFY"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"

# --- フェイク tmux ----------------------------------------------------------
# FAKE_TMUX_DROPS: テキストが pane に乗らない回数。0 なら初回で乗る。
#   send-keys <text>  : $WORKDIR/calls に追記。drop 残があれば pane に書かない
#   capture-pane      : $WORKDIR/pane の内容を返す
#   list-panes        : 対象 pane が存在することにする
cat >"$FAKE_BIN/tmux" <<'FAKE'
#!/usr/bin/env bash
set -eu
CALLS="$WORKDIR/calls"
PANE="$WORKDIR/pane"
DROPS="$WORKDIR/drops"
case "${1:-}" in
  list-panes)
    echo "testsess:0.2"
    ;;
  send-keys)
    # send-keys -t <target> <arg>
    text="${4:-}"
    echo "send-keys:$text" >>"$CALLS"
    if [ "$text" = "Enter" ]; then
      # FAKE_NO_SUBMIT=1 なら Enter を打っても走り出さない (取りこぼしの再現)
      [ -n "${FAKE_NO_SUBMIT:-}" ] || echo "esc interrupt" >>"$PANE"
      exit 0
    fi
    if [ "$text" = "C-u" ]; then
      : >"$PANE"
      exit 0
    fi
    remaining="$(cat "$DROPS")"
    if [ "$remaining" -gt 0 ]; then
      echo "$((remaining - 1))" >"$DROPS"   # 今回は握り潰す (pane に乗らない)
    else
      printf '%s\n' "$text" >>"$PANE"
    fi
    ;;
  capture-pane)
    cat "$PANE" 2>/dev/null || true
    ;;
  *)
    ;;
esac
FAKE
chmod +x "$FAKE_BIN/tmux"

export WORKDIR
export PATH="$FAKE_BIN:$PATH"
export SQUAD_SESSION=testsess
# 既定 5 回だと失敗ケースで 30 秒待つことになるため、テストでは 3 回に縮める
export SQUAD_SEND_RETRIES=3

MSG="新しいタスクがあります。/home/gisen/work/squad/queue/projects/trial/tasks/worker2.yaml を確認してください。"

run_case() {
  local drops="$1"
  : >"$WORKDIR/calls"
  : >"$WORKDIR/pane"
  echo "$drops" >"$WORKDIR/drops"
  set +e
  bash "$NOTIFY" W2 "$MSG" >"$WORKDIR/out" 2>&1
  RC=$?
  set -e
}

fail() {
  echo "FAIL: $1" >&2
  echo "--- calls ---" >&2
  cat "$WORKDIR/calls" >&2 || true
  echo "--- output ---" >&2
  cat "$WORKDIR/out" >&2 || true
  exit 1
}

# --- case 1: 初回で乗る → 再送しない ---------------------------------------
run_case 0
[ "$RC" -eq 0 ] || fail "case1: 正常系で exit $RC"
sends="$(grep -c "send-keys:$MSG" "$WORKDIR/calls")"
[ "$sends" -eq 1 ] || fail "case1: テキスト送信が $sends 回 (期待 1)"
grep -q "send-keys:Enter" "$WORKDIR/calls" || fail "case1: Enter が送られていない"
grep -q "send-keys:C-u" "$WORKDIR/calls" && fail "case1: 不要な C-u が送られた"
echo "ok: case1 初回で乗れば再送しない"

# --- case 2: 1 回落ちる → C-u してから再送し、最終的に Enter ----------------
run_case 1
[ "$RC" -eq 0 ] || fail "case2: リトライで復帰せず exit $RC"
sends="$(grep -c "send-keys:$MSG" "$WORKDIR/calls")"
[ "$sends" -eq 2 ] || fail "case2: テキスト送信が $sends 回 (期待 2)"
grep -q "send-keys:C-u" "$WORKDIR/calls" || fail "case2: 再送前の C-u が無い"
grep -q "send-keys:Enter" "$WORKDIR/calls" || fail "case2: Enter が送られていない"
echo "ok: case2 取りこぼしたら C-u して再送する"

# --- case 3: 一度も乗らない → Enter を打たずに失敗を返す --------------------
run_case 99
[ "$RC" -ne 0 ] || fail "case3: 乗らなかったのに exit 0"
grep -q "send-keys:Enter" "$WORKDIR/calls" && fail "case3: 乗っていないのに Enter を打った"
grep -qE "[0-9]+ 回試しても" "$WORKDIR/out" || fail "case3: 失敗メッセージが出ていない"
echo "ok: case3 乗らなければ Enter を打たず失敗させる"

# --- case 4: Enter を打っても走り出さなければ発注失敗にする ---------------
# 入力欄に乗ったことと送信されたことは別。実測でここを見ておらず 2 時間半
# 取りこぼした (Dispatcher からは作業中に見えていた)。
: >"$WORKDIR/calls"; : >"$WORKDIR/pane"; echo 0 >"$WORKDIR/drops"
set +e
FAKE_RESULT=x FAKE_NO_SUBMIT=1 bash "$VERIFY_NOTIFY" W2 "$MSG" >"$WORKDIR/out" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "case4: 走り出していないのに exit 0 (発注できたことにしてしまう)"
grep -q "反応しません" "$WORKDIR/out" || fail "case4: 未反応を報告していない"
[ "$(grep -c 'send-keys:Enter' "$WORKDIR/calls")" -eq 2 ] || fail "case4: Enter を打ち直していない"
echo "ok: case4 Enter 後に走り出さなければ失敗させる"

echo "PASS: test_notify_worker.sh"
