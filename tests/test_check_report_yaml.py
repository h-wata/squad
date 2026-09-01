#!/usr/bin/env python3
# ruff: noqa: CPY001
"""check_report_yaml.py のテスト.

見たいのは 2 点だけ:
  - 判断を隠せなくする (assumptions 必須)
  - 検証を省いたことを隠せなくする (verify: があるなら verdict の裏取りを要求)
内容の当否は判定しないので、そこはテストしない。
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'scripts'))

from check_report_yaml import check_report_yaml  # noqa: E402

TASK_WITH_VERIFY = """task_id: T1
project: pj
verify:
  commands:
    - "pytest -q"
"""
TASK_NO_VERIFY = """task_id: T1
project: pj
"""


def _project(tmp_path: Path, task_text: str | None = TASK_WITH_VERIFY) -> Path:
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    (pj / 'tasks').mkdir(parents=True)
    (pj / 'reports').mkdir(parents=True)
    if task_text is not None:
        (pj / 'tasks' / 'worker1.yaml').write_text(task_text)
    return pj


def _report(pj: Path, body: str) -> Path:
    p = pj / 'reports' / 'worker1_report.yaml'
    p.write_text(body)
    return p


def _statuses(results: list) -> dict[int, str]:
    return {r.item: r.status for r in results}


def _ng_items(results: list) -> set[int]:
    return {r.item for r in results if r.status == 'NG'}


def test_missing_assumptions_is_ng(tmp_path: Path) -> None:
    pj = _project(tmp_path)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: completed\nverdict_path: ""\n')
    assert 1 in _ng_items(check_report_yaml(p))


def test_assumptions_none_is_accepted(tmp_path: Path) -> None:
    """判断が無かったなら "none" と明記すればよい (書く手間を負わせるのが目的ではない)."""
    pj = _project(tmp_path, TASK_NO_VERIFY)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: none\n')
    assert _statuses(check_report_yaml(p))[1] == 'OK'


def test_empty_assumptions_is_ng(tmp_path: Path) -> None:
    pj = _project(tmp_path, TASK_NO_VERIFY)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: ""\n')
    assert 1 in _ng_items(check_report_yaml(p))


def test_completed_without_verdict_is_ng_when_task_has_verify(tmp_path: Path) -> None:
    """自己申告の pass を通さない (今回 Opencode worker が実際にこれをやった)."""
    pj = _project(tmp_path)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: completed\nverify_status: pass\nassumptions: none\n')
    assert 2 in _ng_items(check_report_yaml(p))


def test_no_verify_in_task_needs_no_verdict(tmp_path: Path) -> None:
    pj = _project(tmp_path, TASK_NO_VERIFY)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: none\n')
    assert not _ng_items(check_report_yaml(p))


def test_blocked_report_needs_no_verdict(tmp_path: Path) -> None:
    """途中で詰まった報告まで verdict を要求すると、blocked を出す動機を削いでしまう."""
    pj = _project(tmp_path)
    p = _report(pj, 'task_id: T1\nworker: worker1\nstatus: blocked\nassumptions: none\n')
    assert not _ng_items(check_report_yaml(p))


def test_verdict_must_exist(tmp_path: Path) -> None:
    pj = _project(tmp_path)
    p = _report(
        pj,
        'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: none\nverdict_path: "reports/nope.yaml"\n',
    )
    assert 2 in _ng_items(check_report_yaml(p))


def test_verdict_fail_blocks_completed(tmp_path: Path) -> None:
    pj = _project(tmp_path)
    (pj / 'reports' / 'worker1_verdict.yaml').write_text('task_id: T1\nresult: fail\n')
    p = _report(
        pj,
        'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: none\nverdict_path: "worker1_verdict.yaml"\n',
    )
    assert 3 in _ng_items(check_report_yaml(p))


def test_verdict_task_id_must_match(tmp_path: Path) -> None:
    """別タスクの verdict を流用して pass を名乗れないこと."""
    pj = _project(tmp_path)
    (pj / 'reports' / 'worker1_verdict.yaml').write_text('task_id: OTHER\nresult: pass\n')
    p = _report(
        pj,
        'task_id: T1\nworker: worker1\nstatus: completed\nassumptions: none\nverdict_path: "worker1_verdict.yaml"\n',
    )
    assert 3 in _ng_items(check_report_yaml(p))


def test_passing_report_has_no_ng(tmp_path: Path) -> None:
    pj = _project(tmp_path)
    (pj / 'reports' / 'worker1_verdict.yaml').write_text('task_id: T1\nresult: pass\n')
    p = _report(
        pj,
        'task_id: T1\nworker: worker1\nstatus: completed\n'
        'assumptions:\n  - "受け入れ条件の解釈をこう決めた"\n'
        'verdict_path: "worker1_verdict.yaml"\n',
    )
    assert not _ng_items(check_report_yaml(p))


def test_verdict_report_itself_is_exempt(tmp_path: Path) -> None:
    """Verdict / review は別 schema なので verdict の裏取りを求めない."""
    pj = _project(tmp_path)
    p = pj / 'reports' / 'worker1_verdict.yaml'
    p.write_text('task_id: T1\nworker: worker1\nresult: pass\nassumptions: none\n')
    assert not _ng_items(check_report_yaml(p))


def test_broken_yaml_is_ng_but_does_not_raise(tmp_path: Path) -> None:
    pj = _project(tmp_path)
    p = _report(pj, 'task_id: [unclosed\n')
    assert _ng_items(check_report_yaml(p))
