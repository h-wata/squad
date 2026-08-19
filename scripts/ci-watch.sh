#!/usr/bin/env bash
# ci-watch.sh — gh の JSON だけで CI の失敗/ハングを検知し、pi に一次切り分けを依頼する。
#
# 設計方針（この分担を守ること）:
#   - 検知そのものに LLM は使わない。失敗判定もハング判定も gh の JSON だけで機械的に
#     決まる。LLM を挟むと誤検知と遅延が増えるだけ。
#   - pi (scripts/pi-log-triage.sh) は「異常を検知した後のログ一次切り分け」にだけ使う。
#   - pi が返す next_check / candidate_causes は人間が読む手がかりであって、実行してよい
#     指示ではない。このスクリプトはそれを inbox に記録するだけで、絶対に自動実行しない。
#
# 使い方:
#   scripts/ci-watch.sh <PR番号>
#   scripts/ci-watch.sh --all-open
#
# 環境変数:
#   CI_WATCH_STALL_SECONDS  ハング判定の閾値秒数 (既定 900。0 以上の整数のみ)。
#                           プロジェクトごとに「最長の正常な run 時間 + 余裕」で設定する
#                           こと。既定値より短い正常 run しか無いプロジェクトでも、
#                           cold cache 等で伸びるケースを想定して余裕を持たせる。
#                           例: 通常 5 分・cold build で 12 分程度なら 900〜1200 秒。
#   CI_WATCH_REPO           対象 repo (owner/repo)。省略時は gh のデフォルト repo (cwd の remote)
#   CI_WATCH_INBOX          追記先 (既定 queue/_inbox.md)
#   CI_WATCH_PI_TRIAGE      pi-log-triage.sh のパス (既定 scripts/pi-log-triage.sh。テスト用)
#
# 終了コード:
#   0  異常なし（対象 PR すべて正常）
#   1  引数エラー / 依存コマンド不足 / 環境変数の設定不正
#   2  1 件以上の異常（失敗またはハング）を検知した
#   3  gh/jq の操作自体が失敗した（API 障害・認証・rate limit 等。「run が存在しない」
#      正常系とは区別する）
#
# 実行環境: Linux (GNU coreutils の `date -d` と util-linux の `flock` に依存)。
# macOS/BSD では `date -d` の解釈が異なるため動作しない。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STALL_SECONDS="${CI_WATCH_STALL_SECONDS:-900}"
INBOX="${CI_WATCH_INBOX:-queue/_inbox.md}"
PI_TRIAGE="${CI_WATCH_PI_TRIAGE:-$SCRIPT_DIR/pi-log-triage.sh}"
TRIAGE_DIR="$(dirname "$INBOX")/ci-watch-triage"

# CI_WATCH_STALL_SECONDS は 0 以上の整数のみ許容する。
case "$STALL_SECONDS" in
  ''|*[!0-9]*)
    echo "エラー: CI_WATCH_STALL_SECONDS は 0 以上の整数を指定する (got: '${STALL_SECONDS}')" >&2
    exit 1
    ;;
esac

REPO_ARGS=()
if [ -n "${CI_WATCH_REPO:-}" ]; then
  REPO_ARGS=(--repo "$CI_WATCH_REPO")
fi

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

[ $# -lt 1 ] && usage 1
case "$1" in
  -h|--help) usage 0 ;;
esac
TARGET="$1"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "エラー: $1 コマンドが見つかりません" >&2; exit 1; }
}
require_cmd gh
require_cmd jq
require_cmd flock

case "$TARGET" in
  --all-open) : ;;
  ''|*[!0-9]*) echo "エラー: 引数は PR 番号または --all-open を指定する" >&2; usage 1 ;;
esac

# 対象 PR の最新 CI run 情報を LAST_RUN_JSON に格納する (run が無ければ空文字。異常ではない)。
# 戻り値: 0 = gh/jq 呼び出し自体は成功。1 = gh/jq の操作自体が失敗 (API 障害・認証・
# rate limit 等) — この場合 stderr にエラーメッセージを出す。「run がまだ無い」正常系と
# 「gh/jq が動かない」異常系を混同しないための分離 (blocking B2)。
get_latest_run() {
  local pr="$1"
  LAST_RUN_JSON=""

  local view_raw
  if ! view_raw="$(gh pr view "$pr" "${REPO_ARGS[@]}" --json headRefName 2>&1)"; then
    echo "エラー: gh pr view #${pr} が失敗した: ${view_raw}" >&2
    return 1
  fi
  local head_ref
  if ! head_ref="$(jq -r '.headRefName // empty' <<<"$view_raw" 2>&1)"; then
    echo "エラー: gh pr view #${pr} の出力を jq で解釈できなかった: ${head_ref}" >&2
    return 1
  fi
  if [ -z "$head_ref" ]; then
    echo "エラー: PR #${pr} の headRefName が空だった (予期しない gh 応答)" >&2
    return 1
  fi

  local list_raw
  if ! list_raw="$(gh run list "${REPO_ARGS[@]}" --branch "$head_ref" \
      --json databaseId,status,conclusion,headSha,event,workflowName,url --limit 1 2>&1)"; then
    echo "エラー: gh run list (branch=${head_ref}) が失敗した: ${list_raw}" >&2
    return 1
  fi
  local parsed
  if ! parsed="$(jq -c '.[0] // empty' <<<"$list_raw" 2>&1)"; then
    echo "エラー: gh run list の出力を jq で解釈できなかった: ${parsed}" >&2
    return 1
  fi
  LAST_RUN_JSON="$parsed"
  return 0
}

# 実行中 job のうち status=in_progress の経過時間 (job の startedAt 起点) が閾値を超えたら
# 「<ステップ名> (elapsed <秒>s)」を1行出力する。無ければ何も出さず非ゼロで返る。
# startedAt が null/空文字の job は「まだ計測できない」として静かにスキップする (NB2)。
detect_stall() {
  local run_id="$1" threshold="$2"
  local jobs_json
  jobs_json="$(gh run view "$run_id" "${REPO_ARGS[@]}" --json jobs 2>/dev/null | jq -c '.jobs // []')" || return 1
  local now_epoch
  now_epoch="$(date -u +%s)"
  local job
  while IFS= read -r job; do
    [ -z "$job" ] && continue
    local job_status
    job_status="$(jq -r '.status' <<<"$job")"
    [ "$job_status" = "in_progress" ] || continue
    local job_started
    job_started="$(jq -r '.startedAt // empty' <<<"$job")"
    [ -z "$job_started" ] && continue
    local started_epoch
    started_epoch="$(date -u -d "$job_started" +%s 2>/dev/null)" || continue
    local elapsed=$(( now_epoch - started_epoch ))
    if [ "$elapsed" -gt "$threshold" ]; then
      local step_name
      step_name="$(jq -r '[.steps[]? | select(.status=="in_progress")][0].name // empty' <<<"$job")"
      [ -z "$step_name" ] && step_name="$(jq -r '.name' <<<"$job")"
      printf '%s (elapsed %ds)\n' "$step_name" "$elapsed"
      return 0
    fi
  done < <(jq -c '.[]' <<<"$jobs_json")
  return 1
}

# run_id をキーにした排他区間で "$@" を実行する (blocking B1)。重複確認・triage 処理・
# inbox 追記をこの下で一括して行うことで、同一 run を並行処理する2プロセスが競合して
# inbox に同じ行を2重追記するのを防ぐ。
with_run_lock() {
  local run_id="$1"; shift
  mkdir -p "$TRIAGE_DIR"
  (
    flock -x 9
    "$@"
  ) 9>"$TRIAGE_DIR/.lock.${run_id}"
}

# 検知結果 (失敗/ハング) を queue/_inbox.md に1行追記する。同じ run URL の行が既にあれば
# 追記しない。ログが取れれば pi-log-triage.sh に渡すが、失敗しても全体は止めない。
# 呼び出し元は必ず with_run_lock 経由で呼ぶこと (このロジック単体には排他制御が無い)。
# with_run_lock 経由で動的呼び出しされるため、shellcheck の静的呼び出し解析には
# 直接の呼び出し箇所が見えない (未使用ではない)。
# shellcheck disable=SC2329
append_inbox() {
  local pr="$1" url="$2" step_desc="$3" log_file="$4" log_ok="$5" run_id="$6"

  if [ -f "$INBOX" ] && grep -qF "$url" "$INBOX"; then
    echo "info: $url は既に inbox に記録済み、スキップ" >&2
    return 0
  fi

  local triage_line=""
  if [ "$log_ok" -eq 0 ]; then
    mkdir -p "$TRIAGE_DIR"
    local triage_yaml="$TRIAGE_DIR/${run_id}.yaml"
    if [ -e "$triage_yaml" ]; then
      rm -f "$triage_yaml"
    fi
    if bash "$PI_TRIAGE" "$log_file" "$triage_yaml" >/dev/null 2>&1; then
      triage_line="  - triage: $triage_yaml"
    else
      echo "warn: pi-log-triage.sh が失敗した。triage 無しの生検知結果のみ記録する" >&2
    fi
  else
    echo "warn: run のログが取得できなかった。triage 無しの生検知結果のみ記録する" >&2
  fi

  mkdir -p "$(dirname "$INBOX")"
  {
    echo "- [ ] [CI] PR #${pr} ${url} — ${step_desc}"
    [ -n "$triage_line" ] && echo "$triage_line"
  } >> "$INBOX"
  echo "appended: PR #${pr} ${url} — ${step_desc}"
}

handle_failure() {
  local pr="$1" run_id="$2" url="$3" conclusion="$4"
  local log_file log_ok=1
  log_file="$(mktemp)"
  # set -e 下で途中終了しても tmpfile を確実に消す (NB1)。
  # local な log_file は関数終了後にスコープ外になるため、シングルクォートで
  # 発火時に評価する版だと解決できない。ここで即値展開させる。
  # shellcheck disable=SC2064
  trap "rm -f '$log_file'" EXIT
  if gh run view "$run_id" "${REPO_ARGS[@]}" --log-failed > "$log_file" 2>/dev/null && [ -s "$log_file" ]; then
    log_ok=0
  fi
  local failed_step
  failed_step="$(gh run view "$run_id" "${REPO_ARGS[@]}" --json jobs 2>/dev/null \
    | jq -r '[.jobs[]?.steps[]? | select(.conclusion=="failure")][0].name // empty')" || failed_step=""
  [ -z "$failed_step" ] && failed_step="(不明なステップ、conclusion=${conclusion})"
  with_run_lock "$run_id" append_inbox "$pr" "$url" "$failed_step" "$log_file" "$log_ok" "$run_id"
  rm -f "$log_file"
}

handle_hang() {
  local pr="$1" run_id="$2" url="$3" stalled_step="$4"
  local log_file log_ok=1
  log_file="$(mktemp)"
  # local な log_file は関数終了後にスコープ外になるため、シングルクォートで
  # 発火時に評価する版だと解決できない。ここで即値展開させる。
  # shellcheck disable=SC2064
  trap "rm -f '$log_file'" EXIT
  if gh run view "$run_id" "${REPO_ARGS[@]}" --log > "$log_file" 2>/dev/null && [ -s "$log_file" ]; then
    log_ok=0
  fi
  with_run_lock "$run_id" append_inbox "$pr" "$url" "ハング中: ${stalled_step}" "$log_file" "$log_ok" "$run_id"
  rm -f "$log_file"
}

# 1 PR を処理する。戻り値: 0=正常, 1=異常検知 (失敗/ハング), 2=gh/jq の操作エラー。
process_pr() {
  local pr="$1"
  if ! get_latest_run "$pr"; then
    return 2
  fi
  local run_json="$LAST_RUN_JSON"
  if [ -z "$run_json" ]; then
    echo "info: PR #${pr} — CI run が見つからない" >&2
    return 0
  fi

  local status conclusion run_id url
  status="$(jq -r '.status' <<<"$run_json")"
  conclusion="$(jq -r '.conclusion // empty' <<<"$run_json")"
  run_id="$(jq -r '.databaseId' <<<"$run_json")"
  url="$(jq -r '.url // empty' <<<"$run_json")"

  case "$conclusion" in
    failure|cancelled|timed_out)
      handle_failure "$pr" "$run_id" "$url" "$conclusion"
      return 1
      ;;
  esac

  if [ "$status" != "completed" ]; then
    local stalled_step
    if stalled_step="$(detect_stall "$run_id" "$STALL_SECONDS")"; then
      handle_hang "$pr" "$run_id" "$url" "$stalled_step"
      return 1
    fi
  fi

  return 0
}

main() {
  local prs=()
  if [ "$TARGET" = "--all-open" ]; then
    local list_raw
    if ! list_raw="$(gh pr list "${REPO_ARGS[@]}" --state open --json number 2>&1)"; then
      echo "エラー: gh pr list --state open が失敗した: ${list_raw}" >&2
      exit 3
    fi
    local numbers
    if ! numbers="$(jq -r '.[].number' <<<"$list_raw" 2>&1)"; then
      echo "エラー: gh pr list の出力を jq で解釈できなかった: ${numbers}" >&2
      exit 3
    fi
    if [ -n "$numbers" ]; then
      while IFS= read -r n; do
        [ -n "$n" ] && prs+=("$n")
      done <<<"$numbers"
    fi
  else
    prs=("$TARGET")
  fi

  local exit_code=0
  local pr rc
  for pr in "${prs[@]}"; do
    rc=0
    process_pr "$pr" || rc=$?
    case "$rc" in
      0) : ;;
      1) [ "$exit_code" -lt 2 ] && exit_code=2 ;;
      2) exit_code=3 ;;
    esac
  done
  exit "$exit_code"
}

main
