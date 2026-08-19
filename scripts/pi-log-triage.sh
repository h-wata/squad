#!/usr/bin/env bash
# pi-log-triage.sh — CI / test / lint ログを Pi で根拠付きに絞り込む。
#
# Usage: pi-log-triage.sh <input-log> <output-yaml> [timeout-seconds]
#
# 入力ログそのものは変更しない。Pi には read/grep/find/ls だけを許可し、出力は
# 一時ファイルを経由して原子的に保存する。判断を含むレビュー・仕様・task YAML
# には使わないこと。
#
# Pi に渡す前に、既知の secret パターン (Authorization ヘッダ、GitHub token、AWS
# キー、Bearer token、URL 埋め込み credential、SSH 秘密鍵ブロック) をマスクした
# 一時コピーを作り、Pi にはそのコピーだけを読ませる (元ログは無傷)。local-vllm は
# LAN 上のサービスであり、CI ログに紛れ込んだ secret がそのまま送信されるのを防ぐ
# ための最低限の対策。既知パターン外の secret は防げないので、CI ログに
# secret を残さない (secret はログに出さない/マスクする) 運用が引き続き前提となる。

set -euo pipefail

# 送信前に既知の secret パターンをマスクする。行数を変えない (置換のみ) ことで、
# Pi が返す行番号がマスク後ログの行番号として一貫するようにする。
#
# 前段の awk は RFC 7230 の obsolete line folding (Authorization: ヘッダの値が
# 次行以降に続き、継続行が空白/タブで始まる形式) を検出し、継続行を丸ごと
# 置換する。ヘッダ行自体は awk では触らず、後段の sed の Authorization 規則に
# 任せる (1行ごとの置換だけで完結させ、行数を変えない設計を維持するため)。
mask_secrets() {
    awk '
        BEGIN { fold = 0 }
        {
            line = $0
            if (fold && (line ~ /^[ \t]/)) {
                print "[REDACTED FOLDED HEADER VALUE]"
                next
            }
            fold = 0
            if (tolower(line) ~ /authorization"?[ \t]*:/) {
                fold = 1
            }
            print line
        }
    ' | sed -E \
        -e '/-----BEGIN [A-Z ]*PRIVATE KEY-----/,/-----END [A-Z ]*PRIVATE KEY-----/{
              /-----BEGIN [A-Z ]*PRIVATE KEY-----/b
              /-----END [A-Z ]*PRIVATE KEY-----/b
              s/.*/[REDACTED PRIVATE KEY LINE]/
            }' \
        -e 's/(Authorization"?[[:space:]]*:[[:space:]]*).*/\1<redacted>/gI' \
        -e 's/\bBearer[[:space:]]+[A-Za-z0-9._~+\/=-]+/Bearer <redacted>/g' \
        -e 's/\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b/\1_<redacted>/g' \
        -e 's/\bgithub_pat_[A-Za-z0-9_]{20,}\b/github_pat_<redacted>/g' \
        -e 's/\bAKIA[0-9A-Z]{16}\b/AKIA<redacted>/g' \
        -e 's/(aws_secret_access_key[[:space:]]*[:=][[:space:]]*)[^[:space:]]+/\1<redacted>/gI' \
        -e 's#(https?://)[^/@[:space:]]+:[^/@[:space:]]+@#\1<redacted>@#g'
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    echo "Usage: $0 <input-log> <output-yaml> [timeout-seconds]" >&2
    exit 64
fi

INPUT_LOG="$1"
OUTPUT_YAML="$2"
TIMEOUT_SECONDS="${3:-120}"

if [ ! -f "$INPUT_LOG" ] || [ ! -r "$INPUT_LOG" ]; then
    echo "error: input log is not a readable regular file: $INPUT_LOG" >&2
    exit 66
fi

case "$TIMEOUT_SECONDS" in
    ''|*[!0-9]*)
        echo "error: timeout-seconds must be a positive integer" >&2
        exit 64
        ;;
esac

if [ "$TIMEOUT_SECONDS" -eq 0 ]; then
    echo "error: timeout-seconds must be greater than zero" >&2
    exit 64
fi

if [ -e "$OUTPUT_YAML" ]; then
    echo "error: refusing to overwrite existing output: $OUTPUT_YAML" >&2
    exit 73
fi

OUTPUT_DIR="$(dirname "$OUTPUT_YAML")"
OUTPUT_BASE="$(basename "$OUTPUT_YAML")"
mkdir -p "$OUTPUT_DIR"
TEMP_OUTPUT="$(mktemp "$OUTPUT_DIR/.${OUTPUT_BASE}.XXXXXX")"
MASKED_LOG="$(mktemp)"
mask_secrets < "$INPUT_LOG" > "$MASKED_LOG"
trap 'rm -f "$TEMP_OUTPUT" "$MASKED_LOG"' EXIT

normalize_and_validate_output() {
    python3 - "$TEMP_OUTPUT" <<'PY'
import re
import sys
from pathlib import Path

import yaml


path = Path(sys.argv[1])
text = path.read_text(encoding='utf-8')
fence = re.fullmatch(
    r'```(?:yaml|yml)[ \t]*\r?\n(?P<body>.*?)\r?\n?```[ \t]*\r?\n?\Z',
    text,
    flags=re.DOTALL | re.IGNORECASE,
)
if fence:
    text = fence.group('body')
elif re.search(r'(^|\n)[ \t]*```', text):
    raise SystemExit('error: Pi output has a non-outer Markdown fence')

if text and not text.endswith(chr(10)):
    text += chr(10)


try:
    result = yaml.safe_load(text)
except yaml.YAMLError as exc:
    raise SystemExit(f'error: Pi output is not valid YAML: {exc}') from exc

if not isinstance(result, dict):
    raise SystemExit('error: Pi output must be a YAML mapping')

required = {'failed_step', 'failure_signals', 'candidate_causes', 'next_check', 'unknowns'}
missing = required - result.keys()
if missing:
    raise SystemExit(f"error: Pi output is missing required fields: {', '.join(sorted(missing))}")


def is_line(value: object) -> bool:
    return isinstance(value, (str, int)) and not isinstance(value, bool)


if not isinstance(result['failed_step'], str) or not isinstance(result['next_check'], str):
    raise SystemExit('error: failed_step and next_check must be strings')
if not isinstance(result['failure_signals'], list) or not all(
    isinstance(signal, dict)
    and is_line(signal.get('line'))
    and isinstance(signal.get('text'), str)
    for signal in result['failure_signals']
):
    raise SystemExit('error: failure_signals must contain line/text mappings')
if not isinstance(result['candidate_causes'], list) or not all(
    isinstance(cause, dict)
    and isinstance(cause.get('hypothesis'), str)
    and cause.get('confidence') in {'low', 'medium', 'high'}
    and isinstance(cause.get('evidence_lines'), list)
    and all(is_line(line) for line in cause['evidence_lines'])
    for cause in result['candidate_causes']
):
    raise SystemExit('error: candidate_causes must contain valid hypothesis/evidence mappings')
if not isinstance(result['unknowns'], list) or not all(isinstance(item, str) for item in result['unknowns']):
    raise SystemExit('error: unknowns must be a list of strings')

path.write_text(text, encoding='utf-8')
PY
}

SYSTEM_PROMPT=$(cat <<'PROMPT'
あなたは W5 の CI・テスト・lint ログ一次抽出専任です。

渡されたログだけを、read / grep / find / ls の読み取り専用ツールで調べること。ネットワーク、git、gh、シェル書き込み、ソース編集、他ファイルの探索は行わない。レビュー、仕様、task YAML、報告文の判断・要約には使わない。

出力は Markdown fence や前置きなしの YAML だけにする。確証がなければ root cause と断定せず unknown とし、すべての仮説にログ内の行番号付き根拠を付ける。次の形式を厳守する:

failed_step: "<失敗した job / command。見つからなければ unknown>"
failure_signals:
  - line: "<行番号または unknown>"
    text: "<短い原文断片>"
candidate_causes:
  - hypothesis: "<仮説。断定しない>"
    confidence: low|medium|high
    evidence_lines: ["<行番号>"]
next_check: "<Claude が元ログまたは環境で確認すべき最小の次手>"
unknowns:
  - "<残る不確実性>"
PROMPT
)

PROMPT="入力ログは ${MASKED_LOG} です。ログを機械的に絞り込み、指定形式の YAML を返してください。"

timeout "$TIMEOUT_SECONDS" pi -p \
    --provider local-vllm \
    --model nemotron-35-lightning \
    --no-session \
    --no-context-files \
    --no-extensions \
    --no-skills \
    --no-prompt-templates \
    --no-themes \
    --tools read,grep,find,ls \
    --system-prompt "$SYSTEM_PROMPT" \
    "$PROMPT" < /dev/null > "$TEMP_OUTPUT"

if [ ! -s "$TEMP_OUTPUT" ]; then
    echo "error: Pi returned empty output" >&2
    exit 70
fi

normalize_and_validate_output

mv "$TEMP_OUTPUT" "$OUTPUT_YAML"
trap - EXIT
echo "saved: $OUTPUT_YAML"
