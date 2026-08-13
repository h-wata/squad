#!/bin/bash
# watch.sh の report 配達 ledger (Issue #22 / PR #21 cross-review F1・F3 の根本対応)
# に対する挙動テスト。
#
# grep によるコード存在確認ではなく、watch.sh 本体から ledger_claim() / ledger_commit()
# / ledger_release() / ledger_baseline_seed() をそのまま source して実際の判定挙動を
# 検証する。関数定義より後 (while true ループ) は実行しないよう、ループ開始行より前
# だけを抜き出して source する。
#
# report-bridge ループ (claim -> 通知 -> commit/release) の結合テストは
# tests/test_watch_report_bridge.sh 側で watch.sh を実プロセスとして動かして行う。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCH_SH="$SCRIPT_DIR/watch.sh"

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

# ledger は実運用の queue/ ではなくテスト用の一時ファイルに向ける
export WATCH_LEDGER_FILE="$TMPDIR_T/ledger"

# "OWNED=()" 以降は起動時ログ・BOOT_DELAY sleep・メインループを含むため、
# 関数定義のみを含む手前までを source する。
CUTOFF_LINE="$(grep -n '^OWNED=()' "$WATCH_SH" | head -n1 | cut -d: -f1)"
if [ -z "$CUTOFF_LINE" ]; then
    echo "FAIL: watch.sh 内に 'OWNED=()' が見つからず、関数定義を安全に source できない"
    exit 1
fi

FUNCS_FILE="$TMPDIR_T/funcs.sh"
head -n "$((CUTOFF_LINE - 1))" "$WATCH_SH" > "$FUNCS_FILE"

# shellcheck disable=SC1090
source "$FUNCS_FILE"

pass=0
fail=0

# ledger の記録を読む (本体と同じ 3 列形式)
led_mt() { awk -F'\t' -v p="$1" 'NF>=3 && $3==p{v=$1} END{print v}' "$LEDGER_FILE" 2>/dev/null; }
led_ut() { awk -F'\t' -v p="$1" 'NF>=3 && $3==p{v=$2} END{print v}' "$LEDGER_FILE" 2>/dev/null; }

check() {
    local desc="$1" f="$2" m="$3" expect="$4" got
    if ledger_claim "$f" "$m" > /dev/null; then   # stdout は claim 前の記録
        got="NOTIFY"
    else
        got="SKIP"
    fi
    if [ "$got" = "$expect" ]; then
        echo "PASS: $desc (path=$(basename "$f") m=$m -> $got)"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc (path=$(basename "$f") m=$m -> got $got, expect $expect)"
        fail=$((fail + 1))
    fi
}

# claim の戻り値 "<token>\t<前 mtime>\t<前 lease>" から token だけ取り出す
tok_of() { printf '%s' "${1%%$'\t'*}"; }

# 現在 ledger に入っている claim (lease 値 = token) をそのまま使って commit する。
# 実運用では claim の戻り値を持ち回るが、テストでは check() が戻り値を捨てるため。
commit_pending() { ledger_commit "$1" "$2" "$(led_ut "$1")"; }

# claim -> 送信成功 -> commit までを 1 回分行う (実運用の正常系と同じ手順)
deliver() {
    local f="$1" m="$2" rec
    rec="$(ledger_claim "$f" "$m")" || return 1
    ledger_commit "$f" "$m" "$(tok_of "$rec")"
}

assert_eq() {
    local desc="$1" got="$2" expect="$3"
    if [ "$got" = "$expect" ]; then
        echo "PASS: $desc (=$got)"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc (got $got, expect $expect)"
        fail=$((fail + 1))
    fi
}

A="/q/projects/pj_a/reports/worker1_report.yaml"
B="/q/projects/pj_b/reports/worker2_review.yaml"

# 1. ledger がまだ無い状態での初回 claim -> 通知 (ファイルも生成される)
check "初回 claim (ledger 未作成)" "$A" "100.5" "NOTIFY"
assert_eq "ledger ファイルが生成される" "$([ -f "$LEDGER_FILE" ] && echo yes || echo no)" "yes"
assert_eq "claim 直後は配達中 (lease 期限が入る)" "$([ "$(led_ut "$A")" != "0" ] && echo pending || echo delivered)" "pending"

# 2. 配達中 (lease 有効) の間は他の watcher が同じ版を claim できない
check "配達中の同一 mtime を再 claim" "$A" "100.5" "SKIP"

# 3. 送信成功で配達済み (lease 0) に確定し、以後は lease 期限に関係なく再通知されない
commit_pending "$A" "100.5"
assert_eq "commit で配達済みになる" "$(led_ut "$A")" "0"
check "配達済みの report は再通知されない" "$A" "100.5" "SKIP"

# 4. 同一秒内に書き直された report も別の版として通知する (小数秒まで比較する)。
#    整数秒に切り捨てると in_progress -> blocked の書き直しが恒久的に握り潰される。
check "同一秒・小数部のみ差異 (100.5 -> 100.9)" "$A" "100.9" "NOTIFY"
commit_pending "$A" "100.9"
check "同一秒でも古い小数部 (100.5) には巻き戻らない" "$A" "100.5" "SKIP"

# 5. report が更新されて mtime が進んだら再通知する
check "mtime が進んだ report" "$A" "101.0" "NOTIFY"
commit_pending "$A" "101.0"
check "配達済みの同一 mtime は再通知されない" "$A" "101.0" "SKIP"
check "さらに新しい版は通知する" "$A" "101.2" "NOTIFY"
commit_pending "$A" "101.2"

# 6. 別 path は独立に判定される
check "別 report の初回 claim" "$B" "100.5" "NOTIFY"
check "別 report の再 claim" "$B" "100.5" "SKIP"

# 7. 1 path につき 1 行しか持たない (ledger が単調増加しない)
assert_eq "ledger 行数 (path 数と一致)" "$(wc -l < "$LEDGER_FILE" | tr -d ' ')" "2"
assert_eq "A の記録 mtime は最新のみ" "$(led_mt "$A")" "101.2"

# 8. F1 回帰: 別 watcher プロセス (別セッション) が配達済みなら、こちらは再通知しない。
#    担当が A→B→A と移っても二重通知にならないことの中核。
C="/q/projects/pj_c/reports/worker3_report.yaml"
bash -c "export WATCH_LEDGER_FILE='$LEDGER_FILE'; source '$FUNCS_FILE'
         rec=\$(ledger_claim '$C' '200.0') && ledger_commit '$C' '200.0' \"\${rec%%\$'\\t'*}\""
rc_other=$?
assert_eq "別プロセスの配達は成功" "$rc_other" "0"
check "別プロセスが配達済みの report" "$C" "200.0" "SKIP"

# 9. 別プロセスが配達した後に report が更新されたら、こちらは通知する
check "別プロセス配達後に更新された report" "$C" "201.0" "NOTIFY"
commit_pending "$C" "201.0"

# 10. 同時 claim (flock による直列化): 同じ path/mtime を並行して claim しても
#     成功するのは 1 プロセスだけ。
D="/q/projects/pj_d/reports/worker1_report.yaml"
RES_DIR="$TMPDIR_T/race"
mkdir -p "$RES_DIR"
for i in 1 2 3 4 5; do
    bash -c "export WATCH_LEDGER_FILE='$LEDGER_FILE'
             source '$FUNCS_FILE'
             ledger_claim '$D' '300.0' > /dev/null && echo ok > '$RES_DIR/$i'" &
done
wait
assert_eq "並行 claim 5 本のうち成功は 1 本のみ" "$(find "$RES_DIR" -type f | wc -l | tr -d ' ')" "1"

# 11. lease 期限切れ: claim したまま配達を完了しなかった report は、期限が切れると
#     別の watcher が再び claim できる (watcher が送信前に死んでも通知が消えない)。
#     LEDGER_LEASE=0 で「即座に期限切れ」を作る。
K="/q/projects/pj_k/reports/worker1_report.yaml"
SAVED_LEASE="$LEDGER_LEASE"
LEDGER_LEASE=0
check "lease 0 で claim (配達しないまま放置)" "$K" "400.0" "NOTIFY"
check "lease 期限切れなら再 claim できる" "$K" "400.0" "NOTIFY"
LEDGER_LEASE="$SAVED_LEASE"
commit_pending "$K" "400.0"
check "配達済みになれば lease に関係なく skip" "$K" "400.0" "SKIP"

# 12. 古い mtime を掴んだ watcher が ledger を巻き戻さない
#     A が 101 を配達済みにした後、更新前の 100 を掴んだ B が claim しても通知しない。
E="/q/projects/pj_e/reports/worker1_report.yaml"
deliver "$E" "101.0"
check "古い mtime (更新前スナップショット) は claim しない" "$E" "100.0" "SKIP"
assert_eq "ledger は巻き戻らない" "$(led_mt "$E")" "101.0"

# 13. ledger_release: 送信に失敗したときのロールバック。
#     claim 前が未登録なら行ごと消え、次サイクルで再通知できる。
G="/q/projects/pj_g/reports/worker2_report.yaml"
IFS=$'\t' read -r tok_g pmt_g put_g <<< "$(ledger_claim "$G" "300.0")"
assert_eq "未登録からの claim は空の前記録を返す" "$pmt_g$put_g" ""
ledger_release "$G" "$tok_g" "$pmt_g" "$put_g"
assert_eq "release で ledger から消える" "$(led_mt "$G")" ""
check "release 後は再通知できる" "$G" "300.0" "NOTIFY"

# 14. release は「削除」ではなく「claim 前の記録に戻す」。
#     行ごと消すと、直前に配達済みだった古い版の記録まで失われ、更新前の mtime を
#     掴んでいた別 watcher がその古い版を再 claim して二重通知できてしまう。
I="/q/projects/pj_i/reports/worker1_report.yaml"
deliver "$I" "100.0"
# 新版 101 を claim (通知しようとした)
IFS=$'\t' read -r tok_i pmt_i put_i <<< "$(ledger_claim "$I" "101.0")"
assert_eq "claim は上書き前の記録を返す" "$pmt_i/$put_i" "100.0/0"
ledger_release "$I" "$tok_i" "$pmt_i" "$put_i"   # 送信に失敗 -> ロールバック
assert_eq "release で 100 に戻る (行は消えない)" "$(led_mt "$I")" "100.0"
assert_eq "戻した記録は配達済みのまま" "$(led_ut "$I")" "0"
check "巻き戻った隙に古い版 100 を再 claim できない" "$I" "100.0" "SKIP"
check "新版 101 は次サイクルで再通知できる" "$I" "101.0" "NOTIFY"

# 15. release は他 watcher が新しい版で claim し直した記録を壊さない
commit_pending "$I" "101.0"
ledger_release "$I" "999999999" "" ""
assert_eq "自分の claim でなければ release しない" "$(led_mt "$I")" "101.0"

# 16. 3 列に満たない行 (壊れた行) は無視され、正常な行の判定に影響しない
L="/q/projects/pj_l/reports/worker1_report.yaml"
printf 'broken-line\n' >> "$LEDGER_FILE"
check "壊れた行があっても新規 report は通知される" "$L" "500.0" "NOTIFY"
commit_pending "$L" "500.0"
check "壊れた行があっても配達済み判定は機能する" "$L" "500.0" "SKIP"

# 16b. PR #24 Codex review 4th round B1 回帰:
#      lease が切れていても、記録より古い mtime は claim させない。claim を許すと
#      ledger の mtime (新しい方) と呼び出し側が持つ mtime (古い方) が食い違い、
#      送信後の commit が空振りして lease 切れ後に二重通知される。
N1="/q/projects/pj_n1/reports/worker1_report.yaml"
SAVED_LEASE="$LEDGER_LEASE"
LEDGER_LEASE=0
check "lease 0 で新版 101 を claim (配達しないまま放置)" "$N1" "101.0" "NOTIFY"
check "lease 切れでも古い mtime 100 は claim しない" "$N1" "100.0" "SKIP"
assert_eq "古い claim を弾いても ledger は巻き戻らない" "$(led_mt "$N1")" "101.0"
check "lease 切れの同一版 101 は再 claim できる" "$N1" "101.0" "NOTIFY"
LEDGER_LEASE="$SAVED_LEASE"

# 16c. PR #24 Codex review 4th round B2 回帰:
#      lease 切れ後に別 watcher が再 claim した記録を、遅れて戻ってきた元 watcher が
#      commit / release で壊さない (claim token で所有者を識別する)。
N2="/q/projects/pj_n2/reports/worker1_report.yaml"
LEDGER_LEASE=0
tok_a="$(tok_of "$(ledger_claim "$N2" "700.0")")"   # A が claim (即 lease 切れ)
LEDGER_LEASE="$SAVED_LEASE"
tok_b="$(tok_of "$(ledger_claim "$N2" "700.0")")"   # B が再 claim
assert_eq "再 claim の token は元 claim と異なる" "$([ "$tok_a" != "$tok_b" ] && echo differ || echo same)" "differ"
ledger_commit "$N2" "700.0" "$tok_a"                # A の遅れた commit
assert_eq "他 watcher の claim を勝手に commit しない" "$(led_ut "$N2")" "$tok_b"
ledger_release "$N2" "$tok_a" "" ""                 # A の遅れた release
assert_eq "他 watcher の claim を release で壊さない" "$(led_ut "$N2")" "$tok_b"
ledger_commit "$N2" "700.0" "$tok_b"                # B の commit は通る
assert_eq "再 claim した watcher は commit できる" "$(led_ut "$N2")" "0"

# 16d. PR #24 Claude review #1 回帰: claim token は同一秒の別 claim とも衝突しない。
#      新しい mtime の claim は既存 lease を待たずに成立するため、期限値だけを token に
#      すると担当切替の瞬間に 2 watcher が同じ token を持ちうる。
N3="/q/projects/pj_n3/reports/worker1_report.yaml"
tok_1="$(tok_of "$(ledger_claim "$N3" "800.0")")"
tok_2="$(tok_of "$(ledger_claim "$N3" "800.5")")"   # 同じ秒に別 watcher が新しい版を claim
assert_eq "同一秒の 2 claim でも token は異なる" \
    "$([ "$tok_1" != "$tok_2" ] && echo differ || echo same)" "differ"
ledger_release "$N3" "$tok_1" "" ""                 # 先行 claim の遅れた release
assert_eq "先行 claim の release は後続 claim を壊さない" "$(led_ut "$N3")" "$tok_2"

# 16e. PR #24 Claude review #8: mtime 巻き戻しによる skip は専用の戻り値で区別する
#      (呼び出し側が「気づけない抑止」をログに残せるようにするため)。
deliver "$N3" "801.0" > /dev/null
ledger_claim "$N3" "800.0" > /dev/null
assert_eq "巻き戻し skip は LEDGER_RC_STALE を返す" "$?" "$LEDGER_RC_STALE"
ledger_claim "$N3" "801.0" > /dev/null
assert_eq "配達済み skip は 1 を返す" "$?" "1"

# 16f. PR #24 Claude review 6th #1 回帰: ledger を読めない状態で claim しても、
#      既存の配達済み記録を消さない (部分的な内容で mv すると全 report 一斉再通知になる)。
if [ "$(id -u)" -ne 0 ]; then
    R1="/q/projects/pj_r/reports/worker1_report.yaml"
    R2="/q/projects/pj_r/reports/worker2_report.yaml"
    deliver "$R1" "950.0" > /dev/null
    before_r="$(cat "$LEDGER_FILE")"
    chmod 000 "$LEDGER_FILE"
    check "ledger を読めないときの claim は通知側に倒れる" "$R2" "951.0" "NOTIFY"
    chmod 644 "$LEDGER_FILE"
    assert_eq "読めない ledger を部分内容で上書きしない" "$(cat "$LEDGER_FILE")" "$before_r"
else
    echo "SKIP: ledger 読み取り不可テスト (root 実行では chmod を迂回できるため)"
fi

# 16g. PR #24 Claude review 6th #8 回帰: find %T@ の 20 桁 mtime でも下位桁の差を
#      正しく比較する (awk double は約 16 桁で桁落ちし、新しい版が STALE 扱いになる)。
G2="/q/projects/pj_g2/reports/worker1_report.yaml"
deliver "$G2" "1786499353.1215575750" > /dev/null
check "下位 1 桁だけ新しい mtime は通知する" "$G2" "1786499353.1215575751" "NOTIFY"
commit_pending "$G2" "1786499353.1215575751"
check "下位 1 桁だけ古い mtime は claim しない" "$G2" "1786499353.1215575750" "SKIP"

# 17. ledger を書けない場合、commit / release は失敗 (非 0) を返す。
#     呼び出し側はログを出すだけでよい (lease 期限切れで再 claim される)。
#     root では chmod を迂回できるためスキップ。
if [ "$(id -u)" -ne 0 ]; then
    M="/q/projects/pj_m/reports/worker1_report.yaml"
    tok_m="$(tok_of "$(ledger_claim "$M" "600.0")")"
    RO_PARENT="$(dirname "$LEDGER_FILE")"
    chmod 500 "$RO_PARENT"
    if ledger_commit "$M" "600.0" "$tok_m" 2>/dev/null; then commit_rc=0; else commit_rc=1; fi
    if ledger_release "$M" "$tok_m" "" "" 2>/dev/null; then rel_rc=0; else rel_rc=1; fi
    chmod 700 "$RO_PARENT"
    assert_eq "ledger を書けないとき commit は失敗を返す" "$commit_rc" "1"
    assert_eq "ledger を書けないとき release は失敗を返す" "$rel_rc" "1"
    assert_eq "失敗時は ledger を書き換えない" "$(led_mt "$M")" "600.0"
else
    echo "SKIP: commit/release 失敗テスト (root 実行では chmod を迂回できるため)"
fi

# 17b. PR #24 Claude review #10: claim 側の異常系は「通知する」側に倒れること。
#      ここが fail-closed に反転すると report が黙って Dispatcher に届かなくなる。
if [ "$(id -u)" -ne 0 ]; then
    P1="/q/projects/pj_p1/reports/worker1_report.yaml"
    RO_PARENT="$(dirname "$LEDGER_FILE")"
    chmod 500 "$RO_PARENT"
    check "ledger を書けなくても claim は通知側に倒れる" "$P1" "900.0" "NOTIFY"
    # 記録できなかった claim は token を返さない。ledger に無い token を返すと呼び出し側が
    # 「claim 記録済み」と誤認し、ログと再送時期が実態と食い違う (Codex review 7th B1)。
    rec_p1="$(ledger_claim "$P1" "900.5")"
    assert_eq "記録できなかった claim は token を返さない" "$(tok_of "$rec_p1")" ""
    chmod 700 "$RO_PARENT"
fi

# 17c. lock file 自体を開けない場合も通知する (9> のリダイレクトが失敗し rc=1 になる)
SAVED_LOCK="$LEDGER_LOCK"
LEDGER_LOCK="$TMPDIR_T/no_such_dir/lock"
P2="/q/projects/pj_p2/reports/worker1_report.yaml"
check "lock file を開けなくても claim は通知側に倒れる" "$P2" "910.0" "NOTIFY"
LEDGER_LOCK="$SAVED_LOCK"

# 18. ledger_baseline_seed は担当 project だけでなく queue/projects 配下の全 report を
#     配達済みとして登録する。後から起動した別セッションの watcher は「ledger がある =
#     seed 済み」としか判断しないため、担当分しか seed しないとその watcher が自分の
#     担当 project の過去 report を一斉通知してしまう。
QUEUE_DIR="$TMPDIR_T/queue"
LEDGER_FILE="$TMPDIR_T/queue/.report_ledger"
# shellcheck disable=SC2034  # source した watch.sh 側の関数が参照する
LEDGER_LOCK="${LEDGER_FILE}.lock"
mkdir -p "$QUEUE_DIR/projects/pj_a/reports" "$QUEUE_DIR/projects/pj_b/reports"
echo "status: completed" > "$QUEUE_DIR/projects/pj_a/reports/worker1_report.yaml"
echo "status: completed" > "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml"
echo "status: completed" > "$QUEUE_DIR/projects/pj_b/reports/worker3_review.yaml"
echo "not a report"      > "$QUEUE_DIR/projects/pj_b/reports/notes.md"

ledger_baseline_seed > /dev/null
assert_eq "seed 後に ledger が存在する" "$([ -f "$LEDGER_FILE" ] && echo yes || echo no)" "yes"
assert_eq "seed は全 project の report を登録 (report 3 件のみ)" \
    "$(wc -l < "$LEDGER_FILE" | tr -d ' ')" "3"
assert_eq "seed した行は配達済み (lease 0)" \
    "$(led_ut "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml")" "0"
check "非担当 project の既存 report は再通知されない" \
    "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml" \
    "$(find "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml" -printf '%T@')" "SKIP"

# 18c. PR #24 Claude review 6th #4: seed が lock file を開けない場合は WARN を出して
#      1 を返す (無言の return 0 だと、seed されないまま監視が始まって一斉通知になる
#      原因がログから追えない)。
SAVED_LOCK_SEED="$LEDGER_LOCK"
SAVED_FILE_SEED="$LEDGER_FILE"
LEDGER_FILE="$TMPDIR_T/queue_seedfail/.report_ledger"
LEDGER_LOCK="$TMPDIR_T/no_such_dir_seed/lock"
mkdir -p "$TMPDIR_T/queue_seedfail"
seed_out="$(ledger_baseline_seed 2>&1)"; seed_rc=$?
assert_eq "lock を開けない seed は失敗を返す" "$seed_rc" "1"
assert_eq "seed 失敗は WARN をログする" \
    "$(echo "$seed_out" | grep -q 'WARN.*seed' && echo yes || echo no)" "yes"
assert_eq "seed 失敗時は ledger を作らない" "$([ -f "$LEDGER_FILE" ] && echo yes || echo no)" "no"
LEDGER_LOCK="$SAVED_LOCK_SEED"
LEDGER_FILE="$SAVED_FILE_SEED"

# 19. seed 済み ledger がある状態で seed を再実行しても上書きしない (先着優先)
before="$(cat "$LEDGER_FILE")"
echo "status: completed" > "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml"
ledger_baseline_seed > /dev/null
assert_eq "ledger 済みなら seed は何もしない" "$(cat "$LEDGER_FILE")" "$before"
check "seed 後に書かれた report は通知される" \
    "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml" \
    "$(find "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml" -printf '%T@')" "NOTIFY"

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
