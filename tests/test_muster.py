#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.py `muster` (中隊ビュー) の判定ロジックのテスト.

表示の整形ではなく、誤ると嘘の状況認識になる 3 点だけを見る:
project の担当 session 解決 / 未配達 report のカウント / squad 無関係な
tmux session の除外。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
import squad as squad_cli  # noqa: E402
from ledger import delivery_key  # noqa: E402
from ledger import ReportLedger  # noqa: E402
from ledger import report_identity  # noqa: E402

RID = '11111111-1111-4111-8111-111111111111'


def _project(root: Path, name: str, owner: str | None = None) -> Path:
    d = root / name
    (d / 'reports').mkdir(parents=True)
    if owner is not None:
        (d / '.squad_session').write_text(owner + '\n')
    return d


def test_project_owner_falls_back_to_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SQUAD_DEFAULT_OWNER', raising=False)
    assert squad_cli.project_owner(_project(tmp_path, 'marked', 'rmf')) == 'rmf'
    assert squad_cli.project_owner(_project(tmp_path, 'bare')) == 'ros-agents'
    # 空マーカーは「担当なし」ではなく既定 owner。空を担当扱いすると report が宙に浮く
    assert squad_cli.project_owner(_project(tmp_path, 'empty', '')) == 'ros-agents'


def test_owner_map_groups_projects_by_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SQUAD_DEFAULT_OWNER', raising=False)
    _project(tmp_path, 'a', 'rmf')
    _project(tmp_path, 'b', 'rmf')
    _project(tmp_path, 'c')
    monkeypatch.setattr(squad_cli, 'QUEUE_DIR', tmp_path)
    assert squad_cli.owner_map() == {'rmf': ['a', 'b'], 'ros-agents': ['c']}


def test_pending_reports_counts_only_undelivered(tmp_path: Path) -> None:
    pj = _project(tmp_path, 'pj', 'rmf')
    done = pj / 'reports' / 'worker1_report.yaml'
    done.write_text(f'report_id: "{RID}"\nstatus: completed\n')
    todo = pj / 'reports' / 'worker2_report.yaml'
    todo.write_text('report_id: "22222222-2222-4222-8222-222222222222"\nstatus: completed\n')
    # report_id を持たない report も「握り潰さない」= 未配達として数える
    (pj / 'reports' / 'worker3_report.yaml').write_text('status: completed\n')

    ledger = ReportLedger(tmp_path / 'ledger.db')
    claim = ledger.claim('pj', RID, str(done), 'a' * 64)
    assert ledger.commit('pj', RID, claim.token)

    assert squad_cli.pending_reports(pj, ledger) == 2


def test_pending_reports_uses_ledger_key_for_review_files(tmp_path: Path) -> None:
    """Review report は report_id を持たない schema なので `review:<path>:<sha>` で照合する."""
    pj = _project(tmp_path, 'pj', 'rmf')
    review = pj / 'reports' / 'worker4_review.yaml'
    review.write_text('task_id: REV-001\nverdict: approve\n')

    ledger = ReportLedger(tmp_path / 'ledger.db')
    sha, meta, parse_error = report_identity(review.read_bytes())
    key, _ = delivery_key(meta, sha, parse_error, str(review))
    assert key.startswith('review:')
    claim = ledger.claim('pj', key, str(review), sha)
    assert ledger.commit('pj', key, claim.token)

    # 配達済みなので 0。report_id を直接読む実装だとここが 1 になる
    assert squad_cli.pending_reports(pj, ledger) == 0


def test_is_squad_session_excludes_unrelated_tmux(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(squad_cli, 'watcher_pid', lambda _s: None)
    owners = {'rmf': ['a']}
    assert squad_cli.is_squad_session('rmf', owners)
    # 担当 project も watcher も log も無い tmux session (利用者の手動セッション) は出さない
    assert not squad_cli.is_squad_session('1', owners)


# --- order / hq: 他 session を触る破壊的コマンドのガード ---


def _order_args(**kw) -> argparse.Namespace:
    base = {'message': 'やあ', 'sessions': '', 'all': False, 'dry_run': True}
    return argparse.Namespace(**{**base, **kw})


def _live(monkeypatch: pytest.MonkeyPatch, sessions: list[str], squad: list[str]) -> None:
    monkeypatch.setattr(squad_cli, 'owner_map', dict)
    monkeypatch.setattr(squad_cli, 'tmux_sessions', lambda: set(sessions))
    monkeypatch.setattr(squad_cli, 'is_squad_session', lambda s, _o: s in squad)


def test_order_without_target_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _live(monkeypatch, ['rmf', 'pochi'], ['rmf', 'pochi'])
    # 宛先を書き忘れた / -s が空 → 全 squad へのブロードキャストに落ちてはいけない
    assert squad_cli.cmd_order(_order_args(), {}) == 1
    assert squad_cli.cmd_order(_order_args(sessions='  ,  '), {}) == 1


def test_order_all_is_limited_to_squad_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _live(monkeypatch, ['rmf', 'pochi', 'mytmux'], ['rmf', 'pochi'])
    assert squad_cli.cmd_order(_order_args(all=True), {}) == 0
    # 利用者の手動 session (mytmux) には撃たない
    assert '送信先: pochi, rmf' in capsys.readouterr().out


def test_order_skips_dead_session_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _live(monkeypatch, ['rmf'], ['rmf'])
    sent = []
    monkeypatch.setattr(
        squad_cli.subprocess, 'run', lambda cmd, **kw: sent.append(cmd) or subprocess.CompletedProcess(cmd, 0, '', '')
    )
    # 停止中の session は skip するが、成功と報告してはいけない
    assert squad_cli.cmd_order(_order_args(sessions='rmf,gone', dry_run=False), {}) == 1
    assert len(sent) == 1 and sent[0][1:] == ['0.0', 'やあ']


def test_hq_rejects_names_tmux_would_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    called = []
    monkeypatch.setattr(squad_cli, '_tmux', lambda *a: called.append(a))
    # tmux は "." / ":" を "_" にするので、以降の target 指定が全部外れる
    assert squad_cli.cmd_hq(argparse.Namespace(session='my.hq'), {}) == 1
    assert squad_cli.cmd_hq(argparse.Namespace(session='my:hq'), {}) == 1
    assert called == []
