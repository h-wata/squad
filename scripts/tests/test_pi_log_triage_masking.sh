#!/usr/bin/env bash
# test_pi_log_triage_masking.sh — scripts/pi-log-triage.sh の secret マスク (B4) を検証する。
#
# 各マスク規則を、他規則のトリガー文字列を一切含まない専用の入力行でテストする
# (2巡目 cross-review B4: 従来の複合ログでは Authorization 規則を無効化しても
# 他規則が同じ行の secret を隠してしまい、テストが赤くならなかった)。
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

# 1件の独立した secret 入力を pi-log-triage.sh に通し、Pi (スタブ) が実際に読んだ
# コピーに secret が残っていないことを確認する。log_lines は他規則のトリガー文字列を
# 一切含まない専用の行にすること。
run_masking_case() {
  local name="$1" secret="$2"; shift 2
  local dir input out capture
  dir="$(mktemp -d)"
  input="$dir/input.log"
  out="$dir/out.yaml"
  capture="$dir/seen.txt"
  printf '%s\n' "$@" > "$input"

  set +e
  PI_STUB_CAPTURE="$capture" bash "$TRIAGE" "$input" "$out" 30 >/dev/null 2>&1
  set -e

  if [ ! -f "$capture" ]; then
    fail "$name: Pi に渡されたログを取得できなかった"
  elif grep -qF "$secret" "$capture"; then
    fail "$name: マスクされずに Pi へ渡された ('$secret' が見つかった)"
  else
    pass "$name: マスクされて Pi へ渡らなかった"
  fi
  rm -rf "$dir"
}

echo "=== 各マスク規則を、他規則に依存しない独立入力でテストする ==="

run_masking_case "Authorization (Basic, timestamp接頭辞, 全大文字)" \
  "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" \
  '2026-08-19T16:18:00Z AUTHORIZATION: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=='

run_masking_case "Authorization (小文字, 行頭でない)" \
  "dGVzdC1sb3dlcmNhc2UtdmFsdWU=" \
  'curl -H "authorization: Basic dGVzdC1sb3dlcmNhc2UtdmFsdWU="'

run_masking_case "Bearer token (Authorization ヘッダを介さない単独出現)" \
  "sk-should-be-masked-1234567890" \
  '2026-08-20T00:00:01Z token=Bearer sk-should-be-masked-1234567890'

# detect-secrets はコメントと同じ行にある secret しか allowlist できないため、
# `\` 継続行の引数には付けられない。ここだけ変数に出してから渡す。
gh_token_ghp='ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # pragma: allowlist secret
gh_line_ghp='2026-08-20T00:00:02Z export TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'  # pragma: allowlist secret
run_masking_case "GitHub token (ghp_)" "$gh_token_ghp" "$gh_line_ghp"

gh_token_gho='gho_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'  # pragma: allowlist secret
gh_line_gho='2026-08-20T00:00:02Z export TOKEN=gho_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'  # pragma: allowlist secret
run_masking_case "GitHub token (gho_)" "$gh_token_gho" "$gh_line_gho"

gh_token_ghu='ghu_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC'  # pragma: allowlist secret
gh_line_ghu='2026-08-20T00:00:02Z export TOKEN=ghu_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC'  # pragma: allowlist secret
run_masking_case "GitHub token (ghu_)" "$gh_token_ghu" "$gh_line_ghu"

gh_token_ghs='ghs_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD'  # pragma: allowlist secret
gh_line_ghs='2026-08-20T00:00:02Z export TOKEN=ghs_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD'  # pragma: allowlist secret
run_masking_case "GitHub token (ghs_)" "$gh_token_ghs" "$gh_line_ghs"

gh_token_pat='github_pat_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE'  # pragma: allowlist secret
gh_line_pat='2026-08-20T00:00:02Z export TOKEN=github_pat_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE'  # pragma: allowlist secret
run_masking_case "GitHub token (github_pat_)" "$gh_token_pat" "$gh_line_pat"

aws_key='AKIAABCDEFGHIJKLMNOP'  # pragma: allowlist secret
aws_key_line='2026-08-20T00:00:03Z AKIAABCDEFGHIJKLMNOP is the key id'  # pragma: allowlist secret
run_masking_case "AWS access key ID" "$aws_key" "$aws_key_line"

aws_secret='abcd1234EXAMPLEsecretkeyvalue0000000000'  # pragma: allowlist secret
aws_secret_line='2026-08-20T00:00:04Z aws_secret_access_key=abcd1234EXAMPLEsecretkeyvalue0000000000'  # pragma: allowlist secret
run_masking_case "AWS secret (aws_secret_access_key=...)" "$aws_secret" "$aws_secret_line"

url_cred_secret='s3cr3tpassw0rd'  # pragma: allowlist secret
url_cred_line='2026-08-20T00:00:05Z git clone https://alice:s3cr3tpassw0rd@github.com/example/repo.git'  # pragma: allowlist secret
run_masking_case "URL 埋め込み credential" "$url_cred_secret" "$url_cred_line"

ssh_body='b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY'  # pragma: allowlist secret
ssh_begin='2026-08-20T00:00:06Z -----BEGIN OPENSSH PRIVATE KEY-----'  # pragma: allowlist secret
ssh_body_line='2026-08-20T00:00:07Z b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY'  # pragma: allowlist secret
ssh_end='2026-08-20T00:00:08Z -----END OPENSSH PRIVATE KEY-----'
run_masking_case "SSH private key body" "$ssh_body" "$ssh_begin" "$ssh_body_line" "$ssh_end"

echo
echo "=== 構造チェック: 非 secret 行の保持・行数維持・indirection・原本無変更 ==="

STRUCT_DIR="$(mktemp -d)"
STRUCT_INPUT="$STRUCT_DIR/input.log"
STRUCT_OUT="$STRUCT_DIR/out.yaml"
STRUCT_CAPTURE="$STRUCT_DIR/seen.txt"
struct_auth='2026-08-20T00:00:00Z AUTHORIZATION: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=='  # pragma: allowlist secret
struct_ssh_begin='2026-08-20T00:00:01Z -----BEGIN OPENSSH PRIVATE KEY-----'  # pragma: allowlist secret
struct_ssh_body='2026-08-20T00:00:02Z b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY'  # pragma: allowlist secret
struct_ssh_end='2026-08-20T00:00:03Z -----END OPENSSH PRIVATE KEY-----'
struct_failed='2026-08-20T00:00:04Z FAILED tests/test_x.py::test_y'
printf '%s\n' "$struct_auth" "$struct_ssh_begin" "$struct_ssh_body" "$struct_ssh_end" "$struct_failed" \
  > "$STRUCT_INPUT"
PI_STUB_CAPTURE="$STRUCT_CAPTURE" bash "$TRIAGE" "$STRUCT_INPUT" "$STRUCT_OUT" 30 >/dev/null 2>&1

echo "--- Pi (スタブ) が実際に見たログ (構造チェック用) ---"
cat "$STRUCT_CAPTURE" 2>&1 || true
echo "---"

if grep -qF "FAILED tests/test_x.py::test_y" "$STRUCT_CAPTURE"; then
  pass "structure: 切り分けに必要な診断行 (FAILED ...) はマスクで壊れず残った"
else
  fail "structure: 非 secret の診断行までマスクで消えてしまった"
fi

lines_in="$(wc -l < "$STRUCT_INPUT")"
lines_out="$(wc -l < "$STRUCT_CAPTURE")"
if [ "$lines_in" -eq "$lines_out" ]; then
  pass "structure: マスク前後で行数が一致する (行番号参照が壊れない): $lines_in"
else
  fail "structure: マスクで行数が変わった (in=$lines_in out=$lines_out)、行番号参照が壊れる"
fi

if [ "$STRUCT_CAPTURE" != "$STRUCT_INPUT" ]; then
  pass "indirection: マスク済みコピーが元ログとは別ファイルとして Pi に渡された"
else
  fail "indirection: 元ログのパスがそのまま Pi に渡っていた"
fi

if grep -qF "QWxhZGRpbjpvcGVuIHNlc2FtZQ==" "$STRUCT_INPUT"; then
  pass "original: 元の入力ログ自体は無加工のまま残っている"
else
  fail "original: 元の入力ログまで書き換わってしまった"
fi
rm -rf "$STRUCT_DIR"

echo
echo "================================================================"
echo "結果: PASS=$PASS FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
exit 0
