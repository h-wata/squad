# ruff: noqa: CPY001
"""テスト全体の共通設定."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchd が Dispatcher に送るたびに state/dispatcher.json へ書くので、本番の squad/state に書かせない.

    個々のテストが SQUAD_STATE_DIR を設定すればそちらが優先される。
    """
    monkeypatch.setenv('SQUAD_STATE_DIR', str(tmp_path / '_state'))


@pytest.fixture(autouse=True)
def _isolate_task_evals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchd の task 完了時の自動評価を既定で止め、出力先も tmp にする (本番の metrics/ に書かせない).

    自動評価を見るテスト (tests/test_task_eval.py) は Config で有効にする。
    """
    monkeypatch.setenv('WATCH_TASK_EVAL', '0')
    monkeypatch.setenv('WATCH_TASK_EVALS_FILE', str(tmp_path / '_metrics' / 'task_evals.jsonl'))
