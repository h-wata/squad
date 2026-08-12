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
# 設定 (env): WATCH_INTERVAL(s) / WATCH_STALL_CYCLES / WATCH_STALL_RESUME_CYCLES / WATCH_BOOT_DELAY(s)
#
# 複数セッション並行運用 (SQUAD_SESSION を変えて start.sh を複数起動する場合):
#   queue/projects/<pj>/.squad_session に担当セッション名を1行書くと、その project の
#   report-bridge / 停止検知 / discovery はそのセッションの watcher だけが行う。
#   マーカーが無い project は SQUAD_DEFAULT_OWNER (既定 ros-agents) の担当。
#   report の「通知済み」状態は queue/.report_ledger (全 watcher 共有・永続) で管理し、
#   flock で直列化する。担当セッションが移っても通知済み判定はそのまま引き継がれる。
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${SQUAD_SESSION:-ros-agents}"
QUEUE_DIR="${WATCH_QUEUE_DIR:-$SCRIPT_DIR/queue}"   # 上書きはテスト用 (実運用では既定のまま)
DISPATCHER="$SESSION:0.0"
INTERVAL="${WATCH_INTERVAL:-15}"
STALL_CYCLES="${WATCH_STALL_CYCLES:-4}"   # 無変化がこの回数続いたら停止疑い (既定: 15s*4=60s)
STALL_RESUME_CYCLES="${WATCH_STALL_RESUME_CYCLES:-2}"  # 再停止再通報の解禁に必要な連続活動再開サイクル数 (既定: 15s*2=30s。初回停止判定60sの半分、かつ1サイクルの偶然の揺れでは解禁されない値)
BOOT_DELAY="${WATCH_BOOT_DELAY:-12}"      # agent 起動待ち
DISCOVERY_INTERVAL="${WATCH_DISCOVERY_INTERVAL:-900}"  # 仕事の発見走査の間隔 (既定 15分)
DISCOVERY_MAX="${WATCH_DISCOVERY_MAX:-10}"             # 1サイクルで inbox に積む新規上限
SWEEP_INTERVAL="${WATCH_SWEEP_INTERVAL:-14400}"        # 新規ゼロ時の周回レビュー間隔 (既定 4h)
# 複数セッション並行運用: project ごとの担当セッションを queue/projects/<pj>/.squad_session
# (1行のセッション名) で割り当てる。マーカーが無い project は DEFAULT_OWNER の担当。
# 各 watcher は自分の担当 project しか監視しないため、通知が他セッションへ漏れない。
DEFAULT_OWNER="${SQUAD_DEFAULT_OWNER:-ros-agents}"
# discovery の seen/inbox もセッションごとに分離 (既定セッションは従来のファイル名を維持)
if [ "$SESSION" = "$DEFAULT_OWNER" ]; then
    SEEN_FILE="$QUEUE_DIR/.discovery_seen"             # 既知候補のキー集合 (再起動跨ぎで永続)
    INBOX_FILE="$QUEUE_DIR/_inbox.md"                  # triage inbox
else
    SEEN_FILE="$QUEUE_DIR/.discovery_seen.$SESSION"
    INBOX_FILE="$QUEUE_DIR/_inbox.$SESSION.md"
fi
# report 通知済み ledger (全セッションの watcher で共有・永続)。
# セッションごとに分けてはいけない: 担当が A→B→A と移っても「B が通知済み」を
# A が参照できることが目的 (Issue #22 / PR #21 cross-review F1)。
LEDGER_FILE="${WATCH_LEDGER_FILE:-$QUEUE_DIR/.report_ledger}"
LEDGER_LOCK="${LEDGER_FILE}.lock"
GC_INTERVAL="${WATCH_GC_INTERVAL:-1800}"               # merged worktree GC の間隔 (既定 30分)
WORKTREE_GLOB="${WATCH_WORKTREE_GLOB:-$(dirname "$SCRIPT_DIR")/*-wt-*}"  # GC 対象 worktree の glob (既定: リポジトリの親 dir)

# マーカー未設定 project の可視化 (起動時 1 回のみ)。フォールバック挙動自体は変えない。
warn_missing_markers() {
    local d name
    for d in "$QUEUE_DIR/projects"/*/; do
        [ -d "$d" ] || continue
        name="$(basename "$d")"
        if [ ! -f "$d/.squad_session" ]; then
            log "[WARN] project ${name} has no .squad_session marker; falling back to default owner ${DEFAULT_OWNER}"
        fi
    done
}

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

# Dispatcher pane へ 1 メッセージ送る。send-keys が失敗したら 1 を返す
# (呼び出し側が「通知できなかった」ことを検知できるようにする)。
#
# 本文と Enter を 1 回の send-keys にまとめない: tmux 側で Enter が届かず次の
# メッセージと連結される事象があるため、間に sleep を挟んで別々に送る。
notify_dispatcher() {
    local msg="$1"
    # stderr は捨てない。discovery / sweep / stall 通報は戻り値を見ないため、
    # 握り潰すと「通報した」というログだけ残って実際には届いていない状態になる。
    tmux send-keys -t "$DISPATCHER" "$msg" || return 1
    sleep 0.5
    tmux send-keys -t "$DISPATCHER" Enter || return 1
    sleep 0.3
    return 0
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

# float epoch 比較 a>b。awk の double は約 16 桁しか保持できず、find %T@ の 20 桁
# (秒 10 桁 + ナノ秒 10 桁) では下位桁が落ちて「異なる mtime を同一以下」と誤判定する
# (PR #24 Claude review 6th #8)。整数部は数値で、小数部はゼロ詰めした固定長文字列の
# 辞書順で比較する (同じ長さの数字列なら辞書順 = 数値順)。
gt() {
    LC_ALL=C awk -v a="$1" -v b="${2:-0}" 'BEGIN{
        na = split(a, x, "."); nb = split(b, y, ".")
        ia = x[1] + 0; ib = y[1] + 0
        if (ia != ib) exit !(ia > ib)
        fa = (na > 1 ? x[2] : ""); fb = (nb > 1 ? y[2] : "")
        while (length(fa) < length(fb)) fa = fa "0"
        while (length(fb) < length(fa)) fb = fb "0"
        exit !(fa "" > fb "")
    }'
}

# flock の有無 (util-linux が無い環境ではロック無しで動作させる)
if command -v flock >/dev/null 2>&1; then HAVE_FLOCK=1; else HAVE_FLOCK=0; fi

# report ledger: report の配達状態を全 watcher 共有のファイルで管理する (Issue #22)。
# 1 report path あたり 1 行 "<mtime>\t<lease>\t<path>"。
#   lease 0                    = 配達済み (Dispatcher へ送信成功)
#   lease "<epoch 秒>:<nonce>" = 配達中 (誰かが claim して送信しようとしている)
#
# 「claim = 配達済み」にはしない (claim と送信成功の 2 段階にする)。claim した時点で
# 配達済みにすると、送信に失敗した report や、送信前に watcher が死んだ report が
# 「通知済み」として残り、二度と橋渡しされない。lease にしておけば、期限が切れた
# 時点で誰か (別セッションの watcher でもよい) が再び claim して配達をやり直せる。
# プロセスメモリ上の再送キューでは、watcher の再起動や project の担当変更で状態が
# 失われて同じ穴が残る (Codex review 3 巡目 blocking 1)。
#
#   ledger_claim <path> <mtime>   配達権を取れたら 0、他が配達済み/配達中なら 1。
#                                 成功時は "<claim token>\t<claim 前の mtime>\t<claim 前の lease>"
#                                 を stdout に返す (commit / rollback 用)。
#   ledger_commit <path> <mtime> <token>  送信成功後に「配達済み」へ確定する。
#   ledger_release <path> <token> <前 mtime> <前 lease>  送信失敗時に元の記録へ戻す。
#
# commit / release は「自分が書いた claim がまだ残っているか」を claim token
# (= claim 時に書いた lease 期限値) で照合してから更新する。mtime だけで照合すると、
# A の lease が切れた後に B が同じ mtime を再 claim した状況で、遅れて戻ってきた A が
# B の claim を勝手に commit したり、B の有効な claim を release で消したりできてしまう
# (PR #24 Codex review 4th round)。
#
# token は "<lease 期限 epoch 秒>:<nonce>" の形にする。期限値だけでは一意にならない:
# 新しい mtime の claim は既存 lease の期限を待たずに成立するため、担当が切り替わる
# 瞬間などに 2 つの watcher が同じ秒に同じ path を claim すると、期限値 (now+LEASE) が
# 一致してしまう (PR #24 Claude review #1)。nonce に PID と $RANDOM を混ぜることで
# プロセスをまたいでも token が衝突しないようにする。lease 切れ判定では ':' より前
# (期限 epoch 秒) だけを見る。
#
# commit / release に失敗しても (ledger が書けない等)、lease 期限切れで再び claim
# されるので通知が永久に消えることはない。副作用は「約 LEDGER_LEASE 秒後に再通知」。
#
# 記録済み mtime より「真に新しい」場合だけ新しい版として claim する (等値ではなく
# 単調増加で判定)。等値だけを弾くと、A が mtime 101 を claim した直後に、更新前の
# 100 を掴んだ B が ledger を 100 に巻き戻して通知し、次のサイクルで 101 が再度
# claim されて同じ版が二重通知される。副作用として、記録済みの mtime を下回る report は
# 通知されない: `cp -p` での復元など過去 mtime を保持した置き換えや、システム時計が
# 巻き戻った後に書かれた report が該当する。report は常に同一マシンで現在時刻のまま
# 書かれる、という前提に依存している。この抑止は気づけないと困るので、path ごとに
# 1 回だけ WARN をログに出す (LEDGER_RC_STALE)。
#
# ledger にアクセスできない異常時 (lock file を開けない・書き込めない) は「未配達」
# 側に倒す。握り潰し (気づけない) より再通知 (煩いが気づける) を選ぶ、という
# watch.sh 全体の方針に合わせる。このため subshell の「配達済み/配達中」だけを 9 と
# いう専用の終了コードで表し、リダイレクト失敗など他の異常 (bash が返す 1) と区別する。
#
# mtime は find %T@ が返す文字列 (小数秒込み) をそのまま記録・比較する。整数秒に
# 切り捨てると、同一秒内に書き直された report (例: in_progress -> blocked) が同じ版と
# みなされて恒久的に握り潰される (PR #24 Claude review #2)。この経路の mtime は
# すべて同じ find %T@ から得ているので、取得経路の違いによる精度の揺れは起きない。
# 同一版かどうかは文字列一致、新旧の判定だけ数値比較 (gt) で行う。
LEDGER_RC_SEEN=9      # subshell 内で「配達済み/配達中なので claim しない」終了コード
LEDGER_RC_STALE=10    # 記録より古い mtime (巻き戻し) なので claim しない終了コード
LEDGER_LEASE="${WATCH_LEDGER_LEASE:-60}"   # 配達 lease の有効秒数

# path の現在の記録を "<mtime>\t<lease>" で返す (未登録なら空)。
# 3 列に満たない行は無視する (この ledger 形式は本 PR が初出で、旧形式は存在しない。
# 中間リビジョンの 2 列形式は mtime が整数秒で書かれており、小数秒込みの現行 mtime と
# 突き合わせても正しく「配達済み」と読めないため、互換で読む価値がない —
# PR #24 Claude review 6th #3。もし残っていたら ledger ごと削除して作り直すこと)。
_ledger_lookup() {
    awk -F'\t' -v p="$1" '
        NF >= 3 && $3 == p { mt = $1; ut = $2 }
        END { if (mt != "") printf "%s\t%s", mt, ut }
    ' "$LEDGER_FILE" 2>/dev/null
}

# path の行を除いた ledger を出力する (書き換えの土台)。
_ledger_without() {
    awk -F'\t' -v p="$1" 'NF < 3 || $3 != p' "$LEDGER_FILE" 2>/dev/null
}

# path の行を差し替えた ledger 全体を一時ファイルに作り、atomic に置き換える。
# mtime (第 2 引数) が空なら行を削除する。成功で 0、失敗なら何も変更せず 1 を返す。
#
# 「既存 ledger を読めなければ何も変更しない」ことが重要: 読めないまま新しい行だけを
# 書いた一時ファイルを mv すると、他 report の配達済み記録が全消失し、queue 全体の
# 一斉再通知になる (PR #24 Claude review 6th #1)。グループコマンドの終了ステータスは
# 最後のコマンドのものになるため、awk (_ledger_without) の失敗を個別に判定する。
_ledger_rewrite() {
    local f="$1" mt="${2:-}" ut="${3:-}" tmp="${LEDGER_FILE}.tmp.$$"
    if [ -f "$LEDGER_FILE" ]; then
        _ledger_without "$f" > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    else
        : > "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    fi
    if [ -n "$mt" ]; then
        printf '%s\t%s\t%s\n' "$mt" "$ut" "$f" >> "$tmp" 2>/dev/null || { rm -f "$tmp"; return 1; }
    fi
    mv -f "$tmp" "$LEDGER_FILE" 2>/dev/null || { rm -f "$tmp"; return 1; }
}

ledger_claim() {
    local f="$1" m="$2" rc
    (
        if [ "$HAVE_FLOCK" -eq 1 ]; then
            # ロックを取れなかった場合は「未配達」側に倒す
            flock -w 5 9 || exit 0
        fi
        local rec mt ut now token
        rec="$(_ledger_lookup "$f")"
        mt=""; ut=""
        [ -n "$rec" ] && IFS=$'\t' read -r mt ut <<< "$rec"
        now=$(date +%s)
        if [ -n "$mt" ]; then
            if [ "$m" = "$mt" ]; then
                # 同じ版。配達済み (lease 0)、または他 watcher が配達中なら触らない。
                # lease が切れていれば配達やり直しとして claim し直す。
                if [ "$ut" = "0" ] || [ "$now" -lt "${ut%%:*}" ] 2>/dev/null; then
                    exit "$LEDGER_RC_SEEN"
                fi
            elif ! gt "$m" "$mt"; then
                # 記録より古い版は lease の状態によらず常に skip。claim を許すと記録上の
                # mtime と呼び出し側が持つ mtime が食い違い、送信後の commit が空振りして
                # lease 切れ後に二重通知される (PR #24 Codex review 4th round)。
                exit "$LEDGER_RC_STALE"
            fi
        fi
        token="$((now + LEDGER_LEASE)):$$-$RANDOM"
        # 書き込みに失敗した場合 (権限・容量・ledger を読めない等) も claim 成功として
        # 扱う。ledger が更新されないので毎サイクル再通知されて煩いが、通知が消える
        # よりは気づける (この場合 token は ledger に無いので commit / release は空振り
        # し、呼び出し側が claim 未記録の WARN を出す)。
        _ledger_rewrite "$f" "$m" "$token"
        printf '%s\t%s\t%s' "$token" "$mt" "$ut"
        exit 0
    ) 9>"$LEDGER_LOCK"
    rc=$?
    # 9 / 10 以外 (0 = claim 成功、1 = lock file を開けない等の異常) はすべて通知させる
    case "$rc" in
        "$LEDGER_RC_SEEN")  return 1 ;;
        "$LEDGER_RC_STALE") return "$LEDGER_RC_STALE" ;;
        *)                  return 0 ;;
    esac
}

# 送信成功後に配達済み (lease 0) へ確定する。自分の claim でなくなっていれば何もしない。
# ledger を書けなければ 1 を返す (lease 期限切れ後に再通知される)。
ledger_commit() {
    local f="$1" m="$2" token="${3:-}"
    (
        if [ "$HAVE_FLOCK" -eq 1 ]; then
            flock -w 5 9 || exit 1
        fi
        local rec mt ut
        rec="$(_ledger_lookup "$f")"
        [ -n "$rec" ] || exit 1
        IFS=$'\t' read -r mt ut <<< "$rec"
        [ "$mt" = "$m" ] || exit 0              # 別の版で上書きされている = 何もしない
        [ "$ut" = "0" ] && exit 0               # 既に配達済み
        [ "$ut" = "$token" ] || exit 0          # 別 watcher の claim = 触らない
        _ledger_rewrite "$f" "$m" 0 || exit 1
        exit 0
    ) 9>"$LEDGER_LOCK"
}

# 送信失敗時に claim を取り消す。ledger の記録が自分の claim (token 一致) のままなら、
# claim 前の記録 (prev_mt / prev_ut、claim 前が未登録だったなら空文字) に戻す。
# 取り消せたら 0、ledger を操作できなかったら 1 を返す (lease 期限切れで再 claim される)。
#
# 行を消すのではなく「元の記録に戻す」ことが重要。単に消すと、直前に配達済みだった
# 古い版の記録まで失われ、更新前の mtime を掴んでいた別 watcher がその古い版を
# 再 claim して二重通知できてしまう。
ledger_release() {
    local f="$1" token="${2:-}" prev_mt="${3:-}" prev_ut="${4:-}"
    (
        if [ "$HAVE_FLOCK" -eq 1 ]; then
            flock -w 5 9 || exit 1
        fi
        local rec cur_ut
        rec="$(_ledger_lookup "$f")"
        [ -n "$rec" ] || exit 0
        cur_ut="${rec#*$'\t'}"
        # 自分の claim がそのまま残っている場合だけ戻す。lease 切れ後に別 watcher が
        # 再 claim していたら (token 不一致) 触らない。
        [ "$cur_ut" = "$token" ] || exit 0
        _ledger_rewrite "$f" "$prev_mt" "$prev_ut" || exit 1
        exit 0
    ) 9>"$LEDGER_LOCK"
}

# ledger 未作成時の初期 seed: 既存 report をすべて「通知済み」として一括登録する
# (この機構の導入時や queue の作り直し時に、過去 report が一斉通知されるのを防ぐ)。
#
# 対象は担当 project ではなく queue/projects 配下の全 report にする。後から起動した
# 別セッションの watcher は「ledger ファイルがある = seed 済み」とだけ判断するため、
# 担当分しか seed しないと、その watcher が自分の担当 project の過去 report を
# 一斉通知してしまう (Codex review P1)。
#
# 部分生成された ledger が他プロセスから見えないよう、一時ファイルに書いてから
# flock 下で atomic に mv する。mv 直前に他プロセスが既に ledger を作っていたら
# 何もしない (先着優先)。
ledger_baseline_seed() {
    local tmp seeded=0
    tmp="$(mktemp "${LEDGER_FILE}.seed.XXXXXX" 2>/dev/null)" || {
        # seed できないまま監視を始めると既存 report が一斉通知される。抑止側に倒すと
        # 正当な未通知 report まで消えるので通知は止めないが、必ずログに残す。
        log "[WARN] ledger baseline seed 用の一時ファイルを作成できません (${LEDGER_FILE}.seed.*)。" \
            "既存 report が一斉通知される可能性があります"
        return 1
    }
    find "$QUEUE_DIR/projects" \
        \( -path '*/reports/worker*_report.yaml' -o -path '*/reports/worker*_review.yaml' \) \
        -printf '%T@\t%p\n' 2>/dev/null \
        | awk -F'\t' '{print $1 "\t0\t" $2}' > "$tmp"
    seeded=$(grep -c . "$tmp" 2>/dev/null || true)
    (
        if [ "$HAVE_FLOCK" -eq 1 ]; then
            flock -w 5 9 || exit 1
        fi
        [ -f "$LEDGER_FILE" ] && exit 2        # 先に他プロセスが seed 済み (正常)
        mv -f "$tmp" "$LEDGER_FILE" || exit 1
    ) 9>"$LEDGER_LOCK"
    local rc=$?
    rm -f "$tmp"
    if [ "$rc" -eq 0 ]; then
        log "ledger baseline: 既存 report ${seeded} 件を通知済みとして登録 (通知なし)"
    elif [ "$rc" -ne 2 ]; then
        # lock file を開けない・flock を取れない・mv 失敗。seed されないまま監視が
        # 始まると既存 report が一斉通知されるため、原因の手掛かりを必ず残す
        # (mktemp 失敗だけでなく全経路をログする。PR #24 Claude review 6th #4)。
        log "[WARN] ledger baseline seed に失敗しました (lock/書き込み不可): $LEDGER_FILE" \
            "既存 report が一斉通知される可能性があります"
        return 1
    fi
    return 0
}

# このセッションが担当する project ディレクトリ一覧 (毎サイクル再計算し、実行中の
# project 追加やマーカー変更にも追従する)。結果はグローバル配列 OWNED に入る。
refresh_owned_projects() {
    OWNED=()
    local d owner
    for d in "$QUEUE_DIR/projects"/*/; do
        [ -d "$d" ] || continue
        if [ -f "$d/.squad_session" ]; then
            owner=$(head -n1 "$d/.squad_session" 2>/dev/null | tr -d '[:space:]')
        else
            owner="$DEFAULT_OWNER"
        fi
        [ "$owner" = "$SESSION" ] && OWNED+=("${d%/}")
    done
}

newest_mtime() {
    # 担当 project 内で find 述語にマッチするファイルの最新 mtime (epoch.float)。無ければ空。
    [ "${#OWNED[@]}" -eq 0 ] && return
    find "${OWNED[@]}" "$@" -printf '%T@\n' 2>/dev/null | sort -nr | head -n1
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
    local cfgs=""
    [ "${#OWNED[@]}" -gt 0 ] && cfgs=$(find "${OWNED[@]}" -maxdepth 1 -name discovery.yaml 2>/dev/null)
    if [ -z "$cfgs" ]; then
        log "discovery: 設定なし (担当 project に discovery.yaml を置くと有効化)"
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
        # 送信に失敗したら PENDING_NUDGE に積み、メインループが毎サイクル再送を試みる。
        # SEEN_FILE の既知化は取り消さない (候補は inbox に記録済みで、失われるのは
        # nudge だけ。既知化を取り消すと重複 inbox 行が増える)。(6th #2)
        notify_dispatcher "[DISCOVERY] 新規候補 ${added} 件を ${INBOX_FILE} に追加。空き worker に自動起票してください (task-yaml-author → 通知)。merge gate は人間が維持。" \
            || { PENDING_NUDGE="[DISCOVERY] 新規候補を ${INBOX_FILE} に追加済み。確認して空き worker に起票してください。"
                 log "[WARN] Dispatcher への discovery 通知に失敗 (次サイクルで再送)"; }
        return
    fi
    # 新規ゼロ: idle を遊ばせず、throttle 付きで「一通りレビュー(sweep)」を投げる
    local now2; now2=$(date +%s)
    if [ $(( now2 - LAST_SWEEP )) -ge "$SWEEP_INTERVAL" ]; then
        echo "- [ ] $(date '+%Y-%m-%dT%H:%M:%S%z') [sweep] all: 新規タスクなし。既存コード/open PR/backlog の一通りレビュー・監査  \`sweep:${now2}\`" >> "$INBOX_FILE"
        log "discovery: 新規なし -> [SWEEP] 周回レビューを inbox 投入"
        notify_dispatcher "[SWEEP] 新規タスクなし。空き worker がいれば既存コード/open PR/backlog の一通りレビュー・監査を1件だけ割り当ててください (全員稼働中なら何もしない)。" \
            || { PENDING_NUDGE="[SWEEP] 周回レビュー候補を ${INBOX_FILE} に投入済み。空き worker がいれば割り当ててください。"
                 log "[WARN] Dispatcher への sweep 通知に失敗 (次サイクルで再送)"; }
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

OWNED=()                   # このセッション担当の project dir 一覧 (refresh_owned_projects が更新)
declare -A PANE_HASH       # worker -> 直近 pane ハッシュ
declare -A PANE_STALL      # worker -> 無変化カウント
declare -A STALL_NOTIFIED  # worker -> 通報済みタスク mtime
declare -A RESUME_COUNT    # worker -> 通報後の連続活動再開カウント
declare -A STALE_SEEN      # report path -> 前サイクルで STALE になった mtime
declare -A STALE_LOGGED    # report path -> mtime 巻き戻し抑止をログ済みの mtime

mkdir -p "$QUEUE_DIR"

log "watcher start (session=$SESSION interval=${INTERVAL}s stall=${STALL_CYCLES} stall_resume=${STALL_RESUME_CYCLES} discovery=${DISCOVERY_INTERVAL}s sweep=${SWEEP_INTERVAL}s gc=${GC_INTERVAL}s boot_delay=${BOOT_DELAY}s ledger=${LEDGER_FILE})"
# ledger が無ければ、監視を始める前に既存 report を通知済みとして一括登録する
# (この機構の導入時や queue 作り直し時の一斉通知を防ぐ)。ledger があれば何もしない =
# 初回サイクルから ledger の内容だけで判定するので、watcher 停止中に書かれた report も
# 再起動後に拾える。
[ -f "$LEDGER_FILE" ] || ledger_baseline_seed
warn_missing_markers
sleep "$BOOT_DELAY"

LAST_DISCOVERY=0
LAST_SWEEP=0
PENDING_NUDGE=""          # 送信に失敗した discovery / sweep nudge (毎サイクル再送を試みる)
LAST_GC=0
while true; do
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        log "session '$SESSION' が無いので終了"
        break
    fi

    refresh_owned_projects

    # --- 1. report-bridge: 新規/更新された report を Dispatcher へ橋渡し ---
    #   status: blocked (検証ゲート 3 回 fail) は [INBOX] 付きで人間判断に回す。
    #
    # 通知するかどうかは共有 ledger (queue/.report_ledger) だけで決める。
    # 「担当が切り替わった project の過去 report を既読化する」というヒューリスティック
    # (PR #21) は不要になったので削除した。ledger は project の担当セッションに関係なく
    # 「誰かが既に通知したか」を持つため、
    #   - A→B→A と担当が移っても B 通知済み report は再通知されない (Issue #22 / F1)
    #   - マーカーの mtime を担当切替時刻の代理に使う必要が無い (F3)
    # 起動時の baseline は ledger_baseline_seed が監視開始前に済ませている。
    while IFS=$'\t' read -r m f; do
        [ -z "$f" ] && continue
        # 配達権を取る (他が配達済み / 配達中の lease を持っていれば skip)
        claim_rec=$(ledger_claim "$f" "$m")
        claim_rc=$?
        if [ "$claim_rc" -ne 0 ]; then
            # mtime 巻き戻しによる抑止は気づけないと困るのでログに残す。ただし
            # 2 つの watcher が同じ project を走査する切替の瞬間には、古い find
            # スナップショットを掴んだ側にも STALE が出る (次サイクルで新しい mtime を
            # 拾って解消する良性の競合)。この一時的な STALE で「1 回だけの WARN」を
            # 消費すると、後で本物の握り潰しが起きたとき無警告になるため、同じ path・
            # 同じ mtime が 2 サイクル連続したときだけ本物とみなして 1 回 WARN する
            # (PR #24 Claude review 6th #6)。
            if [ "$claim_rc" -eq "$LEDGER_RC_STALE" ]; then
                if [ "${STALE_SEEN[$f]:-}" = "$m" ]; then
                    if [ "${STALE_LOGGED[$f]:-}" != "$m" ]; then
                        STALE_LOGGED[$f]="$m"
                        log "[WARN] mtime が ledger の記録より古いため通知しません: $f" \
                            "(巻き戻し防止。意図した再通知なら touch してください)"
                    fi
                else
                    STALE_SEEN[$f]="$m"
                fi
            fi
            continue
        fi
        IFS=$'\t' read -r claim_token prev_mt prev_ut <<< "$claim_rec"
        wnum=$(basename "$f" | grep -oE 'worker[0-9]+' | grep -oE '[0-9]+')
        kind=report; echo "$f" | grep -q '_review.yaml' && kind=review
        status=$(grep -m1 -E '^status:' "$f" 2>/dev/null | awk '{print $2}')
        if [ "$status" = "blocked" ]; then
            log "report 検知(blocked): $f -> Dispatcher [INBOX] 通知"
            notify_dispatcher "[INBOX] Worker${wnum} が blocked: 検証ゲート未通過。${f} の notes/verdict を確認し、ユーザーに優先報告してください。"
            sent=$?
        else
            log "report 検知: $f -> Dispatcher 通知"
            notify_dispatcher "Worker${wnum} ${kind}: ${f} を確認してください。(watcher 自動橋渡し)"
            sent=$?
        fi
        # 送信できて初めて「配達済み」に確定する。失敗したら claim 前の記録に戻して
        # 次サイクルで再送する。commit / release 自体に失敗しても (ledger が書けない等)
        # lease 期限切れで再び claim されるので、通知が永久に消えることはない。
        # claim_token が空 = claim は fail-open した (flock を取れない等で ledger に
        # 記録が無い)。commit / release は token 不一致で空振りするだけなので、
        # 「配達済みにした / 取り消した」と実態と逆のログを出さない
        # (PR #24 Claude review 6th #5)。
        if [ "$sent" -eq 0 ]; then
            if [ -z "$claim_token" ]; then
                log "[WARN] 通知したが ledger に claim を記録できていません: $f (毎サイクル再通知される可能性)"
            else
                ledger_commit "$f" "$m" "$claim_token" \
                    || log "[WARN] ledger を更新できず: $f (約${LEDGER_LEASE}s 後に再通知される可能性)"
            fi
        else
            if [ -z "$claim_token" ]; then
                log "[WARN] Dispatcher への送信に失敗: $f (ledger に claim は無く、次サイクルで再送)"
            elif ledger_release "$f" "$claim_token" "$prev_mt" "$prev_ut"; then
                log "[WARN] Dispatcher への送信に失敗: $f (claim を取り消し、次サイクルで再送)"
            else
                log "[WARN] Dispatcher への送信に失敗: $f (ledger を戻せず、約${LEDGER_LEASE}s 後に再送)"
            fi
        fi
    done < <([ "${#OWNED[@]}" -gt 0 ] && find "${OWNED[@]}" \
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
            RESUME_COUNT[$N]=0
            continue
        fi

        # pane が存在しない場合 (例: SQUAD_ENABLE_CODEX=0 で Pane 6/W4 が無い) は
        # capture-pane が空を返し続けて停止通報ループに入るため、このサイクルはスキップする。
        if ! tmux list-panes -t "$SESSION" -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null | grep -qx "$pane"; then
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
            RESUME_COUNT[$N]=0
        else
            PANE_HASH[$N]="$h"
            PANE_STALL[$N]=0
            # 通報済みタスクのみ再開カウントを進める (未通報タスクでは無駄にカウントしない)
            if [ -n "${STALL_NOTIFIED[$N]:-}" ] && [ "${STALL_NOTIFIED[$N]}" = "$task_m" ]; then
                RESUME_COUNT[$N]=$(( ${RESUME_COUNT[$N]:-0} + 1 ))
                if [ "${RESUME_COUNT[$N]}" -ge "$STALL_RESUME_CYCLES" ]; then
                    unset "STALL_NOTIFIED[$N]"
                    RESUME_COUNT[$N]=0
                    log "Worker${N}: 活動再開を検知 → 再停止時の再通報を有効化"
                fi
            fi
        fi

        if [ "${PANE_STALL[$N]}" -ge "$STALL_CYCLES" ] && [ "${STALL_NOTIFIED[$N]:-}" != "$task_m" ]; then
            secs=$(( INTERVAL * STALL_CYCLES ))
            # squad hook の直近イベントで「停止」と「完了・report書き忘れ」を区別。
            # state/w{N}.json の last_event_at が直近 5 分以内なら、worker は hook を出している
            # = 応答可能な状態 = 「停止」ではなく「完了 (or 入力待ち) で report 未出」とみなす。
            # event 種別 (Stop / Notification / etc.) には依存しない (Claude Code バージョン間で
            # type field 名が揺れるため、鮮度ベースで判定する)。
            state_file="$SCRIPT_DIR/squad/state/w${N}.json"
            hook_event=""
            if [ -f "$state_file" ]; then
                hook_event=$(python3 -c "
import json
from datetime import datetime
from datetime import timezone
try:
    d = json.load(open('$state_file'))
    ev = d.get('last_event', '')
    ts = d.get('last_event_at', '')
    if ev and ts:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        if 0 <= age <= 300:
            print(ev)
except Exception:
    pass
" 2>/dev/null)
            fi
            if [ -n "$hook_event" ]; then
                log "Worker${N}: stall 検知だが hook=$hook_event のため完了通報に分類"
                stall_msg="Worker${N} は完了 (hook=$hook_event) していますが task が pending のままです (約${secs}s 経過)。pane ${pane#"$SESSION":} を確認し、report を書くよう促してください。"
            else
                log "Worker${N}: 約${secs}s 停止 (タスク未報告) -> Dispatcher 通報"
                stall_msg="Worker${N} が約${secs}s 停止しています (タスク割当済・report 未出力)。pane ${pane#"$SESSION":} を確認し、必要なら再送/clear してください。"
            fi
            # 送信できたときだけ通報済みにする。失敗時は次サイクルで再試行される (6th #2)
            if notify_dispatcher "$stall_msg"; then
                STALL_NOTIFIED[$N]="$task_m"
            else
                log "[WARN] Worker${N} の停止通報を送信できず (次サイクルで再試行)"
            fi
        fi
    done

    # --- 4. Discovery: 低頻度で仕事を発見し inbox へ (新規ゼロ時は throttle 付き sweep) ---
    # 前回送信に失敗した nudge があれば先に再送を試みる (成功するまで毎サイクル)
    if [ -n "$PENDING_NUDGE" ] && notify_dispatcher "$PENDING_NUDGE"; then
        log "保留していた Dispatcher 通知を再送しました"
        PENDING_NUDGE=""
    fi
    now_ts=$(date +%s)
    if [ $(( now_ts - LAST_DISCOVERY )) -ge "$DISCOVERY_INTERVAL" ]; then
        run_discovery
        LAST_DISCOVERY="$now_ts"
    fi

    # --- 5. worktree GC: merged+clean な専用 worktree を掛除 ---
    # glob ベースで project 単位に絞れないため、複数セッション並行時の重複実行を避けて
    # 既定セッションの watcher だけが担当する。
    if [ "$SESSION" = "$DEFAULT_OWNER" ] && [ $(( now_ts - LAST_GC )) -ge "$GC_INTERVAL" ]; then
        gc_worktrees
        LAST_GC="$now_ts"
    fi

    sleep "$INTERVAL"
done
