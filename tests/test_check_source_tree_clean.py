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
from check_source_tree_clean import extract_worktree_entries  # noqa: E402
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


# --- SQUAD-251: source_tree_clean のネスト形式 (report 全文からの抽出) --------------
#
# check_source_tree_clean.py はもともと report 全体を ledger.parse_scalars (トップ
# レベルの `key: value` だけ拾う簡易パーサ) に通していたため、`source_tree_clean:`
# の下にネストして書かれた source_worktree 等を一切読めず、規約どおりの report を
# 「4 フィールドとも欠損」として誤って NG 判定していた。extract_worktree_entries()
# はこの report 全文からの抽出を担い、フラット形式 (トップレベル直書き, 後方互換) と
# source_tree_clean: ネスト形式 (マッピング = 単一 worktree、リスト = 複数 worktree)
# の両方を同じ検証にかけられるようにする。

WORKTREE_2 = '/home/gisen/rmf_ws/src/rmf/rmf_traffic'


def _errors_for(text: str) -> list[str]:
    """Report 全文を extract → 各エントリを検証、というエンドツーエンドの経路."""
    errors: list[str] = []
    for entry in extract_worktree_entries(text):
        errors.extend(check_source_tree_clean(entry))
    return errors


def test_list_format_multi_worktree_normal_case_has_no_errors() -> None:
    """リスト形式 (複数 worktree) の正常系."""
    text = f"""\
report_id: "00000000-0000-4000-8000-000000000000"
source_tree_clean:
  - source_worktree: {WORKTREE}
    status_command: "git -C {WORKTREE} status -s"
    source_tree_status: ""
    checked_at: "2026-08-28T22:45:46+09:00"
  - source_worktree: {WORKTREE_2}
    status_command: "git -C {WORKTREE_2} status -s"
    source_tree_status: ""
    checked_at: "2026-08-28T22:45:46+09:00"
"""
    entries = extract_worktree_entries(text)
    assert len(entries) == 2
    assert _errors_for(text) == []


def test_mapping_format_single_worktree_normal_case_has_no_errors() -> None:
    """マッピング形式 (単一 worktree) の正常系."""
    text = f"""\
report_id: "00000000-0000-4000-8000-000000000000"
source_tree_clean:
  source_worktree: {WORKTREE}
  status_command: "git -C {WORKTREE} status -s"
  source_tree_status: ""
  checked_at: "2026-08-28T22:45:46+09:00"
"""
    entries = extract_worktree_entries(text)
    assert len(entries) == 1
    assert _errors_for(text) == []


def test_nested_list_status_command_mismatch_fails() -> None:
    text = f"""\
source_tree_clean:
  - source_worktree: {WORKTREE}
    status_command: "git -C {WORKTREE_2} status -s"
    source_tree_status: ""
    checked_at: "2026-08-28T22:45:46+09:00"
"""
    errors = _errors_for(text)
    assert any('status_command' in e for e in errors)


def test_nested_mapping_checked_at_without_timezone_fails() -> None:
    text = f"""\
source_tree_clean:
  source_worktree: {WORKTREE}
  status_command: "git -C {WORKTREE} status -s"
  source_tree_status: ""
  checked_at: "2026-08-28T22:45:46"
"""
    errors = _errors_for(text)
    assert any('タイムゾーン' in e for e in errors)


def test_nested_block_scalar_dirty_status_is_detected() -> None:
    """worker1 の実 report と同じ block scalar (`|`) 書式で dirty を検出できること."""
    text = f"""\
source_tree_clean:
  - source_worktree: {WORKTREE}
    status_command: "git -C {WORKTREE} status -s"
    source_tree_status: |
      ?? .kioku-mesh.yaml
      ?? watch.log
    checked_at: "2026-08-28T22:45:46+09:00"
"""
    entries = extract_worktree_entries(text)
    assert entries[0]['source_tree_status'] == '?? .kioku-mesh.yaml\n?? watch.log'
    errors = _errors_for(text)
    assert any('clean ではない' in e for e in errors)


def test_nested_list_missing_required_field_in_one_entry_fails() -> None:
    """2 件のうち 1 件だけ checked_at が欠けていても検出されること."""
    text = f"""\
source_tree_clean:
  - source_worktree: {WORKTREE}
    status_command: "git -C {WORKTREE} status -s"
    source_tree_status: ""
    checked_at: "2026-08-28T22:45:46+09:00"
  - source_worktree: {WORKTREE_2}
    status_command: "git -C {WORKTREE_2} status -s"
    source_tree_status: ""
"""
    entries = extract_worktree_entries(text)
    assert len(entries) == 2
    assert 'checked_at' not in entries[1]
    errors = _errors_for(text)
    assert any('checked_at' in e and '欠損' in e for e in errors)


def test_flat_format_still_works_without_source_tree_clean_wrapper() -> None:
    """後方互換: source_tree_clean: ラッパーの無い旧来のフラット形式."""
    text = f"""\
report_id: "00000000-0000-4000-8000-000000000000"
source_worktree: "{WORKTREE}"
status_command: "git -C {WORKTREE} status -s"
source_tree_status: ""
checked_at: "2026-08-28T22:45:46+09:00"
"""
    entries = extract_worktree_entries(text)
    assert len(entries) == 1
    assert _errors_for(text) == []


def test_report_with_neither_flat_nor_nested_fields_reports_all_missing() -> None:
    """source_tree_clean も旧フラットフィールドも一切無い report は全欠損として fail する."""
    text = 'report_id: "00000000-0000-4000-8000-000000000000"\ntask_id: TASK-1\n'
    errors = _errors_for(text)
    assert len(errors) >= len(REQUIRED_FIELDS)


# --- サボタージュ検証: わざと壊した report が期待どおり fail することの確認 ---------


def test_sabotage_second_worktree_in_list_is_dirty_but_first_is_clean() -> None:
    """複数 worktree のうち後方だけが dirty でも見逃さないこと (先頭だけ見て pass しない)."""
    text = f"""\
source_tree_clean:
  - source_worktree: {WORKTREE}
    status_command: "git -C {WORKTREE} status -s"
    source_tree_status: ""
    checked_at: "2026-08-28T22:45:46+09:00"
  - source_worktree: {WORKTREE_2}
    status_command: "git -C {WORKTREE_2} status -s"
    source_tree_status: " M some/dirty/file.py"
    checked_at: "2026-08-28T22:45:46+09:00"
"""
    errors = _errors_for(text)
    assert any('clean ではない' in e for e in errors)


def test_sabotage_nested_semicolon_path_switch_still_fails() -> None:
    """SQUAD-254 B2 の ';' 切替偽装が、ネスト形式でも通らないこと."""
    sabotage = '/tmp/pr38-rereview-sabotage'
    clean = '/tmp/pr38-rereview'
    worktree = f'{sabotage}; git -C {clean}'
    text = f"""\
source_tree_clean:
  source_worktree: {worktree}
  status_command: "git -C {sabotage}; git -C {clean} status -s"
  source_tree_status: ""
  checked_at: "2026-08-28T22:45:46+09:00"
"""
    errors = _errors_for(text)
    assert any('メタ文字' in e for e in errors)


def test_sabotage_flat_format_claims_clean_via_wrong_word_still_fails() -> None:
    """`source_tree_status: "clean"` のような説明語は空文字列ではないので dirty 扱いになる.

    実際に worker2 の report がこの誤りをしていた。空文字列だけが clean の申告になる。
    """
    text = f"""\
source_tree_clean:
  source_worktree: {WORKTREE}
  status_command: "git -C {WORKTREE} status -s"
  source_tree_status: "clean"
  checked_at: "2026-08-28T22:45:46+09:00"
"""
    errors = _errors_for(text)
    assert any('clean ではない' in e for e in errors)
