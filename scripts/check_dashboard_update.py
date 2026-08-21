#!/usr/bin/env python3
# ruff: noqa: CPY001
"""dashboard 更新 (dashboard.md / dashboards/<project>.md) を機械検査する (SQUAD-259).

下位モデルへの dashboard-updater 委譲実験の前提として、更新前後の dashboard を
イベント仕様 (events.yaml/json) と突き合わせて検証する。特に「進行中タスクを
『直近の完了タスク』欄に書いてしまう」既知の再発バグ (dashboard.md の Worker
ステータス表『直近のタスク』列、dashboards/<project>.md の完了タスク表群) の
検出を主目的とする。

見出しや列名をハードコードしすぎると壊れやすいため、期待する表・列が見つからない
場合は NG ではなく WARN として報告し、処理を継続する (誤検知で運用を止めない方針)。

使い方: check_dashboard_update.py <before-dir> <after-dir> <events.yaml-or-json>
  <before-dir>/<after-dir> はそれぞれ dashboard.md / dashboard_history.md /
  dashboards/<project>.md / dashboards/<project>_history.md を含むディレクトリ。
"""

from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

import yaml

TABLE_SEP_RE = re.compile(r'^\|[\s:|-]+\|$')
UPDATE_LINE_RE = re.compile(r'^更新: ')
LAST_UPDATE_LINE_RE = re.compile(r'^\*\*最終更新\*\*:')
UPDATE_LINE_MAX_LEN = 120


def load_events(path: Path) -> list[dict]:
    """イベント仕様ファイル (YAML/JSON) を読み込む. JSON は YAML の superset として扱う."""
    return yaml.safe_load(path.read_text())


def read_text(path: Path) -> str | None:
    """ファイルが存在すれば内容を返す、無ければ None (欠損と空文字を区別するため)."""
    return path.read_text() if path.exists() else None


def _split_row(line: str) -> list[str]:
    inner = line.strip()
    if inner.startswith('|'):
        inner = inner[1:]
    if inner.endswith('|'):
        inner = inner[:-1]
    return [cell.strip() for cell in inner.split('|')]


def find_tables(text: str) -> list[dict]:
    """Markdown table を全て見つける. 各要素は {heading, header, start, end, rows}.

    start/end は行 index (0-origin, end は exclusive)。heading は直前に出た見出し行。
    """
    lines = text.splitlines()
    tables: list[dict] = []
    heading = ''
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith('#'):
            heading = line.lstrip('#').strip()
            i += 1
            continue
        if line.strip().startswith('|') and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1].strip()):
            header = _split_row(line)
            start = i
            j = i + 2
            rows = []
            while j < n and lines[j].strip().startswith('|'):
                rows.append(_split_row(lines[j]))
                j += 1
            tables.append({'heading': heading, 'header': header, 'start': start, 'end': j, 'rows': rows})
            i = j
            continue
        i += 1
    return tables


def find_worker_status_table(text: str) -> dict | None:
    """Worker ステータス表 (dashboard.md) を探す."""
    for table in find_tables(text):
        if 'Worker' in table['header'] and '状態' in table['header']:
            return table
    return None


def find_active_task_table(text: str) -> dict | None:
    """Active タスク表 (dashboards/<project>.md) を探す. worktree 列の有無で完了タスク表と区別する."""
    for table in find_tables(text):
        if 'Task' in table['header'] and 'worktree' in table['header']:
            return table
    return None


def find_completed_task_tables(text: str) -> list[dict]:
    """完了タスク表群 (見出しが日付単位で複数に分かれる) を探す. 完了日 列の有無で判定する."""
    return [t for t in find_tables(text) if 'Task' in t['header'] and '完了日' in t['header']]


def worker_row_number(worker: str) -> str:
    """'worker3' のような id から行照合に使う番号だけを取り出す."""
    m = re.search(r'(\d+)', worker)
    return m.group(1) if m else worker


def find_worker_row(table: dict, worker: str) -> list[str] | None:
    """Worker ステータス表から該当 worker の行を探す."""
    if 'Worker' not in table['header']:
        return None
    worker_col = table['header'].index('Worker')
    num = worker_row_number(worker)
    for row in table['rows']:
        if len(row) > worker_col and re.search(rf'\b{re.escape(num)}\b', row[worker_col]):
            return row
    return None


def check_active_task(
    after_text: str, task_id: str, expected_status: str, event_type: str
) -> tuple[list[str], list[str]]:
    """検査項目1: Active タスク表に task_id の行があり、状態が expected_status と一致する."""
    errors: list[str] = []
    warnings: list[str] = []
    table = find_active_task_table(after_text)
    if table is None:
        warnings.append('Active タスク表が見つからない')
        return errors, warnings
    task_col = table['header'].index('Task')
    row = next((r for r in table['rows'] if len(r) > task_col and r[task_col] == task_id), None)
    if row is None:
        if event_type != 'completed':
            errors.append(f'Active タスク表に {task_id} の行が見つからない')
        return errors, warnings
    status_col = next((i for i, h in enumerate(table['header']) if h in ('状態', 'ステータス')), None)
    if status_col is None:
        warnings.append('Active タスク表に状態列が見つからない (進行状況を別表で管理している可能性)')
    elif expected_status and expected_status not in row[status_col]:
        errors.append(
            f'Active タスク表の {task_id} の状態が expected_status と不一致: '
            f'{row[status_col]!r} (期待: {expected_status!r})'
        )
    return errors, warnings


def check_worker_status(after_text: str, worker: str) -> list[str]:
    """検査項目2: Worker ステータス表の該当行の存在確認 (欄名ゆれが大きいため WARN のみ)."""
    table = find_worker_status_table(after_text)
    if table is None:
        return ['Worker ステータス表が見つからない']
    if find_worker_row(table, worker) is None:
        return [f'Worker ステータス表に {worker} の行が見つからない']
    return []


def check_completion_invariant_dashboard(
    before_text: str, after_text: str, worker: str, task_id: str, event_type: str
) -> tuple[list[str], list[str]]:
    """検査項目3/4 (dashboard.md 版): Worker ステータス表『直近のタスク』列の不変性."""
    errors: list[str] = []
    warnings: list[str] = []
    before_table = find_worker_status_table(before_text)
    after_table = find_worker_status_table(after_text)
    if before_table is None or after_table is None:
        warnings.append('Worker ステータス表が見つからない (直近のタスク列チェック省略)')
        return errors, warnings
    col_idx = next((i for i, h in enumerate(after_table['header']) if h == '直近のタスク'), None)
    if col_idx is None:
        warnings.append('「直近のタスク」列が見つからない (欄名ゆれの可能性)')
        return errors, warnings
    before_row = find_worker_row(before_table, worker)
    after_row = find_worker_row(after_table, worker)
    if before_row is None or after_row is None:
        warnings.append(f'Worker ステータス表に {worker} の行が見つからない (直近のタスク列チェック省略)')
        return errors, warnings
    before_val = before_row[col_idx] if len(before_row) > col_idx else ''
    after_val = after_row[col_idx] if len(after_row) > col_idx else ''
    if event_type != 'completed':
        if before_val != after_val:
            errors.append(
                f'event_type={event_type!r} なのに直近のタスク列が変化している (既知の再発バグ): '
                f'{before_val!r} -> {after_val!r}'
            )
    elif task_id not in after_val:
        errors.append(f'completed イベントなのに直近のタスク列に {task_id} が含まれない: {after_val!r}')
    return errors, warnings


def _completed_rows(text: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for table in find_completed_task_tables(text):
        rows.extend(tuple(r) for r in table['rows'])
    return rows


def check_completion_invariant_project(
    before_text: str, after_text: str, task_id: str, event_type: str
) -> tuple[list[str], list[str]]:
    """検査項目3/4 (dashboards/<project>.md 版): 完了タスク表群の不変性."""
    errors: list[str] = []
    warnings: list[str] = []
    if not find_completed_task_tables(after_text):
        warnings.append('完了タスク表が見つからない')
    before_rows = _completed_rows(before_text)
    after_rows = _completed_rows(after_text)
    if event_type != 'completed':
        if before_rows != after_rows:
            errors.append(
                'event_type != completed なのに完了タスク表群が変化している '
                '(既知の再発バグ: 進行中タスクを完了欄に誤記載)'
            )
    else:
        found = any(any(task_id in cell for cell in row) for row in after_rows)
        if not found:
            errors.append(f'completed イベントなのに完了タスク表群に {task_id} の行が見つからない')
    return errors, warnings


def check_update_line(after_text: str) -> list[str]:
    """検査項目5: 「更新:」行が1行だけ・120文字以内・「前回:」を含まない."""
    errors: list[str] = []
    lines = [line for line in after_text.splitlines() if UPDATE_LINE_RE.match(line)]
    if len(lines) != 1:
        errors.append(f'「更新:」行が1行ではない (見つかった数: {len(lines)})')
        return errors
    line = lines[0]
    if len(line) > UPDATE_LINE_MAX_LEN:
        errors.append(f'「更新:」行が{UPDATE_LINE_MAX_LEN}文字を超えている ({len(line)}文字)')
    if '前回:' in line:
        errors.append('「更新:」行に「前回:」が含まれている')
    return errors


def _is_subsequence(sub: list[str], full: list[str]) -> bool:
    """引数 sub の各行が full 内に元の順序を保ったまま (連続でなくてよい) 全て出現するか."""
    it = iter(full)
    return all(any(item == line for item in it) for line in sub)


def check_history_appended(
    before_text: str | None, after_text: str | None, history_path: Path
) -> tuple[list[str], list[str]]:
    """検査項目6: 履歴ファイルへの追記確認.

    実運用の履歴ファイル (dashboard_history.md / dashboards/<project>_history.md) は
    最新の更新をファイル先頭 (タイトル直後) に**先頭挿入**する運用になっており、
    末尾行は更新のたびに変わるとは限らない (SQUAD-262 で実物検証して判明)。
    「最終行が変わったか」ではなく「行数が増え、かつ更新前の全行が順序を保って
    残っているか (先頭挿入・末尾追記のどちらでも検出できる)」で判定する。
    """
    errors: list[str] = []
    warnings: list[str] = []
    if before_text is None or after_text is None:
        warnings.append(f'履歴ファイルが見つからない: {history_path}')
        return errors, warnings
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    if not (len(after_lines) > len(before_lines) and _is_subsequence(before_lines, after_lines)):
        errors.append(f'履歴ファイルへの追記が確認できない: {history_path}')
    return errors, warnings


def _allowed_ranges(text: str) -> list[tuple[int, int]]:
    """検査項目1-6が扱う行範囲 (表・「更新:」行・「**最終更新**:」行) を返す (項目7用)."""
    ranges = [
        (t['start'], t['end'])
        for t in find_tables(text)
        if ('Worker' in t['header'] and '状態' in t['header'])
        or ('Task' in t['header'] and 'worktree' in t['header'])
        or ('Task' in t['header'] and '完了日' in t['header'])
    ]
    for i, line in enumerate(text.splitlines()):
        if UPDATE_LINE_RE.match(line) or LAST_UPDATE_LINE_RE.match(line):
            ranges.append((i, i + 1))
    return ranges


def _in_ranges(idx: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= idx < end for start, end in ranges)


def check_unrelated_changes(before_text: str, after_text: str) -> list[str]:
    """検査項目7: 検査対象セクション (表・「更新:」系の行) 以外の変更を検出する."""
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    before_ranges = _allowed_ranges(before_text)
    after_ranges = _allowed_ranges(after_text)
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    problems: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        before_ok = all(_in_ranges(i, before_ranges) for i in range(i1, i2))
        after_ok = all(_in_ranges(j, after_ranges) for j in range(j1, j2))
        if not (before_ok and after_ok):
            problems.append(f'許可対象外の変更を検出 (before 行 {i1 + 1}-{i2} / after 行 {j1 + 1}-{j2})')
    return problems


def check_event(before_dir: Path, after_dir: Path, event: dict, checked_files: set[str]) -> list[tuple[str, str]]:
    """1 イベント分の検査を行い (status, message) のリストを返す."""
    results: list[tuple[str, str]] = []
    project = event['project']
    task_id = event['task_id']
    worker = event['worker']
    event_type = event['event_type']
    expected_status = event.get('expected_status', '')

    index_before = read_text(before_dir / 'dashboard.md')
    index_after = read_text(after_dir / 'dashboard.md')
    proj_rel = f'dashboards/{project}.md'
    proj_before = read_text(before_dir / proj_rel)
    proj_after = read_text(after_dir / proj_rel)

    if index_after is None or proj_after is None:
        results.append(('NG', f'[{task_id}] after 側の dashboard ファイルが見つからない ({proj_rel})'))
        return results

    errors, warnings = check_active_task(proj_after, task_id, expected_status, event_type)
    results += [('NG', f'[{task_id}] {proj_rel}: {e}') for e in errors]
    results += [('WARN', f'[{task_id}] {proj_rel}: {w}') for w in warnings]

    results += [('WARN', f'[{task_id}] dashboard.md: {w}') for w in check_worker_status(index_after, worker)]

    if index_before is not None:
        errors, warnings = check_completion_invariant_dashboard(index_before, index_after, worker, task_id, event_type)
        results += [('NG', f'[{task_id}] dashboard.md: {e}') for e in errors]
        results += [('WARN', f'[{task_id}] dashboard.md: {w}') for w in warnings]
    else:
        results.append(('WARN', f'[{task_id}] dashboard.md (before) が見つからない'))

    if proj_before is not None:
        errors, warnings = check_completion_invariant_project(proj_before, proj_after, task_id, event_type)
        results += [('NG', f'[{task_id}] {proj_rel}: {e}') for e in errors]
        results += [('WARN', f'[{task_id}] {proj_rel}: {w}') for w in warnings]
    else:
        results.append(('WARN', f'[{task_id}] {proj_rel} (before) が見つからない'))

    for label, before_text, after_text, history_rel in (
        ('dashboard.md', index_before, index_after, 'dashboard_history.md'),
        (proj_rel, proj_before, proj_after, f'dashboards/{project}_history.md'),
    ):
        if label in checked_files:
            continue
        checked_files.add(label)

        results += [('NG', f'{label}: {e}') for e in check_update_line(after_text)]

        hist_before = read_text(before_dir / history_rel)
        hist_after = read_text(after_dir / history_rel)
        errors, warnings = check_history_appended(hist_before, hist_after, before_dir / history_rel)
        results += [('NG', f'{label}: {e}') for e in errors]
        results += [('WARN', f'{label}: {w}') for w in warnings]

        if before_text is not None:
            results += [('NG', f'{label}: {p}') for p in check_unrelated_changes(before_text, after_text)]
        else:
            results.append(('WARN', f'{label}: before ファイルが見つからないため無関係セクションチェックを省略'))

    return results


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print('usage: check_dashboard_update.py <before-dir> <after-dir> <events.yaml-or-json>', file=sys.stderr)
        return 1

    before_dir, after_dir, events_path = Path(argv[0]), Path(argv[1]), Path(argv[2])
    events = load_events(events_path)

    checked_files: set[str] = set()
    results: list[tuple[str, str]] = []
    for event in events:
        results += check_event(before_dir, after_dir, event, checked_files)

    ng_count = sum(1 for status, _ in results if status == 'NG')
    warn_count = sum(1 for status, _ in results if status == 'WARN')
    for status, message in results:
        stream = sys.stderr if status == 'NG' else sys.stdout
        print(f'{status}: {message}', file=stream)
    print(f'合計: NG={ng_count} WARN={warn_count}')

    return 1 if ng_count else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
