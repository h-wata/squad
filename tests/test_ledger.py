#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.ledger.ReportLedger のテスト.

配達の主キーは (project, report_id) であり、mtime は一切使わない (SQUAD-215/216)。
旧テスト (mtime ベースの claim/stale/seed 契約) は ID ベース契約へ置換した。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import backoff_seconds  # noqa: E402
from ledger import BACKOFF_SECONDS  # noqa: E402
from ledger import delivery_key  # noqa: E402
from ledger import DELIVERED  # noqa: E402
from ledger import find_reports  # noqa: E402
from ledger import normalize_report_id  # noqa: E402
from ledger import report_identity  # noqa: E402
from ledger import ReportLedger  # noqa: E402

PJ_A, PJ_B = 'pj_a', 'pj_b'
ID1 = '11111111-1111-4111-8111-111111111111'
ID2 = '22222222-2222-4222-8222-222222222222'
PATH_A = '/q/projects/pj_a/reports/worker1_report.yaml'
PATH_B = '/q/projects/pj_b/reports/worker2_review.yaml'
SHA1 = 'a' * 64
SHA2 = 'b' * 64


@pytest.fixture
def led(tmp_path: Path) -> ReportLedger:
    return ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)


def _delivered(led: ReportLedger, project: str, rid: str, path: str = PATH_A, sha: str = SHA1) -> None:
    """Claim -> commit までを 1 回分行う (実運用の正常系と同じ手順)."""
    c = led.claim(project, rid, path, sha)
    assert c.ok
    assert led.commit(project, rid, c.token)


def _row(led: ReportLedger, project: str, rid: str) -> tuple:
    conn = sqlite3.connect(led.path)
    try:
        return conn.execute(
            'SELECT path, content_sha256, state, lease, attempt_count, next_attempt_at '
            'FROM deliveries WHERE project=? AND report_id=?',
            (project, rid),
        ).fetchone()
    finally:
        conn.close()


class TestBasicClaimCommit:
    def test_initial_claim_notifies(self, led: ReportLedger) -> None:
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).ok

    def test_ledger_file_created(self, led: ReportLedger) -> None:
        led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led.exists()

    def test_claim_returns_token_and_first_attempt(self, led: ReportLedger) -> None:
        c = led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert c.token is not None
        assert c.attempt == 1

    def test_second_claim_while_leased_is_held(self, led: ReportLedger) -> None:
        led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).status == 'held'

    def test_delivered_report_not_renotified(self, led: ReportLedger) -> None:
        _delivered(led, PJ_A, ID1)
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).status == 'seen'

    def test_same_project_other_id_is_independent(self, led: ReportLedger) -> None:
        _delivered(led, PJ_A, ID1)
        assert led.claim(PJ_A, ID2, PATH_A, SHA2).ok

    def test_same_id_other_project_is_independent(self, led: ReportLedger) -> None:
        _delivered(led, PJ_A, ID1)
        assert led.claim(PJ_B, ID1, PATH_B, SHA1).ok

    def test_commit_records_delivered_state(self, led: ReportLedger) -> None:
        _delivered(led, PJ_A, ID1)
        _path, _sha, state, lease, _attempts, next_at = _row(led, PJ_A, ID1)
        assert (state, lease, next_at) == (DELIVERED, '', 0)


class TestMtimeIndependence:
    """mtime は配達の同一性・順序・seed 判定のどれにも使わない (SQUAD-216 の核)."""

    def test_same_content_different_id_is_new_delivery(self, led: ReportLedger) -> None:
        """内容が完全に同じでも report_id が違えば別の報告として通知する."""
        _delivered(led, PJ_A, ID1, PATH_A, SHA1)
        assert led.claim(PJ_A, ID2, PATH_A, SHA1).ok

    def test_moved_report_same_id_is_not_renotified(self, led: ReportLedger) -> None:
        """mv/cp で path が変わり mtime が過去へ戻っても、同じ ID なら再通知しない."""
        _delivered(led, PJ_A, ID1, PATH_A, SHA1)
        moved = '/q/projects/pj_a/reports/archive/worker1_report.yaml'
        assert led.claim(PJ_A, ID1, moved, SHA1).status == 'seen'

    def test_edited_report_same_id_is_not_renotified(self, led: ReportLedger) -> None:
        """同じ ID のまま内容だけ書き換えても配達済みは覆らない (再通知は ID の更新で行う)."""
        _delivered(led, PJ_A, ID1, PATH_A, SHA1)
        assert led.claim(PJ_A, ID1, PATH_A, SHA2).status == 'seen'

    def test_ledger_has_no_mtime_api(self) -> None:
        import ledger

        assert not hasattr(ledger, 'mtime_str')
        assert not hasattr(ledger, 'mtime_gt')
        assert not hasattr(ReportLedger, 'seed_delivered')  # 担当変更 seed 用 API も廃止
        assert not hasattr(ReportLedger, 'release')  # 再送予定を記録する fail() に置換


class TestClockWindBack:
    """時計が後退しても配達判定は壊れない (時刻は backoff にしか使わない)."""

    def test_delivered_stays_delivered_when_clock_goes_back(
        self, led: ReportLedger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _delivered(led, PJ_A, ID1)
        monkeypatch.setattr(time, 'time', lambda: 0.0)  # 時計が 1970 まで巻き戻る
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).status == 'seen'

    def test_new_id_still_notified_when_clock_goes_back(
        self, led: ReportLedger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _delivered(led, PJ_A, ID1)
        monkeypatch.setattr(time, 'time', lambda: 0.0)
        assert led.claim(PJ_A, ID2, PATH_A, SHA2).ok


class TestCrossProcess:
    def test_delivered_by_other_process_not_renotified(self, tmp_path: Path) -> None:
        led_a = ReportLedger(tmp_path / 'ledger.db')
        _delivered(led_a, PJ_A, ID1)
        led_b = ReportLedger(tmp_path / 'ledger.db')  # 別インスタンス = 別セッション watcher
        assert not led_b.claim(PJ_A, ID1, PATH_A, SHA1).ok

    def test_new_id_after_other_process_delivery_notifies(self, tmp_path: Path) -> None:
        led_a = ReportLedger(tmp_path / 'ledger.db')
        _delivered(led_a, PJ_A, ID1)
        led_b = ReportLedger(tmp_path / 'ledger.db')
        assert led_b.claim(PJ_A, ID2, PATH_A, SHA2).ok


class TestConcurrentClaim:
    def test_concurrent_claim_only_one_succeeds(self, tmp_path: Path) -> None:
        def try_claim(_: int) -> bool:
            return ReportLedger(tmp_path / 'ledger.db').claim(PJ_A, ID1, PATH_A, SHA1).ok

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(try_claim, range(5)))
        assert sum(results) == 1

    def test_concurrent_claim_after_delivery_all_skip(self, tmp_path: Path) -> None:
        _delivered(ReportLedger(tmp_path / 'ledger.db'), PJ_A, ID1)

        def try_claim(_: int) -> bool:
            return ReportLedger(tmp_path / 'ledger.db').claim(PJ_A, ID1, PATH_A, SHA1).ok

        with ThreadPoolExecutor(max_workers=5) as ex:
            assert sum(ex.map(try_claim, range(5))) == 0


class TestLeaseExpiry:
    def test_expired_lease_allows_reclaim(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).ok
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).ok  # lease 期限切れなので再 claim できる

    def test_delivered_ignores_expired_lease(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        c = led0.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led0.commit(PJ_A, ID1, c.token)
        assert not led0.claim(PJ_A, ID1, PATH_A, SHA1).ok

    def test_reclaim_token_differs_from_original(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(PJ_A, ID1, PATH_A, SHA1).token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(PJ_A, ID1, PATH_A, SHA1).token
        assert tok_a != tok_b

    def test_late_commit_from_expired_claim_ignored(self, tmp_path: Path) -> None:
        """期限切れ claim を持つ A が、B の claim を勝手に配達済みにできない."""
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(PJ_A, ID1, PATH_A, SHA1).token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(PJ_A, ID1, PATH_A, SHA1).token
        led0.commit(PJ_A, ID1, tok_a)
        assert _row(led0, PJ_A, ID1)[3] == tok_b  # lease は B のまま

    def test_late_fail_from_expired_claim_ignored(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        tok_a = led0.claim(PJ_A, ID1, PATH_A, SHA1).token
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        tok_b = led1.claim(PJ_A, ID1, PATH_A, SHA1).token
        led0.fail(PJ_A, ID1, tok_a)
        assert _row(led0, PJ_A, ID1)[3] == tok_b

    def test_reclaiming_watcher_can_commit(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=0)
        led0.claim(PJ_A, ID1, PATH_A, SHA1)
        led1 = ReportLedger(tmp_path / 'ledger.db', lease_seconds=60)
        c_b = led1.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led1.commit(PJ_A, ID1, c_b.token)
        assert _row(led1, PJ_A, ID1)[2] == DELIVERED


class TestRetryBackoff:
    """送信/commit 失敗時だけ backoff で再送する (試行回数の上限は設けない)."""

    def test_backoff_schedule_is_15s_60s_5m_30m_capped(self) -> None:
        assert BACKOFF_SECONDS == (15, 60, 300, 1800)
        assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [15, 60, 300, 1800]

    def test_backoff_has_no_attempt_limit(self) -> None:
        """何回失敗しても間隔は 30 分で頭打ちになり、諦めて 0 にはならない."""
        assert backoff_seconds(5) == 1800
        assert backoff_seconds(100) == 1800
        assert backoff_seconds(10_000) == 1800

    def test_fail_keeps_row_pending_and_schedules_retry(self, led: ReportLedger) -> None:
        c = led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led.fail(PJ_A, ID1, c.token)
        _path, _sha, state, lease, attempts, next_at = _row(led, PJ_A, ID1)
        assert (state, lease, attempts) == ('pending', '', 1)
        assert next_at >= int(time.time()) + backoff_seconds(1) - 1

    def test_claim_held_until_next_attempt_at(self, led: ReportLedger) -> None:
        c = led.claim(PJ_A, ID1, PATH_A, SHA1)
        led.fail(PJ_A, ID1, c.token)
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).status == 'held'  # まだ 15 秒経っていない

    def test_claim_resumes_after_next_attempt_at(self, led: ReportLedger, monkeypatch: pytest.MonkeyPatch) -> None:
        c = led.claim(PJ_A, ID1, PATH_A, SHA1)
        led.fail(PJ_A, ID1, c.token)
        later = time.time() + BACKOFF_SECONDS[0] + 1
        monkeypatch.setattr(time, 'time', lambda: later)
        c2 = led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert c2.ok
        assert c2.attempt == 2  # 再送であることが通知に出せる

    def test_attempt_count_grows_across_failures(self, led: ReportLedger, monkeypatch: pytest.MonkeyPatch) -> None:
        now = time.time()
        for expected in (1, 2, 3):
            monkeypatch.setattr(time, 'time', lambda now=now: now)
            c = led.claim(PJ_A, ID1, PATH_A, SHA1)
            assert c.attempt == expected
            led.fail(PJ_A, ID1, c.token)
            now += backoff_seconds(expected) + 1

    def test_persisted_backoff_survives_process_restart(self, tmp_path: Path) -> None:
        led_a = ReportLedger(tmp_path / 'ledger.db')
        c = led_a.claim(PJ_A, ID1, PATH_A, SHA1)
        led_a.fail(PJ_A, ID1, c.token)
        led_b = ReportLedger(tmp_path / 'ledger.db')  # 再起動相当 (メモリ状態は失われる)
        assert led_b.claim(PJ_A, ID1, PATH_A, SHA1).status == 'held'

    def test_delivered_after_retry_stops_resending(self, led: ReportLedger, monkeypatch: pytest.MonkeyPatch) -> None:
        c = led.claim(PJ_A, ID1, PATH_A, SHA1)
        led.fail(PJ_A, ID1, c.token)
        later = time.time() + BACKOFF_SECONDS[0] + 1
        monkeypatch.setattr(time, 'time', lambda: later)
        c2 = led.claim(PJ_A, ID1, PATH_A, SHA1)
        assert led.commit(PJ_A, ID1, c2.token)
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).status == 'seen'


class TestFailOpen:
    """DB を扱えないときは通知側へ倒す (握り潰さない)."""

    def test_claim_fails_open_when_db_unreadable(self, led: ReportLedger) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による読み取り不可を再現できない')
        _delivered(led, PJ_A, ID1)
        os.chmod(led.path, 0)
        try:
            c = led.claim(PJ_A, ID2, PATH_A, SHA2)
            assert c.ok
            assert c.token is None  # 記録できていないことを呼び出し側に伝える
        finally:
            os.chmod(led.path, stat.S_IRUSR | stat.S_IWUSR)

    def test_unreadable_db_not_partially_overwritten(self, led: ReportLedger) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による読み取り不可を再現できない')
        _delivered(led, PJ_A, ID1)
        before = led.path.read_bytes()
        os.chmod(led.path, 0)
        led.claim(PJ_A, ID2, PATH_A, SHA2)
        os.chmod(led.path, stat.S_IRUSR | stat.S_IWUSR)
        assert led.path.read_bytes() == before

    def test_unopenable_db_path_fails_open(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'no_such_dir' / 'ledger.db')
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).ok

    def test_commit_fails_when_unwritable(self, led: ReportLedger) -> None:
        if os.geteuid() == 0:
            pytest.skip('root では chmod による書き込み不可を再現できない')
        tok = led.claim(PJ_A, ID1, PATH_A, SHA1).token
        os.chmod(led.path.parent, 0o500)
        try:
            assert not led.commit(PJ_A, ID1, tok)
        finally:
            os.chmod(led.path.parent, 0o700)

    def test_memory_backoff_applies_when_db_unusable(self, tmp_path: Path) -> None:
        """DB が使えない間も同じ backoff を適用する (毎サイクル全速力で鳴らさない)."""
        led0 = ReportLedger(tmp_path / 'no_such_dir' / 'ledger.db')
        c = led0.claim(PJ_A, ID1, PATH_A, SHA1)
        assert c.ok and c.token is None
        led0.fail(PJ_A, ID1, c.token)
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).status == 'held'

    def test_memory_backoff_expires_and_resends(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        led0 = ReportLedger(tmp_path / 'no_such_dir' / 'ledger.db')
        c = led0.claim(PJ_A, ID1, PATH_A, SHA1)
        led0.fail(PJ_A, ID1, c.token)
        later = time.time() + BACKOFF_SECONDS[0] + 1
        monkeypatch.setattr(time, 'time', lambda: later)
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).ok  # 沈黙せず再送する


class TestDbReset:
    """DB を失っても沈黙しない (重複側へ倒れる)."""

    def test_report_renotified_after_db_loss(self, led: ReportLedger) -> None:
        _delivered(led, PJ_A, ID1)
        led.path.unlink()
        assert led.claim(PJ_A, ID1, PATH_A, SHA1).ok


class TestReportIdentity:
    def test_identity_extracts_sha_and_scalars(self) -> None:
        sha, meta, err = report_identity(b'report_id: "abc"\ntask_id: T1\nsummary: |\n  block body\n')
        assert err == ''
        assert meta['report_id'] == 'abc'
        assert meta['task_id'] == 'T1'
        assert 'summary' not in meta  # block scalar 本文は読まない
        assert len(sha) == 64

    def test_identity_reports_decode_error(self) -> None:
        _sha, meta, err = report_identity(b'\xff\xfe not utf-8')
        assert err and meta == {}

    def test_normalize_accepts_uuid_variants(self) -> None:
        canonical = normalize_report_id(ID1)
        assert normalize_report_id(ID1.upper()) == canonical
        assert normalize_report_id(ID1.replace('-', '')) == canonical

    def test_normalize_rejects_non_uuid(self) -> None:
        assert normalize_report_id('TBD') == ''
        assert normalize_report_id('') == ''

    def test_delivery_key_uses_report_id(self) -> None:
        rid, invalid = delivery_key({'report_id': ID1}, SHA1, '', PATH_A)
        assert (rid, invalid) == (ID1, '')

    def test_delivery_key_flags_missing_id(self) -> None:
        rid, invalid = delivery_key({}, SHA1, '', PATH_A)
        assert invalid
        assert rid == f'INVALID:{PATH_A}:{SHA1}'  # path + 内容ハッシュ由来。UUID は推測しない

    def test_delivery_key_flags_non_uuid_id(self) -> None:
        _rid, invalid = delivery_key({'report_id': 'TBD'}, SHA1, '', PATH_A)
        assert invalid

    def test_delivery_key_flags_parse_error(self) -> None:
        rid, invalid = delivery_key({}, SHA1, 'decode error', PATH_A)
        assert invalid == 'decode error'
        assert rid == f'INVALID:{PATH_A}:{SHA1}'

    def test_invalid_key_changes_when_content_changes(self) -> None:
        """Schema 準拠に直せば別キーとして改めて通知される (直したのに黙らない)."""
        assert delivery_key({}, SHA1, '', PATH_A)[0] != delivery_key({}, SHA2, '', PATH_A)[0]

    def test_invalid_key_differs_by_path_for_same_content(self) -> None:
        """同一内容・別 path の invalid report はキーが衝突せず両方通知対象になる (F2 回帰)."""
        key_a, _ = delivery_key({}, SHA1, '', PATH_A)
        key_b, _ = delivery_key({}, SHA1, '', PATH_B)
        assert key_a != key_b

    def test_delivery_key_review_without_id_is_not_invalid(self) -> None:
        """review.yaml は schema 上 report_id を持たないため、欠落は invalid 扱いしない."""
        rid, invalid = delivery_key({}, SHA1, '', PATH_B)
        assert invalid == ''
        assert rid == f'review:{PATH_B}:{SHA1}'

    def test_delivery_key_review_still_uses_report_id_if_present(self) -> None:
        rid, invalid = delivery_key({'report_id': ID1}, SHA1, '', PATH_B)
        assert (rid, invalid) == (ID1, '')


class TestFindReports:
    def test_finds_only_report_and_review_with_project(self, tmp_path: Path) -> None:
        pj = tmp_path / 'projects' / 'pj'
        d = pj / 'reports'
        d.mkdir(parents=True)
        (d / 'worker1_report.yaml').write_text('x')
        (d / 'worker2_review.yaml').write_text('x')
        (d / 'notes.md').write_text('x')
        assert set(find_reports([pj])) == {
            ('pj', str(d / 'worker1_report.yaml')),
            ('pj', str(d / 'worker2_review.yaml')),
        }

    def test_archive_subdir_is_not_scanned(self, tmp_path: Path) -> None:
        pj = tmp_path / 'projects' / 'pj'
        (pj / 'reports' / 'archive').mkdir(parents=True)
        (pj / 'reports' / 'archive' / 'worker1_report.yaml').write_text('x')
        assert find_reports([pj]) == []

    def test_missing_dir_is_silently_empty(self, tmp_path: Path) -> None:
        assert find_reports([tmp_path / 'does_not_exist']) == []


class TestNoBaselineSeed:
    """ledger が無い状態からの一括登録 (baseline seed) は行わない (F1 回帰).

    新規導入・DB 消失・再作成のいずれも「ledger が存在しない」という観測だけでは区別
    できない。区別せずに既存 report を delivered へ登録すると、DB 消失時にまだ配達して
    いない report まで沈黙させてしまう。鳴らない経路をゼロにする方針のもとでは、
    導入直後に一斉通知になる方を受け入れる。
    """

    def test_baseline_seed_api_is_gone(self) -> None:
        assert not hasattr(ReportLedger, 'baseline_seed')


class TestMigration:
    """旧 ledger は delivered ID へ推測変換せず、空の配達表へ移行する."""

    def test_legacy_text_at_ledger_path_is_replaced_with_empty_table(self, tmp_path: Path) -> None:
        custom = tmp_path / 'custom-ledger'
        custom.write_text('100.5\t0\t/q/projects/pj/reports/worker1_report.yaml\n')
        led0 = ReportLedger(custom)
        assert led0.migrate() == 'text'
        assert led0.is_sqlite()
        assert led0.claim('pj', ID1, 'x', SHA1).ok  # 旧行は引き継がない = 1 回再通知される

    def test_legacy_text_at_default_path_is_set_aside(self, tmp_path: Path) -> None:
        legacy = tmp_path / '.report_ledger'
        legacy.write_text('100.5\t0\t/q/projects/pj/reports/worker1_report.yaml\n')
        led0 = ReportLedger(tmp_path / '.report_ledger.db')
        assert led0.migrate(legacy) == 'legacy-text'
        assert led0.is_sqlite()
        assert not legacy.exists()
        assert (tmp_path / '.report_ledger.legacy').exists()  # 参照用に残す

    def test_legacy_sqlite_schema_is_dropped(self, tmp_path: Path) -> None:
        db = tmp_path / 'ledger.db'
        conn = sqlite3.connect(db)
        conn.execute('CREATE TABLE reports (path TEXT PRIMARY KEY, mtime TEXT NOT NULL, lease TEXT NOT NULL)')
        conn.execute("INSERT INTO reports VALUES('/q/projects/pj/reports/worker1_report.yaml', '100.5', '0')")
        conn.commit()
        conn.close()
        led0 = ReportLedger(db)
        assert led0.migrate() == 'legacy-table'
        assert led0.claim('pj', ID1, 'x', SHA1).ok
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'reports' not in tables and 'deliveries' in tables

    def test_migrate_is_noop_on_new_schema(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db')
        _delivered(led0, PJ_A, ID1)
        assert led0.migrate() == ''
        assert led0.claim(PJ_A, ID1, PATH_A, SHA1).status == 'seen'  # 記録が消えていない

    def test_migrate_is_noop_when_no_ledger(self, tmp_path: Path) -> None:
        led0 = ReportLedger(tmp_path / 'ledger.db')
        assert led0.migrate(tmp_path / 'nope') == ''
        assert not led0.exists()

    def test_concurrent_text_migration_does_not_erase_migrated_records(self, tmp_path: Path) -> None:
        """2 watcher が同時に旧形式と判定しても、後から来た側が空 DB で上書きしない."""
        legacy = tmp_path / 'custom-ledger'
        legacy.write_text('100.5\t0\t/some/report.yaml\n')
        a, b = ReportLedger(legacy), ReportLedger(legacy)
        assert not a.is_sqlite() and not b.is_sqlite()
        assert a.migrate() == 'text'
        _delivered(a, PJ_A, ID1)
        assert b.migrate() == ''  # 既に sqlite3 化済みなので触らない
        assert b.claim(PJ_A, ID1, PATH_A, SHA1).status == 'seen'
