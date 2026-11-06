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
    for prefix in ('>', '-', '*', '+', '–'):
        assert detect_status(f'{prefix} Thinking… (12s)\n{prefix} Working (1m 2s)\n❯ ')['status'] == 'idle'


def test_legacy_working_spinner_is_busy() -> None:
    assert detect_status('✻ Working (1m 2s)\n❯ ')['status'] == 'busy'


def test_symbol_and_spinner_on_different_lines_is_idle() -> None:
    assert detect_status('✻\nThinking… (12s\n❯ ')['status'] == 'idle'


# Codex の実 pane (tmux capture-pane) の形。作業中は最下部のスピナー行、完了後は区切り線 + プロンプト
CODEX_WORKING = """\
• Ran stat -c '%y %n' queue/projects/*/tasks/worker4.yaml | sort
  └ 2026-07-02 18:24:58.292757912 +0900 queue/projects/mesh-mem/tasks/worker4.yaml
    … +24 lines (ctrl + t to view transcript)

• Working (1m 06s • esc to interrupt)

› Summarize recent commits
  gpt-5.6-terra high · ~/work/squad · Context 87% left · 5h 98% left
"""
CODEX_DONE = """\
• Ran stat -c '%y %n' queue/projects/*/tasks/worker4.yaml | sort
─ Worked for 7m 07s ────────────────────────────────────────────
• worker4 の cross-review を完了し、報告を作成しました。

› Improve documentation in @filename
  gpt-5.6-terra high · ~/work · Context 77% left · 5h 67% left
"""


def test_codex_working_is_busy() -> None:
    assert detect_status(CODEX_WORKING)['status'] == 'busy'
    assert detect_status('• Working (6s • esc to interrupt)')['status'] == 'busy'
    assert detect_status('• Working (1h 2m 3s • esc to interrupt)')['status'] == 'busy'


def test_codex_finished_is_idle() -> None:
    assert detect_status(CODEX_DONE)['status'] == 'idle'


def test_codex_quoted_working_text_is_idle() -> None:
    assert detect_status('例: • Working (1m 06s • esc to interrupt)\n› ')['status'] == 'idle'
    assert detect_status('> • Working (1m 06s • esc to interrupt)\n› ')['status'] == 'idle'


def test_codex_permission_wins_over_working() -> None:
    assert detect_status(CODEX_WORKING + 'Do you want to proceed?\n 1. Yes')['status'] == 'permission_wait'
