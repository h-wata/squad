#!/usr/bin/env bash
# test_pi_log_triage_masking.sh — scripts/pi-log-triage.sh の secret マスク (B4) を検証する。
#
# 実 pi (local-vllm) は叩かない。PATH の先頭に置いたスタブ `pi` が、実際に呼ばれた
# プロンプトから「Pi に読ませるログのパス」を抽出し、その内容をコピーして残す。
# pi-log-triage.sh 自身は変更していない (呼び出し元の環境変数 PI_STUB_CAPTURE をスタブ
# 側が読むだけ)。
#
# 使い方: bash scripts/tests/test_pi_log_triage_masking.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TRIAGE="$REPO_ROOT/scripts/pi-log-triage.sh"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

FAKE_BIN="$WORKDIR/bin"
mkdir -p "$FAKE_BIN"

# 実 pi の代わりに、渡されたプロンプト文字列から「入力ログは <path> です。」の <path> を
# 抜き出し、$PI_STUB_CAPTURE にコピーする。pi-log-triage.sh の出力検証を通すため、
# 妥当な YAML も標準出力に返す。
cat > "$FAKE_BIN/pi" <<'FAKE_PI'
#!/usr/bin/env bash
set -euo pipefail
prompt="${@: -1}"
path="$(printf '%s' "$prompt" | sed -n 's/^.*入力ログは \(.*\) です。.*$/\1/p')"
if [ -n "${PI_STUB_CAPTURE:-}" ] && [ -n "$path" ] && [ -f "$path" ]; then
  cp "$path" "$PI_STUB_CAPTURE"
fi
cat <<'YAML'
failed_step: "n/a"
failure_signals: []
candidate_causes: []
next_check: "n/a"
unknowns: []
YAML
FAKE_PI
chmod +x "$FAKE_BIN/pi"
export PATH="$FAKE_BIN:$PATH"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "FAIL: $1"; }

INPUT_LOG="$WORKDIR/input.log"
# heredoc ではなく individual echo にする: 行末に detect-secrets 用の
# pragma コメントを安全に付けるため (heredoc 本文にコメントを書くとそのまま
# ログの中身になってしまう)。ここに出てくる値はすべてテスト用のダミー。
{
  echo '2026-08-20T00:00:00Z Authorization: Bearer sk-should-be-masked-1234567890'  # pragma: allowlist secret
  echo '2026-08-20T00:00:01Z curl -H "Authorization: token ghp_1234567890abcdefghijklmnopqrstuvwxzz"'  # pragma: allowlist secret
  echo '2026-08-20T00:00:02Z export AKIAABCDEFGHIJKLMNOP and aws_secret_access_key=abcd1234EXAMPLEsecretkeyvalue0000000000'  # pragma: allowlist secret
  echo '2026-08-20T00:00:03Z git clone https://alice:s3cr3tpassw0rd@github.com/example/repo.git'  # pragma: allowlist secret
  echo '2026-08-20T00:00:04Z -----BEGIN OPENSSH PRIVATE KEY-----'  # pragma: allowlist secret
  echo '2026-08-20T00:00:05Z b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY'  # pragma: allowlist secret
  echo '2026-08-20T00:00:06Z -----END OPENSSH PRIVATE KEY-----'  # pragma: allowlist secret
  echo '2026-08-20T00:00:07Z FAILED tests/test_x.py::test_y'
} > "$INPUT_LOG"

OUT_YAML="$WORKDIR/out.yaml"
CAPTURE="$WORKDIR/seen_log.txt"

echo "=== pi-log-triage.sh を実行し、Pi (スタブ) が実際に読んだログを捕捉する ==="
PI_STUB_CAPTURE="$CAPTURE" bash "$TRIAGE" "$INPUT_LOG" "$OUT_YAML" 30
echo "--- saved output ---"
cat "$OUT_YAML"

if [ -f "$CAPTURE" ]; then
  pass "capture: Pi に渡されたログのコピーを取得できた"
else
  fail "capture: Pi に渡されたログを取得できなかった (プロンプトのパス抽出失敗?)"
  echo "結果: PASS=$PASS FAIL=$FAIL"
  exit 1
fi

echo
echo "--- Pi (スタブ) が実際に見たログ ---"
cat "$CAPTURE"
echo "---"

assert_not_leaked() {
  local needle="$1" desc="$2"
  if grep -qF "$needle" "$CAPTURE"; then
    fail "leak: $desc がマスクされずに Pi へ渡された ('$needle' が見つかった)"
  else
    pass "masked: $desc はマスクされて Pi へ渡らなかった"
  fi
}

assert_not_leaked "sk-should-be-masked-1234567890" "Bearer token"  # pragma: allowlist secret
assert_not_leaked "ghp_1234567890abcdefghijklmnopqrstuvwxzz" "GitHub token (ghp_)"  # pragma: allowlist secret
assert_not_leaked "AKIAABCDEFGHIJKLMNOP" "AWS access key ID"  # pragma: allowlist secret
assert_not_leaked "abcd1234EXAMPLEsecretkeyvalue0000000000" "AWS secret (aws_secret_access_key=...)"  # pragma: allowlist secret
assert_not_leaked "s3cr3tpassw0rd" "URL 埋め込み credential"  # pragma: allowlist secret
assert_not_leaked "b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY" "SSH private key body"  # pragma: allowlist secret

if grep -qF "FAILED tests/test_x.py::test_y" "$CAPTURE"; then
  pass "structure: 切り分けに必要な診断行 (FAILED ...) はマスクで壊れず残った"
else
  fail "structure: 非 secret の診断行までマスクで消えてしまった"
fi

lines_in="$(wc -l < "$INPUT_LOG")"
lines_out="$(wc -l < "$CAPTURE")"
if [ "$lines_in" -eq "$lines_out" ]; then
  pass "structure: マスク前後で行数が一致する (行番号参照が壊れない): $lines_in"
else
  fail "structure: マスクで行数が変わった (in=$lines_in out=$lines_out)、行番号参照が壊れる"
fi

# Pi に渡したのは元ログと別ファイルであること (indirection が実際に効いていることの確認)。
if [ "$CAPTURE" != "$INPUT_LOG" ]; then
  pass "indirection: マスク済みコピーが元ログとは別ファイルとして Pi に渡された"
else
  fail "indirection: 元ログのパスがそのまま Pi に渡っていた"
fi

# 元の入力ログ自体は変更されていないこと (ヘッダの「入力ログそのものは変更しない」契約)。
if grep -qF "sk-should-be-masked-1234567890" "$INPUT_LOG"; then
  pass "original: 元の入力ログ自体は無加工のまま残っている"
else
  fail "original: 元の入力ログまで書き換わってしまった"
fi

echo
echo "================================================================"
echo "結果: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
