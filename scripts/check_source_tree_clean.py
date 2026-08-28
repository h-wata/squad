#!/usr/bin/env python3
# ruff: noqa: CPY001
"""report の source_tree_clean 自己申告を機械検証する (SQUAD-249, SQUAD-248 NB1).

`source_worktree` と `status_command` が独立した自由記述だと、対象パスと実際に
`git status -s` を実行したパスが食い違っていても `source_tree_status` が空文字列
であることだけで見かけ上 clean を装える。`status_command` を
`git -C <source_worktree> status -s` の固定書式に限定し、`<source_worktree>` を
report 自身の `source_worktree` フィールドと文字列一致させることで、この「守った
ふり」を機械的に検出する。

report は 2 通りの書き方を受け付ける (SQUAD-251):
  (1) フラット形式: `source_worktree` / `status_command` / `checked_at` /
      `source_tree_status` を report のトップレベルに直接書く (単一 worktree、
      `queue/templates/report.yaml` の既定形式)。
  (2) `source_tree_clean:` ネスト形式: 上記 4 フィールドをその下に
      マッピング (単一 worktree) またはリスト (複数 worktree) で書く。
      rmf_ros2 と rmf_traffic のように複数リポジトリを同時に確認する
      タスクで使う。

どちらの形式で書かれていても、各 worktree エントリごとに同じ検証を行う。

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

# ledger.parse_scalars と同じ「key: value」の行パーサ・block scalar マーカー定義。
# private な ledger._SCALAR_RE / ledger._BLOCK_MARKERS は import しない
# (SLF 越境を避けるため、同じ定義を独立して持つ)。
_LINE_KV_RE = re.compile(r'^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$')
_BLOCK_MARKERS = ('|', '>', '|-', '>-', '|+', '>+')
_SOURCE_TREE_CLEAN_HEADER_RE = re.compile(r'^source_tree_clean:\s*$')

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


def _unquote(val: str) -> str:
    """`parse_scalars` と同じ引用符除去 (先頭末尾が同じ引用符なら剥がす)."""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
        return val[1:-1]
    return val


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _consume_block_scalar(lines: list[str], start: int, key_indent: int) -> tuple[str, int]:
    r"""`key: |` の直後から、`key_indent` より深いインデントの本文行を読む.

    `source_tree_status` は実際の `git status -s` の生出力 (複数行) を
    そのまま書くために block scalar (`|`) で書かれることが多い
    (worker1 の実 report がそう)。チョンピング指定 (`|-` / `|+` 等) の
    厳密な改行差異は区別せず、本文行を `\n` で連結するだけの最小実装。
    """
    body: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _line_indent(line) <= key_indent:
            break
        body.append(line.strip())
        i += 1
    return '\n'.join(body), i


def _parse_entry_lines(lines: list[str], start: int, end: int, item_indent: int) -> dict[str, str]:
    """`[start, end)` の範囲 (1 エントリ分) から `key: value` を集めて dict にする.

    `item_indent` はこのエントリの `key:` 行が並ぶインデント幅
    (リスト形式ならダッシュの次の桁、マッピング形式ならブロックの
    インデント幅そのもの)。
    """
    entry: dict[str, str] = {}
    i = start
    while i < end:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        m = _LINE_KV_RE.match(line.strip())
        if not m:
            i += 1
            continue
        key, val = m.group(1), _unquote(m.group(2))
        i += 1
        if val in _BLOCK_MARKERS:
            body, i = _consume_block_scalar(lines, i, item_indent)
            entry[key] = body
        else:
            entry[key] = val
    return entry


def extract_worktree_entries(text: str) -> list[dict[str, str]]:
    """Report 本文から検証対象の worktree エントリを 1 つ以上取り出す.

    `source_tree_clean:` キーが無ければ、旧来どおりトップレベルの
    `source_worktree` 等をそのまま 1 エントリとして扱う (フラット形式、
    後方互換)。`source_tree_clean:` があれば、その下のインデントブロックを
    リスト (`- key: value` の繰り返し = 複数 worktree) またはマッピング
    (`key: value` の並び = 単一 worktree) として解釈する。`source_tree_status`
    が block scalar (`|`) で書かれている場合も本文を読む。
    """
    lines = text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if _SOURCE_TREE_CLEAN_HEADER_RE.match(line)), None)

    if header_idx is None:
        flat = parse_scalars(text)
        return [flat] if flat else [{}]

    i = header_idx + 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or not lines[i][0].isspace():
        return [{}]

    block_indent = _line_indent(lines[i])
    is_list = lines[i].lstrip().startswith('- ')

    if not is_list:
        end = i
        while end < len(lines) and (not lines[end].strip() or _line_indent(lines[end]) >= block_indent):
            end += 1
        return [_parse_entry_lines(lines, i, end, block_indent)]

    entries: list[dict[str, str]] = []
    item_starts: list[int] = []
    j = i
    while j < len(lines):
        line = lines[j]
        if line.strip() and _line_indent(line) < block_indent:
            break
        if line.strip() and _line_indent(line) == block_indent and line.lstrip().startswith('- '):
            item_starts.append(j)
        j += 1
    block_end = j

    for idx, item_start in enumerate(item_starts):
        item_end = item_starts[idx + 1] if idx + 1 < len(item_starts) else block_end
        # ダッシュ行自体も `- key: value` を含みうるので、ダッシュを剥がした
        # 仮想行として先頭行を差し込み、続きの行 (item_start+1 .. item_end) と
        # まとめて 1 エントリとして解釈する。
        dash_line = lines[item_start]
        key_col = _line_indent(dash_line) + 2
        virtual_first_line = ' ' * key_col + dash_line.lstrip()[2:]
        entry_lines = [*lines[:item_start], virtual_first_line, *lines[item_start + 1 :]]
        entries.append(_parse_entry_lines(entry_lines, item_start, item_end, key_col))

    return entries if entries else [{}]


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: check_source_tree_clean.py <report.yaml> [...]', file=sys.stderr)
        return 1

    exit_code = 0
    for raw_path in argv:
        path = Path(raw_path)
        entries = extract_worktree_entries(path.read_text())

        all_errors: list[str] = []
        for idx, entry in enumerate(entries, start=1):
            label = entry.get('source_worktree') or f'entry {idx}/{len(entries)}'
            all_errors.extend(f'[{label}] {err}' for err in check_source_tree_clean(entry))

        if all_errors:
            exit_code = 1
            print(f'{path}: NG', file=sys.stderr)
            for err in all_errors:
                print(f'  - {err}', file=sys.stderr)
        else:
            print(f'{path}: OK (source_tree_clean, {len(entries)} worktree(s))')

    return exit_code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
