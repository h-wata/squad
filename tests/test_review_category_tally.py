#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts/review_category_tally.py (review 指摘の category 集計, Issue #136) のテスト.

tmp_path に実ファイルの review / report YAML を置いて集計する (mock 注入はしない)。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'squad'))
import graph  # noqa: E402
import review_category_tally as tally  # noqa: E402

RID1 = '11111111-1111-4111-8111-111111111111'
RID2 = '22222222-2222-4222-8222-222222222222'


def _write(path: Path, text: str, mtime: int = 1_700_000_000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    os.utime(path, (mtime, mtime))
    return path


# findings 形式 (テンプレートどおり)。category 付き 1 件と category 無し 1 件
FINDINGS_REVIEW = """task_id: SQUAD-10
verdict: approve_with_comments
pr_url: https://github.com/o/r/pull/10
findings:
  - severity: minor
    category: stale-state
    file: a.py
    line: 1
    description: "前タスクの report を読んだ"
  - severity: nit
    file: b.py
    line: 2
    description: "category 無し"
completed_at: "2026-11-01T10:00:00+09:00"
"""
# blocking / non_blocking 形式 (実物に多い形)。文字列だけの要素もある
BLOCKING_REVIEW = """task_id: SQUAD-20
verdict: request_changes
pr_url: https://github.com/o/r/pull/20
blocking:
  - id: B1
    category: stale-state
    area: "watchd"
    finding: "古い状態を読んだ"
non_blocking:
  - id: NB1
    category: naming-semantics
    finding: "名前が意味と違う"
  - "文字列だけの指摘"
completed_at: "2026-11-05T10:00:00+09:00"
"""
# worker の report。issues の category 付きだけを数える
WORKER_REPORT = f"""report_id: "{RID1}"
task_id: SQUAD-30
status: completed
pr_url: https://github.com/o/r/pull/30
issues:
  - category: shared-file-conflict
    issue: "CHANGELOG.md が衝突した"
  - "category の無い自由文"
  - issue: "dict でも category が無ければ数えない"
completed_at: "2026-11-03T10:00:00+09:00"
"""


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports'
    _write(reports / 'archive' / 'SQUAD-10__review-aaaa.yaml', FINDINGS_REVIEW)
    _write(reports / 'worker4_review.yaml', BLOCKING_REVIEW)
    _write(reports / 'worker1_report.yaml', WORKER_REPORT)
    return q


def _by_cat(rows: list[dict]) -> dict[str, dict]:
    return {r['category']: r for r in rows}


def test_findings_format_counted_by_category(queue: Path) -> None:
    rows = _by_cat(tally.tally(tally.collect(queue)))
    assert 'SQUAD-10' in rows['stale-state']['tasks']


def test_blocking_non_blocking_format_counted(queue: Path) -> None:
    rows = _by_cat(tally.tally(tally.collect(queue)))
    assert rows['stale-state']['count'] == 2  # findings 形式 1 + blocking 1
    assert 'SQUAD-20' in rows['stale-state']['tasks']
    assert rows['naming-semantics']['count'] == 1


def test_review_without_category_is_uncategorized(queue: Path) -> None:
    rows = tally.tally(tally.collect(queue))
    assert rows[-1]['category'] == tally.UNCATEGORIZED  # uncategorized は最後の行
    assert rows[-1]['count'] == 2  # findings の category 無し 1 + non_blocking の文字列 1


def test_report_issues_only_with_category(queue: Path) -> None:
    rows = _by_cat(tally.tally(tally.collect(queue)))
    assert rows['shared-file-conflict']['count'] == 1
    assert rows['shared-file-conflict']['tasks'] == ['SQUAD-30']
    # report の category 無し issues は uncategorized にも入れない
    assert 'SQUAD-30' not in rows[tally.UNCATEGORIZED]['tasks']


def test_count_first_last_and_prs(queue: Path) -> None:
    row = _by_cat(tally.tally(tally.collect(queue)))['stale-state']
    assert row['count'] == 2
    assert row['first_seen'] == '2026-11-01T01:00:00+00:00'
    assert row['last_seen'] == '2026-11-05T01:00:00+00:00'
    assert row['prs'] == ['https://github.com/o/r/pull/10', 'https://github.com/o/r/pull/20']
    assert row['approx'] is False


def test_mtime_fallback_is_marked_approx(tmp_path: Path) -> None:
    q = tmp_path / 'queue'
    _write(
        q / 'projects' / 'pj' / 'reports' / 'worker4_review.yaml',
        'task_id: T1\nverdict: approve\nfindings:\n  - category: missing-test\n    description: x\n',
        mtime=1_700_000_000,
    )
    row = _by_cat(tally.tally(tally.collect(q)))['missing-test']
    assert row['approx'] is True
    assert row['first_seen'] == '2023-11-14T22:13:20+00:00'


def test_archive_copy_is_not_double_counted(queue: Path) -> None:
    """配達前の worker4_review.yaml と reports/archive/ の写し (同じ中身) は 1 件として数える."""
    reports = queue / 'projects' / 'pj' / 'reports'
    _write(reports / 'archive' / 'SQUAD-20__review-bbbb.yaml', BLOCKING_REVIEW)
    _write(reports / 'archive' / f'SQUAD-30__{RID1}.yaml', WORKER_REPORT + '# 写しの末尾が違っても report_id で同じ\n')
    rows = _by_cat(tally.tally(tally.collect(queue)))
    assert rows['stale-state']['count'] == 2
    assert rows['shared-file-conflict']['count'] == 1


def test_nested_findings_dict_and_unknown_category(tmp_path: Path) -> None:
    """Findings が {blocking: [...], non_blocking: [...]} の入れ子でも読む。語彙外の category も弾かない."""
    q = tmp_path / 'queue'
    _write(
        q / 'projects' / 'pj' / 'reports' / 'archive' / 'worker4_review_T1.yaml',
        'task_id: T1\nverdict: approve\nfindings:\n  blocking:\n    - category: maintainability\n'
        '      finding: x\n  non_blocking:\n    - finding: y\n',
    )
    rows = _by_cat(tally.tally(tally.collect(q)))
    assert rows['maintainability']['count'] == 1
    assert rows[tally.UNCATEGORIZED]['count'] == 1


def test_non_review_findings_are_ignored(tmp_path: Path) -> None:
    """Review でない YAML (設計 report 等) の findings は数えない."""
    q = tmp_path / 'queue'
    _write(
        q / 'projects' / 'pj' / 'reports' / 'worker4_design_1.yaml',
        f'report_id: "{RID2}"\ntask_id: T1\nstatus: completed\nfindings:\n  - category: other\n    d: x\n',
    )
    assert tally.tally(tally.collect(q)) == []


def test_markdown_and_json_output(queue: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert tally.main(['--queue-dir', str(queue)]) == 0
    out = capsys.readouterr().out
    assert '| category | 件数 | 初出 | 直近 | PR |' in out
    assert '| stale-state | 2 | 2026-11-01 | 2026-11-05 |' in out
    assert out.strip().splitlines()[-1].startswith('| uncategorized | 2 |')
    assert tally.main(['--queue-dir', str(queue), '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r['category']: r['count'] for r in data['categories']}['stale-state'] == 2
    assert data['categories'][-1]['category'] == tally.UNCATEGORIZED


def test_pr_column_truncates_after_three(tmp_path: Path) -> None:
    rows = [
        {
            'category': 'missing-test',
            'count': 5,
            'first_seen': '2026-11-01T00:00:00+00:00',
            'last_seen': '2026-11-02T00:00:00+00:00',
            'prs': [f'https://github.com/o/r/pull/{i}' for i in range(1, 6)],
            'tasks': [],
            'approx': False,
        }
    ]
    line = tally.render_markdown(rows)[2]
    assert '#1, #2, #3 他 2 件' in line


def _review(category: str, task_id: str) -> str:
    return (
        f'task_id: {task_id}\nverdict: approve\npr_url: https://github.com/o/r/pull/1\n'
        f'findings:\n  - category: {category}\n    description: x\n'
    )


def test_inbox_appends_once_on_second_and_not_on_third(tmp_path: Path) -> None:
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    inbox = _write(q / '_inbox.md', '# Inbox\n\n- [ ] 既存の行\n')
    _write(reports / 'R1__review-1.yaml', _review('stale-state', 'SQUAD-1'))
    assert tally.append_inbox(inbox, tally.tally(tally.collect(q))) == []  # 1 件目では積まない
    _write(reports / 'R2__review-2.yaml', _review('stale-state', 'SQUAD-2'))
    added = tally.append_inbox(inbox, tally.tally(tally.collect(q)))
    assert len(added) == 1
    assert '`category:stale-state`' in added[0]
    assert 'SQUAD-1' in added[0] and 'SQUAD-2' in added[0]
    _write(reports / 'R3__review-3.yaml', _review('stale-state', 'SQUAD-3'))
    assert tally.append_inbox(inbox, tally.tally(tally.collect(q))) == []  # 3 件目では増えない
    text = inbox.read_text(encoding='utf-8')
    assert text.startswith('# Inbox\n\n- [ ] 既存の行\n')
    assert text.count('category:stale-state') == 1


def test_inbox_marker_is_exact_and_skips_uncategorized(tmp_path: Path) -> None:
    """`category:stale-state-x` の行があっても stale-state は別の候補。uncategorized は候補にしない."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    inbox = _write(q / '_inbox.md', '- [ ] 別の候補 `category:stale-state-x`\n')
    for i in (1, 2):
        _write(reports / f'R{i}__review-{i}.yaml', _review('stale-state', f'SQUAD-{i}'))
        _write(
            reports / f'U{i}__review-{i}.yaml',
            f'task_id: U{i}\nverdict: approve\nfindings:\n  - description: x\n',
        )
    added = tally.append_inbox(inbox, tally.tally(tally.collect(q)))
    assert len(added) == 1 and '`category:stale-state`' in added[0]
    assert 'uncategorized' not in inbox.read_text(encoding='utf-8')


def test_main_inbox_option(queue: Path, capsys: pytest.CaptureFixture[str]) -> None:
    inbox = queue / '_inbox.md'
    assert tally.main(['--queue-dir', str(queue), '--inbox', str(inbox)]) == 0
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1
    capsys.readouterr()
    assert tally.main(['--queue-dir', str(queue), '--inbox', str(inbox)]) == 0
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1


def test_graph_sync_writes_categories_and_inbox(queue: Path) -> None:
    """`squad graph sync` (graph.sync) が集計結果を .graph.db に書き、inbox に候補を積む."""
    inbox = queue / '_inbox.md'
    db = queue / '.graph.db'
    graph.sync(queue, db, inbox_path=inbox)
    conn = sqlite3.connect(db)
    try:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                'SELECT category, count, first_seen, last_seen, prs_json, approx FROM review_categories'
            )
        }
    finally:
        conn.close()
    assert rows['stale-state'][:3] == (2, '2026-11-01T01:00:00+00:00', '2026-11-05T01:00:00+00:00')
    assert json.loads(rows['stale-state'][3]) == ['https://github.com/o/r/pull/10', 'https://github.com/o/r/pull/20']
    assert rows[tally.UNCATEGORIZED][0] == 2
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1
    graph.sync(queue, db, inbox_path=inbox)  # 作り直しても inbox は増えない
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1


def test_graph_sync_without_inbox_path_does_not_write_inbox(queue: Path) -> None:
    graph.sync(queue, queue / '.graph.db')
    assert not (queue / '_inbox.md').exists()
