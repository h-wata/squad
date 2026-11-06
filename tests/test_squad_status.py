#!/usr/bin/env python3
# ruff: noqa: CPY001
"""detect_status: スピナー動詞の一覧でなく行の形で作業中を判定する (#79)."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from squad import detect_status  # noqa: E402


@pytest.mark.parametrize(
    'tail',
    [
        '✶ Nucleating… (4m 52s · ↓ 18.4k tokens)',
        '✢ Lollygagging… (12s',
        '✢ Burrowing… (1m 3s',
        '✢ Meandering… (5s · esc to interrupt)',
        '✢ Propagating… (9s',
        '✻ Topsy-turvying… (3s',
    ],
)
def test_spinner_with_unknown_verb_is_busy(tail: str) -> None:
    assert detect_status(f'out\n{tail}\n❯ ')['status'] == 'busy'


@pytest.mark.parametrize('tail', ['✻ Sautéed for 14m 29s · done', '╭───╮\n❯ \n╰───╯', ''])
def test_finished_and_prompt_are_idle(tail: str) -> None:
    assert detect_status(tail)['status'] == 'idle'


def test_permission_wins_over_spinner() -> None:
    tail = '✶ Nucleating… (4m 52s\nDo you want to proceed?\n 1. Yes'
    assert detect_status(tail)['status'] == 'permission_wait'


def test_quoted_spinner_text_is_idle() -> None:
    assert detect_status('例: Nucleating… (4m 52s · ↓ 18.4k tokens)\n❯ ')['status'] == 'idle'


def test_blockquote_and_bullet_spinner_text_is_idle() -> None:
    for prefix in ('>', '-', '*'):
        assert detect_status(f'{prefix} Thinking… (12s)\n{prefix} Working (1m 2s)\n❯ ')['status'] == 'idle'


def test_legacy_working_spinner_is_busy() -> None:
    assert detect_status('✻ Working (1m 2s)\n❯ ')['status'] == 'busy'


def test_symbol_and_spinner_on_different_lines_is_idle() -> None:
    assert detect_status('✻\nThinking… (12s\n❯ ')['status'] == 'idle'
