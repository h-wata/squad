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
DISCOVERY_INTERVAL="${WATCH_DISCOVERY_INTERVAL:-900}"  # 仕事の発見走査の間隔 (既定 15分)
DISCOVERY_MAX="${WATCH_DISCOVERY_MAX:-10}"             # 1サイクルで inbox に積む新規上限
SWEEP_INTERVAL="${WATCH_SWEEP_INTERVAL:-14400}"        # 新規ゼロ時の周回レビュー間隔 (既定 4h)
SEEN_FILE="$QUEUE_DIR/.discovery_seen"                 # 既知候補のキー集合 (再起動跨ぎで永続)
INBOX_FILE="$QUEUE_DIR/_inbox.md"                      # triage inbox
GC_INTERVAL="${WATCH_GC_INTERVAL:-1800}"               # merged worktree GC の間隔 (既定 30分)
WORKTREE_GLOB="${WATCH_WORKTREE_GLOB:-/home/gisen/work/*-wt-*}"  # GC 対象 worktree の glob

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

# ---- Discovery: 定期的に仕事を発見 → triage inbox → Dispatcher 自動起票 ----
# 注: watcher(bash) は「発見 + dedup + inbox 積み + Dispatcher nudge」まで。
# task YAML 生成と空き worker 割当は Dispatcher(Claude) が nudge を受けて自律処理する。
# 以下 disc_* は run_discovery の local (pj/repo/gh_repo/labels/todo_paths/added/baseline)
# を bash の動的スコープ経由で参照する。

add_candidate() {
    local key="$1" source="$2" pj="$3" desc="$4"
    grep -qxF "$key" "$SEEN_FILE" 2>/dev/null && return 1
    if [ "${baseline:-0}" -eq 1 ]; then
        echo "$key" >> "$SEEN_FILE"        # 既存 backlog は既知化のみ (通知しない)
        return 0
    fi
    [ "${added:-0}" -ge "$DISCOVERY_MAX" ] && return 1
    echo "$key" >> "$SEEN_FILE"
    echo "- [ ] $(date '+%Y-%m-%dT%H:%M:%S%z') [$source] ${pj}: ${desc}  \`${key}\`" >> "$INBOX_FILE"
    added=$(( ${added:-0} + 1 ))
    return 0
}

disc_issues() {
    local label_args=() l
    if [ -n "$labels" ]; then
        IFS=',' read -ra _L <<< "$labels"
        for l in "${_L[@]}"; do label_args+=(--label "$l"); done
    fi
    local num title
    while IFS=$'\t' read -r num title; do
        [ -z "$num" ] && continue
        add_candidate "${pj}:issue:${gh_repo}#${num}" issue "$pj" "Issue #${num}: ${title}"
    done < <(timeout 30 gh issue list -R "$gh_repo" --state open "${label_args[@]}" --limit 30 --json number,title 2>/dev/null \
        | python3 -c 'import json,sys
try:
  for i in json.load(sys.stdin): print(str(i["number"])+"\t"+i["title"])
except Exception: pass' 2>/dev/null)
}

disc_pr() {
    local num rd draft title
    while IFS=$'\t' read -r num rd draft title; do
        [ -z "$num" ] && continue
        [ "$draft" = "True" ] && continue
        case "$rd" in APPROVED|CHANGES_REQUESTED) continue ;; esac   # レビュー未完のみ
        add_candidate "${pj}:pr:${gh_repo}#${num}:review" pr "$pj" "PR #${num} レビュー待ち: ${title}"
    done < <(timeout 30 gh pr list -R "$gh_repo" --state open --limit 30 --json number,title,reviewDecision,isDraft 2>/dev/null \
        | python3 -c 'import json,sys
try:
  for i in json.load(sys.stdin): print(str(i["number"])+"\t"+(i.get("reviewDecision") or "")+"\t"+str(i.get("isDraft"))+"\t"+i["title"])
except Exception: pass' 2>/dev/null)
}

disc_ci() {
    local id wf br
    while IFS=$'\t' read -r id wf br; do
        [ -z "$id" ] && continue
        add_candidate "${pj}:ci:${id}" ci "$pj" "CI 失敗: ${wf} (${br})"
    done < <(timeout 30 gh run list -R "$gh_repo" --status failure --limit 10 --json databaseId,workflowName,headBranch 2>/dev/null \
        | python3 -c 'import json,sys
try:
  for i in json.load(sys.stdin): print(str(i["databaseId"])+"\t"+i["workflowName"]+"\t"+(i.get("headBranch") or ""))
except Exception: pass' 2>/dev/null)
}

disc_todo() {
    local p f rest line text h _P
    IFS=',' read -ra _P <<< "$todo_paths"
    for p in "${_P[@]}"; do
        while IFS= read -r m; do
            [ -z "$m" ] && continue
            f=${m%%:*}; rest=${m#*:}; line=${rest%%:*}; text=${rest#*:}
            text=$(printf '%s' "$text" | sed -E 's/^[[:space:]]*//; s/[[:space:]]+$//')
            h=$(printf '%s|%s' "$f" "$text" | cksum | cut -d' ' -f1)
            add_candidate "${pj}:todo:${h}" todo "$pj" "${f}:${line} ${text}"
        done < <(grep -rnE 'TODO|FIXME|XXX' "$repo/$p" 2>/dev/null | head -n 50)
    done
}

run_discovery() {
    local cfgs
    cfgs=$(find "$QUEUE_DIR/projects" -maxdepth 2 -name discovery.yaml 2>/dev/null)
    if [ -z "$cfgs" ]; then
        log "discovery: 設定なし (queue/projects/*/discovery.yaml を置くと有効化)"
        return
    fi
    mkdir -p "$QUEUE_DIR"
    local baseline=0
    [ ! -f "$SEEN_FILE" ] && baseline=1       # 初回 (SEEN 無し) は既存 backlog を黙って既知化
    touch "$SEEN_FILE"
    [ -f "$INBOX_FILE" ] || printf '# Discovery Triage Inbox\n\nwatcher が発見した未処理候補。Dispatcher が起票したら [x] にする。\n\n' > "$INBOX_FILE"
    local added=0 cfg

    while read -r cfg; do
        [ -z "$cfg" ] && continue
        local pj repo gh_repo labels todo_paths sources enabled
        pj=$(basename "$(dirname "$cfg")")
        dcfg() { grep -m1 -E "^$1:" "$cfg" 2>/dev/null | sed -E "s/^$1:[[:space:]]*//" | tr -d "\"'" ; }
        enabled=$(dcfg enabled); [ "$enabled" = "false" ] && continue
        repo=$(dcfg repo); gh_repo=$(dcfg gh_repo)
        labels=$(dcfg issue_labels); todo_paths=$(dcfg todo_paths)
        sources=$(dcfg sources); [ -z "$sources" ] && sources="issues,pr,ci,todo"

        case ",$sources," in *,issues,*) [ -n "$gh_repo" ] && disc_issues ;; esac
        case ",$sources," in *,pr,*)     [ -n "$gh_repo" ] && disc_pr ;; esac
        case ",$sources," in *,ci,*)     [ -n "$gh_repo" ] && disc_ci ;; esac
        case ",$sources," in *,todo,*)   [ -n "$repo" ] && [ -n "$todo_paths" ] && disc_todo ;; esac
    done <<< "$cfgs"

    if [ "$baseline" -eq 1 ]; then
        log "discovery: baseline 完了 (既存 backlog を既知化、通知なし)"
        return
    fi
    if [ "$added" -gt 0 ]; then
        log "discovery: 新規候補 ${added} 件 -> inbox + Dispatcher 通知"
        notify_dispatcher "[DISCOVERY] 新規候補 ${added} 件を ${INBOX_FILE} に追加。空き worker に自動起票してください (task-yaml-author → 通知)。merge gate は人間が維持。"
        return
    fi
    # 新規ゼロ: idle を遊ばせず、throttle 付きで「一通りレビュー(sweep)」を投げる
    local now2; now2=$(date +%s)
    if [ $(( now2 - LAST_SWEEP )) -ge "$SWEEP_INTERVAL" ]; then
        echo "- [ ] $(date '+%Y-%m-%dT%H:%M:%S%z') [sweep] all: 新規タスクなし。既存コード/open PR/backlog の一通りレビュー・監査  \`sweep:${now2}\`" >> "$INBOX_FILE"
        log "discovery: 新規なし -> [SWEEP] 周回レビューを inbox 投入"
        notify_dispatcher "[SWEEP] 新規タスクなし。空き worker がいれば既存コード/open PR/backlog の一通りレビュー・監査を1件だけ割り当ててください (全員稼働中なら何もしない)。"
        LAST_SWEEP="$now2"
    else
        log "discovery: 新規なし (self-archive, 次 sweep まで約 $(( (SWEEP_INTERVAL - (now2 - LAST_SWEEP)) / 60 )) 分)"
    fi
}

# ---- worktree GC: merged かつ clean な専用 worktree だけ自動掛除 ----
# dirty(未コミット変更) / 未merge / 判定不能(fetch失敗) は絶対に触らない。
gc_worktrees() {
    local wt main branch removed=0 skipped=0
    for wt in $WORKTREE_GLOB; do
        wt="${wt%/}"
        [ -d "$wt" ] || continue
        git -C "$wt" rev-parse --git-dir >/dev/null 2>&1 || continue
        main=$(git -C "$wt" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')
        [ -z "$main" ] && continue
        [ "$(realpath "$wt" 2>/dev/null)" = "$(realpath "$main" 2>/dev/null)" ] && continue  # main worktree は対象外
        # 未コミット変更があれば触らない
        if [ -n "$(git -C "$wt" status --porcelain 2>/dev/null)" ]; then
            skipped=$((skipped+1)); log "gc skip (dirty): $wt"; continue
        fi
        branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null)
        # origin/main を更新できなければ merged 判定不能 → skip
        if ! git -C "$main" fetch -q origin main 2>/dev/null; then
            skipped=$((skipped+1)); log "gc skip (fetch fail): $wt"; continue
        fi
        if git -C "$main" branch --merged origin/main --format '%(refname:short)' 2>/dev/null | grep -qx "$branch"; then
            if git -C "$main" worktree remove "$wt" 2>/dev/null; then
                removed=$((removed+1)); log "gc removed (merged+clean): $wt [$branch]"
            else
                skipped=$((skipped+1)); log "gc skip (remove failed): $wt"
            fi
        else
            skipped=$((skipped+1)); log "gc skip (not merged): $wt [$branch]"
        fi
    done
    [ "$removed" -gt 0 ] && log "gc: ${removed} worktree を掛除 (skip ${skipped})"
}

declare -A REPORT_SEEN     # report path -> mtime
declare -A PANE_HASH       # worker -> 直近 pane ハッシュ
declare -A PANE_STALL      # worker -> 無変化カウント
declare -A STALL_NOTIFIED  # worker -> 通報済みタスク mtime

log "watcher start (session=$SESSION interval=${INTERVAL}s stall=${STALL_CYCLES} discovery=${DISCOVERY_INTERVAL}s sweep=${SWEEP_INTERVAL}s gc=${GC_INTERVAL}s boot_delay=${BOOT_DELAY}s)"
sleep "$BOOT_DELAY"

FIRST=1
LAST_DISCOVERY=0
LAST_SWEEP=0
LAST_GC=0
while true; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        log "session '$SESSION' が無いので終了"
        break
    fi

    # --- 1. report-bridge: 新規/更新された report を Dispatcher へ橋渡し ---
    #   status: blocked (検証ゲート 3 回 fail) は [INBOX] 付きで人間判断に回す。
    while IFS=$'\t' read -r m f; do
        [ -z "$f" ] && continue
        if [ "${REPORT_SEEN[$f]:-}" != "$m" ]; then
            REPORT_SEEN[$f]="$m"
            if [ "$FIRST" -eq 0 ]; then
                wnum=$(basename "$f" | grep -oE 'worker[0-9]+' | grep -oE '[0-9]+')
                kind=report; echo "$f" | grep -q '_review.yaml' && kind=review
                status=$(grep -m1 -E '^status:' "$f" 2>/dev/null | awk '{print $2}')
                if [ "$status" = "blocked" ]; then
                    log "report 検知(blocked): $f -> Dispatcher [INBOX] 通知"
                    notify_dispatcher "[INBOX] Worker${wnum} が blocked: 検証ゲート未通過。${f} の notes/verdict を確認し、ユーザーに優先報告してください。"
                else
                    log "report 検知: $f -> Dispatcher 通知"
                    notify_dispatcher "Worker${wnum} ${kind}: ${f} を確認してください。(watcher 自動橋渡し)"
                fi
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

    # --- 4. Discovery: 低頻度で仕事を発見し inbox へ (新規ゼロ時は throttle 付き sweep) ---
    now_ts=$(date +%s)
    if [ $(( now_ts - LAST_DISCOVERY )) -ge "$DISCOVERY_INTERVAL" ]; then
        run_discovery
        LAST_DISCOVERY="$now_ts"
    fi

    # --- 5. worktree GC: merged+clean な専用 worktree を掛除 ---
    if [ $(( now_ts - LAST_GC )) -ge "$GC_INTERVAL" ]; then
        gc_worktrees
        LAST_GC="$now_ts"
    fi

    FIRST=0
    sleep "$INTERVAL"
done
