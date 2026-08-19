#!/usr/bin/env bash
# local-coder が使う pi の接続設定を ~/.pi/agent/models.json に symlink する。
#
# agent 定義 (.claude/agents/local-coder.md) はリポジトリで追跡されるが、pi の
# provider 設定はホーム配下にあり追跡されない。片方だけ持っていくと、agent は
# あるのに接続先が無いという状態になる (旧 vllm-consultant がこの壊れ方をした)。
#
# 使い方: ./scripts/link-pi-config.sh [--force]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/config/pi/models.json"
DEST="$HOME/.pi/agent/models.json"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

[ -f "$SRC" ] || { echo "エラー: $SRC が無い"; exit 1; }

if [ -L "$DEST" ]; then
    current="$(readlink -f "$DEST")"
    if [ "$current" = "$(readlink -f "$SRC")" ]; then
        echo "既に link 済み: $DEST -> $SRC"
        exit 0
    fi
    echo "警告: $DEST は別の場所を指す symlink ($current)"
elif [ -e "$DEST" ]; then
    # 実ファイルを黙って壊さない。他の provider が入っていることがある。
    if diff -q "$SRC" "$DEST" >/dev/null 2>&1; then
        echo "内容は同一。symlink に張り替える"
    else
        echo "既存の $DEST はリポジトリの設定と内容が違う。差分:"
        diff -u "$DEST" "$SRC" || true
        if [ "$FORCE" -ne 1 ]; then
            echo
            echo "退避せずに上書きはしない。内容を確認して、進めるなら --force を付けて再実行する。"
            exit 1
        fi
    fi
    backup="$DEST.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$DEST" "$backup"
    echo "退避: $backup"
fi

mkdir -p "$(dirname "$DEST")"
ln -sfn "$SRC" "$DEST"
echo "link 完了: $DEST -> $SRC"
echo
echo "疎通確認:"
echo "  curl -fsS -m 5 \$(python3 -c 'import json;print(json.load(open(\"$SRC\"))[\"providers\"][\"local-vllm\"][\"baseUrl\"])')/models"
