#!/bin/bash
# watch.sh の report 通知済み ledger (Issue #22 / PR #21 cross-review F1・F3 の根本対応)
# に対する挙動テスト。
#
# grep によるコード存在確認ではなく、watch.sh 本体から ledger_claim() をそのまま
# source して実際の判定挙動を検証する。ledger_claim() より後 (while true ループ) は
# 実行しないよう、ループ開始行より前だけを抜き出して source する。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCH_SH="$SCRIPT_DIR/watch.sh"

TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

# ledger は実運用の queue/ ではなくテスト用の一時ファイルに向ける
export WATCH_LEDGER_FILE="$TMPDIR_T/ledger"

# "OWNED=()" 以降は起動時ログ・BOOT_DELAY sleep・メインループを含むため、
# 関数定義 (gt/ledger_claim 等) のみを含む手前までを source する。
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

check() {
    local desc="$1" f="$2" m="$3" expect="$4" got
    if ledger_claim "$f" "$m" > /dev/null; then   # stdout は claim 前の mtime
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
assert_eq "ledger ファイルが生成される" "$([ -f "$WATCH_LEDGER_FILE" ] && echo yes || echo no)" "yes"

# 2. 同じ mtime での再 claim -> 通知しない (毎サイクルの再通知が起きない)
check "同一 mtime の再 claim" "$A" "100.5" "SKIP"

# 3. 小数部だけが違う同一秒 -> 同一とみなして通知しない
#    (find %T@ の小数部は取得経路で揺れうるため整数秒に切り捨てて比較する)
check "同一秒・小数部のみ差異 (100.5 -> 100.9)" "$A" "100.9" "SKIP"
check "同一秒・小数部なし (100.5 -> 100)" "$A" "100" "SKIP"

# 4. report が更新されて mtime が進んだら再通知する
check "mtime が進んだ report" "$A" "101.0" "NOTIFY"
check "進んだ mtime の再 claim" "$A" "101.2" "SKIP"

# 5. 別 path は独立に判定される
check "別 report の初回 claim" "$B" "100.5" "NOTIFY"
check "別 report の再 claim" "$B" "100.5" "SKIP"

# 6. 1 path につき 1 行しか持たない (ledger が単調増加しない)
assert_eq "ledger 行数 (path 数と一致)" "$(wc -l < "$WATCH_LEDGER_FILE" | tr -d ' ')" "2"
assert_eq "A の記録 mtime は最新のみ" \
    "$(awk -F'\t' -v p="$A" '$2==p{print $1}' "$WATCH_LEDGER_FILE" | tr '\n' ',')" "101,"

# 7. F1 回帰: 別 watcher プロセス (別セッション) が通知済みなら、こちらは再通知しない。
#    担当が A→B→A と移っても二重通知にならないことの中核。
C="/q/projects/pj_c/reports/worker3_report.yaml"
bash -c "export WATCH_LEDGER_FILE='$WATCH_LEDGER_FILE'; source '$FUNCS_FILE'; ledger_claim '$C' '200.0'"
rc_other=$?
assert_eq "別プロセスの初回 claim は成功" "$rc_other" "0"
check "別プロセスが通知済みの report" "$C" "200.0" "SKIP"

# 8. 別プロセスが通知した後に report が更新されたら、こちらは通知する
check "別プロセス通知後に更新された report" "$C" "201.0" "NOTIFY"

# 9. 同時 claim (flock による直列化): 同じ path/mtime を並行して claim しても
#    成功するのは 1 プロセスだけ。
D="/q/projects/pj_d/reports/worker1_report.yaml"
RES_DIR="$TMPDIR_T/race"
mkdir -p "$RES_DIR"
for i in 1 2 3 4 5; do
    bash -c "export WATCH_LEDGER_FILE='$WATCH_LEDGER_FILE'
             source '$FUNCS_FILE'
             ledger_claim '$D' '300.0' && echo ok > '$RES_DIR/$i'" &
done
wait
assert_eq "並行 claim 5 本のうち成功は 1 本のみ" "$(find "$RES_DIR" -type f | wc -l | tr -d ' ')" "1"

# 10. ledger_baseline_seed は担当 project だけでなく queue/projects 配下の全 report を
#     登録する。後から起動した別セッションの watcher は「ledger がある = seed 済み」と
#     しか判断しないため、担当分しか seed しないとその watcher が自分の担当 project の
#     過去 report を一斉通知してしまう (Codex review P1 の回帰テスト)。
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
check "非担当 project の既存 report は再通知されない" \
    "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml" \
    "$(find "$QUEUE_DIR/projects/pj_b/reports/worker2_report.yaml" -printf '%T@')" "SKIP"

# 11. seed 済み ledger がある状態で seed を再実行しても上書きしない (先着優先)
before="$(cat "$LEDGER_FILE")"
echo "status: completed" > "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml"
ledger_baseline_seed > /dev/null
assert_eq "ledger 済みなら seed は何もしない" "$(cat "$LEDGER_FILE")" "$before"
check "seed 後に書かれた report は通知される" \
    "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml" \
    "$(find "$QUEUE_DIR/projects/pj_a/reports/worker4_report.yaml" -printf '%T@')" "NOTIFY"

# 12. 古い mtime を掴んだ watcher が ledger を巻き戻さない (Codex review P2 の回帰)
#     A が 101 を claim した後、更新前の 100 を掴んだ B が claim しようとしても通知せず、
#     ledger も 101 のまま。等値だけを弾く実装だと 100 に巻き戻り、次サイクルで 101 が
#     再 claim されて同じ版が二重通知される。
E="/q/projects/pj_e/reports/worker1_report.yaml"
check "新しい mtime を claim" "$E" "101.0" "NOTIFY"
check "古い mtime (更新前スナップショット) は claim しない" "$E" "100.0" "SKIP"
assert_eq "ledger は巻き戻らない" \
    "$(awk -F'\t' -v p="$E" '$2==p{print $1}' "$LEDGER_FILE")" "101"
check "巻き戻っていないので同じ版は再通知されない" "$E" "101.0" "SKIP"

# 13. ledger_release: 通知に失敗したときのロールバック (Codex review P1 の回帰)
#     claim 済みの行が消え、次サイクルで同じ report を再通知できる。
G="/q/projects/pj_g/reports/worker2_report.yaml"
check "claim する" "$G" "300.0" "NOTIFY"
ledger_release "$G" "300.0" ""
assert_eq "release で ledger から消える (claim 前が未登録なら)" \
    "$(awk -F'\t' -v p="$G" '$2==p{print $1}' "$LEDGER_FILE")" ""
check "release 後は再通知できる" "$G" "300.0" "NOTIFY"

# 14. release は他 watcher が新しい mtime で claim し直した記録を消さない
ledger_claim "$G" "301.0" > /dev/null
ledger_release "$G" "300.0" ""
assert_eq "自分の claim でなければ release しない" \
    "$(awk -F'\t' -v p="$G" '$2==p{print $1}' "$LEDGER_FILE")" "301"

# 14b. release は「削除」ではなく「claim 前の値に戻す」(Codex review blocking 1 の回帰)。
#      行ごと消すと、直前に通知済みだった古い版の記録まで失われ、更新前の mtime を
#      掴んでいた別 watcher がその古い版を再 claim して二重通知できてしまう。
I="/q/projects/pj_i/reports/worker1_report.yaml"
check "旧版 100 を通知済みにする" "$I" "100.0" "NOTIFY"
prev_i=$(ledger_claim "$I" "101.0")            # 新版 101 を claim (通知しようとした)
assert_eq "claim は上書き前の mtime を返す" "$prev_i" "100"
ledger_release "$I" "101.0" "$prev_i"          # 通知に失敗 -> ロールバック
assert_eq "release で 100 に戻る (行は消えない)" \
    "$(awk -F'\t' -v p="$I" '$2==p{print $1}' "$LEDGER_FILE")" "100"
check "巻き戻った隙に古い版 100 を再 claim できない" "$I" "100.0" "SKIP"
check "新版 101 は次サイクルで再通知できる" "$I" "101.0" "NOTIFY"

# 14c. ledger を操作できない場合、release は失敗 (非 0) を返す。
#      呼び出し側はこれを見て再送キューに積む。root では chmod を迂回できるためスキップ。
if [ "$(id -u)" -ne 0 ]; then
    J="/q/projects/pj_j/reports/worker1_report.yaml"
    ledger_claim "$J" "500.0" > /dev/null
    chmod 400 "$LEDGER_FILE"
    RO_PARENT="$(dirname "$LEDGER_FILE")"
    chmod 500 "$RO_PARENT"
    if ledger_release "$J" "500.0" "" 2>/dev/null; then rel_rc=0; else rel_rc=1; fi
    chmod 700 "$RO_PARENT"
    chmod 600 "$LEDGER_FILE"
    assert_eq "ledger を書けないとき release は失敗を返す" "$rel_rc" "1"
    assert_eq "失敗時は ledger を書き換えない" \
        "$(awk -F'\t' -v p="$J" '$2==p{print $1}' "$LEDGER_FILE")" "500"
else
    echo "SKIP: release 失敗テスト (root 実行では chmod を迂回できるため)"
fi

# 15. lock file を開けない異常時は「通知済み」ではなく「通知する」側に倒す
#     (読み取り専用ディレクトリ等で全 report が握り潰されるのを防ぐ。Codex review P1)
#     初回 claim も NOTIFY になるため、それだけでは分岐を通った証明にならない。
#     同じ mtime を 2 回 claim しても両方 NOTIFY = ledger に何も残っていない、まで見る
#     (正常時なら 2 回目は SKIP になる)。root では chmod を迂回できるためスキップ。
if [ "$(id -u)" -ne 0 ]; then
    RO_DIR="$TMPDIR_T/readonly"
    mkdir -p "$RO_DIR"
    SAVED_LEDGER="$LEDGER_FILE"
    SAVED_LOCK="$LEDGER_LOCK"
    LEDGER_FILE="$RO_DIR/.report_ledger"
    LEDGER_LOCK="$RO_DIR/.report_ledger.lock"
    H="/q/projects/pj_h/reports/worker1_report.yaml"
    chmod 500 "$RO_DIR"
    exec 3>&2 2>/dev/null   # リダイレクト失敗の "Permission denied" はテスト出力から隠す
    check "lock file を開けない場合は通知する" "$H" "400.0" "NOTIFY"
    check "lock file を開けない場合は 2 回目も通知する (握り潰さない)" "$H" "400.0" "NOTIFY"
    exec 2>&3 3>&-
    chmod 700 "$RO_DIR"
    assert_eq "lock を開けなかったので ledger 自体が作られない" \
        "$([ -f "$LEDGER_FILE" ] && echo yes || echo no)" "no"
    LEDGER_FILE="$SAVED_LEDGER"
    LEDGER_LOCK="$SAVED_LOCK"
else
    echo "SKIP: lock file オープン失敗テスト (root 実行では chmod を迂回できるため)"
fi

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
