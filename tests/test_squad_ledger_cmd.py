#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.py `ledger` サブコマンド (claim/commit/fail) のテスト.

watchd.py は ReportLedger をプロセス内で直接呼ぶため使わないが、Issue #26 が要求する
手動操作用 CLI として squad.py に実装したもの (squad/ledger.py の薄いラッパ)。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import ReportLedger  # noqa: E402
import squad as squad_cli  # noqa: E402

RID = '11111111-1111-4111-8111-111111111111'
SHA = 'a' * 64


def _run(*argv: str, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    code = squad_cli.main(list(argv))
    return code, capsys.readouterr().out.strip()


def test_claim_commit_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db = tmp_path / 'ledger.db'
    report = tmp_path / 'report.yaml'
    report.write_text('x')

    code, out = _run('ledger', 'claim', 'pj', RID, str(report), SHA, '--ledger-file', str(db), capsys=capsys)
    assert code == 0
    assert '"status": "claim"' in out
    token = out.split('"token": "')[1].split('"')[0]

    code, out = _run('ledger', 'commit', 'pj', RID, token, '--ledger-file', str(db), capsys=capsys)
    assert code == 0
    assert '"ok": true' in out

    code, out = _run('ledger', 'claim', 'pj', RID, str(report), SHA, '--ledger-file', str(db), capsys=capsys)
    assert code == 1  # 配達済みなので claim できない
    assert '"status": "seen"' in out


def test_seed_subcommand_is_gone(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        _run('ledger', 'seed', str(tmp_path), capsys=capsys)


def test_fail_schedules_retry(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    db = tmp_path / 'ledger.db'
    report = tmp_path / 'report.yaml'
    report.write_text('x')

    _, out = _run('ledger', 'claim', 'pj', RID, str(report), SHA, '--ledger-file', str(db), capsys=capsys)
    token = out.split('"token": "')[1].split('"')[0]

    code, out = _run('ledger', 'fail', 'pj', RID, token, '--ledger-file', str(db), capsys=capsys)
    assert code == 0
    assert '"ok": true' in out

    code, out = _run('ledger', 'claim', 'pj', RID, str(report), SHA, '--ledger-file', str(db), capsys=capsys)
    assert code == 1  # backoff 中なので再送しない
    assert '"status": "held"' in out


def _delivered_at(db: Path) -> float:
    conn = sqlite3.connect(db)
    try:
        (at,) = conn.execute('SELECT delivered_at FROM deliveries WHERE report_id = ?', (RID,)).fetchone()
    finally:
        conn.close()
    return float(at)


def test_cli_commit_does_not_record_delivered_at(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """手で叩く `squad ledger commit` は delivered_at を書かない (task 完了時の自動評価の再走査に拾わせない)."""
    db = tmp_path / 'ledger.db'
    report = tmp_path / 'report.yaml'
    report.write_text('x')
    _, out = _run('ledger', 'claim', 'pj', RID, str(report), SHA, '--ledger-file', str(db), capsys=capsys)
    token = out.split('"token": "')[1].split('"')[0]
    code, _ = _run('ledger', 'commit', 'pj', RID, token, '--ledger-file', str(db), capsys=capsys)
    assert code == 0
    assert _delivered_at(db) == 0.0
    assert ReportLedger(db).delivered_since(['pj'], 0.0) == []


def test_commit_records_delivered_at_only_when_asked(tmp_path: Path) -> None:
    """ReportLedger.commit() は既定で delivered_at を書かない. 書くのは record_delivered_at=True のときだけ."""
    db = tmp_path / 'ledger.db'
    led = ReportLedger(db)
    c = led.claim('pj', RID, 'p', SHA)
    assert led.commit('pj', RID, c.token)
    assert _delivered_at(db) == 0.0
    rid2 = '22222222-2222-4222-8222-222222222222'
    c2 = led.claim('pj', rid2, 'p', SHA)
    assert led.commit('pj', rid2, c2.token, record_delivered_at=True)
    assert [r for _p, r, _t in led.delivered_since(['pj'], 0.0)] == [rid2]
