#!/usr/bin/env bash
# opencode-bootstrap.sh — Opencode worker に最初に送る bootstrap 文を標準出力に出す。
#
# start.sh (起動時の --prompt) と notify-worker.sh (--clear で /new した後) の両方が使う。
# 文面を 1 か所に置くため、ここだけで定義する (#71)。
#
# 使い方: scripts/opencode-bootstrap.sh <worker_num>
set -euo pipefail

n="${1:?usage: opencode-bootstrap.sh <worker_num>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
prompt_file="$ROOT/squad/state/worker$n-opencode-instructions.md"

echo "あなたは squad の Worker $n です。まず $prompt_file を Read して運用ルールを頭に入れてください。\
読み終えたら「W$n 待機中」とだけ出力して停止すること。\
起動直後のこのターンでは、それ以外を一切しないこと: queue/ や他プロジェクトを探索しない、\
既存の task YAML / report を読まない、ファイルを変更しない、git / gh コマンドを打たない。\
Dispatcher が tmux 経由で task YAML の絶対パスを通知してくるまで待機します。"
