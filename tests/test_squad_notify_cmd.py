#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.py `notify pull/ack` サブコマンドのテスト (SQUAD-220 Step 4: 小さな ack helper)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
import squad as squad_cli  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402


def _run(*argv: str, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    code = squad_cli.main(list(argv))
    return code, capsys.readouterr().out.strip()


def test_pull_shows_unacked_events(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    nq.enqueue(project='pj', source='report', priority='normal', message='hello', dedupe_key='k1')

    code, out = _run('notify', 'pull', '--queue-dir', str(tmp_path), capsys=capsys)
    assert code == 0
    lines = [json.loads(ln) for ln in out.splitlines()]
    assert len(lines) == 1
    assert lines[0]['message'] == 'hello'


def test_pull_filters_by_priority(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    nq.enqueue(project='pj', source='report', priority='normal', message='m1', dedupe_key='k1')
    nq.enqueue(project='pj', source='blocked', priority='critical', message='m2', dedupe_key='k2')

    code, out = _run('notify', 'pull', '--queue-dir', str(tmp_path), '--priority', 'critical', capsys=capsys)
    assert code == 0
    lines = [json.loads(ln) for ln in out.splitlines()]
    assert len(lines) == 1
    assert lines[0]['priority'] == 'critical'


def test_ack_single_event(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    e = nq.enqueue(project='pj', source='report', priority='normal', message='m1', dedupe_key='k1')

    code, out = _run('notify', 'ack', e['event_id'], '--queue-dir', str(tmp_path), capsys=capsys)
    assert code == 0
    assert '"ok": true' in out
    assert nq.unacked() == []


def test_ack_all(tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    nq.enqueue(project='pj', source='report', priority='normal', message='m1', dedupe_key='k1')
    nq.enqueue(project='pj', source='report', priority='normal', message='m2', dedupe_key='k2')

    code, _out = _run('notify', 'ack', 'all', '--queue-dir', str(tmp_path), capsys=capsys)
    assert code == 0
    assert nq.unacked() == []


def test_pull_respects_session_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = NotificationQueue('othersess', notify_dir_for(tmp_path, 'othersess'))
    other.enqueue(project='pj', source='report', priority='normal', message='not mine', dedupe_key='k1')

    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    code, out = _run('notify', 'pull', '--queue-dir', str(tmp_path), capsys=capsys)
    assert code == 0
    assert out == ''


def test_pull_fails_loudly_on_corrupt_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQUAD-226 critical 1: 破損 queue を「0 件」と表示しない (見落としを防ぐ)."""
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    nq.enqueue(project='pj', source='blocked', priority='critical', message='m1', dedupe_key='k1')
    nq.events_path.write_text('{corrupt')

    code = squad_cli.main(['notify', 'pull', '--queue-dir', str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out.strip() == ''  # 空 queue と誤読させない
    assert json.loads(captured.err)['error'] == 'queue_unreadable'


def test_ack_all_fails_loudly_on_corrupt_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('SQUAD_SESSION', 'testsess')
    nq = NotificationQueue('testsess', notify_dir_for(tmp_path, 'testsess'))
    nq.enqueue(project='pj', source='blocked', priority='critical', message='m1', dedupe_key='k1')
    nq.events_path.write_text('{corrupt')

    assert squad_cli.main(['notify', 'ack', 'all', '--queue-dir', str(tmp_path)]) == 2


def test_dispatcher_instructions_document_pull_ack_protocol() -> None:
    """SQUAD-226 critical 3: 実装した規約 (pull/ack/異常時報告) が指示書に載っていること."""
    doc = (Path(__file__).resolve().parent.parent / 'instructions' / 'dispatcher.md').read_text()
    for needle in (
        'notify pull',
        'notify ack',
        'WATCH_NOTIFY_QUEUE',
        '[QUEUE-ERROR]',
        'queue/notifications/',
    ):
        assert needle in doc, f'dispatcher.md に {needle} の記載が無い'
