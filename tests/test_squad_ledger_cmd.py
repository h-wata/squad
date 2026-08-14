#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.py `ledger` サブコマンド (claim/commit/fail/seed) のテスト.

watchd.py は ReportLedger をプロセス内で直接呼ぶため使わないが、Issue #26 が要求する
手動操作用 CLI として squad.py に実装したもの (squad/ledger.py の薄いラッパ)。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
import squad as squad_cli  # noqa: E402

RID = '11111111-1111-4111-8111-111111111111'
SHA = 'a' * 64


def _run(*argv: str, capsys: pytest.CaptureFixture) -> tuple[int, str]:
    code = squad_cli.main(list(argv))
    return code, capsys.readouterr().out.strip()


def test_claim_commit_seed_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
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

    code, out = _run('ledger', 'seed', str(tmp_path), '--ledger-file', str(db), capsys=capsys)
    assert code == 1  # ledger が既にあるので baseline_seed は -1 (seeded 済み扱い)


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
