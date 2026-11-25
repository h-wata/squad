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
    stale = rows['stale-state']
    assert (stale['task_count'], stale['blocking'], stale['non_blocking']) == (
        2,
        1,
        1,
    )  # findings minor 1 + blocking 1
    assert 'SQUAD-20' in stale['tasks']
    assert (rows['naming-semantics']['blocking'], rows['naming-semantics']['non_blocking']) == (0, 1)


def test_review_without_category_is_uncategorized(queue: Path) -> None:
    rows = tally.tally(tally.collect(queue))
    assert rows[-1]['category'] == tally.UNCATEGORIZED  # uncategorized は最後の行
    assert rows[-1]['non_blocking'] == 2  # findings の category 無し 1 + non_blocking の文字列 1
    assert rows[-1]['task_count'] == 2


def test_report_issues_only_with_category(queue: Path) -> None:
    rows = _by_cat(tally.tally(tally.collect(queue)))
    assert rows['shared-file-conflict']['task_count'] == 1
    assert rows['shared-file-conflict']['non_blocking'] == 1
    assert rows['shared-file-conflict']['tasks'] == ['SQUAD-30']
    # report の category 無し issues は uncategorized にも入れない
    assert 'SQUAD-30' not in rows[tally.UNCATEGORIZED]['tasks']


def test_count_first_last_and_prs(queue: Path) -> None:
    row = _by_cat(tally.tally(tally.collect(queue)))['stale-state']
    assert row['task_count'] == 2
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
    assert (rows['stale-state']['blocking'], rows['stale-state']['non_blocking']) == (1, 1)
    assert rows['shared-file-conflict']['non_blocking'] == 1


def test_nested_findings_dict_and_unknown_category(tmp_path: Path) -> None:
    """Findings が {blocking: [...], non_blocking: [...]} の入れ子でも読む。語彙外の category も弾かない."""
    q = tmp_path / 'queue'
    _write(
        q / 'projects' / 'pj' / 'reports' / 'archive' / 'worker4_review_T1.yaml',
        'task_id: T1\nverdict: approve\nfindings:\n  blocking:\n    - category: maintainability\n'
        '      finding: x\n  non_blocking:\n    - finding: y\n',
    )
    rows = _by_cat(tally.tally(tally.collect(q)))
    assert rows['maintainability']['blocking'] == 1
    assert rows[tally.UNCATEGORIZED]['non_blocking'] == 1


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
    assert '| category | task 数 | blocking | non_blocking | 確認項目 | 初出 | 直近 | PR |' in out
    assert '| stale-state | 2 | 1 | 1 | 0 | 2026-11-01 | 2026-11-05 |' in out
    assert out.strip().splitlines()[-1].startswith('| uncategorized | 2 | 0 | 2 | 0 |')
    assert tally.main(['--queue-dir', str(queue), '--json']) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r['category']: r['task_count'] for r in data['categories']}['stale-state'] == 2
    assert data['categories'][-1]['category'] == tally.UNCATEGORIZED


def test_pr_column_truncates_after_three(tmp_path: Path) -> None:
    rows = [
        {
            'category': 'missing-test',
            'task_count': 5,
            'blocking': 5,
            'non_blocking': 0,
            'checks': 0,
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
                'SELECT category, task_count, blocking, non_blocking, checks, first_seen, last_seen, prs_json, approx '
                'FROM review_categories'
            )
        }
    finally:
        conn.close()
    assert rows['stale-state'][:6] == (2, 1, 1, 0, '2026-11-01T01:00:00+00:00', '2026-11-05T01:00:00+00:00')
    assert json.loads(rows['stale-state'][6]) == ['https://github.com/o/r/pull/10', 'https://github.com/o/r/pull/20']
    assert rows[tally.UNCATEGORIZED][:4] == (2, 0, 2, 0)
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1
    graph.sync(queue, db, inbox_path=inbox)  # 作り直しても inbox は増えない
    assert inbox.read_text(encoding='utf-8').count('`category:stale-state`') == 1


def test_graph_sync_without_inbox_path_does_not_write_inbox(queue: Path) -> None:
    graph.sync(queue, queue / '.graph.db')
    assert not (queue / '_inbox.md').exists()


# ---- 「別 task で繰り返した失敗」だけを候補にする (SQUAD-406, Refs #130 #136) ----


def _findings(task_id: str, *items: str) -> str:
    """Review YAML (findings 形式) を作る. items は 1 要素ぶんの YAML (先頭の `- ` 無し, 2 行目以降は 4 字下げ)."""
    body = ''.join(f'  - {item}\n' for item in items)
    return f'task_id: {task_id}\nverdict: request_changes\nfindings:\n{body}'


def test_same_task_multiple_findings_count_as_one_task(tmp_path: Path) -> None:
    """同じ task_id の中で同じ category の指摘が何件あっても、繰り返しの判定では 1 task."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    _write(
        reports / 'T1__review.yaml',
        _findings(
            'T1',
            'severity: minor\n    category: stale-state\n    description: a',
            'severity: minor\n    category: stale-state\n    description: b',
            'severity: major\n    category: stale-state\n    description: c',
        ),
    )
    rows = tally.tally(tally.collect(q))
    row = _by_cat(rows)['stale-state']
    assert (row['task_count'], row['blocking'], row['non_blocking']) == (1, 1, 2)
    inbox = _write(q / '_inbox.md', '# Inbox\n')
    assert tally.append_inbox(inbox, rows) == []
    assert inbox.read_text(encoding='utf-8') == '# Inbox\n'


def test_distinct_tasks_become_candidate(tmp_path: Path) -> None:
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    for t in ('T1', 'T2'):
        _write(reports / f'{t}__review.yaml', _findings(t, 'severity: minor\n    category: stale-state\n    d: x'))
    added = tally.append_inbox(q / '_inbox.md', tally.tally(tally.collect(q)))
    assert len(added) == 1
    assert '`category:stale-state`' in added[0] and '2 task' in added[0]


def test_task003_like_review_is_not_candidate(tmp_path: Path) -> None:
    """実データの TASK-003 と同じ形 (1 review に maintainability 4 件 / ros2_best_practice 3 件) は候補にしない."""
    q = tmp_path / 'queue'
    items = ['severity: minor\n    category: maintainability\n    description: m'] * 4
    items += ['severity: major\n    category: ros2_best_practice\n    description: r'] * 3
    _write(q / 'projects' / 'mesh' / 'reports' / 'reviewer_report.yaml', _findings('TASK-003', *items))
    rows = _by_cat(tally.tally(tally.collect(q)))
    assert rows['maintainability']['task_count'] == 1
    assert rows['ros2_best_practice']['task_count'] == 1
    assert tally.append_inbox(q / '_inbox.md', list(rows.values())) == []


CHECK_ITEMS = (
    'result: pass\n    category: stale-state\n    viewpoint: a',
    'result: pass_with_comment\n    severity: non-blocking\n    category: stale-state\n    viewpoint: b',
    'status: resolved\n    category: stale-state\n    description: c',
    'status: verified\n    severity: info\n    category: stale-state\n    description: d',
    'severity: info\n    category: stale-state\n    description: e',
    'severity: none\n    resolved: true\n    category: stale-state\n    description: f',
    'severity: resolved\n    category: stale-state\n    description: g',
)


@pytest.mark.parametrize('item', CHECK_ITEMS)
def test_check_item_is_not_failure(tmp_path: Path, item: str) -> None:
    """確認した項目 (result: pass 等) は失敗に数えない. 2 task にあっても候補にしない."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    for t in ('T1', 'T2'):
        _write(reports / f'{t}__review.yaml', _findings(t, item))
    rows = tally.tally(tally.collect(q))
    row = _by_cat(rows)['stale-state']
    assert (row['task_count'], row['blocking'], row['non_blocking'], row['checks']) == (0, 0, 0, 2)
    assert row['tasks'] == []
    assert tally.append_inbox(q / '_inbox.md', rows) == []


FAILURE_ITEMS = (
    ('result: fail\n    severity: blocking\n    category: c', 'blocking'),
    ('result: fail\n    severity: non-blocking\n    category: c', 'non_blocking'),
    ('status: open\n    severity: major\n    category: c', 'blocking'),
    ('status: partial\n    severity: minor\n    category: c', 'non_blocking'),
    ('severity: critical\n    category: c', 'blocking'),
    ('severity: major\n    blocking: false\n    category: c', 'non_blocking'),
    ('severity: minor\n    blocking: true\n    category: c', 'blocking'),
    ('resolved: false\n    blocking: true\n    category: c', 'blocking'),
    ('severity: nit\n    category: c', 'non_blocking'),
    ('severity: suggestion\n    category: c', 'non_blocking'),
    ('category: c\n    description: severity 無し', 'non_blocking'),
)


@pytest.mark.parametrize(('item', 'kind'), FAILURE_ITEMS)
def test_failure_kind(tmp_path: Path, item: str, kind: str) -> None:
    q = tmp_path / 'queue'
    _write(q / 'projects' / 'pj' / 'reports' / 'worker4_review.yaml', _findings('T1', item))
    row = _by_cat(tally.tally(tally.collect(q)))['c']
    assert row[kind] == 1
    assert row['blocking'] + row['non_blocking'] == 1
    assert (row['task_count'], row['checks']) == (1, 0)


def test_blocking_lists_follow_their_key(tmp_path: Path) -> None:
    """トップレベルの blocking / non_blocking 配列は、要素の severity に関わらず配列の名前で分ける."""
    q = tmp_path / 'queue'
    _write(
        q / 'projects' / 'pj' / 'reports' / 'worker4_review.yaml',
        'task_id: T1\nverdict: request_changes\nblocking:\n  - category: c\n    severity: minor\n    f: x\n'
        'non_blocking:\n  - category: c\n    severity: major\n    f: y\n  - "文字列の指摘"\n',
    )
    rows = _by_cat(tally.tally(tally.collect(q)))
    assert (rows['c']['blocking'], rows['c']['non_blocking']) == (1, 1)
    assert rows[tally.UNCATEGORIZED]['non_blocking'] == 1


def test_uncategorized_counts_failures_by_distinct_task(tmp_path: Path) -> None:
    """Uncategorized も失敗だけを数え、task 数は distinct task_id."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    _write(
        reports / 'T1__review.yaml',
        _findings('T1', 'severity: major\n    d: a', 'severity: minor\n    d: b', 'result: pass\n    viewpoint: c'),
    )
    _write(reports / 'T2__review.yaml', _findings('T2', 'status: resolved\n    d: d'))
    row = _by_cat(tally.tally(tally.collect(q)))[tally.UNCATEGORIZED]
    assert (row['task_count'], row['blocking'], row['non_blocking'], row['checks']) == (1, 1, 1, 2)
    assert row['tasks'] == ['T1']


def test_checks_do_not_set_first_seen_or_prs(tmp_path: Path) -> None:
    """初出 / 直近 / PR は失敗した指摘から出す (確認項目だけの review の日付や PR を混ぜない)."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    _write(
        reports / 'T1__review.yaml',
        'task_id: T1\nverdict: approve\npr_url: https://github.com/o/r/pull/1\n'
        'completed_at: "2026-11-01T00:00:00+00:00"\nfindings:\n  - result: pass\n    category: c\n',
    )
    _write(
        reports / 'T2__review.yaml',
        'task_id: T2\nverdict: approve\npr_url: https://github.com/o/r/pull/2\n'
        'completed_at: "2026-11-02T00:00:00+00:00"\nfindings:\n  - severity: minor\n    category: c\n',
    )
    row = _by_cat(tally.tally(tally.collect(q)))['c']
    assert (row['first_seen'], row['last_seen']) == ('2026-11-02T00:00:00+00:00', '2026-11-02T00:00:00+00:00')
    assert row['prs'] == ['https://github.com/o/r/pull/2']


INBOX_DONE = (
    '# Inbox\n\n'
    '- [x] [review-category] category `maintainability` の指摘が 4 件 (TASK-003)。チェック化の候補  '
    '`category:maintainability` → 誤検知 (同一 task TASK-003 内の複数指摘)\n'
    '- [x] [review-category] category `ros2_best_practice` の指摘が 3 件 (TASK-003)。チェック化の候補  '
    '`category:ros2_best_practice` → 誤検知\n'
)


def test_checked_inbox_lines_are_not_added_again(tmp_path: Path) -> None:
    """[x] で閉じた行も既出とみなす. 本当に 2 task になっても、閉じた category は足し直さない."""
    q = tmp_path / 'queue'
    reports = q / 'projects' / 'pj' / 'reports' / 'archive'
    for t in ('T1', 'T2'):
        _write(
            reports / f'{t}__review.yaml',
            _findings(
                t,
                'severity: minor\n    category: maintainability\n    d: x',
                'severity: major\n    category: ros2_best_practice\n    d: y',
            ),
        )
    inbox = _write(q / '_inbox.md', INBOX_DONE)
    rows = tally.tally(tally.collect(q))
    assert {r['category'] for r in rows if r['task_count'] >= 2} == {'maintainability', 'ros2_best_practice'}
    assert tally.append_inbox(inbox, rows) == []
    assert tally.main(['--queue-dir', str(q), '--inbox', str(inbox)]) == 0
    graph.sync(q, q / '.graph.db', inbox_path=inbox)
    assert inbox.read_text(encoding='utf-8') == INBOX_DONE


# schema 4 (#137) の review_categories。件数が 1 列だけだった
SCHEMA4_REVIEW_CATEGORIES = (
    'CREATE TABLE review_categories (category TEXT PRIMARY KEY, count INTEGER NOT NULL, first_seen TEXT NOT NULL, '
    'last_seen TEXT NOT NULL, prs_json TEXT NOT NULL, tasks_json TEXT NOT NULL, approx INTEGER NOT NULL)'
)


def _schema4_db(db: Path) -> None:
    """#137 の sync が作った graph.db と同じ形 (user_version 4、review_categories に旧件数の行) を作る."""
    conn = sqlite3.connect(db)
    for stmt in graph.SCHEMA:
        conn.execute(SCHEMA4_REVIEW_CATEGORIES if 'review_categories' in stmt else stmt)
    conn.execute('INSERT INTO meta VALUES (?, ?)', ('synced_at', '2026-11-24T00:00:00+00:00'))
    conn.execute(
        'INSERT INTO review_categories VALUES (?, ?, ?, ?, ?, ?, ?)',
        ('maintainability', 4, '2026-01-30T00:00:00+00:00', '2026-01-30T00:00:00+00:00', '[]', '["TASK-003"]', 1),
    )
    conn.execute(
        'INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)', ('intent:x/OLD', 'intent', 'x', 'done', '', '', '{}', '')
    )
    conn.execute('PRAGMA user_version = 4')
    conn.commit()
    conn.close()


def test_graph_sync_migrates_schema4_db(queue: Path) -> None:
    """#137 の schema 4 の graph.db を開いても壊れず、sync で新しい列に作り直される (旧行は残らない)."""
    db = queue / '.graph.db'
    _schema4_db(db)
    assert graph.SCHEMA_VERSION > 4
    graph.sync(queue, db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == graph.SCHEMA_VERSION
        cols = [r[1] for r in conn.execute('PRAGMA table_info(review_categories)')]
        rows = {
            r[0]: r[1:]
            for r in conn.execute('SELECT category, task_count, blocking, non_blocking, checks FROM review_categories')
        }
        nodes = {r[0] for r in conn.execute('SELECT node_id FROM nodes')}
    finally:
        conn.close()
    assert cols == [
        'category', 'task_count', 'blocking', 'non_blocking', 'checks',
        'first_seen', 'last_seen', 'prs_json', 'tasks_json', 'approx',
    ]  # fmt: skip
    assert 'maintainability' not in rows  # 旧件数の行は YAML から作り直した結果に置き換わる
    assert rows['stale-state'] == (2, 1, 1, 0)
    assert 'intent:x/OLD' not in nodes  # 他の表も YAML から作り直す (派生索引)
    graph.sync(queue, db)  # 2 回目 (version が一致した DB) も同じ
    assert graph.status(db) is not None
