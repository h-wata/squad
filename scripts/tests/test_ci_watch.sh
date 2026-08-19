#!/usr/bin/env bash
# test_ci_watch.sh — scripts/ci-watch.sh を gh をスタブして検証する。
#
# 実 gh / pi を叩かない: PATH の先頭に置いたフェイク gh (このファイルが生成する) と、
# CI_WATCH_PI_TRIAGE で差し替えたフェイク pi-log-triage.sh で完結させる。
#
# 使い方: bash scripts/tests/test_ci_watch.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CI_WATCH="$REPO_ROOT/scripts/ci-watch.sh"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"

# --- フェイク gh ------------------------------------------------------------
# シナリオは FAKE_GH_SCENARIO で切り替える (normal / failure / hang / hang_no_log / all_open)。
cat > "$FAKE_BIN/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
scenario="${FAKE_GH_SCENARIO:-normal}"
sub="$1 $2"

case "$sub" in
  "pr view")
    echo '{"headRefName":"test-branch"}'
    ;;
  "pr list")
    if [ "$scenario" = "all_open" ]; then
      echo '[{"number":101},{"number":102}]'
    else
      echo '[]'
    fi
    ;;
  "run list")
    case "$scenario" in
      normal)
        echo '[{"databaseId":1001,"status":"completed","conclusion":"success","headSha":"aaa","event":"pull_request","workflowName":"CI","url":"https://example.invalid/actions/runs/1001"}]'
        ;;
      failure|triage_fail)
        echo '[{"databaseId":2002,"status":"completed","conclusion":"failure","headSha":"bbb","event":"pull_request","workflowName":"CI","url":"https://example.invalid/actions/runs/2002"}]'
        ;;
      hang|hang_no_log)
        echo '[{"databaseId":32269209831,"status":"in_progress","conclusion":null,"headSha":"decfdc6","event":"pull_request","workflowName":"CI","url":"https://github.com/h-wata/kioku-mesh/actions/runs/32269209831"}]'
        ;;
      all_open)
        if [ "$3" = "--branch" ] && [ "${4:-}" = "test-branch" ]; then
          echo '[{"databaseId":1001,"status":"completed","conclusion":"success","headSha":"aaa","event":"pull_request","workflowName":"CI","url":"https://example.invalid/actions/runs/1001"}]'
        else
          echo '[]'
        fi
        ;;
      *)
        echo '[]'
        ;;
    esac
    ;;
  "run view")
    if [[ " $* " == *" --log-failed "* ]]; then
      echo "FAILED tests/test_example.py::test_case"
      echo "AssertionError: boom"
    elif [[ " $* " == *" --log "* ]]; then
      if [ "$scenario" = "hang_no_log" ]; then
        exit 1
      fi
      echo "2026-08-19T15:18:07Z ##[group]Install zenohd"
      echo "... still installing, no output for a while ..."
    elif [[ " $* " == *" jobs "* ]]; then
      case "$scenario" in
        failure|triage_fail)
          echo '{"jobs":[{"status":"completed","startedAt":"2026-01-01T00:00:00Z","name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"completed","conclusion":"failure","name":"Run pytest"}]}]}'
          ;;
        hang|hang_no_log)
          started="$(date -u -d "-700 seconds" +"%Y-%m-%dT%H:%M:%SZ")"
          printf '{"jobs":[{"status":"in_progress","startedAt":"%s","name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"in_progress","name":"Install zenohd"}]}]}\n' "$started"
          ;;
        *)
          echo '{"jobs":[]}'
          ;;
      esac
    else
      echo "fake gh run view: unhandled args: $*" >&2
      exit 1
    fi
    ;;
  *)
    echo "fake gh: unhandled subcommand: $*" >&2
    exit 1
    ;;
esac
FAKE_GH
chmod +x "$FAKE_BIN/gh"

# --- フェイク pi-log-triage.sh (成功) ----------------------------------------
cat > "$WORKDIR/pi-triage-ok.sh" <<'FAKE_TRIAGE_OK'
#!/usr/bin/env bash
set -euo pipefail
[ "$#" -ge 2 ] || { echo "usage: $0 <log> <yaml> [timeout]" >&2; exit 64; }
OUT="$2"
[ -e "$OUT" ] && { echo "error: refusing to overwrite existing output: $OUT" >&2; exit 73; }
cat > "$OUT" <<'YAML'
failed_step: "Run pytest"
failure_signals:
  - line: 1
    text: "FAILED tests/test_example.py::test_case"
candidate_causes:
  - hypothesis: "stub triage"
    confidence: low
    evidence_lines: [1]
next_check: "read the raw log"
unknowns: []
YAML
echo "saved: $OUT"
FAKE_TRIAGE_OK
chmod +x "$WORKDIR/pi-triage-ok.sh"

# --- フェイク pi-log-triage.sh (常に失敗) ------------------------------------
cat > "$WORKDIR/pi-triage-fail.sh" <<'FAKE_TRIAGE_FAIL'
#!/usr/bin/env bash
echo "error: simulated vLLM timeout" >&2
exit 70
FAKE_TRIAGE_FAIL
chmod +x "$WORKDIR/pi-triage-fail.sh"

export PATH="$FAKE_BIN:$PATH"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

run_case() {
  # 1回だけ実行し、出力とステータスをファイル/変数に残す (呼び出し元でアサーションに使う)。
  local inbox="$1" scenario="$2" triage="$3"; shift 3
  set +e
  FAKE_GH_SCENARIO="$scenario" CI_WATCH_INBOX="$inbox" CI_WATCH_PI_TRIAGE="$triage" \
    bash "$CI_WATCH" "$@" > "$WORKDIR/last_output.txt" 2>&1
  return_code=$?
  set -e
}

echo "=== case 1: normal run -> 何も出力せず exit 0 ==="
INBOX1="$WORKDIR/inbox1.md"
run_case "$INBOX1" normal "$WORKDIR/pi-triage-ok.sh" 42
echo "--- raw output ---"
cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 0 ]; then pass "case1: exit 0"; else fail "case1: exit code was $return_code, want 0"; fi
if [ ! -f "$INBOX1" ]; then pass "case1: inbox not created"; else fail "case1: inbox unexpectedly created"; fi

echo
echo "=== case 2: failure run -> inbox に1行追記、非ゼロ終了 ==="
INBOX2="$WORKDIR/inbox2.md"
run_case "$INBOX2" failure "$WORKDIR/pi-triage-ok.sh" 43
echo "--- raw output ---"
cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"
cat "$INBOX2" 2>&1 || true
if [ "$return_code" -ne 0 ]; then pass "case2: non-zero exit"; else fail "case2: exit was 0, want non-zero"; fi
if grep -qF "[CI] PR #43" "$INBOX2" 2>/dev/null && grep -qF "Run pytest" "$INBOX2"; then
  pass "case2: inbox line has PR + failed step"
else
  fail "case2: inbox missing expected line"
fi
if grep -qF "triage:" "$INBOX2" 2>/dev/null; then
  pass "case2: triage child line present"
else
  fail "case2: triage child line missing"
fi

echo
echo "=== case 3: ハング run (Install zenohd, 700s > 600s既定) を検知、ログ取得も失敗するが継続 ==="
INBOX3="$WORKDIR/inbox3.md"
run_case "$INBOX3" hang_no_log "$WORKDIR/pi-triage-ok.sh" 44
echo "--- raw output ---"
cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"
cat "$INBOX3" 2>&1 || true
if [ "$return_code" -ne 0 ]; then pass "case3: non-zero exit"; else fail "case3: exit was 0, want non-zero"; fi
if grep -qF "Install zenohd" "$INBOX3" 2>/dev/null; then
  pass "case3: hang detected on Install zenohd"
else
  fail "case3: inbox missing stalled step name"
fi
if grep -qF "triage:" "$INBOX3" 2>/dev/null; then
  fail "case3: triage line present despite log fetch failure (must be skipped)"
else
  pass "case3: no triage attempted when log fetch failed (ci-watch did not crash)"
fi

echo
echo "=== case 4: 同じ run を2回処理しても inbox は重複しない ==="
INBOX4="$WORKDIR/inbox4.md"
run_case "$INBOX4" hang "$WORKDIR/pi-triage-ok.sh" 45
first_run_rc="$return_code"
first_lines="$(wc -l < "$INBOX4")"
run_case "$INBOX4" hang "$WORKDIR/pi-triage-ok.sh" 45
second_run_rc="$return_code"
second_lines="$(wc -l < "$INBOX4")"
echo "--- inbox after 2 runs ---"
cat "$INBOX4"
echo "--- line counts: 1st=$first_lines 2nd=$second_lines ---"
if [ "$first_run_rc" -ne 0 ] && [ "$second_run_rc" -ne 0 ]; then
  pass "case4: both runs report the anomaly (non-zero exit)"
else
  fail "case4: expected non-zero exit both times (got $first_run_rc, $second_run_rc)"
fi
if [ "$first_lines" -eq "$second_lines" ]; then
  pass "case4: no duplicate line appended on second run"
else
  fail "case4: inbox grew from $first_lines to $second_lines lines on duplicate run"
fi
if [ "$(grep -c "\[CI\] PR #45" "$INBOX4")" -eq 1 ]; then
  pass "case4: exactly one [CI] PR #45 line"
else
  fail "case4: expected exactly one [CI] PR #45 line, got $(grep -c "\[CI\] PR #45" "$INBOX4")"
fi

echo
echo "=== case 5: pi-log-triage.sh が失敗しても ci-watch.sh は検知結果を出して続行する ==="
INBOX5="$WORKDIR/inbox5.md"
run_case "$INBOX5" triage_fail "$WORKDIR/pi-triage-fail.sh" 46
echo "--- raw output ---"
cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"
cat "$INBOX5" 2>&1 || true
if [ "$return_code" -ne 0 ]; then pass "case5: non-zero exit despite triage failure"; else fail "case5: exit was 0"; fi
if grep -qF "[CI] PR #46" "$INBOX5" 2>/dev/null; then
  pass "case5: raw detection still written to inbox"
else
  fail "case5: inbox missing raw detection line"
fi
if grep -qF "triage:" "$INBOX5" 2>/dev/null; then
  fail "case5: triage line present despite triage script failure"
else
  pass "case5: no triage line recorded (triage script failed as expected)"
fi

echo
echo "=== 追加確認: pi の next_check / candidate_causes を自動実行するコードが無いこと ==="
# next_check/candidate_causes という語自体は「実行しない」という設計方針コメントに登場して
# よい。禁止したいのは、triage の YAML を読み込んで exec/eval/sh -c 等で実行する経路が
# 存在すること。ci-watch.sh は triage の出力 (YAML ファイルパス) を inbox に書くだけで
# 中身を読み込みすらしないので、eval 系コマンドが一切無いことを確認する。
if grep -nE '\b(eval|exec|source)\b|sh[[:space:]]+-c|bash[[:space:]]+-c' "$CI_WATCH" \
    | grep -v '^\s*#'; then
  fail "extra: ci-watch.sh に eval/exec/source/-c 実行が見つかった (自動実行の疑い)"
else
  pass "extra: ci-watch.sh に triage 出力を実行する経路が無い (eval/exec/-c 系コマンド無し)"
fi
if grep -qE '\.\s*"\$triage' "$CI_WATCH"; then
  fail "extra: ci-watch.sh が triage YAML を source している"
else
  pass "extra: ci-watch.sh は triage YAML を source していない"
fi

echo
echo "================================================================"
echo "結果: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
