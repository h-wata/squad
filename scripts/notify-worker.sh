#!/usr/bin/env bash
# notify-worker.sh — tmux worker への通知を timing 込みでラップする
#
# Dispatcher が worker に「新タスク通知 / モデル切替 / clear」を送るときに使う。
# 手で send-keys を並べると以下で頻繁にハマるのを吸収する:
#   - メッセージと Enter を同一 send-keys にまとめると壊れる → 別コマンド + sleep
#   - /model 切替直後にタスク通知を送ると drop する → 切替後 sleep 2.5 を入れる
#   - /clear 直後も反映待ちが要る → sleep 1.5
# 送信後に「届いたか (submitted)」と「走り出したか (started)」を別々に確認して報告する。
#
# 使い方:
#   scripts/notify-worker.sh <W1|W2|W3|W4|pane> "<message>" [--model <opus|sonnet|haiku>] [--clear] [--no-new]
#
# 例:
#   scripts/notify-worker.sh W2 "新しいタスクがあります。.../worker2.yaml を確認してください。"
#   scripts/notify-worker.sh W1 "....worker1.yaml を確認してください。" --model sonnet
#   scripts/notify-worker.sh W2 "....worker2.yaml を確認してください。" --clear --model sonnet
#   scripts/notify-worker.sh W4 "....worker4.yaml を確認してください。"          # /new 自動送信
#   scripts/notify-worker.sh W4 "....worker4.yaml を確認してください。" --no-new  # /new をスキップ
#
# 環境変数:
#   SQUAD_SESSION  tmux セッション名 (既定: ros-agents)
#
# W4(Codex): 毎回 /new でフレッシュ会話を開始しクレジット累積を抑制。--no-new で抑制可。
set -euo pipefail

SESSION="${SQUAD_SESSION:-ros-agents}"
# send_line の再送回数。既定 5 回 (待ち 3+6+9+12=30 秒) で Opencode の起動を待ちきれる。
SEND_RETRIES="${SQUAD_SEND_RETRIES:-5}"

# 誤爆ガード: SQUAD_SESSION 未設定のまま複数 Squad (watcher) が動いている場合、
# 既定 ros-agents への送信は他 Squad の worker を壊す事故になるため中断する。
# (2026-08-08: kiokumesh の Dispatcher が env 無しで ros-agents の W2 に送った実例あり)
if [ -z "${SQUAD_SESSION:-}" ]; then
  SCRIPT_DIR_="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  watcher_count=$(pgrep -cf "$SCRIPT_DIR_/watch.sh" 2>/dev/null || echo 0)
  if [ "$watcher_count" -gt 1 ]; then
    echo "エラー: SQUAD_SESSION が未設定で、watcher が ${watcher_count} 個動いています (複数 Squad 並行運用中)。" >&2
    echo "SQUAD_SESSION=<自分の session> を付けて再実行してください。" >&2
    exit 1
  fi
fi

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

[ $# -lt 2 ] && usage 1

WORKER="$1"; shift
MESSAGE="$1"; shift
MODEL=""
DO_CLEAR=0
NO_NEW=0

while [ $# -gt 0 ]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    --clear) DO_CLEAR=1; shift ;;
    --no-new) NO_NEW=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

# worker ラベル → pane 番号 (start.sh の構成に追従)
#   W1=0.1 W2=0.2 W3=0.3  Codex W4=0.6  (0.4=Terminal, 0.5=Aux-Shell は worker ではない)
case "${WORKER,,}" in
  w1) PANE="0.1"; IS_CODEX=0 ;;
  w2) PANE="0.2"; IS_CODEX=0 ;;
  w3) PANE="0.3"; IS_CODEX=0 ;;
  w4) PANE="0.6"; IS_CODEX=1 ;;
  0.[0-9]) PANE="$WORKER"; IS_CODEX=$([ "$WORKER" = "0.6" ] && echo 1 || echo 0) ;;
  *) echo "unknown worker/pane: $WORKER (expected W1..W4 or pane like 0.1)" >&2; exit 1 ;;
esac

TARGET="${SESSION}:${PANE}"

# pane の存在確認
if ! tmux list-panes -t "$SESSION" -F '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null | grep -qx "$TARGET"; then
  if [ "$IS_CODEX" -eq 1 ]; then
    echo "W4 は無効化されています (SQUAD_ENABLE_CODEX=0 で起動された可能性があります)。設計レビュー / cross-review は Claude W1-W3 に振ってください。" >&2
  else
    echo "pane not found: $TARGET (session=$SESSION)。tmux 起動済みか確認してください。" >&2
  fi
  exit 1
fi

# 送信の状態は 3 つに分かれる。混ぜると誤報になる (SQUAD-281)。
#   landed    : 入力欄に本文が乗った (pane に本文 or 貼り付けマーカーが見える)
#   submitted : Enter が効いて入力欄から本文が消えた = 送信された
#   started   : TUI に中断案内 ("esc to interrupt" 等) が出た = worker が走り出した
#
# 以前は started だけを見て、出なければ「worker が反応しません / 発注できたと見なさない
# こと」と報告していた。送信時に worker が別処理中だったり即答したりすると中断案内を
# 捉えられず、届いているのに失敗と誤報する。Dispatcher がそれを信じて再送すると同じ
# タスクを二重発注する事故になる (2026-09-09 に 15 回以上の誤報)。
# そこで submitted (届いたか) と started (走り出したか) を別の状態として扱い、
# 失敗を名乗るのは submitted すら確認できないときだけにする。

# 入力欄領域だけを切り出す。TUI は入力欄を枠 (╭ / ┌) で描くので最後の枠上辺から末尾までを
# 入力欄とみなす。枠が見つからない TUI では末尾 N 行で代用する。
INPUT_TAIL_LINES="${SQUAD_INPUT_TAIL_LINES:-6}"
input_area() {
  tmux capture-pane -pt "$TARGET" | awk -v n="$INPUT_TAIL_LINES" '
    { l[NR] = $0; if ($0 ~ /╭|┌/) s = NR }
    END { if (!s) s = (NR > n ? NR - n + 1 : 1); for (i = s; i <= NR; i++) print l[i] }'
}

# pane 側は折り返しで改行が入り、TUI が行頭に枠線 (┃ 等) を描くため、空白を消すだけでは
# 本文の途中に枠線が残って一致しない。ASCII 印字文字だけ残せば折り返しも枠線も落ちる。
ascii_only() { LC_ALL=C tr -cd '!-~'; }

# 長文は TUI が "[Pasted text #1 +42 lines]" に畳むため本文自体が pane に出ない。
# その場合は貼り付けマーカーの有無で「乗った」を判定する (症状 B)。
PASTE_MARKER='Pastedtext'

# 本文が TUI に届いているか (入力欄に限らず pane 全体を見る)
landed() {
  local probe="$1"
  [ -z "$probe" ] && return 0
  tmux capture-pane -pt "$TARGET" | ascii_only | LC_ALL=C grep -qF "$probe" && return 0
  input_area | ascii_only | LC_ALL=C grep -qF "$PASTE_MARKER"
}

# 本文がまだ入力欄に残っているか (= Enter が効いていない)
input_has_text() {
  local probe="$1" area
  [ -z "$probe" ] && return 1
  area="$(input_area | ascii_only)"
  printf '%s' "$area" | LC_ALL=C grep -qF "$probe" && return 0
  printf '%s' "$area" | LC_ALL=C grep -qF "$PASTE_MARKER"
}

# worker が走り出したか。Opencode は "esc interrupt"、Claude Code は "esc to interrupt"。
started() { tmux capture-pane -pt "$TARGET" | LC_ALL=C grep -q 'interrupt'; }

# $2 秒まで待って submitted を確認する。started が見えた時点でも submitted 確定とみなす。
wait_submitted() {
  local probe="$1" secs="$2" i
  for ((i = 0; i < secs; i++)); do
    started && return 0
    input_has_text "$probe" || return 0
    sleep 1
  done
  return 1
}

# Enter を打った後の確認。戻り値は「送信できたか」だけを表す。
# 着手が確認できなくても 0 を返す — ここで非 0 を返すと Dispatcher が再送して二重発注に
# なるため。何が確認できて何ができていないかは文言で伝える。
confirm_submitted() {
  local probe="$1" i
  if ! wait_submitted "$probe" 8; then
    echo "[notify-worker] 本文が入力欄に残っています (Enter が効いていません)。Enter だけ打ち直します (本文は貼り直しません)" >&2
    tmux send-keys -t "$TARGET" Enter
    if ! wait_submitted "$probe" 5; then
      echo "[notify-worker] 送信を確認できませんでした: $TARGET" >&2
      echo "[notify-worker] 本文が入力欄に残ったままの可能性があります。発注できたと見なさず、pane を確認してください (tmux attach -t $SESSION)。" >&2
      return 1
    fi
  fi
  for ((i = 0; i < 5; i++)); do
    started && return 0
    sleep 1
  done
  echo "[notify-worker] 送信は確認できましたが、着手は確認できませんでした: $TARGET" >&2
  echo "[notify-worker] 届いてはいるので再送しないでください (二重発注になります)。pane を確認してください (tmux attach -t $SESSION)。" >&2
  return 0
}

# 1行ずつ送る小関数: テキスト → sleep → Enter (同一 send-keys にまとめない)
#
# Enter を打つ前に「本当に入力欄へ乗ったか」を pane から確認して、乗っていなければ
# 打ち直す。TUI が起動直後・描画中だと send-keys のテキストが黙って捨てられ、
# Dispatcher は送ったつもりなのに worker は待機し続ける、という取りこぼしが起きる
# (Opencode W3 で再現。Claude でも /model 直後に同種の drop があり sleep で凌いでいた)。
#
# 照合はプローブ (テキスト内で最も長い ASCII 連続部分。タスク通知なら YAML の絶対パス) を
# 使う。最長を選ぶのは、タスク ID や記号列より絶対パスのほうが長く、他の pane 内容と
# 偶然一致しにくいため。ASCII 連続部分が無いテキストは素通しする。
#
# $3 に 1 を渡したときだけ送信確認まで行う (/clear や /model は即応答で確認が空振りする)。
send_line() {
  local text="$1"; local pre_enter_sleep="${2:-0.6}"; local confirm="${3:-0}"
  local probe attempt
  # LC_ALL=C は必須。UTF-8 ロケールだと [!-~] が照合順序で解釈され、環境によっては
  # (この環境の grep は ugrep) ASCII 連続部分に一致しない。C ロケールならバイト単位に
  # なり、マルチバイト文字は 0x80 以上なので自然に除外される。
  probe="$(printf '%s' "$text" | LC_ALL=C grep -oE '[!-~]{8,}' \
    | awk '{ if (length($0) > length(best)) best = $0 } END { print best }' || true)"
  for attempt in $(seq 1 "$SEND_RETRIES"); do
    if [ "$attempt" -gt 1 ] && landed "$probe"; then
      # 前の試行で既に乗っている。貼り直すと入力欄に本文が積み上がり、Enter で
      # 同じタスクを複数回発注する事故になる (症状 C) ため、貼らず Enter だけ送る。
      echo "[notify-worker] 本文は既に入力欄にあります。貼り直さず Enter だけ送ります" >&2
    else
      tmux send-keys -t "$TARGET" "$text"
      sleep "$pre_enter_sleep"
    fi
    if landed "$probe"; then
      tmux send-keys -t "$TARGET" Enter
      [ "$confirm" = "1" ] || return 0
      confirm_submitted "$probe"
      return $?
    fi
    if [ "$attempt" -eq "$SEND_RETRIES" ]; then
      break
    fi
    # 待ち時間を伸ばしながら再送する。worker の CLI 起動中 (Opencode は 30 秒前後)
    # は入力を受け付けないため、固定間隔だと起動を待ちきれない。
    echo "[notify-worker] 入力欄にテキストが乗っていません。${attempt}/${SEND_RETRIES} 回目、再送します" >&2
    # 部分的に乗っていた場合の重複入力を避けるため、行を消してから打ち直す。
    # C-u (行クリア) であって C-c ではない: Codex は Ctrl-C 1 回で終了してしまう。
    tmux send-keys -t "$TARGET" C-u
    sleep "$((attempt * 3))"
  done
  echo "[notify-worker] $SEND_RETRIES 回試しても入力欄に乗りませんでした: $TARGET" >&2
  echo "[notify-worker] worker がまだ起動中か、pane が応答していません。" >&2
  echo "[notify-worker] pane を直接確認してください (tmux attach -t $SESSION)。" >&2
  return 1
}

# --clear (Claude のみ。Codex には /clear 概念が無いのでスキップ)
if [ "$DO_CLEAR" -eq 1 ]; then
  if [ "$IS_CODEX" -eq 1 ]; then
    echo "[notify-worker] W4(Codex) には --clear は無効。スキップします。" >&2
  else
    send_line "/clear" 0.5
    sleep 1.5
  fi
fi

# --model (Claude のみ。Codex は /model コマンドが無い)
if [ -n "$MODEL" ]; then
  if [ "$IS_CODEX" -eq 1 ]; then
    echo "[notify-worker] W4(Codex) はモデル切替不可。--model $MODEL を無視します。" >&2
  else
    send_line "/model $MODEL" 0.5
    # 切替反映前にタスク通知を送ると drop するため十分待つ (経験則: 2.5s)
    sleep 2.5
  fi
fi

# /new (Codex のみ。--no-new 指定時はスキップ)
# 独立タスクごとにフレッシュ会話を開始しクレジット累積を抑制する
if [ "$IS_CODEX" -eq 1 ] && [ "$NO_NEW" -eq 0 ]; then
  send_line "/new" 0.5
  # /new 後に新規会話への切替完了を待つ (/clear 後と同等以上、2.5s)
  sleep 2.5
fi

# 本文通知
send_line "$MESSAGE" 0.8 1

# 着手確認のため少し待って pane 末尾を表示
sleep 3
echo "=== ${WORKER^^} (${TARGET}) 直近出力 ==="
tmux capture-pane -t "$TARGET" -p | grep -vE '^[[:space:]]*$' | tail -8
