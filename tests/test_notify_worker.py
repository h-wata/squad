#!/usr/bin/env python3
# ruff: noqa: CPY001
"""notify-worker.sh の送信確認 / 着手確認テスト (SQUAD-281).

本番 session (halow 等) を巻き込まないよう、テスト専用の tmux session を作り、その pane で
Claude Code 風の TUI (入力欄を枠で描き、送信中は "esc to interrupt" を出す) を模したモックを
走らせて notify-worker.sh を実行する。session は fixture で必ず kill する。

確認する契約:
  1. 送信できたら exit 0 で、本文がちょうど 1 回だけ transcript に入る
  2. Enter が 1 回落ちても、本文を貼り直さず Enter だけ打ち直して 1 回だけ発注する (症状 B/C)
  3. 中断案内が出ない worker (別処理中 / 即答) でも失敗扱いにせず、
     「送信は確認できましたが、着手は確認できませんでした」と報告する (症状 A)
"""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

REPO = Path(__file__).resolve().parent.parent
NOTIFY = REPO / 'scripts' / 'notify-worker.sh'

pytestmark = pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not available')

# 入力欄を枠で描き、Enter で transcript に流す最小 TUI。
# MOCK_MODE=drop-first-enter: 最初の Enter を握り潰す (症状 B の再現)
# MOCK_MODE=no-interrupt:     中断案内を一切出さない (症状 A の再現)
# MOCK_MODE=blind-start:      起動直後 4 秒は描画せず Ctrl-U も効かない (症状 C の再現)
MOCK_TUI = r"""
import os, select, sys, termios, time, tty

mode = os.environ.get('MOCK_MODE', 'normal')
log = open(os.environ['MOCK_LOG'], 'w', buffering=1)
transcript, buf, busy_until, dropped = [], b'', 0.0, False
blind_until = time.time() + 4 if mode == 'blind-start' else 0.0

def draw():
    if time.time() < blind_until:   # 描画が追いつかず入力欄が見えない状態
        return
    out = ['\033[H\033[J']
    out += transcript[-8:]
    text = buf.decode('utf-8', 'replace')
    out.append('╭' + '─' * 60 + '╮')
    for i in range(0, max(len(text), 1), 40):   # 実物と同じく折り返して描く
        out.append(('│ > ' if i == 0 else '│   ') + text[i:i + 40])
    out.append('╰' + '─' * 60 + '╯')
    if time.time() < busy_until:
        out.append('esc to interrupt')
    sys.stdout.write('\r\n'.join(out) + '\r\n')
    sys.stdout.flush()

tty.setraw(sys.stdin.fileno())
draw()
while True:
    if select.select([sys.stdin], [], [], 0.2)[0]:
        ch = sys.stdin.buffer.raw.read(1)
        if not ch:
            break
        if ch in (b'\r', b'\n'):
            if mode == 'drop-first-enter' and not dropped:
                dropped = True
            elif buf:
                line = buf.decode('utf-8', 'replace')
                log.write(line + '\n')
                transcript.append('> ' + line[:60])
                buf = b''
                if mode != 'no-interrupt':
                    busy_until = time.time() + 6
        elif ch == b'\x15':      # Ctrl-U
            if mode != 'blind-start':
                buf = b''
        elif ch == b'\x03':      # Ctrl-C は届いてはいけない
            log.write('!!! CTRL-C RECEIVED\n')
        else:
            buf += ch
    draw()
"""

MSG = '新しいタスクがあります。/home/gisen/work/squad/queue/projects/squad/tasks/worker9.yaml を確認してください。'
PROBE = '/home/gisen/work/squad/queue/projects/squad/tasks/worker9.yaml'


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['tmux', *args], capture_output=True, text=True, check=False)


@pytest.fixture
def pane(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """モック TUI を動かすテスト専用 session を作り、終了時に必ず kill する."""
    session = f'squad281test-{uuid.uuid4().hex[:8]}'
    mock = tmp_path / 'mock_tui.py'
    mock.write_text(MOCK_TUI)
    log = tmp_path / 'submitted.log'
    _tmux(
        'new-session',
        '-d',
        '-s',
        session,
        '-x',
        '100',
        '-y',
        '30',
        f'MOCK_LOG={log} python3 {mock}',
    )
    try:
        time.sleep(1.0)
        yield session, log
    finally:
        _tmux('kill-session', '-t', session)


def _notify(session: str, mode: str = 'normal') -> subprocess.CompletedProcess:
    env = {**os.environ, 'SQUAD_SESSION': session, 'MOCK_MODE': mode}
    _tmux('set-environment', '-t', session, 'MOCK_MODE', mode)
    return subprocess.run(
        ['bash', str(NOTIFY), '0.0', MSG],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


def _restart_mock(session: str, tmp_path: Path, log: Path, mode: str) -> None:
    """Mode を変えてモックを起動し直す (respawn-pane で同じ pane を再利用)."""
    mock = tmp_path / 'mock_tui.py'
    _tmux(
        'respawn-pane',
        '-k',
        '-t',
        f'{session}:0.0',
        f'MOCK_MODE={mode} MOCK_LOG={log} python3 {mock}',
    )
    time.sleep(1.0)


def test_submits_exactly_once(pane: tuple[str, Path]) -> None:
    session, log = pane
    proc = _notify(session)
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().count(PROBE) == 1, log.read_text()
    assert 'CTRL-C' not in log.read_text()


def test_resends_enter_without_repasting(pane: tuple[str, Path], tmp_path: Path) -> None:
    """Enter が 1 回落ちても、貼り直さず Enter だけで 1 回だけ発注する (症状 B/C)."""
    session, log = pane
    _restart_mock(session, tmp_path, log, 'drop-first-enter')
    proc = _notify(session, 'drop-first-enter')
    assert proc.returncode == 0, proc.stderr
    assert 'Enter だけ打ち直します' in proc.stderr
    body = log.read_text()
    assert body.count(PROBE) == 1, body  # 二重発注していない
    assert 'CTRL-C' not in body


def test_reports_sent_but_not_started(pane: tuple[str, Path], tmp_path: Path) -> None:
    """中断案内が出なくても失敗にせず、確認できた範囲を正確に報告する (症状 A)."""
    session, log = pane
    _restart_mock(session, tmp_path, log, 'no-interrupt')
    proc = _notify(session, 'no-interrupt')
    assert proc.returncode == 0, proc.stderr  # 誤報 → 再送 → 二重発注 を防ぐ
    assert '送信は確認できましたが、着手は確認できませんでした' in proc.stderr
    assert '反応しません' not in proc.stderr
    assert log.read_text().count(PROBE) == 1


def test_does_not_repaste_when_body_already_landed(pane: tuple[str, Path], tmp_path: Path) -> None:
    """描画が遅れて再送に回っても、既に乗っている本文を貼り直さない (症状 C)."""
    session, log = pane
    _restart_mock(session, tmp_path, log, 'blind-start')
    proc = _notify(session, 'blind-start')
    assert proc.returncode == 0, proc.stderr
    assert '貼り直さず Enter だけ送ります' in proc.stderr
    lines = [ln for ln in log.read_text().splitlines() if ln]
    assert len(lines) == 1, lines  # 発注は 1 回だけ
    assert lines[0].count(PROBE) == 1, lines[0]  # 本文が入力欄で積み上がっていない
