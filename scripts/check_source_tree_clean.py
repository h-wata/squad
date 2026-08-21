#!/usr/bin/env python3
# ruff: noqa: CPY001
"""report の source_tree_clean 自己申告を機械検証する (SQUAD-249, SQUAD-248 NB1).

`source_worktree` と `status_command` が独立した自由記述だと、対象パスと実際に
`git status -s` を実行したパスが食い違っていても `source_tree_status` が空文字列
であることだけで見かけ上 clean を装える。`status_command` を
`git -C <source_worktree> status -s` の固定書式に限定し、`<source_worktree>` を
report 自身の `source_worktree` フィールドと文字列一致させることで、この「守った
ふり」を機械的に検出する。

使い方: check_source_tree_clean.py <report.yaml> [...]
  各 report について検証し、違反があれば理由を stderr に列挙して exit 1 する。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import parse_scalars  # noqa: E402

REQUIRED_FIELDS = ('source_worktree', 'status_command', 'checked_at', 'source_tree_status')

# source_worktree の許可文字 (allowlist)。英数字 / ASCII path 記号のみで、シェルメタ文字
# (`;` `&` `|` `` ` `` `$` `(` `)` `<` `>`)・空白・改行/タブ等の制御文字を含め一切通さない
# (SQUAD-254 B2)。denylist ではなく allowlist にしたのは、シェル特殊文字の一覧は環境や
# シェルによって漏れがあり得るため。この repo の実際の worktree パス
# (/home/gisen/work/squad-wt-squad249 等) は英数字と `/ _ - .` のみで構成されており、
# 非 ASCII 文字は許可対象に含めていない。
_SAFE_WORKTREE_RE = re.compile(r'[A-Za-z0-9/_.~-]+')


def has_safe_worktree_characters(source_worktree: str) -> bool:
    """`source_worktree` がシェルへ渡しても単一トークンとして解釈される文字だけか.

    `re.match` + `^...$` は使わない。Python の `re` は `$` が文字列末尾の改行の
    "直前" にもマッチする仕様のため、`match()` は末尾に改行が付いた文字列に対しても
    先頭からの部分一致を真として返してしまい、末尾改行だけを混入させた偽装が
    allowlist を素通りする (SQUAD-256)。`fullmatch` は文字列全体を消費しない限り
    真を返さないため、この末尾改行 bypass が起きない。
    """
    return bool(_SAFE_WORKTREE_RE.fullmatch(source_worktree))


def status_command_for(source_worktree: str) -> str:
    """`source_worktree` に対して唯一許される `status_command` の固定書式."""
    return f'git -C {source_worktree} status -s'


def check_source_tree_clean(meta: dict[str, str]) -> list[str]:
    """Report の top-level scalars を検証し、違反理由のリストを返す (空なら clean 確認 OK).

    「フィールドが存在しない (欠損)」と「フィールドは存在するが値が空文字列 (=clean の
    明示的な申告)」を区別する。`dict.get(key, '')` だけでは両者を区別できず、何も
    申告していない report が見かけ上 clean と判定されてしまう (SQUAD-252 B1)。
    """
    errors: list[str] = [
        f'{field} が report に存在しません (必須フィールド欠損)' for field in REQUIRED_FIELDS if field not in meta
    ]

    worktree = meta.get('source_worktree', '')
    status_command = meta.get('status_command', '')
    if not worktree:
        errors.append('source_worktree が空です')
    elif not Path(worktree).is_absolute():
        # 末尾スラッシュ / `..` を含む絶対パス / symlink 経由の絶対パスは同一ディレクトリへの
        # 別表記として許容する (正規化はしない)。ここで弾くのは絶対パスでないものだけ
        # (SQUAD-252 B2: '.' のような CWD 依存の値が通っていた)。
        errors.append(f'source_worktree が絶対パスではありません: {worktree!r}')
    elif not has_safe_worktree_characters(worktree):
        # `;` 等のシェルメタ文字を含む source_worktree は is_absolute() と文字列一致だけでは
        # 弾けない。status_command をシェルで実行すると `;` 以降が別コマンドとして走り、
        # dirty な申告対象ではなく別の clean なディレクトリの status を偽装できる
        # (SQUAD-254 B2)。
        errors.append(f'source_worktree にシェルメタ文字または空白が含まれています: {worktree!r}')
    else:
        expected = status_command_for(worktree)
        if status_command != expected:
            errors.append(f'status_command が固定書式と不一致: {status_command!r} (期待: {expected!r})')

    checked_at = meta.get('checked_at', '')
    if not checked_at:
        errors.append('checked_at が空です')
    else:
        try:
            parsed = datetime.fromisoformat(checked_at)
        except ValueError:
            errors.append(f'checked_at が ISO8601 として解釈できません: {checked_at!r}')
        else:
            if parsed.tzinfo is None:
                errors.append(f'checked_at にタイムゾーンがありません: {checked_at!r}')

    if 'source_tree_status' in meta and meta['source_tree_status'].strip():
        errors.append('source_tree_status が空ではありません (作業ツリーが clean ではない)')

    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: check_source_tree_clean.py <report.yaml> [...]', file=sys.stderr)
        return 1

    exit_code = 0
    for raw_path in argv:
        path = Path(raw_path)
        meta = parse_scalars(path.read_text())
        errors = check_source_tree_clean(meta)
        if errors:
            exit_code = 1
            print(f'{path}: NG', file=sys.stderr)
            for err in errors:
                print(f'  - {err}', file=sys.stderr)
        else:
            print(f'{path}: OK (source_tree_clean)')

    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
