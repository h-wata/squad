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
# シナリオは FAKE_GH_SCENARIO で切り替える。
#   normal              : run 1件、成功
#   failure / triage_fail: run 1件、失敗 (conclusion=failure)
#   hang                : run が in_progress、job が経過1000秒 (既定900秒超) でハング、
#                         --log は成功する
#   hang_no_log         : hang と同じだが --log が失敗する (ログ取得不能ケース)
#   hang_null_started   : run は in_progress だが job の startedAt が null (NB2)
#   no_runs             : gh run list が空配列を返す (CI run がまだ無いケース)
#   all_open            : gh pr list が複数 PR を返す。各 PR の run list は normal 相当
#   gh_fail_pr_view     : gh pr view 自体が失敗する (B2, 単一PRパス)
#   gh_fail_pr_list     : gh pr list 自体が失敗する (B2, --all-open パス)
#   run_list_empty      : gh run list が exit 0 のまま空 stdout を返す (2巡目レビュー B2:
#                         単一PRパスでも --all-open パスでも同じ get_latest_run を通るため
#                         このシナリオ1つで両パスをカバーする)
#   jobs_fetch_fail     : run は in_progress を返すが、gh run view --json jobs だけが
#                         非ゼロで失敗する (2巡目レビュー B2)
#   run_missing_fields  : gh run list が [{}] (databaseId/status/url 欠落) を返す
#                         (3巡目レビュー B2 残件)
#   date_fail           : run は in_progress・startedAt も正常値を返すが、FAKE_BIN の
#                         date コマンド自体が -d 呼び出しで失敗する (3巡目レビュー B2 残件)
cat > "$FAKE_BIN/gh" <<'FAKE_GH'
#!/usr/bin/env bash
set -euo pipefail
scenario="${FAKE_GH_SCENARIO:-normal}"
sub="$1 $2"

case "$sub" in
  "pr view")
    if [ "$scenario" = "gh_fail_pr_view" ]; then
      echo "simulated: gh: authentication failed (HTTP 401)" >&2
      exit 1
    fi
    echo '{"headRefName":"test-branch"}'
    ;;
  "pr list")
    if [ "$scenario" = "gh_fail_pr_list" ]; then
      echo "simulated: gh: API rate limit exceeded" >&2
      exit 1
    fi
    if [ "$scenario" = "all_open" ] || [ "$scenario" = "run_list_empty" ]; then
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
      hang|hang_no_log|hang_null_started|jobs_fetch_fail|date_fail)
        echo '[{"databaseId":32269209831,"status":"in_progress","conclusion":null,"headSha":"decfdc6","event":"pull_request","workflowName":"CI","url":"https://github.com/h-wata/kioku-mesh/actions/runs/32269209831"}]'
        ;;
      no_runs)
        echo '[]'
        ;;
      all_open)
        echo '[{"databaseId":1001,"status":"completed","conclusion":"success","headSha":"aaa","event":"pull_request","workflowName":"CI","url":"https://example.invalid/actions/runs/1001"}]'
        ;;
      run_list_empty)
        echo -n ""
        ;;
      run_missing_fields)
        echo '[{}]'
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
      if [ "$scenario" = "jobs_fetch_fail" ]; then
        echo "simulated: gh: connection reset by peer" >&2
        exit 1
      fi
      case "$scenario" in
        failure|triage_fail)
          echo '{"jobs":[{"status":"completed","startedAt":"2026-01-01T00:00:00Z","name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"completed","conclusion":"failure","name":"Run pytest"}]}]}'
          ;;
        hang|hang_no_log)
          started="$(date -u -d "-1000 seconds" +"%Y-%m-%dT%H:%M:%SZ")"
          printf '{"jobs":[{"status":"in_progress","startedAt":"%s","name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"in_progress","name":"Install zenohd"}]}]}\n' "$started"
          ;;
        hang_null_started)
          echo '{"jobs":[{"status":"in_progress","startedAt":null,"name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"in_progress","name":"Install zenohd"}]}]}'
          ;;
        date_fail)
          # startedAt は正規の値。壊すのは PATH 上のフェイク date バイナリ側
          # (このケース専用に FAKE_DATE_BIN を PATH の先頭に置いて呼ぶ)。
          echo '{"jobs":[{"status":"in_progress","startedAt":"2026-08-19T15:00:00Z","name":"lint-and-test","steps":[{"status":"completed","conclusion":"success","name":"Set up job"},{"status":"in_progress","name":"Install zenohd"}]}]}'
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

# --- フェイク date (-d だけ失敗させる。date_fail シナリオ専用、PATH には常設しない) -----
FAKE_DATE_BIN="$WORKDIR/fake_date_bin"
mkdir -p "$FAKE_DATE_BIN"
cat > "$FAKE_DATE_BIN/date" <<'FAKE_DATE'
#!/usr/bin/env bash
for a in "$@"; do
  if [ "$a" = "-d" ]; then
    echo "simulated: date -d failure (fake_date_bin)" >&2
    exit 1
  fi
done
exec /usr/bin/date "$@"
FAKE_DATE
chmod +x "$FAKE_DATE_BIN/date"

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

# --- フェイク pi-log-triage.sh (成功、ただし少し遅い: 並行実行の競合窓を広げる用) -----
cat > "$WORKDIR/pi-triage-slow.sh" <<'FAKE_TRIAGE_SLOW'
#!/usr/bin/env bash
set -euo pipefail
[ "$#" -ge 2 ] || { echo "usage: $0 <log> <yaml> [timeout]" >&2; exit 64; }
OUT="$2"
[ -e "$OUT" ] && { echo "error: refusing to overwrite existing output: $OUT" >&2; exit 73; }
sleep 0.4
cat > "$OUT" <<'YAML'
failed_step: "n/a"
failure_signals: []
candidate_causes: []
next_check: "n/a"
unknowns: []
YAML
echo "saved: $OUT"
FAKE_TRIAGE_SLOW
chmod +x "$WORKDIR/pi-triage-slow.sh"

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

# 1回だけ実行し、出力とステータスをファイル/変数に残す。
run_case() {
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
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 0 ]; then pass "case1: exit 0"; else fail "case1: exit code was $return_code, want 0"; fi
if [ ! -f "$INBOX1" ]; then pass "case1: inbox not created"; else fail "case1: inbox unexpectedly created"; fi

echo
echo "=== case 2: failure run -> inbox に1行追記、非ゼロ終了 (exit 2) ==="
INBOX2="$WORKDIR/inbox2.md"
run_case "$INBOX2" failure "$WORKDIR/pi-triage-ok.sh" 43
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"; cat "$INBOX2" 2>&1 || true
if [ "$return_code" -eq 2 ]; then pass "case2: exit 2 (anomaly)"; else fail "case2: exit was $return_code, want 2"; fi
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
echo "=== case 3: ハング run (Install zenohd, 1000s > 既定900s) を検知、ログ取得も失敗するが継続 ==="
INBOX3="$WORKDIR/inbox3.md"
run_case "$INBOX3" hang_no_log "$WORKDIR/pi-triage-ok.sh" 44
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"; cat "$INBOX3" 2>&1 || true
if [ "$return_code" -eq 2 ]; then pass "case3: exit 2 (anomaly)"; else fail "case3: exit was $return_code, want 2"; fi
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
echo "--- inbox after 2 runs ---"; cat "$INBOX4"
echo "--- line counts: 1st=$first_lines 2nd=$second_lines ---"
if [ "$first_run_rc" -eq 2 ] && [ "$second_run_rc" -eq 2 ]; then
  pass "case4: both runs report the anomaly (exit 2)"
else
  fail "case4: expected exit 2 both times (got $first_run_rc, $second_run_rc)"
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
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
echo "--- inbox content ---"; cat "$INBOX5" 2>&1 || true
if [ "$return_code" -eq 2 ]; then pass "case5: exit 2 despite triage failure"; else fail "case5: exit was $return_code, want 2"; fi
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
echo "=== case 6 (B1): 2プロセス同時起動でも inbox の該当run行は1つだけ ==="
INBOX6="$WORKDIR/inbox6.md"
FAKE_GH_SCENARIO=hang CI_WATCH_INBOX="$INBOX6" CI_WATCH_PI_TRIAGE="$WORKDIR/pi-triage-slow.sh" \
  bash "$CI_WATCH" 47 > "$WORKDIR/race_a.log" 2>&1 &
pid_a=$!
FAKE_GH_SCENARIO=hang CI_WATCH_INBOX="$INBOX6" CI_WATCH_PI_TRIAGE="$WORKDIR/pi-triage-slow.sh" \
  bash "$CI_WATCH" 47 > "$WORKDIR/race_b.log" 2>&1 &
pid_b=$!
set +e
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?
set -e
echo "--- process a output ---"; cat "$WORKDIR/race_a.log"
echo "--- process b output ---"; cat "$WORKDIR/race_b.log"
echo "--- inbox after concurrent race ---"; cat "$INBOX6" 2>&1 || true
if [ "$rc_a" -eq 2 ] && [ "$rc_b" -eq 2 ]; then
  pass "case6: both concurrent processes report the anomaly"
else
  fail "case6: expected both to exit 2 (got a=$rc_a b=$rc_b)"
fi
race_lines="$(grep -c "\[CI\] PR #47" "$INBOX6" 2>/dev/null || echo 0)"
if [ "$race_lines" -eq 1 ]; then
  pass "case6: exactly 1 line after concurrent race (flock serialized append_inbox)"
else
  fail "case6: got $race_lines lines for PR #47, want 1 (inbox race not prevented)"
fi

echo
echo "=== case 7 (B2): gh pr view 自体が失敗 (単一PR) -> stderr メッセージ + exit 3 ==="
INBOX7="$WORKDIR/inbox7.md"
run_case "$INBOX7" gh_fail_pr_view "$WORKDIR/pi-triage-ok.sh" 48
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then pass "case7: exit 3 (operational failure)"; else fail "case7: exit was $return_code, want 3"; fi
if grep -qF "authentication failed" "$WORKDIR/last_output.txt"; then
  pass "case7: stderr carries the gh failure message"
else
  fail "case7: stderr message missing"
fi
if [ -f "$INBOX7" ]; then
  fail "case7: inbox created despite operational failure (should not treat as a normal/anomaly detection)"
else
  pass "case7: no inbox entry written for an operational failure"
fi

echo
echo "=== case 8 (B2): gh pr list 自体が失敗 (--all-open) -> stderr メッセージ + exit 3 ==="
INBOX8="$WORKDIR/inbox8.md"
run_case "$INBOX8" gh_fail_pr_list "$WORKDIR/pi-triage-ok.sh" --all-open
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then pass "case8: exit 3 (operational failure)"; else fail "case8: exit was $return_code, want 3"; fi
if grep -qF "rate limit exceeded" "$WORKDIR/last_output.txt"; then
  pass "case8: stderr carries the gh pr list failure message"
else
  fail "case8: stderr message missing"
fi

echo
echo "=== case 9 (NB4): --all-open 経路、複数 PR とも正常なら exit 0 ==="
INBOX9="$WORKDIR/inbox9.md"
run_case "$INBOX9" all_open "$WORKDIR/pi-triage-ok.sh" --all-open
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 0 ]; then pass "case9: --all-open with 2 normal PRs exits 0"; else fail "case9: exit was $return_code, want 0"; fi
if [ ! -f "$INBOX9" ]; then pass "case9: no inbox entries for normal PRs"; else fail "case9: unexpected inbox entries"; fi

echo
echo "=== case 10 (NB4): CI run が0件 (まだ走っていない) -> 正常系として exit 0 ==="
INBOX10="$WORKDIR/inbox10.md"
run_case "$INBOX10" no_runs "$WORKDIR/pi-triage-ok.sh" 49
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 0 ]; then pass "case10: run 0件は正常系 (exit 0)"; else fail "case10: exit was $return_code, want 0"; fi
if [ ! -f "$INBOX10" ]; then pass "case10: no inbox entry for a PR with no runs yet"; else fail "case10: unexpected inbox entry"; fi

echo
echo "=== case 11 (NB2): startedAt が null の in_progress job は正しくスキップされる ==="
INBOX11="$WORKDIR/inbox11.md"
run_case "$INBOX11" hang_null_started "$WORKDIR/pi-triage-ok.sh" 50
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 0 ]; then pass "case11: null startedAt does not trigger a false hang (exit 0)"; else fail "case11: exit was $return_code, want 0"; fi
if [ ! -f "$INBOX11" ]; then pass "case11: no inbox entry written"; else fail "case11: unexpected inbox entry"; fi

echo
echo "=== case 12 (NB2): CI_WATCH_STALL_SECONDS の不正値を拒否する ==="
set +e
CI_WATCH_STALL_SECONDS="not-a-number" bash "$CI_WATCH" 51 > "$WORKDIR/badstall1.log" 2>&1
rc1=$?
CI_WATCH_STALL_SECONDS="-5" bash "$CI_WATCH" 51 > "$WORKDIR/badstall2.log" 2>&1
rc2=$?
set -e
echo "--- non-integer: exit $rc1 ---"; cat "$WORKDIR/badstall1.log"
echo "--- negative: exit $rc2 ---"; cat "$WORKDIR/badstall2.log"
if [ "$rc1" -eq 1 ] && grep -qF "CI_WATCH_STALL_SECONDS" "$WORKDIR/badstall1.log"; then
  pass "case12: non-integer CI_WATCH_STALL_SECONDS rejected with exit 1"
else
  fail "case12: non-integer value not rejected as expected (rc=$rc1)"
fi
if [ "$rc2" -eq 1 ] && grep -qF "CI_WATCH_STALL_SECONDS" "$WORKDIR/badstall2.log"; then
  pass "case12: negative CI_WATCH_STALL_SECONDS rejected with exit 1"
else
  fail "case12: negative value not rejected as expected (rc=$rc2)"
fi

echo
echo "=== case 13 (B2, 2巡目): run list が exit 0 のまま空 stdout (単一PR) -> exit 3 ==="
INBOX13="$WORKDIR/inbox13.md"
run_case "$INBOX13" run_list_empty "$WORKDIR/pi-triage-ok.sh" 52
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then
  pass "case13: run list の空 stdout (単一PR) は操作エラーとして exit 3"
else
  fail "case13: exit was $return_code, want 3 (run list 空出力が『run無し』正常系と混同されている)"
fi
if [ -f "$INBOX13" ]; then
  fail "case13: 操作エラーなのに inbox が作られた"
else
  pass "case13: 操作エラー時に inbox エントリを作らない"
fi

echo
echo "=== case 14 (B2, 2巡目): run list が exit 0 のまま空 stdout (--all-open) -> exit 3 ==="
INBOX14="$WORKDIR/inbox14.md"
run_case "$INBOX14" run_list_empty "$WORKDIR/pi-triage-ok.sh" --all-open
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then
  pass "case14: run list の空 stdout (--all-open) は操作エラーとして exit 3"
else
  fail "case14: exit was $return_code, want 3"
fi

echo
echo "=== case 15 (B2, 2巡目): in-progress run で gh run view --json jobs が非ゼロ -> exit 3 ==="
INBOX15="$WORKDIR/inbox15.md"
run_case "$INBOX15" jobs_fetch_fail "$WORKDIR/pi-triage-ok.sh" 53
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then
  pass "case15: jobs 取得の非ゼロ失敗は『ハング無し』ではなく操作エラーとして exit 3"
else
  fail "case15: exit was $return_code, want 3 (detect_stall がハング無しと操作失敗を混同している)"
fi
if grep -qF "connection reset by peer" "$WORKDIR/last_output.txt"; then
  pass "case15: stderr に gh の失敗理由が伝播している"
else
  fail "case15: stderr に操作エラーの詳細が無い"
fi
if [ -f "$INBOX15" ]; then
  fail "case15: 操作エラーなのに inbox が作られた"
else
  pass "case15: 操作エラー時に inbox エントリを作らない"
fi

echo
echo "=== case 16 (B2, 3巡目残件): run JSON の必須フィールド欠落 ([{}]) -> exit 3 ==="
INBOX16="$WORKDIR/inbox16.md"
run_case "$INBOX16" run_missing_fields "$WORKDIR/pi-triage-ok.sh" 54
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then
  pass "case16: run JSON の必須フィールド欠落は操作エラーとして exit 3"
else
  fail "case16: exit was $return_code, want 3 (databaseId/status/url 欠落を正常系と誤認している)"
fi
if [ -f "$INBOX16" ]; then
  fail "case16: 操作エラーなのに inbox が作られた"
else
  pass "case16: 操作エラー時に inbox エントリを作らない"
fi

echo
echo "=== case 17 (B2, 3巡目残件): 正常な startedAt でも date -d 自体が失敗 -> exit 3 ==="
INBOX17="$WORKDIR/inbox17.md"
set +e
PATH="$FAKE_DATE_BIN:$PATH" FAKE_GH_SCENARIO=date_fail CI_WATCH_INBOX="$INBOX17" \
  CI_WATCH_PI_TRIAGE="$WORKDIR/pi-triage-ok.sh" bash "$CI_WATCH" 55 > "$WORKDIR/last_output.txt" 2>&1
return_code=$?
set -e
echo "--- raw output ---"; cat "$WORKDIR/last_output.txt"
echo "--- exit code: $return_code ---"
if [ "$return_code" -eq 3 ]; then
  pass "case17: date -d の失敗は continue で握り潰さず操作エラーとして exit 3"
else
  fail "case17: exit was $return_code, want 3 (date -d 失敗を『ハング無し』として握り潰している)"
fi
if grep -qF "date で解釈できなかった" "$WORKDIR/last_output.txt"; then
  pass "case17: stderr に date 失敗の詳細が伝播している"
else
  fail "case17: stderr に date 失敗の詳細が無い"
fi
if [ -f "$INBOX17" ]; then
  fail "case17: 操作エラーなのに inbox が作られた"
else
  pass "case17: 操作エラー時に inbox エントリを作らない"
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
# shellcheck disable=SC2016
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
