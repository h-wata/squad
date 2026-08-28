#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts.check_dashboard_update のテスト (SQUAD-259).

fixture の dashboard.md / dashboards/<project>.md は tmp_path 上に作り、実運用の
dashboard.md 等は直接読み書きしない。acceptance_criteria の各項目を 1:1 でテストに
マッピングする。
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from check_dashboard_update import check_history_appended  # noqa: E402
from check_dashboard_update import check_unrelated_changes  # noqa: E402
from check_dashboard_update import check_update_line  # noqa: E402
from check_dashboard_update import find_active_task_table  # noqa: E402
from check_dashboard_update import find_tables  # noqa: E402
from check_dashboard_update import main  # noqa: E402

PROJECT = 'squad'
TASK_ID = 'SQUAD-999'
WORKER = 'worker3'

DASHBOARD_MD = """# マルチPJ ダッシュボード (Index)

**最終更新**: 2026-08-21 10:00 JST

更新: SQUAD-998 完了

## Worker ステータス

| Worker | Pane | Agent | 現在のPJ | 状態 | 直近のタスク |
|--------|------|-------|----------|------|------------|
| Worker 3 | 3 | Claude (Sonnet) | squad | 稼働中 | SQUAD-998 (完了) |

## アクティブ Project

| PJ 名 | active タスク数 | 担当 Worker | dashboard |
|-------|----------------|------------|-----------|
| squad | 1 | W3 | [dashboards/squad.md](dashboards/squad.md) |
"""

# SQUAD-264: dashboards/kioku-mesh.md 等、worktree 列を持たない実運用の Active タスク表
# (Task ID | Worker | Agent | PR | サマリ)。squad.md 以外の project convention を再現する。
PROJECT_MD_NO_WORKTREE_COLUMN = """# kioku-mesh Dashboard

**最終更新**: 2026-08-21 10:00 JST

更新: TASK-998 完了

## Active タスク

| Task ID | Worker | Agent | PR | サマリ |
|---------|--------|-------|----|--------|
| TASK-999 | W3 | Claude | - | テスト用タスク |

## 完了タスク (2026-08-21)

| Task | Worker | 内容 | 成果物 | 完了日 |
|------|--------|------|--------|--------|
| TASK-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |
"""

PROJECT_MD = """# squad Dashboard

**最終更新**: 2026-08-21 10:00 JST

更新: SQUAD-998 完了

## Active タスク

| Task | Worker | 内容 | worktree | branch | 開始日 |
|------|--------|------|----------|--------|--------|
| SQUAD-999 | W3 | テスト用タスク | /tmp/wt | squad-999 | 2026-08-21 |

## 完了タスク (2026-08-21)

| Task | Worker | 内容 | 成果物 | 完了日 |
|------|--------|------|--------|--------|
| SQUAD-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |
"""

DASHBOARD_HISTORY = '更新: SQUAD-998 完了\n'
PROJECT_HISTORY = '更新: SQUAD-998 完了\n'


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_before(root: Path) -> None:
    _write(root, 'dashboard.md', DASHBOARD_MD)
    _write(root, f'dashboards/{PROJECT}.md', PROJECT_MD)
    _write(root, 'dashboard_history.md', DASHBOARD_HISTORY)
    _write(root, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY)


def _event(event_type: str, expected_status: str = '') -> dict:
    return {
        'project': PROJECT,
        'task_id': TASK_ID,
        'worker': WORKER,
        'event_type': event_type,
        'expected_status': expected_status,
        'artifacts': [],
    }


def _write_events(root: Path, events: list[dict]) -> Path:
    import json

    path = root / 'events.json'
    path.write_text(json.dumps(events))
    return path


def test_correctly_updated_dashboard_returns_exit_0(tmp_path: Path) -> None:
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    _write(after, 'dashboard.md', DASHBOARD_MD.replace('SQUAD-998 完了', 'SQUAD-999 実装中'))
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD.replace('SQUAD-998 完了', 'SQUAD-999 実装中'))
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 実装中\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 実装中\n')

    events_path = _write_events(tmp_path, [_event('dispatched', expected_status='')])
    assert main([str(before), str(after), str(events_path)]) == 0


def test_in_progress_task_written_to_completed_section_returns_exit_1(tmp_path: Path) -> None:
    """検査項目3: 既知の再発バグ (進行中タスクを完了欄に誤記載) を検出する."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    # dispatched イベントなのに完了タスク表に SQUAD-999 を追記してしまう誤り
    bad_project_md = PROJECT_MD.replace(
        '| SQUAD-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |\n',
        '| SQUAD-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |\n'
        '| SQUAD-999 | W3 | 実装中のタスク | - | 2026-08-21 |\n',
    )
    _write(after, 'dashboard.md', DASHBOARD_MD)
    _write(after, f'dashboards/{PROJECT}.md', bad_project_md)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 実装中\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 実装中\n')

    events_path = _write_events(tmp_path, [_event('dispatched')])
    exit_code = main([str(before), str(after), str(events_path)])
    assert exit_code == 1


def test_in_progress_task_written_to_dashboard_last_task_column_returns_exit_1(tmp_path: Path) -> None:
    """検査項目3: dashboard.md『直近のタスク』列版の既知の再発バグ検出."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    bad_dashboard_md = DASHBOARD_MD.replace('SQUAD-998 (完了)', 'SQUAD-999 (実装中)')
    _write(after, 'dashboard.md', bad_dashboard_md)
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 実装中\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 実装中\n')

    events_path = _write_events(tmp_path, [_event('dispatched')])
    assert main([str(before), str(after), str(events_path)]) == 1


def test_update_line_with_zenkai_prefix_returns_exit_1(tmp_path: Path) -> None:
    """検査項目5: 「更新:」行に「前回:」を連結した場合の検出."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    bad_dashboard_md = DASHBOARD_MD.replace('更新: SQUAD-998 完了', '更新: SQUAD-999 完了 前回: SQUAD-998 完了')
    _write(after, 'dashboard.md', bad_dashboard_md)
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 完了\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 完了\n')

    events_path = _write_events(tmp_path, [_event('completed')])
    assert main([str(before), str(after), str(events_path)]) == 1


def test_history_not_appended_returns_exit_1(tmp_path: Path) -> None:
    """検査項目6: 履歴ファイルへの追記が無い場合の検出."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    _write(after, 'dashboard.md', DASHBOARD_MD.replace('SQUAD-998 完了', 'SQUAD-999 完了'))
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD.replace('SQUAD-998 完了', 'SQUAD-999 完了'))
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY)  # 追記なし
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY)  # 追記なし

    events_path = _write_events(tmp_path, [_event('completed')])
    assert main([str(before), str(after), str(events_path)]) == 1


def test_unrelated_section_changed_returns_exit_1(tmp_path: Path) -> None:
    """検査項目7: 検査対象外セクションの意図しない改変を検出する."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    bad_dashboard_md = DASHBOARD_MD.replace(
        '# マルチPJ ダッシュボード (Index)', '# マルチPJ ダッシュボード (Index、無断改変)'
    )
    _write(after, 'dashboard.md', bad_dashboard_md)
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 完了\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 完了\n')

    events_path = _write_events(tmp_path, [_event('completed')])
    assert main([str(before), str(after), str(events_path)]) == 1


def test_unknown_format_warns_and_continues_with_exit_0(tmp_path: Path) -> None:
    """fail-soft: 想定外フォーマット (欄が見つからない) は WARN のみで exit 0."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'

    minimal = '# 空の dashboard\n\n更新: 初期化\n'
    _write(before, 'dashboard.md', minimal)
    _write(before, f'dashboards/{PROJECT}.md', minimal)
    _write(before, 'dashboard_history.md', '更新: 初期化\n')
    _write(before, f'dashboards/{PROJECT}_history.md', '更新: 初期化\n')

    _write(after, 'dashboard.md', minimal.replace('初期化', '更新1回目'))
    _write(after, f'dashboards/{PROJECT}.md', minimal.replace('初期化', '更新1回目'))
    _write(after, 'dashboard_history.md', '更新: 初期化\n更新: 更新1回目\n')
    _write(after, f'dashboards/{PROJECT}_history.md', '更新: 初期化\n更新: 更新1回目\n')

    events_path = _write_events(tmp_path, [_event('dispatched')])
    assert main([str(before), str(after), str(events_path)]) == 0


def test_completed_event_requires_task_id_in_completed_section(tmp_path: Path) -> None:
    """検査項目4: completed イベントなのに完了欄に task_id が無い場合の検出."""
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    # completed イベントだが完了タスク表に SQUAD-999 を追加し忘れている
    _write(after, 'dashboard.md', DASHBOARD_MD)
    _write(after, f'dashboards/{PROJECT}.md', PROJECT_MD)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 完了\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 完了\n')

    events_path = _write_events(tmp_path, [_event('completed')])
    assert main([str(before), str(after), str(events_path)]) == 1


def test_check_update_line_detects_duplicate_lines() -> None:
    text = '更新: A\n更新: B\n'
    errors = check_update_line(text)
    assert any('1行ではない' in e for e in errors)


def test_check_unrelated_changes_detects_diff_outside_tables() -> None:
    before = '# heading\n\nbody line\n'
    after = '# heading\n\nchanged body line\n'
    problems = check_unrelated_changes(before, after)
    assert problems


def test_find_tables_parses_header_and_rows() -> None:
    tables = find_tables(PROJECT_MD)
    assert any('worktree' in t['header'] for t in tables)
    assert any('完了日' in t['header'] for t in tables)


def test_check_history_appended_detects_prepend_style_addition() -> None:
    """SQUAD-262: 実運用の履歴ファイルはタイトル直後に先頭挿入する運用だった.

    「最終行が変わったか」だけを見る実装では、既存の最終行 (最古のエントリ) が
    変化しないため見逃す。先頭挿入でも検出できることを確認する。
    """
    before = '# タイトル\n\n更新: 古いエントリ\n'
    after = '# タイトル\n\n更新: 新しいエントリ\n更新: 古いエントリ\n'
    errors, warnings = check_history_appended(before, after, Path('dummy_history.md'))
    assert errors == []
    assert warnings == []


def test_check_history_appended_rejects_no_growth_even_if_last_line_changes() -> None:
    """行数が増えていなければ、最終行が変わっていても追記とは認めない."""
    before = '# タイトル\n\n更新: A\n更新: B\n'
    after = '# タイトル\n\n更新: A\n更新: C\n'
    errors, _ = check_history_appended(before, after, Path('dummy_history.md'))
    assert errors


def test_check_history_appended_rejects_existing_lines_dropped() -> None:
    """行数が増えても既存行が失われていれば NG (単なる置換を追記と誤認しない)."""
    before = '# タイトル\n\n更新: A\n更新: B\n'
    after = '# タイトル\n\n更新: C\n更新: D\n更新: E\n'
    errors, _ = check_history_appended(before, after, Path('dummy_history.md'))
    assert errors


def test_find_active_task_table_without_worktree_column(tmp_path: Path) -> None:
    """SQUAD-264: worktree 列を持たない Active タスク表 (kioku-mesh 等の実運用) も検出できる."""
    table = find_active_task_table(PROJECT_MD_NO_WORKTREE_COLUMN)
    assert table is not None
    assert 'Task ID' in table['header']


def test_active_project_table_change_in_dashboard_md_does_not_trigger_ng(tmp_path: Path) -> None:
    """SQUAD-264 false positive 1.

    dashboard.md「アクティブ Project」表の正当な更新が無関係セクション改変
    (項目7) として NG にならないこと.
    """
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write_before(before)

    # active タスク数が 1 -> 0 に変わる正当な更新 (SQUAD-999 が完了した想定)
    completed_dashboard_md = DASHBOARD_MD.replace(
        '| squad | 1 | W3 | [dashboards/squad.md](dashboards/squad.md) |',
        '| squad | 0 | — | [dashboards/squad.md](dashboards/squad.md) |',
    ).replace('SQUAD-998 (完了)', 'SQUAD-999 (完了)')
    completed_project_md = PROJECT_MD.replace(
        '## Active タスク\n\n'
        '| Task | Worker | 内容 | worktree | branch | 開始日 |\n'
        '|------|--------|------|----------|--------|--------|\n'
        '| SQUAD-999 | W3 | テスト用タスク | /tmp/wt | squad-999 | 2026-08-21 |\n',
        '## Active タスク\n\n（なし）\n',
    ).replace(
        '| SQUAD-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |\n',
        '| SQUAD-998 | W3 | 前回のタスク | PR #1 | 2026-08-21 |\n'
        '| SQUAD-999 | W3 | テスト用タスク | PR #2 | 2026-08-21 |\n',
    )

    _write(after, 'dashboard.md', completed_dashboard_md.replace('SQUAD-998 完了', 'SQUAD-999 完了'))
    _write(after, f'dashboards/{PROJECT}.md', completed_project_md.replace('SQUAD-998 完了', 'SQUAD-999 完了'))
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: SQUAD-999 完了\n')
    _write(after, f'dashboards/{PROJECT}_history.md', PROJECT_HISTORY + '更新: SQUAD-999 完了\n')

    events_path = _write_events(tmp_path, [_event('completed')])
    assert main([str(before), str(after), str(events_path)]) == 0


def test_active_task_table_without_worktree_column_updates_cleanly(tmp_path: Path) -> None:
    """SQUAD-264 false positive 2.

    worktree 列を持たない Active タスク表でも、正当な新規タスク追加更新が exit 0 に
    なること (item1 の WARN 化・item7 の誤検知、両方の回帰).
    """
    before = tmp_path / 'before'
    after = tmp_path / 'after'
    _write(before, 'dashboard.md', DASHBOARD_MD)
    _write(before, 'dashboards/kioku-mesh.md', PROJECT_MD_NO_WORKTREE_COLUMN)
    _write(before, 'dashboard_history.md', DASHBOARD_HISTORY)
    _write(before, 'dashboards/kioku-mesh_history.md', PROJECT_HISTORY)

    new_task_row = '| TASK-1000 | W3 | Claude | - | 新規タスク |\n'
    after_project_md = PROJECT_MD_NO_WORKTREE_COLUMN.replace(
        '| TASK-999 | W3 | Claude | - | テスト用タスク |\n',
        '| TASK-999 | W3 | Claude | - | テスト用タスク |\n' + new_task_row,
    ).replace('更新: TASK-998 完了', '更新: TASK-1000 実装中')

    _write(after, 'dashboard.md', DASHBOARD_MD)
    _write(after, 'dashboards/kioku-mesh.md', after_project_md)
    _write(after, 'dashboard_history.md', DASHBOARD_HISTORY + '更新: TASK-1000 実装中\n')
    _write(after, 'dashboards/kioku-mesh_history.md', PROJECT_HISTORY + '更新: TASK-1000 実装中\n')

    events_path = _write_events(
        tmp_path,
        [
            {
                'project': 'kioku-mesh',
                'task_id': 'TASK-1000',
                'worker': 'worker3',
                'event_type': 'dispatched',
                'expected_status': '',
                'artifacts': [],
            }
        ],
    )
    assert main([str(before), str(after), str(events_path)]) == 0
