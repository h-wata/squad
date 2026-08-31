#!/usr/bin/env bash
# local-coder が使う pi の接続設定を ~/.pi/agent/models.json に配置する。
#
# agent 定義 (.claude/agents/local-coder.md) はリポジトリで追跡されるが、pi の
# provider 設定はホーム配下にあり追跡されない。片方だけ持っていくと、agent は
# あるのに接続先が無いという状態になる (旧 vllm-consultant がこの壊れ方をした)。
#
# 以前は symlink を張っていたが、リポジトリ側に LAN の IP を直書きすることに
# なっていた。テンプレートを展開する方式に変え、ホストを LLM_HOST で渡す。
# **symlink ではなく生成ファイルになったので、テンプレートを編集したら
# 張り直しではなく再実行が要る。**
#
# 使い方:
#   ./scripts/link-pi-config.sh [--force]
#   LLM_HOST=other-host.example ./scripts/link-pi-config.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/config/pi/models.json.template"
DEST="$HOME/.pi/agent/models.json"
# 既定は LAN の vLLM ホスト。DNS (cs.local) で引ける前提。IP で渡してもよい。
LLM_HOST="${LLM_HOST:-dell-server01.cs.local}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

[ -f "$TEMPLATE" ] || { echo "エラー: $TEMPLATE が無い"; exit 1; }

# 名前解決できないまま配置すると、pi が起動してから初めて気づくことになる。
if ! getent hosts "$LLM_HOST" >/dev/null 2>&1; then
    echo "警告: $LLM_HOST を名前解決できない。IP を直接渡すなら:"
    echo "  LLM_HOST=192.168.0.x $0 ${1:-}"
    [ "$FORCE" -ne 1 ] && { echo "続けるなら --force を付ける。"; exit 1; }
fi

rendered="$(mktemp)"
trap 'rm -f "$rendered"' EXIT
sed "s|__LLM_HOST__|$LLM_HOST|g" "$TEMPLATE" > "$rendered"
python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$rendered" \
    || { echo "エラー: 展開後の JSON が壊れている"; exit 1; }

if [ -L "$DEST" ]; then
    # 旧方式の symlink が残っている。実ファイルに置き換える。
    echo "旧方式の symlink を検出: $DEST -> $(readlink -f "$DEST")"
    backup="$DEST.bak.$(date +%Y%m%d-%H%M%S)"
    cp -P "$DEST" "$backup"
    echo "退避: $backup"
    rm -f "$DEST"
elif [ -e "$DEST" ]; then
    # 実ファイルを黙って壊さない。他の provider が入っていることがある。
    if diff -q "$rendered" "$DEST" >/dev/null 2>&1; then
        echo "既に同一の内容: $DEST"
        exit 0
    fi
    echo "既存の $DEST はリポジトリの設定と内容が違う。差分:"
    diff -u "$DEST" "$rendered" || true
    if [ "$FORCE" -ne 1 ]; then
        echo
        echo "退避せずに上書きはしない。内容を確認して、進めるなら --force を付けて再実行する。"
        exit 1
    fi
    backup="$DEST.bak.$(date +%Y%m%d-%H%M%S)"
    cp -a "$DEST" "$backup"
    echo "退避: $backup"
fi

mkdir -p "$(dirname "$DEST")"
cp "$rendered" "$DEST"
echo "配置完了: $DEST (LLM_HOST=$LLM_HOST)"
echo
echo "疎通確認:"
echo "  curl -fsS -m 5 -H 'Authorization: Bearer sk-local-dummy' http://$LLM_HOST:4000/v1/models"
