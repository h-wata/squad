#!/bin/bash
# watch.sh — tmux マルチエージェント worker 監視デーモン
#
# 役割:
#   1. report-bridge: worker が reports/*.yaml を書いたら Dispatcher へ自動通知。
#      Codex が send-keys を忘れて/止まっても、report を書きさえすれば Dispatcher に確実に届く。
#   2. 承認オートアンサー: 残存する承認/権限プロンプトを自動受理 (bypass の保険)。
#   3. 停止検知: タスク未報告かつ pane 無変化が続いたら Dispatcher へ通報。
#
# 起動: start.sh が nohup で自動起動。手動: ./watch.sh &
# 設定 (env): WATCH_INTERVAL(s) / WATCH_STALL_CYCLES / WATCH_BOOT_DELAY(s)
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${SESSION_NAME:-ros-agents}"
QUEUE_DIR="$SCRIPT_DIR/queue"
DISPATCHER="$SESSION:0.0"
INTERVAL="${WATCH_INTERVAL:-15}"
STALL_CYCLES="${WATCH_STALL_CYCLES:-4}"   # 無変化がこの回数続いたら停止疑い (既定: 15s*4=60s)
BOOT_DELAY="${WATCH_BOOT_DELAY:-12}"      # agent 起動待ち

# worker 番号 -> tmux pane
pane_for() {
    case "$1" in
        1) echo "$SESSION:0.1" ;;
        2) echo "$SESSION:0.2" ;;
        3) echo "$SESSION:0.3" ;;
        4) echo "$SESSION:0.6" ;;
    esac
}

# 承認 / 権限プロンプト判定 (Claude permission / Codex approval / trust prompt)
APPROVAL_RE='Do you want to proceed|Allow this|Approve|approve|\(y/n\)|press y|1\. Yes|Yes, (and )?(proceed|allow|continue)|Trust (this|the)|allow command|Run command\?|Grant'

log() { echo "[$(date '+%H:%M:%S')] $*"; }

notify_dispatcher() {
    local msg="$1"
    tmux send-keys -t "$DISPATCHER" "$msg"
    sleep 0.5
    tmux send-keys -t "$DISPATCHER" Enter
    sleep 0.3
}

auto_answer() {
    # 承認プロンプトに既定(Yes)で応答する。"(y/n)" 形式は y、それ以外は Enter。
    local pane="$1" cap="$2"
    if echo "$cap" | grep -qiE '\(y/n\)|press y'; then
        tmux send-keys -t "$pane" "y"
        sleep 0.3
        tmux send-keys -t "$pane" Enter
    else
        tmux send-keys -t "$pane" Enter
    fi
    sleep 0.3
}

# float epoch 比較 a>b
gt() { awk -v a="$1" -v b="${2:-0}" 'BEGIN{exit !(a>b)}'; }

newest_mtime() {
    # 指定 find 述語にマッチするファイルの最新 mtime (epoch.float)。無ければ空。
    find "$QUEUE_DIR/projects" "$@" -printf '%T@\n' 2>/dev/null | sort -nr | head -n1
}

declare -A REPORT_SEEN     # report path -> mtime
declare -A PANE_HASH       # worker -> 直近 pane ハッシュ
declare -A PANE_STALL      # worker -> 無変化カウント
declare -A STALL_NOTIFIED  # worker -> 通報済みタスク mtime

log "watcher start (session=$SESSION interval=${INTERVAL}s stall=${STALL_CYCLES} boot_delay=${BOOT_DELAY}s)"
sleep "$BOOT_DELAY"

FIRST=1
while true; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        log "session '$SESSION' が無いので終了"
        break
    fi

    # --- 1. report-bridge: 新規/更新された report を Dispatcher へ橋渡し ---
    while IFS=$'\t' read -r m f; do
        [ -z "$f" ] && continue
        if [ "${REPORT_SEEN[$f]:-}" != "$m" ]; then
            REPORT_SEEN[$f]="$m"
            if [ "$FIRST" -eq 0 ]; then
                wnum=$(basename "$f" | grep -oE 'worker[0-9]+' | grep -oE '[0-9]+')
                kind=report; echo "$f" | grep -q '_review.yaml' && kind=review
                log "report 検知: $f -> Dispatcher 通知"
                notify_dispatcher "Worker${wnum} ${kind}: ${f} を確認してください。(watcher 自動橋渡し)"
            fi
        fi
    done < <(find "$QUEUE_DIR/projects" \
        \( -path '*/reports/worker*_report.yaml' -o -path '*/reports/worker*_review.yaml' \) \
        -printf '%T@\t%p\n' 2>/dev/null)

    # --- 2 & 3. 承認オートアンサー + 停止検知 (worker 1-4) ---
    for N in 1 2 3 4; do
        pane="$(pane_for "$N")"
        task_m="$(newest_mtime -path "*/tasks/worker${N}.yaml")"
        rep_m="$(newest_mtime \( -path "*/reports/worker${N}_report.yaml" -o -path "*/reports/worker${N}_review.yaml" \))"

        # タスク未報告 (pending) か?
        pending=0
        if [ -n "$task_m" ] && { [ -z "$rep_m" ] || gt "$task_m" "$rep_m"; }; then
            pending=1
        fi

        if [ "$pending" -eq 0 ]; then
            PANE_STALL[$N]=0
            continue
        fi

        cap="$(tmux capture-pane -p -t "$pane" 2>/dev/null | tail -n 40)"

        # 承認プロンプトがあれば自動受理
        if echo "$cap" | grep -qiE "$APPROVAL_RE"; then
            log "Worker${N}: 承認プロンプト検知 -> 自動受理"
            auto_answer "$pane" "$cap"
            PANE_STALL[$N]=0
            PANE_HASH[$N]=""
            continue
        fi

        # 無変化が続くか?
        h="$(printf '%s' "$cap" | cksum | cut -d' ' -f1)"
        if [ "${PANE_HASH[$N]:-}" = "$h" ]; then
            PANE_STALL[$N]=$(( ${PANE_STALL[$N]:-0} + 1 ))
        else
            PANE_HASH[$N]="$h"
            PANE_STALL[$N]=0
        fi

        if [ "${PANE_STALL[$N]}" -ge "$STALL_CYCLES" ] && [ "${STALL_NOTIFIED[$N]:-}" != "$task_m" ]; then
            secs=$(( INTERVAL * STALL_CYCLES ))
            log "Worker${N}: 約${secs}s 停止 (タスク未報告) -> Dispatcher 通報"
            notify_dispatcher "Worker${N} が約${secs}s 停止しています (タスク割当済・report 未出力)。pane ${pane#"$SESSION":} を確認し、必要なら再送/clear してください。"
            STALL_NOTIFIED[$N]="$task_m"
        fi
    done

    FIRST=0
    sleep "$INTERVAL"
done
