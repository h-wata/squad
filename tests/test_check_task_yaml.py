#!/usr/bin/env python3
# ruff: noqa: CPY001
"""scripts.check_task_yaml のテスト (SQUAD-260).

task-yaml-author の下位モデル委譲判定に使う機械検証器の各検査項目を fixture YAML で
検証する。実運用の queue/projects/ の task YAML には一切触れない。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from check_task_yaml import check_task_yaml  # noqa: E402
from check_task_yaml import collect_task_id_locations  # noqa: E402
from check_task_yaml import main  # noqa: E402

GOOD = {
    'task_id': 'TASK-001',
    'project': 'pj',
    'assigned_to': 'worker1',
    'agent': 'claude',
    'model': 'sonnet',
    'routing_reason': 'test',
    'priority': 'high',
    'title': 'title',
    'description': 'desc',
    'acceptance_criteria': ['done'],
    'verify': {'commands': ['pytest -q'], 'expect': 'all pass', 'max_attempts': 3},
    'created_at': '2026-08-21T00:00:00+09:00',
}


def _write(tmp_path: Path, data: dict, name: str = 'worker1.yaml') -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


def _statuses(results: list, item: int) -> str:
    return next(r.status for r in results if r.item == item)


def test_valid_yaml_has_no_ng(tmp_path: Path) -> None:
    path = _write(tmp_path, GOOD)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert not any(r.status == 'NG' for r in results)


def test_missing_required_field_is_ng(tmp_path: Path) -> None:
    data = {k: v for k, v in GOOD.items() if k != 'title'}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 2) == 'NG'


def test_invalid_agent_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'agent': 'gpt'}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 3) == 'NG'


def test_invalid_model_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'model': 'gpt-5'}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 4) == 'NG'


def test_model_with_codex_agent_is_warn(tmp_path: Path) -> None:
    data = {**GOOD, 'agent': 'codex', 'model': 'sonnet', 'verify_skip_reason': 'codex は verify 対象外'}
    del data['verify']
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 4) == 'WARN'


def test_worker_number_mismatch_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'assigned_to': 'worker2'}
    path = _write(tmp_path, data, name='worker1.yaml')
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 5) == 'NG'


def test_non_worker_filename_skips_assigned_to_check(tmp_path: Path) -> None:
    data = {**GOOD, 'assigned_to': 'documenter'}
    path = _write(tmp_path, data, name='documenter.yaml')
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 5) == 'OK'


def test_duplicate_task_id_is_ng(tmp_path: Path) -> None:
    path1 = _write(tmp_path, GOOD, name='worker1.yaml')
    path2 = _write(tmp_path, {**GOOD, 'assigned_to': 'worker2'}, name='worker2.yaml')
    locations = collect_task_id_locations([path1, path2])
    results1 = check_task_yaml(path1, locations)
    results2 = check_task_yaml(path2, locations)
    assert _statuses(results1, 6) == 'NG'
    assert _statuses(results2, 6) == 'NG'


def test_unique_task_id_is_ok(tmp_path: Path) -> None:
    path1 = _write(tmp_path, GOOD, name='worker1.yaml')
    path2 = _write(tmp_path, {**GOOD, 'task_id': 'TASK-002', 'assigned_to': 'worker2'}, name='worker2.yaml')
    locations = collect_task_id_locations([path1, path2])
    results1 = check_task_yaml(path1, locations)
    assert _statuses(results1, 6) == 'OK'


def test_empty_acceptance_criteria_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'acceptance_criteria': []}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 7) == 'NG'


def test_verify_with_empty_commands_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'verify': {'commands': [], 'expect': 'pass'}}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 8) == 'NG'


def test_verify_without_expect_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'verify': {'commands': ['pytest -q']}}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 8) == 'NG'


def test_verify_with_non_positive_max_attempts_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'verify': {'commands': ['pytest -q'], 'expect': 'pass', 'max_attempts': 0}}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 8) == 'NG'


def test_no_verify_and_no_skip_reason_is_warn(tmp_path: Path) -> None:
    data = {k: v for k, v in GOOD.items() if k != 'verify'}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 9) == 'WARN'


def test_no_verify_with_skip_reason_is_ok(tmp_path: Path) -> None:
    data = {k: v for k, v in GOOD.items() if k != 'verify'}
    data['verify_skip_reason'] = 'ドキュメント修正のみのため'
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 9) == 'OK'


EVIDENCE_CARD_OK = {
    'claim': 'X が Y である',
    'evidence_as_of': '2026-08-21 の調査',
    'data_window': '該当なし',
    'semantic_definition': 'raw count',
    'current_state_check': '確認したが該当変更なし',
    'disconfirming_check': 'SQL 1本で数え直す',
    'decision_if_false': '調査で打ち切り',
}


def test_evidence_card_missing_field_is_ng(tmp_path: Path) -> None:
    card = {k: v for k, v in EVIDENCE_CARD_OK.items() if k != 'claim'}
    data = {**GOOD, 'evidence_card': card}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 10) == 'NG'


@pytest.mark.parametrize('placeholder', ['-', 'N/A', 'TBD', '', '  '])
def test_evidence_card_placeholder_value_is_ng(tmp_path: Path, placeholder: str) -> None:
    card = {**EVIDENCE_CARD_OK, 'claim': placeholder}
    data = {**GOOD, 'evidence_card': card}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 10) == 'NG'


def test_evidence_card_filled_is_ok(tmp_path: Path) -> None:
    data = {**GOOD, 'evidence_card': EVIDENCE_CARD_OK}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 10) == 'OK'


def test_invalid_created_at_is_ng(tmp_path: Path) -> None:
    data = {**GOOD, 'created_at': 'not-a-date'}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 11) == 'NG'


def test_workspace_missing_is_warn(tmp_path: Path) -> None:
    data = {**GOOD, 'context': {'workspace': str(tmp_path / 'nonexistent-dir')}}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 12) == 'WARN'


def test_workspace_existing_is_ok(tmp_path: Path) -> None:
    data = {**GOOD, 'context': {'workspace': str(tmp_path)}}
    path = _write(tmp_path, data)
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 12) == 'OK'


def test_unparseable_yaml_is_ng_without_raising(tmp_path: Path) -> None:
    path = tmp_path / 'broken.yaml'
    path.write_text('task_id: [\nunterminated')
    results = check_task_yaml(path, collect_task_id_locations([path]))
    assert _statuses(results, 1) == 'NG'
    assert len(results) == 1


def test_main_checks_multiple_files_and_returns_1_on_ng(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    good_path = _write(tmp_path, GOOD, name='worker1.yaml')
    bad_path = _write(tmp_path, {**GOOD, 'agent': 'gpt', 'assigned_to': 'worker2'}, name='worker2.yaml')

    rc = main([str(good_path), str(bad_path)])

    assert rc == 1
    out = capsys.readouterr().out
    assert '合計: 2 ファイル' in out


def test_main_all_files_pass_returns_0(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SQUAD_ROOT を tmp に向ける。向けないと task_id 重複検査が開発者の実 queue を
    # 走査し、そこに TASK-001 があるかどうかでテストの合否が変わる。
    monkeypatch.setenv('SQUAD_ROOT', str(tmp_path))
    good_path = _write(tmp_path, GOOD, name='worker1.yaml')

    rc = main([str(good_path)])

    assert rc == 0
