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
