#!/bin/bash
# report-bridge ループ (claim -> Dispatcher へ送信 -> 成否に応じて commit / release) の
# 結合テスト。ledger 関数単体のテスト (test_watch_report_ledger.sh) では、ループ側の
# 制御フロー — 送信失敗時に配達済みにしてしまわないか、成功後に二重送信しないか —
# を検証できないため、watch.sh を実プロセスとして動かして確認する。
#
# tmux はスタブに差し替える。send-keys は呼び出しをログに記録し、$STUB_DIR/fail_send が
# 存在する間は失敗を返す (Dispatcher pane が消えている状況の再現)。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCH_SH="$SCRIPT_DIR/watch.sh"

TMPDIR_T="$(mktemp -d)"
WATCH_PID=""
WATCH_PID_B=""
cleanup() {
    [ -n "$WATCH_PID" ] && kill "$WATCH_PID" 2>/dev/null
    [ -n "$WATCH_PID_B" ] && kill "$WATCH_PID_B" 2>/dev/null
    rm -rf "$TMPDIR_T"
}
trap cleanup EXIT

STUB_DIR="$TMPDIR_T/stub"
QUEUE="$TMPDIR_T/queue"
REPORTS="$QUEUE/projects/pj_test/reports"
SEND_LOG="$TMPDIR_T/send.log"
LEDGER="$QUEUE/.report_ledger"
mkdir -p "$STUB_DIR" "$REPORTS" "$QUEUE/projects/pj_test/tasks"
echo testsess > "$QUEUE/projects/pj_test/.squad_session"
: > "$SEND_LOG"

cat > "$STUB_DIR/tmux" <<STUB
#!/bin/bash
case "\$1" in
    has-session) exit 0 ;;
    list-panes)  echo "testsess:0.0"; exit 0 ;;
    capture-pane) exit 0 ;;
    send-keys)
        if [ -e "$STUB_DIR/fail_send" ]; then exit 1; fi
        printf '%s\n' "\$*" >> "$SEND_LOG"
        exit 0 ;;
esac
exit 0
STUB
chmod +x "$STUB_DIR/tmux"

pass=0
fail=0
assert_eq() {
    local desc="$1" got="$2" expect="$3"
    if [ "$got" = "$expect" ]; then
        echo "PASS: $desc (=$got)"; pass=$((pass + 1))
    else
        echo "FAIL: $desc (got $got, expect $expect)"; fail=$((fail + 1))
    fi
}

# 送信ログ中の対象 report を含む send-keys 呼び出し数 (Enter 送信は本文を含まない)
sends_for() { grep -c "$1" "$SEND_LOG"; }

# 条件が成立するまで待つ (最大 $2 秒)
wait_until() {
    local cond="$1" limit="${2:-15}" i=0
    while [ "$i" -lt "$((limit * 2))" ]; do
        eval "$cond" && return 0
        sleep 0.5; i=$((i + 1))
    done
    return 1
}

led_ut() { awk -F'\t' -v p="$1" '{path=(NF>=3?$3:$2); if(path==p) v=(NF>=3?$2:0)} END{print v}' "$LEDGER" 2>/dev/null; }

# 起動前に存在する report は seed で配達済みになり、通知されないこと
echo "status: completed" > "$REPORTS/worker1_report.yaml"

# SQUAD_DEFAULT_OWNER はわざと別名にする。既定オーナーと一致すると worktree GC が
# 走り (LAST_GC=0 なので初回サイクルで即実行)、実リポジトリを fetch して数十秒
# ブロックしてしまうため。project の担当は .squad_session マーカーで決まる。
PATH="$STUB_DIR:$PATH" \
SQUAD_SESSION=testsess SQUAD_DEFAULT_OWNER=not-this-session \
WATCH_QUEUE_DIR="$QUEUE" WATCH_BOOT_DELAY=0 WATCH_INTERVAL=1 \
WATCH_DISCOVERY_INTERVAL=999999 WATCH_GC_INTERVAL=999999 WATCH_LEDGER_LEASE=3 \
    bash "$WATCH_SH" > "$TMPDIR_T/watch.log" 2>&1 &
WATCH_PID=$!

sleep 3
assert_eq "起動前からある report は通知されない (seed)" "$(sends_for worker1_report)" "0"

# 1. 新規 report は 1 回だけ通知される
echo "status: completed" > "$REPORTS/worker2_report.yaml"
wait_until '[ "$(sends_for worker2_report)" -ge 1 ]' 15
assert_eq "新規 report が通知される" "$(sends_for worker2_report)" "1"
# 送信ログは send-keys の時点で書かれ、commit はその後 (Enter 送信を挟む) なので待つ
wait_until '[ "$(led_ut "$REPORTS/worker2_report.yaml")" = "0" ]' 10
assert_eq "通知できたら配達済みになる" "$(led_ut "$REPORTS/worker2_report.yaml")" "0"
sleep 3
assert_eq "以降のサイクルで再通知されない" "$(sends_for worker2_report)" "1"

# 2. 送信が失敗する状況では配達済みにせず、復旧後に通知しなおす
touch "$STUB_DIR/fail_send"
echo "status: completed" > "$REPORTS/worker3_report.yaml"
wait_until 'grep -q "送信に失敗" "$TMPDIR_T/watch.log"' 15
assert_eq "送信失敗はログに出る" \
    "$(grep -q '送信に失敗' "$TMPDIR_T/watch.log" && echo yes || echo no)" "yes"
assert_eq "送信に失敗した report は配達済みにならない" \
    "$([ "$(led_ut "$REPORTS/worker3_report.yaml")" = "0" ] && echo delivered || echo not_delivered)" "not_delivered"

rm -f "$STUB_DIR/fail_send"
wait_until '[ "$(sends_for worker3_report)" -ge 1 ]' 20
assert_eq "復旧後に通知される" "$(sends_for worker3_report)" "1"
wait_until '[ "$(led_ut "$REPORTS/worker3_report.yaml")" = "0" ]' 10
assert_eq "復旧後の通知で配達済みになる" "$(led_ut "$REPORTS/worker3_report.yaml")" "0"
sleep 3
assert_eq "復旧後も二重通知しない" "$(sends_for worker3_report)" "1"

# 3. report が更新されたら再通知される
echo "status: blocked" > "$REPORTS/worker2_report.yaml"
wait_until '[ "$(sends_for worker2_report)" -ge 2 ]' 15
assert_eq "更新された report は再通知される" "$(sends_for worker2_report)" "2"
assert_eq "blocked は [INBOX] 付きで通知される" \
    "$(grep -c 'INBOX.*worker2_report' "$SEND_LOG")" "1"
# 後段の担当切替テストのため、worker2 の配達済みを確定させておく
wait_until '[ "$(led_ut "$REPORTS/worker2_report.yaml")" = "0" ]' 10

# 4. 複数 watcher + 担当切替 (Issue #22 F1 のループレベル検証):
#    A(testsess) が配達済みの report は、担当が othersess に移っても B が再通知しない。
#    新規 report は新担当 B だけが 1 回通知する (二重通知なし)。
PATH="$STUB_DIR:$PATH" \
SQUAD_SESSION=othersess SQUAD_DEFAULT_OWNER=not-this-session \
WATCH_QUEUE_DIR="$QUEUE" WATCH_BOOT_DELAY=0 WATCH_INTERVAL=1 \
WATCH_DISCOVERY_INTERVAL=999999 WATCH_GC_INTERVAL=999999 WATCH_LEDGER_LEASE=3 \
    bash "$WATCH_SH" > "$TMPDIR_T/watch_b.log" 2>&1 &
WATCH_PID_B=$!
sleep 2
echo othersess > "$QUEUE/projects/pj_test/.squad_session"   # 担当を A -> B に切替
sleep 4   # 両 watcher が数サイクル回るのを待つ
assert_eq "担当切替後も A 配達済みの report を B が再通知しない (worker2)" \
    "$(sends_for worker2_report)" "2"
assert_eq "担当切替後も A 配達済みの report を B が再通知しない (worker3)" \
    "$(sends_for worker3_report)" "1"

echo "status: completed" > "$REPORTS/worker5_report.yaml"
wait_until '[ "$(sends_for worker5_report)" -ge 1 ]' 15
wait_until '[ "$(led_ut "$REPORTS/worker5_report.yaml")" = "0" ]' 10
sleep 3
assert_eq "新規 report は 1 回だけ通知される (2 watcher でも二重通知なし)" \
    "$(sends_for worker5_report)" "1"
assert_eq "通知したのは新担当セッションの watcher" \
    "$(grep -c 'othersess.*worker5_report' "$SEND_LOG")" "1"

kill "$WATCH_PID_B" 2>/dev/null; WATCH_PID_B=""

kill "$WATCH_PID" 2>/dev/null; WATCH_PID=""

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
