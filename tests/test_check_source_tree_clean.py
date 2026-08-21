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
from check_source_tree_clean import REQUIRED_FIELDS  # noqa: E402

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


def test_missing_source_tree_status_key_fails_even_though_empty_string_is_clean() -> None:
    """SQUAD-252 B1: キー欠損 (get(..., '') では空文字と区別できない) は fail."""
    meta = {k: v for k, v in GOOD.items() if k != 'source_tree_status'}
    errors = check_source_tree_clean(meta)
    assert any('source_tree_status' in e and '欠損' in e for e in errors)


def test_missing_required_field_fails() -> None:
    """SQUAD-252 B1: REQUIRED_FIELDS の各フィールドの欠損を回帰させない."""
    for field in REQUIRED_FIELDS:
        meta = {k: v for k, v in GOOD.items() if k != field}
        errors = check_source_tree_clean(meta)
        assert any(field in e and '欠損' in e for e in errors), f'{field} 欠損が検出されなかった'


def test_relative_source_worktree_fails() -> None:
    """SQUAD-252 B2: '.' のような CWD 依存の相対パスは fail."""
    meta = {**GOOD, 'source_worktree': '.', 'status_command': 'git -C . status -s'}
    errors = check_source_tree_clean(meta)
    assert any('絶対パス' in e for e in errors)


def test_trailing_slash_absolute_path_is_still_accepted() -> None:
    """末尾スラッシュは同一ディレクトリへの別表記として許容する (正規化はしない、文字列一致のみ)."""
    worktree = f'{WORKTREE}/'
    meta = {**GOOD, 'source_worktree': worktree, 'status_command': f'git -C {worktree} status -s'}
    assert check_source_tree_clean(meta) == []


def test_dotdot_absolute_path_is_still_accepted() -> None:
    worktree = f'{WORKTREE}/../squad-wt-squad249'
    meta = {**GOOD, 'source_worktree': worktree, 'status_command': f'git -C {worktree} status -s'}
    assert check_source_tree_clean(meta) == []


def _meta_with_worktree(worktree: str) -> dict[str, str]:
    return {**GOOD, 'source_worktree': worktree, 'status_command': f'git -C {worktree} status -s'}


def test_semicolon_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE}; rm -rf /')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_double_ampersand_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE} && rm -rf /')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_double_pipe_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE} || rm -rf /')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_pipe_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE} | rm -rf /')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_backtick_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE}`rm -rf /`')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_command_substitution_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE}$(rm -rf /)')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_newline_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE}\nrm -rf /')
    errors = check_source_tree_clean(meta)
    assert errors


def test_tab_in_source_worktree_fails() -> None:
    meta = _meta_with_worktree(f'{WORKTREE}\trm -rf /')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_space_in_source_worktree_fails() -> None:
    """許可文字は allowlist (英数字 / -_.~/) のみ。空白は単一トークン性を壊すため拒否する."""
    meta = _meta_with_worktree('/tmp/a b')
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_w4_semicolon_reproduction_fails() -> None:
    """SQUAD-254 B2: W4 の再現手順そのもの (dirty な対象を ';' で clean な別パスへ切替える偽装)."""
    sabotage = '/tmp/pr38-rereview-sabotage'
    clean = '/tmp/pr38-rereview'
    worktree = f'{sabotage}; git -C {clean}'
    meta = {
        'source_worktree': worktree,
        'status_command': f'git -C {sabotage}; git -C {clean} status -s',
        'checked_at': '2026-08-21T12:45:00+09:00',
        'source_tree_status': '',
    }
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_trailing_newline_in_source_worktree_fails() -> None:
    """SQUAD-256: re.match + `^...$` は末尾改行の直前にもマッチするため bypass できていた."""
    worktree = f'{WORKTREE}\n'
    meta = _meta_with_worktree(worktree)
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_multiple_trailing_newlines_in_source_worktree_fails() -> None:
    worktree = f'{WORKTREE}\n\n\n'
    meta = _meta_with_worktree(worktree)
    errors = check_source_tree_clean(meta)
    assert any('メタ文字' in e for e in errors)


def test_trailing_newline_in_status_command_fails() -> None:
    meta = {**GOOD, 'status_command': f'git -C {WORKTREE} status -s\n'}
    errors = check_source_tree_clean(meta)
    assert any('status_command' in e for e in errors)


def test_trailing_newline_in_checked_at_fails() -> None:
    meta = {**GOOD, 'checked_at': '2026-08-21T12:00:00+09:00\n'}
    errors = check_source_tree_clean(meta)
    assert any('checked_at' in e for e in errors)
