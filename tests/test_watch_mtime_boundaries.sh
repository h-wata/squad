#!/bin/bash
# watch.sh の F2 (marker/report mtime 精度不一致) 修正に対する境界条件テスト。
#
# grep によるコード存在確認ではなく、watch.sh 本体から gt()/should_suppress() を
# そのまま source して実際の判定挙動を検証する (PR #21 Codex cross-review F4 対応)。
# should_suppress() より後 (while true ループ) は実行しないよう、ループ開始行より
# 前だけを抜き出して source する。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCH_SH="$SCRIPT_DIR/watch.sh"

# "OWNED=()" 以降は起動時ログ・BOOT_DELAY sleep・メインループを含むため、
# 関数定義 (gt/should_suppress 等) のみを含む手前までを source する。
CUTOFF_LINE="$(grep -n '^OWNED=()' "$WATCH_SH" | head -n1 | cut -d: -f1)"
if [ -z "$CUTOFF_LINE" ]; then
    echo "FAIL: watch.sh 内に 'OWNED=()' が見つからず、関数定義を安全に source できない"
    exit 1
fi

FUNCS_FILE="$(mktemp)"
trap 'rm -f "$FUNCS_FILE"' EXIT
head -n "$((CUTOFF_LINE - 1))" "$WATCH_SH" > "$FUNCS_FILE"

# shellcheck disable=SC1090
source "$FUNCS_FILE"

pass=0
fail=0

check() {
    local desc="$1" cutoff="$2" m="$3" expect="$4" got
    if should_suppress "$cutoff" "$m"; then
        got="SUPPRESS"
    else
        got="NOTIFY"
    fi
    if [ "$got" = "$expect" ]; then
        echo "PASS: $desc (cutoff=$cutoff m=$m -> $got)"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc (cutoff=$cutoff m=$m -> got $got, expect $expect)"
        fail=$((fail + 1))
    fi
}

# 1. marker と report が同一秒 (小数部有無を問わず) -> 通知側に倒すため NOTIFY
check "同一秒 (整数 cutoff, 整数 report)" "200" "200" "NOTIFY"
check "同一秒 (report に小数部あり、marker と同じ整数秒)" "200" "200.9" "NOTIFY"

# 2. marker より真に古い report (小数秒差含む) -> SUPPRESS
check "marker よりわずかに古い report (小数秒差)" "200" "199.9" "SUPPRESS"
check "marker より十分古い report" "250" "200.3" "SUPPRESS"

# 3. marker より真に新しい report (小数秒差含む) -> NOTIFY
check "marker よりわずかに新しい report (小数秒差)" "200" "200.1" "NOTIFY"
check "marker より十分新しい report" "200" "300.2" "NOTIFY"

# 4. Codex review 記載の再現ケース: marker=100.9(整数化で100), report=100.1
#    (実際は report の方が古いが、整数秒精度では同一秒 -> 通知側に倒すため NOTIFY)
check "review 再現: marker=100.9(->100), report=100.1" "100" "100.1" "NOTIFY"

# 5. 秒精度しかない mtime (小数部が無い) でも正しく判定できること
check "秒精度のみ: 古い report" "500" "499" "SUPPRESS"
check "秒精度のみ: 同一秒 report" "500" "500" "NOTIFY"
check "秒精度のみ: 新しい report" "500" "501" "NOTIFY"

# 6. cutoff が空 (初回担当でない/取得失敗) の場合は常に NOTIFY (握り潰さない)
check "cutoff 未設定" "" "123.456" "NOTIFY"

echo "---"
echo "pass=$pass fail=$fail"
[ "$fail" -eq 0 ]
