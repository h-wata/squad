#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad.watchd の振る舞いテスト (ledger 以外).

report-bridge ループ・停止検知・discovery/inbox・.squad_session 絞り込みを、tmux は
FakeTmux (subprocess を使わない) に差し替えて検証する。旧 test_watch_report_bridge.sh
(実 tmux スタブプロセスを起動する結合テスト) と同じ観点を、プロセスを跨がず Watcher
クラスを直接呼び出す形で確認する (Issue #26)。
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from watchd import Config  # noqa: E402
from watchd import posix_cksum  # noqa: E402
from watchd import Watcher  # noqa: E402


def cksum_key(text: str) -> str:
    """外部 coreutils cksum を oracle にした旧 dedup key (実装に依存しない照合用)."""
    out = subprocess.run(['cksum'], input=text.encode(), capture_output=True, check=True).stdout
    return out.split()[0].decode()


class FakeTmux:
    """tmux 呼び出しを記録するだけのスタブ (実 tmux を一切叩かない)."""

    def __init__(self, session: str, panes: set[str] | None = None) -> None:
        self.session = session
        self.panes = (
            panes
            if panes is not None
            else {f'{session}:0.0', f'{session}:0.1', f'{session}:0.2', f'{session}:0.3', f'{session}:0.6'}
        )
        self.sent: list[tuple[str, str]] = []
        self.captures: dict[str, list[str]] = {}  # pane -> 順に返す capture 内容 (末尾を使い回す)
        self.fail_send = False

    def has_session(self) -> bool:
        return True

    def pane_exists(self, pane: str) -> bool:
        return pane in self.panes

    def capture(self, pane: str, lines: int = 40) -> str:
        q = self.captures.get(pane)
        if not q:
            return ''
        return q.pop(0) if len(q) > 1 else q[0]

    def send_keys(self, pane: str, keys: str) -> bool:
        if self.fail_send:
            return False
        self.sent.append((pane, keys))
        return True


def make_project(queue: Path, name: str, session: str | None = None) -> Path:
    d = queue / 'projects' / name
    (d / 'reports').mkdir(parents=True)
    (d / 'tasks').mkdir(parents=True)
    if session:
        (d / '.squad_session').write_text(session + '\n')
    return d


@pytest.fixture
def watcher(tmp_path: Path) -> tuple[Watcher, Path]:
    queue = tmp_path / 'queue'
    cfg = Config(session='testsess', default_owner='not-this-session', queue_dir=queue, interval=1, lease_seconds=60)
    w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
    w.sleep = lambda _s: None  # テストを待たせない
    return w, queue


class TestReportBridge:
    def test_new_report_notified_once(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(w.tmux.sent) == 2  # 本文 + Enter
        assert 'worker1_report.yaml' in w.tmux.sent[0][1]
        w.report_bridge()  # 同じサイクルをもう一度回しても再送しない
        assert len(w.tmux.sent) == 2

    def test_blocked_report_gets_inbox_prefix(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: blocked\n')
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent[0][1].startswith('[INBOX]')

    def test_unowned_project_report_ignored(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='othersess')  # 別セッション担当
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent == []

    def test_default_owner_covers_unmarked_project(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        cfg = Config(session='ros-agents', default_owner='ros-agents', queue_dir=queue, interval=1)
        w = Watcher(cfg=cfg, tmux=FakeTmux('ros-agents'))
        w.sleep = lambda _s: None
        d = make_project(queue, 'pj')  # マーカー無し → default owner が担当
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(w.tmux.sent) == 2

    def test_failed_send_does_not_mark_delivered(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        report = d / 'reports' / 'worker1_report.yaml'
        report.write_text('status: completed\n')
        w.tmux.fail_send = True
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent == []
        w.tmux.fail_send = False
        w.report_bridge()  # 復旧後に再送される
        assert len(w.tmux.sent) == 2

    def test_ownership_switch_does_not_renotify_delivered(self, tmp_path: Path) -> None:
        """A(testsess) が配達済みにした report を、担当が B(othersess) へ移っても再通知しない."""
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='testsess')
        report = d / 'reports' / 'worker1_report.yaml'
        report.write_text('status: completed\n')

        cfg_a = Config(session='testsess', default_owner='none', queue_dir=queue, interval=1)
        w_a = Watcher(cfg=cfg_a, tmux=FakeTmux('testsess'))
        w_a.sleep = lambda _s: None
        w_a.refresh_owned_projects()
        w_a.report_bridge()
        assert len(w_a.tmux.sent) == 2

        (d / '.squad_session').write_text('othersess\n')
        cfg_b = Config(session='othersess', default_owner='none', queue_dir=queue, interval=1)
        w_b = Watcher(cfg=cfg_b, tmux=FakeTmux('othersess'))
        w_b.sleep = lambda _s: None
        w_b.refresh_owned_projects()
        w_b.report_bridge()
        assert w_b.tmux.sent == []  # 共有 ledger で既に配達済みと分かる


class TestStallDetection:
    def test_pending_task_stall_notifies_after_n_cycles(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
        w.cfg.stall_cycles = 2
        w.refresh_owned_projects()
        w.tmux.captures[f'{w.cfg.session}:0.1'] = ['idle screen']
        w.check_workers()  # 1回目: hash 初期化 (stall=0)
        assert w.tmux.sent == []
        w.check_workers()  # 2回目: 無変化を検知 (stall=1, まだ閾値未満)
        assert w.tmux.sent == []
        w.check_workers()  # 3回目: stall=2 で閾値到達
        assert any('約' in m and '停止' in m for _, m in w.tmux.sent)

    def test_completed_task_does_not_stall(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')

        report = d / 'reports' / 'worker1_report.yaml'
        report.touch()  # report の方が新しい mtime にする
        w.cfg.stall_cycles = 1
        w.refresh_owned_projects()
        w.check_workers()
        assert w.tmux.sent == []

    def test_approval_prompt_auto_answered(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'tasks' / 'worker1.yaml').write_text('task_id: T1\n')
        w.refresh_owned_projects()
        w.tmux.captures[f'{w.cfg.session}:0.1'] = ['Do you want to proceed? (y/n)']
        w.check_workers()
        assert (f'{w.cfg.session}:0.1', 'y') in w.tmux.sent
        assert (f'{w.cfg.session}:0.1', 'Enter') in w.tmux.sent

    def test_missing_pane_skipped_without_stall(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'tasks' / 'worker4.yaml').write_text('task_id: T1\n')  # W4 pane 無し想定
        w.tmux.panes.discard(f'{w.cfg.session}:0.6')
        w.cfg.stall_cycles = 1
        w.refresh_owned_projects()
        w.check_workers()
        assert w.tmux.sent == []


class TestDiscovery:
    def test_no_discovery_yaml_is_noop(self, watcher: tuple[Watcher, Path], capsys: pytest.CaptureFixture) -> None:
        w, queue = watcher
        make_project(queue, 'pj', session='testsess')
        w.refresh_owned_projects()
        w.run_discovery()
        out = capsys.readouterr().out
        assert '設定なし' in out

    def test_baseline_run_seeds_without_notifying(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'discovery.yaml').write_text('repo: /tmp/x\ntodo_paths: src\nsources: todo\n')
        w.refresh_owned_projects()
        w.run_discovery()
        assert w.tmux.sent == []
        assert w.cfg.seen_file.exists()

    def test_todo_discovery_writes_inbox(self, tmp_path: Path, watcher: tuple[Watcher, Path]) -> None:
        """Baseline は黙って既知化し、その後に増えた TODO 行だけが inbox に載る."""
        w, queue = watcher
        repo = tmp_path / 'repo'
        (repo / 'src').mkdir(parents=True)
        (repo / 'src' / 'a.py').write_text('# TODO: fix this\nprint(1)\n')
        d = make_project(queue, 'pj', session='testsess')
        (d / 'discovery.yaml').write_text(f'repo: {repo}\ntodo_paths: src\nsources: todo\n')
        w.refresh_owned_projects()
        w.run_discovery()  # 1 回目 = baseline (既知化のみ、通知なし)
        assert w.cfg.inbox_file.exists()
        assert 'TODO: fix this' not in w.cfg.inbox_file.read_text()
        w.run_discovery()  # 2 回目: 候補は既知化済みなので再追記されない
        assert 'TODO: fix this' not in w.cfg.inbox_file.read_text()
        if shutil.which('cksum'):
            # 永続 key は旧 watch.sh (cksum) と同一でなければならない (F3)
            expected = f'pj:todo:{cksum_key(f"{repo / 'src' / 'a.py'}|# TODO: fix this")}'
            assert expected in w.cfg.seen_file.read_text().split()

        (repo / 'src' / 'b.py').write_text('# TODO: brand new\n')
        w.run_discovery()
        assert 'TODO: brand new' in w.cfg.inbox_file.read_text()
        assert any('DISCOVERY' in m for _, m in w.tmux.sent)

    @pytest.mark.skipif(shutil.which('cksum') is None, reason='coreutils cksum が無い')
    def test_todo_dedup_key_stays_posix_cksum_compatible(self, tmp_path: Path, watcher: tuple[Watcher, Path]) -> None:
        """旧 watch.sh (cksum) が書いた .discovery_seen を引き継いでも再通知しない (F3)."""
        w, queue = watcher
        repo = tmp_path / 'repo'
        (repo / 'src').mkdir(parents=True)
        todo = '# TODO: fix this'
        (repo / 'src' / 'a.py').write_text(f'{todo}\nprint(1)\n')
        d = make_project(queue, 'pj', session='testsess')
        (d / 'discovery.yaml').write_text(f'repo: {repo}\ntodo_paths: src\nsources: todo\n')
        queue.mkdir(parents=True, exist_ok=True)
        legacy_key = f'pj:todo:{cksum_key(f"{repo / 'src' / 'a.py'}|{todo}")}'
        w.cfg.seen_file.write_text(legacy_key + '\n')  # 旧形式の seen file を引き継いだ状態

        w.refresh_owned_projects()
        w.run_discovery()  # baseline ではない (seen file が既にある)
        assert 'TODO: fix this' not in w.cfg.inbox_file.read_text()
        assert not any('DISCOVERY' in m for _, m in w.tmux.sent)

    def test_disabled_discovery_skipped(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'discovery.yaml').write_text('enabled: false\nrepo: /tmp/x\n')
        w.refresh_owned_projects()
        w.run_discovery()
        assert not w.cfg.inbox_file.exists() or w.cfg.inbox_file.read_text().count('- [ ]') == 0


class TestSessionMarkerFiltering:
    def test_refresh_owned_only_matches_session(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        make_project(queue, 'mine', session='testsess')
        make_project(queue, 'theirs', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['mine']

    def test_unmarked_project_falls_back_to_default_owner(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        make_project(queue, 'pj')  # マーカー無し
        cfg = Config(session='ros-agents', default_owner='ros-agents', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('ros-agents'))
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['pj']


class TestZeroOwnedWarning:
    """SQUAD-210/212: 担当 project 0 件の起動を可視化する (ログ + Dispatcher 通知)."""

    def test_warns_when_no_project_owned(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        queue = tmp_path / 'queue'
        make_project(queue, 'theirs', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        out = capsys.readouterr().out
        assert '[WARN]' in out and 'no projects owned' in out
        assert w.owned == []

    def test_no_warning_when_project_owned(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        queue = tmp_path / 'queue'
        make_project(queue, 'mine', session='testsess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        out = capsys.readouterr().out
        assert 'no projects owned' not in out
        assert [p.name for p in w.owned] == ['mine']

    def test_zero_owned_warning_reaches_dispatcher_pane(self, tmp_path: Path) -> None:
        """PR #28 cross-review blocking 3: print だけでなく notify_dispatcher() で届く."""
        queue = tmp_path / 'queue'
        make_project(queue, 'theirs', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert any('no projects owned' in m for _, m in w.tmux.sent)

    def test_no_dispatcher_notify_when_project_owned(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        make_project(queue, 'mine', session='testsess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.tmux.sent == []


class TestOwnershipChangeSeed:
    """SQUAD-210: 担当変更で新規 owned になった project の既存 report を再通知しない."""

    @staticmethod
    def _report_notifications(sent: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """report-bridge によるメッセージだけを抽出する (0件警告 nudge を除外)."""
        return [s for s in sent if '確認してください' in s[1]]

    def test_preexisting_report_not_renotified_after_ownership_gained(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()  # 初回計算: pj は othersess 担当なのでまだ owned でない
        assert w.owned == []

        (d / '.squad_session').write_text('testsess\n')  # 担当変更
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['pj']
        w.report_bridge()
        assert self._report_notifications(w.tmux.sent) == []  # 担当変更前から存在していた report は再通知されない

    def test_report_created_in_toctou_window_is_still_notified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PR #28 cross-review blocking 1/2: 担当検出〜find_reports() 実行の間に作られた report も飲み込まれない.

        cutoff は refresh_owned_projects() の冒頭 (marker 読み取り前) で取る。
        marker 読み取り〜find_reports() 実行の間に report が新規作成される窓を、
        watchd.find_reports() を差し替えて「呼ばれた瞬間に report を書いてから
        本来の find_reports() を実行する」形で直接エミュレートする。
        """
        import watchd as watchd_module

        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.owned == []

        (d / '.squad_session').write_text('testsess\n')  # 担当変更 (cutoff より前)

        real_find_reports = watchd_module.find_reports

        def _find_reports_after_toctou_write(dirs: object) -> list[tuple[str, str]]:
            # marker は既に読み終わり、cutoff も確定した後 (= find_reports() 実行直前) に
            # report が新規作成される瞬間を模す。
            (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
            return real_find_reports(dirs)

        monkeypatch.setattr(watchd_module, 'find_reports', _find_reports_after_toctou_write)
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['pj']
        monkeypatch.undo()  # report_bridge() 側は本来の find_reports に戻す (無関係な差し替えを残さない)

        w.report_bridge()
        assert len(self._report_notifications(w.tmux.sent)) == 1  # 飲み込まれず通知される

    def test_report_written_after_ownership_change_is_notified(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.owned == []

        (d / '.squad_session').write_text('testsess\n')
        w.refresh_owned_projects()
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')  # 担当変更後に新規作成
        w.report_bridge()
        assert len(self._report_notifications(w.tmux.sent)) == 1  # 新規 report は通常どおり通知される

    def test_default_owner_fallback_ownership_gain_also_seeds(self, tmp_path: Path) -> None:
        """マーカー無し (default_owner フォールバック) で owned になった場合も同様に効く."""
        queue = tmp_path / 'queue'
        d = queue / 'projects' / 'pj'
        (d / 'reports').mkdir(parents=True)
        (d / 'tasks').mkdir(parents=True)
        (d / '.squad_session').write_text('othersess\n')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.owned == []

        (d / '.squad_session').unlink()  # マーカー削除 -> default_owner (testsess) が担当
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['pj']
        w.report_bridge()
        assert self._report_notifications(w.tmux.sent) == []


class TestLedgerPrepare:
    def test_prepare_ledger_migrates_legacy_file(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        queue.mkdir()
        (queue / '.report_ledger').write_text('100.5\t0\t/some/report.yaml\n')
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.prepare_ledger()
        assert not w.ledger.claim('/some/report.yaml', '100.5').ok

    def test_prepare_ledger_migrates_legacy_text_at_custom_path(self, tmp_path: Path) -> None:
        """WATCH_LEDGER_FILE が旧テキスト ledger を指していても sqlite3 化される (F1)."""
        queue = tmp_path / 'queue'
        queue.mkdir()
        custom = tmp_path / 'custom-ledger'
        custom.write_text('100.5\t0\t/some/report.yaml\n')
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue, ledger_file=str(custom))
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.prepare_ledger()
        assert not w.ledger.claim('/some/report.yaml', '100.5').ok  # 既知として抑止
        c = w.ledger.claim('/some/report.yaml', '200.0')  # 新しい版は token 付きで claim できる
        assert c.ok
        assert c.token

    def test_prepare_ledger_keeps_existing_sqlite_db(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        queue.mkdir()
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.ledger.claim('/some/report.yaml', '100.5')  # DB を作って 1 件記録
        w.prepare_ledger()
        assert not w.ledger.claim('/some/report.yaml', '100.5').ok  # 記録が消えていない

    def test_prepare_ledger_falls_back_to_baseline_seed(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='testsess')
        (d / 'reports' / 'worker1_report.yaml').write_text('status: completed\n')
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.prepare_ledger()
        assert w.ledger.exists()


def test_posix_cksum_matches_coreutils() -> None:
    """POSIX cksum(1) の既知ベクタ (旧 .discovery_seen の key と一致すること)."""
    assert posix_cksum(b'') == 4294967295
    assert posix_cksum(b'a') == 1220704766
    assert posix_cksum(b'hello world') == 1135714720
    assert posix_cksum(b'/tmp/x.py|# TODO: fix this') == 2509647034


def test_pane_for_matches_legacy_layout() -> None:
    """旧 pane_for() の worker→pane マッピング (1..3 → 0.1..0.3, 4 → 0.6) を維持する."""
    from watchd import PANE_SUFFIX

    assert PANE_SUFFIX == {1: '0.1', 2: '0.2', 3: '0.3', 4: '0.6'}
