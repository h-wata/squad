#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts.check_source_tree_clean のテスト (SQUAD-249, SQUAD-248 NB1 の解消).

source_worktree と status_command が独立した自由記述だと、パスが食い違っていても
source_tree_status が空文字列であることだけで見かけ上 clean を装える。固定書式
`git -C <source_worktree> status -s` への一致検証がその「守ったふり」を検出する
ことを確認する。
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from check_source_tree_clean import check_source_tree_clean  # noqa: E402

WORKTREE = '/home/gisen/work/squad-wt-squad249'
OTHER_WORKTREE = '/home/gisen/work/some-other-worktree'
GOOD = {
    'source_worktree': WORKTREE,
    'status_command': f'git -C {WORKTREE} status -s',
    'checked_at': '2026-08-21T12:00:00+09:00',
    'source_tree_status': '',
}


def test_valid_report_has_no_errors() -> None:
    assert check_source_tree_clean(GOOD) == []


def test_path_mismatch_between_source_worktree_and_status_command_fails() -> None:
    meta = {**GOOD, 'status_command': f'git -C {OTHER_WORKTREE} status -s'}
    errors = check_source_tree_clean(meta)
    assert any('status_command' in e for e in errors)


def test_status_command_without_git_dash_c_fails() -> None:
    meta = {**GOOD, 'status_command': f'git status -s {WORKTREE}'}
    errors = check_source_tree_clean(meta)
    assert any('status_command' in e for e in errors)


def test_status_command_using_cd_form_fails() -> None:
    meta = {**GOOD, 'status_command': f'cd {WORKTREE} && git status -s'}
    errors = check_source_tree_clean(meta)
    assert any('status_command' in e for e in errors)


def test_checked_at_without_timezone_fails() -> None:
    meta = {**GOOD, 'checked_at': '2026-08-21T12:00:00'}
    errors = check_source_tree_clean(meta)
    assert any('タイムゾーン' in e for e in errors)


def test_checked_at_unparsable_fails() -> None:
    meta = {**GOOD, 'checked_at': 'not-a-date'}
    errors = check_source_tree_clean(meta)
    assert any('ISO8601' in e for e in errors)


def test_non_empty_source_tree_status_is_not_clean() -> None:
    meta = {**GOOD, 'source_tree_status': ' M queue/templates/report.yaml'}
    errors = check_source_tree_clean(meta)
    assert any('clean ではない' in e for e in errors)
