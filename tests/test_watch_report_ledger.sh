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

# ledger の記録を読む (本体と同じ 3 列形式 / 旧 2 列形式の両対応)
led_mt() { awk -F'\t' -v p="$1" '{path=(NF>=3?$3:$2); if(path==p) v=$1} END{print v}' "$LEDGER_FILE" 2>/dev/null; }
led_ut() { awk -F'\t' -v p="$1" '{path=(NF>=3?$3:$2); if(path==p) v=(NF>=3?$2:0)} END{print v}' "$LEDGER_FILE" 2>/dev/null; }

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

# claim -> 送信成功 -> commit までを 1 回分行う (実運用の正常系と同じ手順)
deliver() {
    local f="$1" m="$2"
    ledger_claim "$f" "$m" > /dev/null || return 1
    ledger_commit "$f" "$m"
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

# 3. 小数部だけが違う同一秒 -> 同一とみなす
#    (find %T@ の小数部は取得経路で揺れうるため整数秒に切り捨てて比較する)
check "同一秒・小数部のみ差異 (100.5 -> 100.9)" "$A" "100.9" "SKIP"
check "同一秒・小数部なし (100.5 -> 100)" "$A" "100" "SKIP"

# 4. 送信成功で配達済み (lease 0) に確定し、以後は lease 期限に関係なく再通知されない
ledger_commit "$A" "100.5"
assert_eq "commit で配達済みになる" "$(led_ut "$A")" "0"
check "配達済みの report は再通知されない" "$A" "100.5" "SKIP"

# 5. report が更新されて mtime が進んだら再通知する
check "mtime が進んだ report" "$A" "101.0" "NOTIFY"
ledger_commit "$A" "101.0"
check "配達済みの新しい版も再通知されない" "$A" "101.2" "SKIP"

# 6. 別 path は独立に判定される
check "別 report の初回 claim" "$B" "100.5" "NOTIFY"
check "別 report の再 claim" "$B" "100.5" "SKIP"

# 7. 1 path につき 1 行しか持たない (ledger が単調増加しない)
assert_eq "ledger 行数 (path 数と一致)" "$(wc -l < "$LEDGER_FILE" | tr -d ' ')" "2"
assert_eq "A の記録 mtime は最新のみ" "$(led_mt "$A")" "101"

# 8. F1 回帰: 別 watcher プロセス (別セッション) が配達済みなら、こちらは再通知しない。
#    担当が A→B→A と移っても二重通知にならないことの中核。
C="/q/projects/pj_c/reports/worker3_report.yaml"
bash -c "export WATCH_LEDGER_FILE='$LEDGER_FILE'; source '$FUNCS_FILE'
         ledger_claim '$C' '200.0' > /dev/null && ledger_commit '$C' '200.0'"
rc_other=$?
assert_eq "別プロセスの配達は成功" "$rc_other" "0"
check "別プロセスが配達済みの report" "$C" "200.0" "SKIP"

# 9. 別プロセスが配達した後に report が更新されたら、こちらは通知する
check "別プロセス配達後に更新された report" "$C" "201.0" "NOTIFY"
ledger_commit "$C" "201.0"

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
ledger_commit "$K" "400.0"
check "配達済みになれば lease に関係なく skip" "$K" "400.0" "SKIP"

# 12. 古い mtime を掴んだ watcher が ledger を巻き戻さない
#     A が 101 を配達済みにした後、更新前の 100 を掴んだ B が claim しても通知しない。
E="/q/projects/pj_e/reports/worker1_report.yaml"
deliver "$E" "101.0"
check "古い mtime (更新前スナップショット) は claim しない" "$E" "100.0" "SKIP"
assert_eq "ledger は巻き戻らない" "$(led_mt "$E")" "101"

# 13. ledger_release: 送信に失敗したときのロールバック。
#     claim 前が未登録なら行ごと消え、次サイクルで再通知できる。
G="/q/projects/pj_g/reports/worker2_report.yaml"
prev_g=$(ledger_claim "$G" "300.0")
assert_eq "未登録からの claim は空の記録を返す" "$prev_g" ""
ledger_release "$G" "300.0" "$prev_g"
assert_eq "release で ledger から消える" "$(led_mt "$G")" ""
check "release 後は再通知できる" "$G" "300.0" "NOTIFY"

# 14. release は「削除」ではなく「claim 前の記録に戻す」。
#     行ごと消すと、直前に配達済みだった古い版の記録まで失われ、更新前の mtime を
#     掴んでいた別 watcher がその古い版を再 claim して二重通知できてしまう。
I="/q/projects/pj_i/reports/worker1_report.yaml"
deliver "$I" "100.0"
prev_i=$(ledger_claim "$I" "101.0")            # 新版 101 を claim (通知しようとした)
assert_eq "claim は上書き前の記録を返す" "$prev_i" "$(printf '100\t0')"
ledger_release "$I" "101.0" "$prev_i"          # 送信に失敗 -> ロールバック
assert_eq "release で 100 に戻る (行は消えない)" "$(led_mt "$I")" "100"
assert_eq "戻した記録は配達済みのまま" "$(led_ut "$I")" "0"
check "巻き戻った隙に古い版 100 を再 claim できない" "$I" "100.0" "SKIP"
check "新版 101 は次サイクルで再通知できる" "$I" "101.0" "NOTIFY"

# 15. release は他 watcher が新しい版で claim し直した記録を壊さない
ledger_commit "$I" "101.0"
ledger_release "$I" "100.0" ""
assert_eq "自分の claim でなければ release しない" "$(led_mt "$I")" "101"

# 16. 旧形式 (2 列 "<mtime>\t<path>") の ledger は配達済みとして読む
L="/q/projects/pj_l/reports/worker1_report.yaml"
printf '500\t%s\n' "$L" >> "$LEDGER_FILE"
assert_eq "旧形式の行は配達済みとして読む" "$(led_ut "$L")" "0"
check "旧形式で記録済みの report は再通知されない" "$L" "500.0" "SKIP"
check "旧形式より新しい mtime は通知する" "$L" "501.0" "NOTIFY"

# 17. ledger を書けない場合、commit / release は失敗 (非 0) を返す。
#     呼び出し側はログを出すだけでよい (lease 期限切れで再 claim される)。
#     root では chmod を迂回できるためスキップ。
if [ "$(id -u)" -ne 0 ]; then
    M="/q/projects/pj_m/reports/worker1_report.yaml"
    ledger_claim "$M" "600.0" > /dev/null
    RO_PARENT="$(dirname "$LEDGER_FILE")"
    chmod 500 "$RO_PARENT"
    if ledger_commit "$M" "600.0" 2>/dev/null; then commit_rc=0; else commit_rc=1; fi
    if ledger_release "$M" "600.0" "" 2>/dev/null; then rel_rc=0; else rel_rc=1; fi
    chmod 700 "$RO_PARENT"
    assert_eq "ledger を書けないとき commit は失敗を返す" "$commit_rc" "1"
    assert_eq "ledger を書けないとき release は失敗を返す" "$rel_rc" "1"
    assert_eq "失敗時は ledger を書き換えない" "$(led_mt "$M")" "600"
else
    echo "SKIP: commit/release 失敗テスト (root 実行では chmod を迂回できるため)"
fi

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
