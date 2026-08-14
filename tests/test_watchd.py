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
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import BACKOFF_SECONDS  # noqa: E402
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


ID1 = '11111111-1111-4111-8111-111111111111'
ID2 = '22222222-2222-4222-8222-222222222222'


def write_report(
    d: Path, name: str = 'worker1_report.yaml', report_id: str | None = ID1, status: str = 'completed', **extra: str
) -> Path:
    """Write a schema 準拠の report (report_id=None で ID 欠落の legacy report)."""
    lines = [f'report_id: "{report_id}"'] if report_id else []
    lines += [f'status: {status}', *[f'{k}: {v}' for k, v in extra.items()]]
    p = d / 'reports' / name
    p.write_text('\n'.join(lines) + '\n')
    return p


def notifications(sent: list[tuple[str, str]], marker: str = '[REPORT ') -> list[str]:
    """report-bridge が送った本文だけ抜き出す (Enter や他の nudge を除外)."""
    return [m for _, m in sent if marker in m]


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
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(w.tmux.sent) == 2  # 本文 + Enter
        assert 'worker1_report.yaml' in w.tmux.sent[0][1]
        w.report_bridge()  # 同じサイクルをもう一度回しても再送しない
        assert len(w.tmux.sent) == 2

    def test_notification_carries_machine_readable_identity(self, watcher: tuple[Watcher, Path]) -> None:
        """Dispatcher が重複判定に使う識別子が 1 行で届く (SQUAD-215 設計)."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, task_id='SQUAD-216', git_head='deadbeef')
        w.refresh_owned_projects()
        w.report_bridge()
        msg = notifications(w.tmux.sent)[0]
        assert 'project=pj' in msg
        assert 'worker=1' in msg
        assert 'task_id=SQUAD-216' in msg
        assert f'report_id={ID1}' in msg
        assert 'content_sha256=' in msg
        assert 'git_head=deadbeef' in msg
        assert 'attempt=1' in msg
        assert '\n' not in msg  # send-keys が途中で確定しないよう 1 行に収める

    def test_missing_optional_fields_render_as_unknown(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        msg = notifications(w.tmux.sent)[0]
        assert 'task_id=unknown' in msg and 'git_head=unknown' in msg

    def test_blocked_report_gets_inbox_prefix(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, status='blocked')
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent[0][1].startswith('[INBOX]')

    def test_unowned_project_report_ignored(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='othersess')  # 別セッション担当
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent == []

    def test_default_owner_covers_unmarked_project(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        cfg = Config(session='ros-agents', default_owner='ros-agents', queue_dir=queue, interval=1)
        w = Watcher(cfg=cfg, tmux=FakeTmux('ros-agents'))
        w.sleep = lambda _s: None
        d = make_project(queue, 'pj')  # マーカー無し → default owner が担当
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(w.tmux.sent) == 2

    def test_same_id_in_two_projects_both_notified(self, watcher: tuple[Watcher, Path]) -> None:
        """配達キーは (project, report_id)。project が違えば独立して通知される."""
        w, queue = watcher
        for name in ('pj_a', 'pj_b'):
            write_report(make_project(queue, name, session='testsess'))
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 2

    def test_reissued_report_with_new_id_is_notified_again(self, watcher: tuple[Watcher, Path]) -> None:
        """同じ path でも新しい報告 (新 ID) なら改めて通知される."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        write_report(d, report_id=ID2)
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 2

    def test_moved_report_with_same_id_is_not_renotified(self, watcher: tuple[Watcher, Path]) -> None:
        """Archive から戻す/コピーしても (mtime が変わっても) 同じ ID なら再通知しない."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        p = write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        p.rename(d / 'reports' / 'worker2_report.yaml')  # path も mtime 順序も変わる
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1

    def test_failed_send_is_retried_after_backoff(
        self, watcher: tuple[Watcher, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w.tmux.fail_send = True
        w.refresh_owned_projects()
        w.report_bridge()
        assert w.tmux.sent == []

        w.tmux.fail_send = False
        w.report_bridge()
        assert w.tmux.sent == []  # backoff 中はすぐには再送しない

        later = time.time() + BACKOFF_SECONDS[0] + 1
        monkeypatch.setattr(time, 'time', lambda: later)
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1
        assert 'attempt=2' in notifications(w.tmux.sent)[0]  # 再送だと Dispatcher に分かる

    def test_send_failure_never_gives_up(self, watcher: tuple[Watcher, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """試行回数の上限は無い。長時間の障害後でも 30 分 cap で鳴り続ける."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w.refresh_owned_projects()
        w.tmux.fail_send = True
        now = time.time()
        for i in range(8):
            monkeypatch.setattr(time, 'time', lambda now=now: now)
            w.report_bridge()
            now += BACKOFF_SECONDS[min(i, len(BACKOFF_SECONDS) - 1)] + 1
        w.tmux.fail_send = False
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1  # 諦めずに届く

    def test_ownership_switch_does_not_renotify_delivered(self, tmp_path: Path) -> None:
        """A(testsess) が配達済みにした report を、担当が B(othersess) へ移っても再通知しない."""
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)

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

    def test_two_watchers_same_project_deliver_once(self, tmp_path: Path) -> None:
        """同じ project を 2 watcher が同時に走査しても通知は 1 回 (lease による排他)."""
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj')
        write_report(d)
        ws = []
        for sess in ('a', 'b'):
            cfg = Config(session=sess, default_owner=sess, queue_dir=queue, interval=1)
            w = Watcher(cfg=cfg, tmux=FakeTmux(sess))
            w.sleep = lambda _s: None
            w.refresh_owned_projects()
            ws.append(w)
        for w in ws:
            w.report_bridge()
        assert sum(len(notifications(w.tmux.sent)) for w in ws) == 1

    def test_report_renotified_once_after_ledger_loss(self, watcher: tuple[Watcher, Path]) -> None:
        """DB を失っても沈黙しない (重複側に倒す)."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w.refresh_owned_projects()
        w.report_bridge()
        w.ledger.path.unlink()
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 2
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 2  # 再構築後は再び 1 回だけ


class TestInvalidReport:
    """report_id が無い / 壊れた report を握り潰さない (fail-open)."""

    def test_missing_report_id_is_notified_as_invalid(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, report_id=None)
        w.refresh_owned_projects()
        w.report_bridge()
        msgs = notifications(w.tmux.sent, '[REPORT-INVALID')
        assert len(msgs) == 1
        assert 'report_id 欠落' in msgs[0]
        assert 'content_sha256=' in msgs[0]
        assert 'worker1_report.yaml' in msgs[0]

    def test_non_uuid_report_id_is_notified_as_invalid(self, watcher: tuple[Watcher, Path]) -> None:
        """'TBD' 等を通すと、その値の report が以後すべて握り潰される."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, report_id='TBD')
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(notifications(w.tmux.sent, '[REPORT-INVALID')) == 1

    def test_unparsable_report_is_notified_as_invalid(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        (d / 'reports' / 'worker1_report.yaml').write_bytes(b'\xff\xfe not utf-8')
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(notifications(w.tmux.sent, '[REPORT-INVALID')) == 1

    def test_invalid_report_not_repeated_while_unchanged(self, watcher: tuple[Watcher, Path]) -> None:
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, report_id=None)
        w.refresh_owned_projects()
        w.report_bridge()
        w.report_bridge()
        assert len(notifications(w.tmux.sent, '[REPORT-INVALID')) == 1

    def test_fixed_report_is_notified_normally(self, watcher: tuple[Watcher, Path]) -> None:
        """Schema 準拠で再出力させたら、今度はちゃんと通知される (直しても黙らない)."""
        w, queue = watcher
        d = make_project(queue, 'pj', session='testsess')
        write_report(d, report_id=None)
        w.refresh_owned_projects()
        w.report_bridge()
        write_report(d)  # report_id を付けて再出力
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1


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


class TestOwnershipChange:
    """SQUAD-216: 担当変更時の seed は全廃した (握り潰す窓を作らない)."""

    def test_seed_helpers_are_gone(self) -> None:
        import watchd as watchd_module

        assert not hasattr(Watcher, '_seed_newly_owned')
        assert not hasattr(watchd_module, '_now_mtime_str')

    def test_preexisting_report_is_notified_once_after_ownership_gained(self, tmp_path: Path) -> None:
        """担当変更前から存在した report は 1 回だけ通知される (永久沈黙より重複を選ぶ)."""
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        write_report(d)
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()  # 初回計算: pj は othersess 担当なのでまだ owned でない
        assert w.owned == []

        (d / '.squad_session').write_text('testsess\n')  # 担当変更
        w.refresh_owned_projects()
        assert [p.name for p in w.owned] == ['pj']
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1  # 2 回目以降は ledger が抑止する

    def test_report_arriving_between_marker_change_and_refresh_is_not_swallowed(self, tmp_path: Path) -> None:
        """PR #28 blocking 1/2 の回帰: 担当変更〜watcher が気付くまでに届いた report を握り潰さない.

        旧実装は refresh_owned_projects() の冒頭で cutoff を取り、それより古い mtime の
        report を「担当変更前から在ったもの」とみなして配達済みへ seed していた。この窓に
        書かれた report は mtime が cutoff より前になるため永久に通知されなかった。
        seed 自体を廃したので、この順序でも通常どおり通知される。
        """
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        cfg = Config(session='testsess', default_owner='none', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.owned == []

        (d / '.squad_session').write_text('testsess\n')  # 1. 担当が移る
        write_report(d)  # 2. watcher が気付く前に worker が report を書く
        w.refresh_owned_projects()  # 3. watcher がここで初めて担当を検出する
        assert [p.name for p in w.owned] == ['pj']

        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1  # 飲み込まれず通知される

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
        write_report(d)  # 担当変更後に新規作成
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1

    def test_default_owner_fallback_ownership_gain_notifies_once(self, tmp_path: Path) -> None:
        """マーカー無し (default_owner フォールバック) で owned になった場合も同じ."""
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='othersess')
        write_report(d)
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        w.warn_missing_markers()
        assert w.owned == []

        (d / '.squad_session').unlink()  # マーカー削除 -> default_owner (testsess) が担当
        w.refresh_owned_projects()
        w.report_bridge()
        assert len(notifications(w.tmux.sent)) == 1


class TestLedgerPrepare:
    def _watcher(self, queue: Path) -> Watcher:
        cfg = Config(session='testsess', default_owner='testsess', queue_dir=queue)
        w = Watcher(cfg=cfg, tmux=FakeTmux('testsess'))
        w.sleep = lambda _s: None
        return w

    def test_legacy_text_ledger_migrates_to_empty_table(self, tmp_path: Path) -> None:
        """旧行は delivered へ推測変換しない (変換すると未配達 report を沈黙させ得る)."""
        queue = tmp_path / 'queue'
        queue.mkdir()
        (queue / '.report_ledger').write_text('100.5\t0\t/some/report.yaml\n')
        w = self._watcher(queue)
        w.prepare_ledger()
        assert w.ledger.is_sqlite()
        assert w.ledger.claim('pj', ID1, '/some/report.yaml', 'a' * 64).ok

    def test_migration_is_announced_to_dispatcher(self, tmp_path: Path) -> None:
        """移行で再通知が起きることを Dispatcher が「異常」と誤解しないよう伝える."""
        queue = tmp_path / 'queue'
        queue.mkdir()
        (queue / '.report_ledger').write_text('100.5\t0\t/some/report.yaml\n')
        w = self._watcher(queue)
        w.prepare_ledger()
        assert any('[LEDGER]' in m for _, m in w.tmux.sent)

    def test_legacy_sqlite_schema_migrates_to_empty_table(self, tmp_path: Path) -> None:
        import sqlite3

        queue = tmp_path / 'queue'
        queue.mkdir()
        conn = sqlite3.connect(queue / '.report_ledger.db')
        conn.execute('CREATE TABLE reports (path TEXT PRIMARY KEY, mtime TEXT NOT NULL, lease TEXT NOT NULL)')
        conn.commit()
        conn.close()
        w = self._watcher(queue)
        w.prepare_ledger()
        assert any('[LEDGER]' in m for _, m in w.tmux.sent)
        assert w.ledger.claim('pj', ID1, '/some/report.yaml', 'a' * 64).ok

    def test_prepare_ledger_keeps_existing_new_schema_db(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        queue.mkdir()
        w = self._watcher(queue)
        c = w.ledger.claim('pj', ID1, '/some/report.yaml', 'a' * 64)
        w.ledger.commit('pj', ID1, c.token)
        w.prepare_ledger()
        assert not w.ledger.claim('pj', ID1, '/some/report.yaml', 'a' * 64).ok  # 記録が消えていない
        assert w.tmux.sent == []  # 移行していないので通知もしない

    def test_prepare_ledger_seeds_existing_reports_on_first_install(self, tmp_path: Path) -> None:
        queue = tmp_path / 'queue'
        d = make_project(queue, 'pj', session='testsess')
        write_report(d)
        w = self._watcher(queue)
        w.prepare_ledger()
        assert w.ledger.exists()
        w.refresh_owned_projects()
        w.report_bridge()
        assert notifications(w.tmux.sent) == []  # 導入時に過去 report を一斉通知しない


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
