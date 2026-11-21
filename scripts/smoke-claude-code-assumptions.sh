#!/usr/bin/env bash
# squad が頼る Claude Code の挙動の前提を実機で確かめるスモーク (SQUAD-351)。
#
# Claude Code を更新したら手で 1 回流す。pytest にも CI にも入れない (実際に claude を 3 回呼ぶ。model は haiku)。
# 静的に確かめられる前提は tests/test_claude_code_assumptions.py が見る。
#
# 確かめる前提:
#   1. worker と同じ env (CLAUDE_CONFIG_DIR + CLAUDE_CODE_OAUTH_TOKEN) で claude -p が認証できる
#   2. Bash の子プロセスには CLAUDE_CODE_OAUTH_TOKEN が渡らず、CLAUDE_CONFIG_DIR は残る (env 名の有無だけ見る)
#   3. permission ルール Write(path) は照合されない (deny に書いても Write ツールが通る)
#   4. permission ルール Edit(path) は Write ツールにも効く (deny すると Write ツールが止まる)
# 「グローバル ~/.claude/settings.json の ask が project の allow より優先される」は CLAUDE_CONFIG_DIR を
# 差し替えると再現できず、手元の対話設定を触らずには確かめられないので、このスモークでは見ない
# (config/claude-worker/README.md と静的テストの「ask が空」で押さえる)。
#
# トークンの値は出力・ファイル・argv に出さない。判定は env 名の有無だけで行う。
# 使い方: scripts/smoke-claude-code-assumptions.sh   (SQUAD_CLAUDE_TOKEN_FILE でトークンファイルを変えられる)
# 終了コード: 0 = 全項目が期待どおり / 1 = 期待と違う項目がある / 2 = 前提 (token・claude) が無く実行できない
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOKEN_FILE="${SQUAD_CLAUDE_TOKEN_FILE:-$HOME/.config/squad/claude-oauth-token}"
BASE_SETTINGS="$SCRIPT_DIR/config/claude-worker/settings.base.json"
FAILED=0

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILED=1; }

if ! command -v claude >/dev/null 2>&1; then
    echo "claude が PATH にありません" >&2
    exit 2
fi
if [ ! -r "$TOKEN_FILE" ] || ! grep -q '[^[:space:]]' "$TOKEN_FILE"; then
    echo "トークンファイルが無い、または空です (パスは $TOKEN_FILE)" >&2
    exit 2
fi
if [ ! -f "$BASE_SETTINGS" ]; then
    echo "$BASE_SETTINGS がありません" >&2
    exit 2
fi

# worker と同じく専用の config dir を使う (手元の ~/.claude には触らない)。終了時に削除する。
# 削除は mktemp が作った実在の dir に限り、空変数や / などを渡して広く消さないよう case で絞る。
WORK="$(mktemp -d)" || { echo "mktemp -d に失敗しました" >&2; exit 2; }
# shellcheck disable=SC2329  # trap EXIT から呼ばれる
cleanup() {
    if [ -n "${WORK:-}" ] && [ -d "$WORK" ]; then
        case "$WORK" in
            /tmp/tmp.* | "${TMPDIR:-/tmp}"/tmp.*) rm -rf -- "$WORK" ;;
        esac
    fi
}
trap cleanup EXIT
CONFIG_DIR="$WORK/config"
mkdir -p "$CONFIG_DIR" "$WORK/cwd"
cp "$BASE_SETTINGS" "$CONFIG_DIR/settings.json"
printf '{"hasCompletedOnboarding": true}\n' >"$CONFIG_DIR/.claude.json"

# claude -p を worker と同じ env で呼ぶ。トークンは $(cat) でこの 1 コマンドの env にだけ渡す。
# 標準出力は捨てる (判定はファイルの有無で行う)。$1: 追加の --settings JSON (空可)、$2: prompt
# --allowedTools は複数値を取るので、prompt は -- の後ろに置く (置かないと prompt が許可ツール名として読まれる)。
run_claude() {
    local extra_settings="$1" prompt="$2" args
    args=(-p --model haiku --permission-mode bypassPermissions --allowedTools "Bash Write")
    [ -n "$extra_settings" ] && args+=(--settings "$extra_settings")
    (
        cd "$WORK/cwd" || exit 1
        { set +x; } 2>/dev/null  # bash -x で流しても、下の行の環境変数代入にトークンの値が出ないようにする
        CLAUDE_CONFIG_DIR="$CONFIG_DIR" CLAUDE_CODE_OAUTH_TOKEN="$(cat "$TOKEN_FILE")" \
            timeout 180 claude "${args[@]}" -- "$prompt" >/dev/null 2>&1
    )
}

# --- 1, 2: 認証と Bash 子プロセスの env ---
ENV_OUT="$WORK/env-names.txt"
# shellcheck disable=SC2016  # ${!v+x} は claude の Bash ツール側で展開させる
ENV_CMD='for v in CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CONFIG_DIR; do if [ -n "${!v+x}" ]; then echo "$v=set"; else echo "$v=unset"; fi; done > '"$ENV_OUT"
run_claude "" "Run exactly this one Bash command, then reply DONE and nothing else: $ENV_CMD"
rc=$?
if [ "$rc" -eq 0 ] && [ -s "$ENV_OUT" ]; then
    pass "1. CLAUDE_CONFIG_DIR + OAuth トークンで claude -p が認証でき、Bash ツールが動いた"
else
    fail "1. claude -p が完了しなかった (exit=$rc, env 名の出力 $([ -s "$ENV_OUT" ] && echo あり || echo なし))"
fi
if [ -s "$ENV_OUT" ]; then
    if grep -qx 'CLAUDE_CODE_OAUTH_TOKEN=unset' "$ENV_OUT"; then
        pass "2a. Bash 子プロセスに CLAUDE_CODE_OAUTH_TOKEN は渡らない"
    else
        fail "2a. Bash 子プロセスに CLAUDE_CODE_OAUTH_TOKEN が渡っている (前提 (b) が崩れた。start.sh の認証まわりを見直すこと)"
    fi
    if grep -qx 'CLAUDE_CONFIG_DIR=set' "$ENV_OUT"; then
        pass "2b. Bash 子プロセスに CLAUDE_CONFIG_DIR は残る"
    else
        fail "2b. Bash 子プロセスに CLAUDE_CONFIG_DIR が残らない (前提 (b) が崩れた)"
    fi
else
    fail "2. env 名を取得できなかった"
fi

# --- 3: Write(path) の deny は効かない ---
# 絶対パスの permission ルールは // で始める (/ 始まりは project root 相対)。FILE_* は / 始まりなので 1 つ足す。
FILE_A="$WORK/cwd/write-rule.txt"
run_claude "{\"permissions\":{\"deny\":[\"Write(/$FILE_A)\"]}}" \
    "Use the Write tool to create the file $FILE_A with the content hello. Then reply DONE and nothing else."
if [ -f "$FILE_A" ]; then
    pass "3. deny の Write(path) は照合されず、Write ツールが通った"
else
    fail "3. deny の Write(path) が効いた、または Write ツールが走らなかった (前提 (a) が崩れた可能性。再実行して確かめること)"
fi

# --- 4: Edit(path) の deny は Write ツールにも効く ---
FILE_B="$WORK/cwd/edit-rule.txt"
run_claude "{\"permissions\":{\"deny\":[\"Edit(/$FILE_B)\"]}}" \
    "Use the Write tool to create the file $FILE_B with the content hello. If it is refused, reply REFUSED. Otherwise reply DONE."
if [ ! -f "$FILE_B" ]; then
    pass "4. deny の Edit(path) が Write ツールを止めた"
else
    fail "4. deny の Edit(path) が Write ツールを止めなかった (前提 (a) が崩れた)"
fi

if [ "$FAILED" -eq 0 ]; then
    echo "結果: 全項目が期待どおり"
    exit 0
fi
echo "結果: 期待と違う項目がある"
exit 1
