#!/bin/bash
# watch.sh — squad/watchd.py (Python, stdlib only) を呼ぶ薄いラッパ。
#
# 本体は squad/watchd.py へ全面移植した (Issue #26)。pidfile・起動コマンド・env
# (SQUAD_SESSION 等) の互換を保つため、start.sh / stop.sh / README からは従来通り
# `watch.sh` を叩けば動く。
set -eu
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/squad/watchd.py" "$@"
