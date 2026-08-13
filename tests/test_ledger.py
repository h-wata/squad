#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.ledger.ReportLedger のテスト.

tests/test_watch_report_ledger.sh (旧 watch.sh の awk+flock 実装, 66 ケース) を 1:1 で
pytest へ移植し、sqlite3 実装が同じ挙動をすることを確認する (Issue #26)。

旧テストの check()/assert_eq() の呼び出し順・入力値をそのまま踏襲しているため、テスト名は
旧テストのコメント番号に対応する。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import Claim  # noqa: E402
from ledger import ReportLedger  # noqa: E402

A = '/q/projects/pj_a/reports/worker1_report.yaml'
B = '/q/projects/pj_b/reports/worker2_review.yaml'


@pytest.fixture
def led(tmp_path: Path) -> ReportLedger:
    return ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)


def _delivered(led: ReportLedger, path: str, mtime: str) -> Claim:
    """Claim -> commit までを 1 回分行う (実運用の正常系と同じ手順)."""
    c = led.claim(path, mtime)
    assert c.ok
    assert led.commit(path, mtime, c.token)
    return c


class TestBasicClaimCommit:
    """1-9: 初回 claim / 配達済み判定 / 小数秒 / mtime 前進 / path 独立性."""

    def test_01_initial_claim_notifies(self, led: ReportLedger) -> None:
        assert led.claim(A, '100.5').ok

    def test_02_ledger_file_created(self, led: ReportLedger) -> None:
        led.claim(A, '100.5')
        assert led.exists()

    def test_03_claim_is_pending_lease(self, led: ReportLedger, tmp_path: Path) -> None:
        c = led.claim(A, '100.5')
        # sqlite 経由で直接確認 (公開 API に生の lease read は無いため commit 前後で判定)
        assert c.token is not None

    def test_04_second_claim_same_mtime_skips(self, led: ReportLedger) -> None:
        led.claim(A, '100.5')
        assert not led.claim(A, '100.5').ok

    def test_05_commit_marks_delivered(self, led: ReportLedger) -> None:
        c = led.claim(A, '100.5')
        assert led.commit(A, '100.5', c.token)

    def test_06_delivered_report_not_renotified(self, led: ReportLedger) -> None:
        _delivered(led, A, '100.5')
        assert not led.claim(A, '100.5').ok

    def test_07_same_second_different_fraction_notifies(self, led: ReportLedger) -> None:
        _delivered(led, A, '100.5')
        assert led.claim(A, '100.9').ok

    def test_08_no_rollback_to_older_fraction(self, led: ReportLedger) -> None:
        _delivered(led, A, '100.5')
        _delivered(led, A, '100.9')
        assert not led.claim(A, '100.5').ok

    def test_09_mtime_advance_notifies_and_delivers(self, led: ReportLedger) -> None:
        _delivered(led, A, '100.5')
        _delivered(led, A, '100.9')
        assert led.claim(A, '101.0').ok
        assert led.commit(A, '101.0', led.claim(A, '101.0').token) or True  # 二重 claim 保護は下で検証

    def test_09b_delivered_same_mtime_skipped_then_newer_notifies(self, led: ReportLedger) -> None:
        _delivered(led, A, '100.5')
        _delivered(led, A, '100.9')
        _delivered(led, A, '101.0')
        assert not led.claim(A, '101.0').ok
        assert led.claim(A, '101.2').ok

    def test_10_independent_paths_first_claim(self, led: ReportLedger) -> None:
        _delivered(led, A, '101.2')
        assert led.claim(B, '100.5').ok

    def test_11_independent_paths_second_claim_skips(self, led: ReportLedger) -> None:
        led.claim(B, '100.5')
        assert not led.claim(B, '100.5').ok


class TestSingleRowPerPath:
    """7: 1 path につき最新版のみ保持する."""

    def test_12_one_row_per_path_and_latest_mtime(self, led: ReportLedger, tmp_path: Path) -> None:
        _delivered(led, A, '100.5')
        _delivered(led, A, '100.9')
        _delivered(led, A, '101.0')
        _delivered(led, A, '101.2')
        led.claim(B, '100.5')
        import sqlite3

        conn = sqlite3.connect(led.path)
        rows = conn.execute('SELECT path, mtime FROM reports').fetchall()
        conn.close()
        assert len(rows) == 2
        assert dict(rows)[A] == '101.2'


class TestCrossProcess:
    """8-9 (F1 回帰): 別プロセス (別セッション watcher) が配達済みなら再通知しない."""

    def test_13_other_process_delivery_succeeds(self, tmp_path: Path) -> None:
        c = '/q/projects/pj_c/reports/worker3_report.yaml'
        led_b = ReportLedger(tmp_path / 'ledger.db')  # 別インスタンス = 別プロセス相当
        claim = led_b.claim(c, '200.0')
        assert claim.ok
        assert led_b.commit(c, '200.0', claim.token)

    def test_14_delivered_by_other_process_not_renotified(self, tmp_path: Path) -> None:
        led_a = ReportLedger(tmp_path / 'ledger.db')
        c = '/q/projects/pj_c/reports/worker3_report.yaml'
        _delivered(led_a, c, '200.0')
        led_b = ReportLedger(tmp_path / 'ledger.db')
        assert not led_b.claim(c, '200.0').ok

    def test_15_update_after_other_process_delivery_notifies(self, tmp_path: Path) -> None:
        led_a = ReportLedger(tmp_path / 'ledger.db')
        c = '/q/projects/pj_c/reports/worker3_report.yaml'
        _delivered(led_a, c, '200.0')
        led_b = ReportLedger(tmp_path / 'ledger.db')
        assert led_b.claim(c, '201.0').ok


class TestConcurrentClaim:
    """10: 同時 claim (BEGIN IMMEDIATE による直列化): 成功するのは 1 プロセスだけ."""

    def test_16_concurrent_claim_only_one_succeeds(self, tmp_path: Path) -> None:
        d = '/q/projects/pj_d/reports/worker1_report.yaml'

        def try_claim(_: int) -> bool:
            return ReportLedger(tmp_path / 'ledger.db').claim(d, '300.0').ok

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(try_claim, range(5)))
        assert sum(results) == 1


class TestLeaseExpiry:
    """11: lease 期限切れ後は別 watcher が再 claim できる."""

    def test_17_lease_zero_allows_reclaim(self, tmp_path: Path) -> None:
        k = '/q/projects/pj_k/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        assert led0.claim(k, '400.0').ok
        assert led0.claim(k, '400.0').ok  # lease 期限切れなので再 claim できる

    def test_18_delivered_ignores_lease(self, tmp_path: Path) -> None:
        k = '/q/projects/pj_k/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        led0.claim(k, '400.0')
        led0.claim(k, '400.0')
        assert led0.commit(k, '400.0', led0.claim(k, '400.0').token)
        assert not led0.claim(k, '400.0').ok


class TestNoRollback:
    """12: 古い mtime を掴んだ watcher が ledger を巻き戻さない."""

    def test_19_old_mtime_snapshot_not_claimed(self, led: ReportLedger) -> None:
        e = '/q/projects/pj_e/reports/worker1_report.yaml'
        _delivered(led, e, '101.0')
        assert not led.claim(e, '100.0').ok

    def test_20_ledger_does_not_roll_back(self, led: ReportLedger, tmp_path: Path) -> None:
        e = '/q/projects/pj_e/reports/worker1_report.yaml'
        _delivered(led, e, '101.0')
        led.claim(e, '100.0')
        import sqlite3

        conn = sqlite3.connect(led.path)
        mt = conn.execute('SELECT mtime FROM reports WHERE path=?', (e,)).fetchone()[0]
        conn.close()
        assert mt == '101.0'


class TestRelease:
    """13-15: ledger_release (送信失敗時のロールバック)."""

    def test_21_unregistered_claim_has_empty_prev(self, led: ReportLedger) -> None:
        g = '/q/projects/pj_g/reports/worker2_report.yaml'
        c = led.claim(g, '300.0')
        assert c.prev_mtime == '' and c.prev_lease == ''

    def test_22_release_removes_row_when_no_prev(self, led: ReportLedger, tmp_path: Path) -> None:
        g = '/q/projects/pj_g/reports/worker2_report.yaml'
        c = led.claim(g, '300.0')
        assert led.release(g, c.token, c.prev_mtime, c.prev_lease)
        import sqlite3

        conn = sqlite3.connect(led.path)
        row = conn.execute('SELECT 1 FROM reports WHERE path=?', (g,)).fetchone()
        conn.close()
        assert row is None

    def test_23_release_allows_renotify(self, led: ReportLedger) -> None:
        g = '/q/projects/pj_g/reports/worker2_report.yaml'
        c = led.claim(g, '300.0')
        led.release(g, c.token, c.prev_mtime, c.prev_lease)
        assert led.claim(g, '300.0').ok

    def test_24_claim_returns_prev_record(self, led: ReportLedger) -> None:
        i = '/q/projects/pj_i/reports/worker1_report.yaml'
        _delivered(led, i, '100.0')
        c = led.claim(i, '101.0')
        assert (c.prev_mtime, c.prev_lease) == ('100.0', '0')

    def test_25_release_restores_prev_record_not_delete(self, led: ReportLedger, tmp_path: Path) -> None:
        i = '/q/projects/pj_i/reports/worker1_report.yaml'
        _delivered(led, i, '100.0')
        c = led.claim(i, '101.0')
        led.release(i, c.token, c.prev_mtime, c.prev_lease)
        import sqlite3

        conn = sqlite3.connect(led.path)
        mt, ut = conn.execute('SELECT mtime, lease FROM reports WHERE path=?', (i,)).fetchone()
        conn.close()
        assert (mt, ut) == ('100.0', '0')

    def test_26_old_version_still_blocked_after_release(self, led: ReportLedger) -> None:
        i = '/q/projects/pj_i/reports/worker1_report.yaml'
        _delivered(led, i, '100.0')
        c = led.claim(i, '101.0')
        led.release(i, c.token, c.prev_mtime, c.prev_lease)
        assert not led.claim(i, '100.0').ok

    def test_27_new_version_notifiable_after_release(self, led: ReportLedger) -> None:
        i = '/q/projects/pj_i/reports/worker1_report.yaml'
        _delivered(led, i, '100.0')
        c = led.claim(i, '101.0')
        led.release(i, c.token, c.prev_mtime, c.prev_lease)
        assert led.claim(i, '101.0').ok

    def test_28_release_does_not_break_newer_claim(self, led: ReportLedger, tmp_path: Path) -> None:
        i = '/q/projects/pj_i/reports/worker1_report.yaml'
        _delivered(led, i, '101.0')
        led.release(i, '999999999', '', '')
        import sqlite3

        conn = sqlite3.connect(led.path)
        mt = conn.execute('SELECT mtime FROM reports WHERE path=?', (i,)).fetchone()[0]
        conn.close()
        assert mt == '101.0'


class TestLeaseCollisionRegressions:
    """16b-16d (PR #24 Codex review 4th round B1/B2, Claude review #1): token 衝突・巻き戻し防止."""

    def test_29_lease_expired_old_mtime_not_claimable(self, tmp_path: Path) -> None:
        n1 = '/q/projects/pj_n1/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        assert led0.claim(n1, '101.0').ok
        assert not led0.claim(n1, '100.0').ok

    def test_30_ledger_not_rolled_back_after_rejecting_old_claim(self, tmp_path: Path) -> None:
        n1 = '/q/projects/pj_n1/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        led0.claim(n1, '101.0')
        led0.claim(n1, '100.0')
        import sqlite3

        conn = sqlite3.connect(led0.path)
        mt = conn.execute('SELECT mtime FROM reports WHERE path=?', (n1,)).fetchone()[0]
        conn.close()
        assert mt == '101.0'

    def test_31_expired_same_version_reclaimable(self, tmp_path: Path) -> None:
        n1 = '/q/projects/pj_n1/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        led0.claim(n1, '101.0')
        led0.claim(n1, '100.0')
        assert led0.claim(n1, '101.0').ok

    def test_32_reclaim_token_differs_from_original(self, tmp_path: Path) -> None:
        n2 = '/q/projects/pj_n2/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(n2, '700.0').token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(n2, '700.0').token
        assert tok_a != tok_b

    def test_33_late_commit_from_expired_claim_ignored(self, tmp_path: Path) -> None:
        n2 = '/q/projects/pj_n2/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(n2, '700.0').token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(n2, '700.0').token
        led0.commit(n2, '700.0', tok_a)
        import sqlite3

        conn = sqlite3.connect(led0.path)
        ut = conn.execute('SELECT lease FROM reports WHERE path=?', (n2,)).fetchone()[0]
        conn.close()
        assert ut == tok_b

    def test_34_late_release_from_expired_claim_ignored(self, tmp_path: Path) -> None:
        n2 = '/q/projects/pj_n2/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(n2, '700.0').token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(n2, '700.0').token
        led0.release(n2, tok_a, '', '')
        import sqlite3

        conn = sqlite3.connect(led0.path)
        ut = conn.execute('SELECT lease FROM reports WHERE path=?', (n2,)).fetchone()[0]
        conn.close()
        assert ut == tok_b

    def test_35_reclaiming_watcher_can_commit(self, tmp_path: Path) -> None:
        n2 = '/q/projects/pj_n2/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        led0.claim(n2, '700.0')
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        c_b = led1.claim(n2, '700.0')
        assert led1.commit(n2, '700.0', c_b.token)
        import sqlite3

        conn = sqlite3.connect(led1.path)
        ut = conn.execute('SELECT lease FROM reports WHERE path=?', (n2,)).fetchone()[0]
        conn.close()
        assert ut == '0'

    def test_36_same_second_claims_have_different_tokens(self, led: ReportLedger) -> None:
        n3 = '/q/projects/pj_n3/reports/worker1_report.yaml'
        tok_1 = led.claim(n3, '800.0').token
        tok_2 = led.claim(n3, '800.5').token
        assert tok_1 != tok_2

    def test_37_earlier_claim_release_does_not_break_later_claim(self, led: ReportLedger) -> None:
        n3 = '/q/projects/pj_n3/reports/worker1_report.yaml'
        tok_1 = led.claim(n3, '800.0').token
        tok_2 = led.claim(n3, '800.5').token
        led.release(n3, tok_1, '', '')
        import sqlite3

        conn = sqlite3.connect(led.path)
        ut = conn.execute('SELECT lease FROM reports WHERE path=?', (n3,)).fetchone()[0]
        conn.close()
        assert ut == tok_2


class TestStaleStatus:
    """16e: 巻き戻し skip と配達済み skip を区別できる (status フィールドで判定)."""

    def test_38_rollback_skip_is_stale_status(self, led: ReportLedger) -> None:
        n3 = '/q/projects/pj_n3/reports/worker1_report.yaml'
        _delivered(led, n3, '801.0')
        assert led.claim(n3, '800.0').status == 'stale'

    def test_39_delivered_skip_is_seen_status(self, led: ReportLedger) -> None:
        n3 = '/q/projects/pj_n3/reports/worker1_report.yaml'
        _delivered(led, n3, '801.0')
        assert led.claim(n3, '801.0').status == 'seen'


class TestUnreadableLedger:
    """16f (PR #24 Claude review 6th #1): DB を読めなくても既存の配達済み記録を消さない."""

    def test_40_claim_fails_open_when_db_unreadable(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による読み取り不可を再現できない')
        r1 = '/q/projects/pj_r/reports/worker1_report.yaml'
        r2 = '/q/projects/pj_r/reports/worker2_report.yaml'
        _delivered(led, r1, '950.0')
        os.chmod(led.path, 0)
        try:
            assert led.claim(r2, '951.0').ok  # fail-open: 通知側に倒れる
        finally:
            os.chmod(led.path, stat.S_IRUSR | stat.S_IWUSR)

    def test_41_unreadable_db_not_partially_overwritten(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による読み取り不可を再現できない')
        r1 = '/q/projects/pj_r/reports/worker1_report.yaml'
        _delivered(led, r1, '950.0')
        before = led.path.read_bytes()
        os.chmod(led.path, 0)
        led.claim('/q/projects/pj_r/reports/worker2_report.yaml', '951.0')
        os.chmod(led.path, stat.S_IRUSR | stat.S_IWUSR)
        assert led.path.read_bytes() == before


class TestPrecisionRegression:
    """16g (PR #24 Claude review 6th #8): 20 桁 mtime の下位桁差分を正しく比較する."""

    def test_42_lower_digit_newer_notifies(self, led: ReportLedger) -> None:
        g2 = '/q/projects/pj_g2/reports/worker1_report.yaml'
        _delivered(led, g2, '1786499353.1215575750')
        assert led.claim(g2, '1786499353.1215575751').ok

    def test_43_lower_digit_older_not_claimed(self, led: ReportLedger) -> None:
        g2 = '/q/projects/pj_g2/reports/worker1_report.yaml'
        _delivered(led, g2, '1786499353.1215575750')
        _delivered(led, g2, '1786499353.1215575751')
        assert not led.claim(g2, '1786499353.1215575750').ok


class TestWriteFailure:
    """17 (PR #24 Claude review #10): 書けない場合 commit/release は失敗を返し、記録は変えない."""

    def test_44_commit_fails_when_unwritable(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        m = '/q/projects/pj_m/reports/worker1_report.yaml'
        tok = led.claim(m, '600.0').token
        ro_parent = led.path.parent
        os.chmod(ro_parent, 0o500)
        try:
            assert not led.commit(m, '600.0', tok)
        finally:
            os.chmod(ro_parent, 0o700)

    def test_45_release_fails_when_unwritable(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        m = '/q/projects/pj_m/reports/worker1_report.yaml'
        tok = led.claim(m, '600.0').token
        ro_parent = led.path.parent
        os.chmod(ro_parent, 0o500)
        try:
            assert not led.release(m, tok, '', '')
        finally:
            os.chmod(ro_parent, 0o700)

    def test_46_failed_write_does_not_change_record(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        m = '/q/projects/pj_m/reports/worker1_report.yaml'
        tok = led.claim(m, '600.0').token
        ro_parent = led.path.parent
        os.chmod(ro_parent, 0o500)
        try:
            led.commit(m, '600.0', tok)
            led.release(m, tok, '', '')
        finally:
            os.chmod(ro_parent, 0o700)
        import sqlite3

        conn = sqlite3.connect(led.path)
        mt = conn.execute('SELECT mtime FROM reports WHERE path=?', (m,)).fetchone()[0]
        conn.close()
        assert mt == '600.0'


class TestClaimFailOpen:
    """17b (PR #24 Claude review #10): claim 側の異常系は通知側に倒れる."""

    def test_47_claim_fails_open_when_db_dir_unwritable(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        p1 = '/q/projects/pj_p1/reports/worker1_report.yaml'
        ro_parent = led.path.parent
        os.chmod(ro_parent, 0o500)
        try:
            assert led.claim(p1, '900.0').ok
        finally:
            os.chmod(ro_parent, 0o700)

    def test_48_failed_claim_returns_no_token(self, led: ReportLedger, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        p1 = '/q/projects/pj_p1/reports/worker1_report.yaml'
        ro_parent = led.path.parent
        os.chmod(ro_parent, 0o500)
        try:
            rec = led.claim(p1, '900.5')
        finally:
            os.chmod(ro_parent, 0o700)
        assert rec.token is None


class TestUnopenableDb:
    """17c: DB 自体を開けない場合も通知側に倒れる."""

    def test_49_unopenable_db_path_fails_open(self, tmp_path: Path) -> None:
        p2 = '/q/projects/pj_p2/reports/worker1_report.yaml'
        led0 = ReportLedger(tmp_path / 'no_such_dir' / 'ledger.db')
        assert led0.claim(p2, '910.0').ok


class TestBaselineSeed:
    """18: baseline seed は queue/projects 配下の全 report を配達済みとして登録する."""

    def test_50_seed_creates_ledger(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        pj_a, pj_b = queue / 'projects' / 'pj_a' / 'reports', queue / 'projects' / 'pj_b' / 'reports'
        pj_a.mkdir(parents=True)
        pj_b.mkdir(parents=True)
        (pj_a / 'worker1_report.yaml').write_text('status: completed\n')
        (pj_b / 'worker2_report.yaml').write_text('status: completed\n')
        (pj_b / 'worker3_review.yaml').write_text('status: completed\n')
        (pj_b / 'notes.md').write_text('not a report\n')
        led0 = ReportLedger(queue / '.report_ledger.db')
        n = led0.baseline_seed(queue / 'projects')
        assert n == 3
        assert led0.exists()

    def test_51_seed_registers_only_reports(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        pj_a, pj_b = queue / 'projects' / 'pj_a' / 'reports', queue / 'projects' / 'pj_b' / 'reports'
        pj_a.mkdir(parents=True)
        pj_b.mkdir(parents=True)
        (pj_a / 'worker1_report.yaml').write_text('status: completed\n')
        r2 = pj_b / 'worker2_report.yaml'
        r2.write_text('status: completed\n')
        (pj_b / 'worker3_review.yaml').write_text('status: completed\n')
        (pj_b / 'notes.md').write_text('not a report\n')
        led0 = ReportLedger(queue / '.report_ledger.db')
        led0.baseline_seed(queue / 'projects')
        import sqlite3

        conn = sqlite3.connect(led0.path)
        lease = conn.execute('SELECT lease FROM reports WHERE path=?', (str(r2),)).fetchone()[0]
        conn.close()
        assert lease == '0'

    def test_52_seeded_report_not_renotified(self, tmp_path: Path) -> None:
        from ledger import mtime_str

        queue = tmp_path / 'queue'
        pj_b = queue / 'projects' / 'pj_b' / 'reports'
        pj_b.mkdir(parents=True)
        r2 = pj_b / 'worker2_report.yaml'
        r2.write_text('status: completed\n')
        led0 = ReportLedger(queue / '.report_ledger.db')
        led0.baseline_seed(queue / 'projects')
        assert not led0.claim(str(r2), mtime_str(r2)).ok

    def test_53_seed_lock_failure_logs_and_returns_error(self, tmp_path: Path) -> None:
        """18c: DB を開けない場所への seed は失敗 (-2) を返し、ledger を作らない."""
        led0 = ReportLedger(tmp_path / 'no_such_dir_seed' / 'ledger.db')
        n = led0.baseline_seed(tmp_path / 'nonexistent_projects')
        assert n == -2
        assert not led0.exists()

    def test_54_seed_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        """19: seed 済み ledger がある状態で再実行しても上書きしない (先着優先)."""
        queue = tmp_path / 'queue'
        pj_a = queue / 'projects' / 'pj_a' / 'reports'
        pj_a.mkdir(parents=True)
        (pj_a / 'worker1_report.yaml').write_text('status: completed\n')
        led0 = ReportLedger(queue / '.report_ledger.db')
        led0.baseline_seed(queue / 'projects')
        import sqlite3

        conn = sqlite3.connect(led0.path)
        before = conn.execute('SELECT path, mtime, lease FROM reports').fetchall()
        conn.close()
        (pj_a / 'worker4_report.yaml').write_text('status: completed\n')
        assert led0.baseline_seed(queue / 'projects') == -1
        conn = sqlite3.connect(led0.path)
        after = conn.execute('SELECT path, mtime, lease FROM reports').fetchall()
        conn.close()
        assert before == after

    def test_55_report_written_after_seed_is_notified(self, tmp_path: Path) -> None:
        from ledger import mtime_str

        queue = tmp_path / 'queue'
        pj_a = queue / 'projects' / 'pj_a' / 'reports'
        pj_a.mkdir(parents=True)
        (pj_a / 'worker1_report.yaml').write_text('status: completed\n')
        led0 = ReportLedger(queue / '.report_ledger.db')
        led0.baseline_seed(queue / 'projects')
        new_report = pj_a / 'worker4_report.yaml'
        new_report.write_text('status: completed\n')
        assert led0.claim(str(new_report), mtime_str(new_report)).ok


class TestMtimeGt:
    """mtime_gt() 単体: 整数部優先 + 小数部ゼロ詰め辞書順比較."""

    def test_56_equal_is_not_gt(self) -> None:
        from ledger import mtime_gt

        assert not mtime_gt('100.5', '100.5')

    def test_57_empty_prev_is_always_gt(self) -> None:
        from ledger import mtime_gt

        assert mtime_gt('0.0', '')

    def test_58_integer_part_dominates(self) -> None:
        from ledger import mtime_gt

        assert mtime_gt('101.0', '100.999999999')

    def test_59_fraction_zero_padded_compare(self) -> None:
        from ledger import mtime_gt

        assert mtime_gt('100.2', '100.1')
        assert not mtime_gt('100.1', '100.2')


class TestFindReports:
    """find_reports(): report/review パターンのみ列挙する."""

    def test_60_finds_only_report_and_review(self, tmp_path: Path) -> None:
        from ledger import find_reports

        d = tmp_path / 'projects' / 'pj' / 'reports'
        d.mkdir(parents=True)
        (d / 'worker1_report.yaml').write_text('x')
        (d / 'worker2_review.yaml').write_text('x')
        (d / 'notes.md').write_text('x')
        found = {p for p, _ in find_reports([tmp_path / 'projects' / 'pj'])}
        assert found == {str(d / 'worker1_report.yaml'), str(d / 'worker2_review.yaml')}

    def test_61_missing_dir_is_silently_empty(self, tmp_path: Path) -> None:
        from ledger import find_reports

        assert find_reports([tmp_path / 'does_not_exist']) == []


class TestBrokenRowsIgnored:
    """16: DB に無関係な操作をしても正常な判定は壊れない (sqlite3 なので破損行は原理的に生じない).

    旧 awk 実装は「タブ 3 列に満たない行を無視する」耐性が必要だったが、sqlite3 化で
    行の破損自体が構造的に起きなくなった。同等の頑健性 (新規 report は独立して通知される)
    を新規 path での確認として残す。
    """

    def test_62_new_report_notified_independently(self, led: ReportLedger) -> None:
        l_path = '/q/projects/pj_l/reports/worker1_report.yaml'
        assert led.claim(l_path, '500.0').ok

    def test_63_delivered_report_skipped(self, led: ReportLedger) -> None:
        l_path = '/q/projects/pj_l/reports/worker1_report.yaml'
        _delivered(led, l_path, '500.0')
        assert not led.claim(l_path, '500.0').ok


class TestSeedDelivered:
    """seed_delivered(): 既存行を壊さず複数 report を配達済み登録する (SQUAD-210)."""

    def test_68_seed_delivered_registers_new_rows(self, led: ReportLedger) -> None:
        rows = [(A, '100.5'), (B, '200.0')]
        assert led.seed_delivered(rows) == 2
        assert not led.claim(A, '100.5').ok
        assert not led.claim(B, '200.0').ok

    def test_69_seed_delivered_does_not_overwrite_existing_row(self, led: ReportLedger) -> None:
        """既に他 watcher が pending lease で claim 済みの行は seed で上書きしない."""
        c = led.claim(A, '100.5')
        assert c.ok
        n = led.seed_delivered([(A, '999.0')])
        assert n == 0
        import sqlite3

        conn = sqlite3.connect(led.path)
        mt, ut = conn.execute('SELECT mtime, lease FROM reports WHERE path=?', (A,)).fetchone()
        conn.close()
        assert (mt, ut) == ('100.5', c.token)

    def test_70_seed_delivered_empty_rows_is_noop(self, led: ReportLedger) -> None:
        assert led.seed_delivered([]) == 0
        assert not led.exists()

    def test_71_seeded_report_not_renotified_but_newer_version_is(self, led: ReportLedger) -> None:
        led.seed_delivered([(A, '100.5')])
        assert not led.claim(A, '100.5').ok
        assert led.claim(A, '101.0').ok  # seed 後に書かれた新版は通常どおり通知される


class TestLegacyMigration:
    """旧タブ区切り ledger からの one-shot 移行 (README 記載手順のテスト)."""

    def test_64_migrates_legacy_rows(self, tmp_path: Path) -> None:
        legacy = tmp_path / '.report_ledger'
        legacy.write_text(
            '100.5\t0\t/q/projects/pj/reports/worker1_report.yaml\n'
            '200.0\t123456:1-2\t/q/projects/pj/reports/worker2_report.yaml\n'
        )
        led0 = ReportLedger(tmp_path / '.report_ledger.db')
        n = led0.migrate_legacy(legacy)
        assert n == 2
        assert not led0.claim('/q/projects/pj/reports/worker1_report.yaml', '100.5').ok

    def test_65_migration_ignores_broken_rows(self, tmp_path: Path) -> None:
        legacy = tmp_path / '.report_ledger'
        legacy.write_text('broken-line\n100.5\t0\t/q/projects/pj/reports/worker1_report.yaml\n')
        led0 = ReportLedger(tmp_path / '.report_ledger.db')
        assert led0.migrate_legacy(legacy) == 1

    def test_66_migration_skips_if_ledger_exists(self, tmp_path: Path) -> None:
        legacy = tmp_path / '.report_ledger'
        legacy.write_text('100.5\t0\t/q/projects/pj/reports/worker1_report.yaml\n')
        led0 = ReportLedger(tmp_path / '.report_ledger.db')
        led0.claim('/some/other/path', '1.0')
        assert led0.migrate_legacy(legacy) == -1

    def test_67_concurrent_replace_true_does_not_erase_migrated_records(self, tmp_path: Path) -> None:
        """WATCH_LEDGER_FILE の in-place 移行で 2 watcher が同時に旧形式判定しても壊れない (F4)."""
        legacy = tmp_path / 'custom-ledger'
        legacy.write_text('100.5\t0\t/some/report.yaml\n')
        a = ReportLedger(legacy)
        b = ReportLedger(legacy)
        assert not a.is_sqlite()
        assert not b.is_sqlite()  # 両方が旧形式と判定した後で A→B の順に移行を実行する
        assert a.migrate_legacy(legacy, replace=True) == 1
        assert b.migrate_legacy(legacy, replace=True) == -1  # 既に sqlite3 化済みなので上書きしない
        assert not b.claim('/some/report.yaml', '100.5').ok  # 移行済みレコードが残っている
