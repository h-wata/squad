#!/usr/bin/env python3
# ruff: noqa: CPY001
"""watchd — tmux マルチエージェント worker 監視デーモン (stdlib only).

旧 watch.sh (bash) の全面移植 (Issue #26)。役割は同じ:
  1. report-bridge: worker が reports/*.yaml を書いたら Dispatcher へ自動通知。
     Codex が send-keys を忘れて/止まっても、report を書きさえすれば Dispatcher に届く。
  2. 承認オートアンサー: 残存する承認/権限プロンプトを自動受理 (bypass の保険)。
  3. 停止検知: タスク未報告かつ pane 無変化が続いたら Dispatcher へ通報。
  4. discovery / sweep: Issue/PR/CI/TODO を低頻度で発見して triage inbox + Dispatcher nudge。
  5. worktree GC: merged かつ clean な専用 worktree だけ掛除。

起動: start.sh が nohup で `watch.sh` (このスクリプトの薄いラッパ) を叩く。手動: ./watch.sh &
設定 (env): WATCH_INTERVAL(s) / WATCH_STALL_CYCLES / WATCH_STALL_RESUME_CYCLES / WATCH_BOOT_DELAY(s)
            WATCH_DISCOVERY_INTERVAL / WATCH_DISCOVERY_MAX / WATCH_SWEEP_INTERVAL / WATCH_GC_INTERVAL
            WATCH_QUEUE_DIR / WATCH_LEDGER_FILE / WATCH_LEDGER_LEASE / WATCH_WORKTREE_GLOB
            SQUAD_SESSION / SQUAD_DEFAULT_OWNER
            WATCH_NOTIFY_QUEUE (既定 0 = 従来どおり Pane 0 直送。1 で durable queue 経路)
            WATCH_CRITICAL_FALLBACK_SECONDS / WATCH_NORMAL_FALLBACK_SECONDS / WATCH_LOW_FALLBACK_SECONDS

複数セッション並行運用 (SQUAD_SESSION を変えて start.sh を複数起動する場合):
  queue/projects/<pj>/.squad_session に担当セッション名を1行書くと、その project の
  report-bridge / 停止検知 / discovery はそのセッションの watcher だけが行う。
  マーカーが無い project は SQUAD_DEFAULT_OWNER (既定 ros-agents) の担当。
  report の「通知済み」状態は queue/.report_ledger.db (sqlite3, 全 watcher 共有・永続) で
  (project, report_id) 単位に管理する。担当セッションが移っても通知済み判定はそのまま
  引き継がれ、担当変更時に既存 report を配達済みへ seed する処理は持たない (SQUAD-216)。
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
import glob as globmod
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import Claim  # noqa: E402
from ledger import delivery_key  # noqa: E402
from ledger import find_reports  # noqa: E402
from ledger import parse_scalars  # noqa: E402
from ledger import report_identity  # noqa: E402
from ledger import ReportLedger  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import QueueUnreadableError  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent

# 承認 / 権限プロンプト判定 (Claude permission / Codex approval / trust prompt)
APPROVAL_RE = re.compile(
    r'Do you want to proceed|Allow this|Approve|approve|\(y/n\)|press y|1\. Yes|'
    r'Yes, (and )?(proceed|allow|continue)|Trust (this|the)|allow command|Run command\?|Grant',
    re.IGNORECASE,
)
YN_RE = re.compile(r'\(y/n\)|press y', re.IGNORECASE)
TODO_RE = re.compile(r'TODO|FIXME|XXX')
WORKER_NUM_RE = re.compile(r'worker(\d+)')
PANE_SUFFIX = {1: '0.1', 2: '0.2', 3: '0.3', 4: '0.6'}
CAPTURE_TAIL_LINES = 40
HOOK_FRESH_SECONDS = 300


def _cksum_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7 if c & 0x80000000 else c << 1) & 0xFFFFFFFF
        table.append(c)
    return table


_CKSUM_TABLE = _cksum_table()


def posix_cksum(data: bytes) -> int:
    """POSIX cksum(1) と同じ CRC 値を返す.

    discovery の TODO dedup key は旧 watch.sh が `cksum` で書いた .discovery_seen を
    跨いで永続する。zlib.crc32 は別 variant で値が一致しないため自前で持つ。
    """
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CKSUM_TABLE[(crc >> 24) ^ b]
    n = len(data)
    while n:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CKSUM_TABLE[(crc >> 24) ^ (n & 0xFF)]
        n >>= 8
    return ~crc & 0xFFFFFFFF


def mtime_str(path: os.PathLike[str] | str) -> str:
    """Stat の mtime を「秒.ナノ秒(9桁)」文字列で返す (float 化による桁落ちを避ける).

    停止検知 (task YAML より report が新しいか) 専用。report の配達判定には使わない
    (配達の同一性は report_id、SQUAD-216)。
    """
    ns = os.stat(path).st_mtime_ns
    return f'{ns // 10**9}.{ns % 10**9:09d}'


def mtime_gt(a: str, b: str) -> bool:
    """Mtime 文字列 a が b より真に新しいか (停止検知専用).

    整数部は数値で、小数部はゼロ詰めした固定長文字列の辞書順で比較する
    (20 桁 mtime を float にすると下位桁が落ちて誤判定するため)。
    """
    if not b:
        return True
    ia, _, fa = a.partition('.')
    ib, _, fb = b.partition('.')
    try:
        if int(ia) != int(ib):
            return int(ia) > int(ib)
    except ValueError:
        return a > b
    width = max(len(fa), len(fb))
    return fa.ljust(width, '0') > fb.ljust(width, '0')


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '') or default)
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    v = os.environ.get(name, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on')


@dataclass
class Config:
    """env から解決した watcher 設定."""

    session: str = field(default_factory=lambda: os.environ.get('SQUAD_SESSION') or 'ros-agents')
    default_owner: str = field(default_factory=lambda: os.environ.get('SQUAD_DEFAULT_OWNER') or 'ros-agents')
    queue_dir: Path = field(default_factory=lambda: Path(os.environ.get('WATCH_QUEUE_DIR') or REPO_ROOT / 'queue'))
    interval: int = field(default_factory=lambda: _int_env('WATCH_INTERVAL', 15))
    stall_cycles: int = field(default_factory=lambda: _int_env('WATCH_STALL_CYCLES', 4))
    stall_resume_cycles: int = field(default_factory=lambda: _int_env('WATCH_STALL_RESUME_CYCLES', 2))
    boot_delay: int = field(default_factory=lambda: _int_env('WATCH_BOOT_DELAY', 12))
    discovery_interval: int = field(default_factory=lambda: _int_env('WATCH_DISCOVERY_INTERVAL', 900))
    discovery_max: int = field(default_factory=lambda: _int_env('WATCH_DISCOVERY_MAX', 10))
    sweep_interval: int = field(default_factory=lambda: _int_env('WATCH_SWEEP_INTERVAL', 14400))
    gc_interval: int = field(default_factory=lambda: _int_env('WATCH_GC_INTERVAL', 1800))
    lease_seconds: int = field(default_factory=lambda: _int_env('WATCH_LEDGER_LEASE', 60))
    ledger_file: str = field(default_factory=lambda: os.environ.get('WATCH_LEDGER_FILE', ''))
    worktree_glob: str = field(
        default_factory=lambda: os.environ.get('WATCH_WORKTREE_GLOB') or str(REPO_ROOT.parent / '*-wt-*')
    )
    critical_fallback_seconds: int = field(default_factory=lambda: _int_env('WATCH_CRITICAL_FALLBACK_SECONDS', 300))
    normal_fallback_seconds: int = field(default_factory=lambda: _int_env('WATCH_NORMAL_FALLBACK_SECONDS', 900))
    low_fallback_seconds: int = field(default_factory=lambda: _int_env('WATCH_LOW_FALLBACK_SECONDS', 3600))
    # 段階導入 flag (SQUAD-226)。既定は従来動作 = Pane 0 へ直送。WATCH_NOTIFY_QUEUE=1 で
    # durable queue 経路を opt-in する。flag を外せば (既定に戻せば) 即座に従来動作へ rollback。
    notify_queue_enabled: bool = field(default_factory=lambda: _bool_env('WATCH_NOTIFY_QUEUE', False))

    @property
    def dispatcher(self) -> str:
        return f'{self.session}:0.0'

    @property
    def notify_dir(self) -> Path:
        """通知 queue/ack/health の置き場所 (session で namespace する)."""
        return notify_dir_for(self.queue_dir, self.session)

    @property
    def projects_dir(self) -> Path:
        return self.queue_dir / 'projects'

    @property
    def ledger_path(self) -> Path:
        """sqlite3 ledger の場所 (全セッション共有。セッションごとに分けてはいけない)."""
        return Path(self.ledger_file) if self.ledger_file else self.queue_dir / '.report_ledger.db'

    @property
    def legacy_ledger_path(self) -> Path:
        """旧タブ区切り ledger (移行元)."""
        return self.queue_dir / '.report_ledger'

    @property
    def seen_file(self) -> Path:
        if self.session == self.default_owner:
            return self.queue_dir / '.discovery_seen'
        return self.queue_dir / f'.discovery_seen.{self.session}'

    @property
    def inbox_file(self) -> Path:
        if self.session == self.default_owner:
            return self.queue_dir / '_inbox.md'
        return self.queue_dir / f'_inbox.{self.session}.md'


class Tmux:
    """tmux 呼び出しの薄いラッパ (テストではこのクラスを差し替える)."""

    def __init__(self, session: str) -> None:
        self.session = session

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['tmux', *args], capture_output=True, text=True, check=False)

    def has_session(self) -> bool:
        return self._run(['has-session', '-t', self.session]).returncode == 0

    def pane_exists(self, pane: str) -> bool:
        r = self._run(['list-panes', '-t', self.session, '-F', '#{session_name}:#{window_index}.#{pane_index}'])
        if r.returncode != 0:
            return False
        return pane in r.stdout.split()

    def capture(self, pane: str, lines: int = CAPTURE_TAIL_LINES) -> str:
        r = self._run(['capture-pane', '-p', '-t', pane])
        if r.returncode != 0:
            return ''
        return '\n'.join(r.stdout.splitlines()[-lines:])

    def send_keys(self, pane: str, keys: str) -> bool:
        return self._run(['send-keys', '-t', pane, keys]).returncode == 0


class Watcher:
    """監視ループ本体."""

    def __init__(self, cfg: Config | None = None, tmux: Tmux | None = None) -> None:
        self.cfg = cfg or Config()
        self.tmux = tmux or Tmux(self.cfg.session)
        self.ledger = ReportLedger(self.cfg.ledger_path, lease_seconds=self.cfg.lease_seconds)
        self.nq = NotificationQueue(self.cfg.session, self.cfg.notify_dir)
        self.owned: list[Path] = []
        self.pane_hash: dict[int, str] = {}
        self.pane_stall: dict[int, int] = {}
        self.stall_notified: dict[int, str] = {}
        self.resume_count: dict[int, int] = {}
        self.last_discovery = 0.0
        self.last_sweep = 0.0
        self.last_gc = 0.0
        self._queue_write_ok = True

    # ---- ログ / 通知 ----

    def log(self, msg: str) -> None:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def notify_dispatcher(self, msg: str) -> bool:
        """Dispatcher pane へ 1 メッセージ送る.

        本文と Enter を 1 回の send-keys にまとめない: tmux 側で Enter が届かず次の
        メッセージと連結される事象があるため、間に sleep を挟んで別々に送る。
        """
        pane = self.cfg.dispatcher
        if not self.tmux.send_keys(pane, msg):
            return False
        self.sleep(0.5)
        if not self.tmux.send_keys(pane, 'Enter'):
            return False
        self.sleep(0.3)
        return True

    def _enqueue_notification(
        self, *, project: str, source: str, priority: str, message: str, dedupe_key: str
    ) -> bool:
        """通知を session-local durable queue へ積む (flag off なら従来どおり Pane 0 へ直送).

        書込みに失敗 (disk full 等)、または既存 queue が破損して読めない場合は False を
        返す。呼び出し元は「未配達」として扱い、ledger の backoff / 次サイクルで再試行する。
        破損 queue を空と見なして上書きすると未 ack 通知が永久に消えるため、この経路では
        events.json に一切書かない (SQUAD-226 critical 1)。
        """
        if not self.cfg.notify_queue_enabled:
            return self.notify_dispatcher(message)
        try:
            self.nq.enqueue(project=project, source=source, priority=priority, message=message, dedupe_key=dedupe_key)
            return True
        except QueueUnreadableError as e:
            self.log(f'[WARN] 通知 queue が破損して読めないため enqueue を中止 (既存ファイルは保持): {e}')
            self._queue_write_ok = False
            return False
        except OSError as e:
            self.log(f'[WARN] 通知 queue への書込みに失敗: {e}')
            self._queue_write_ok = False
            return False

    def _process_queue_fallbacks(self, now: float) -> None:
        """未 ack event が priority 別閾値を超えたら、Pane 0 へ 1 行だけ fallback する.

        本文そのものではなく件数だけを送る (queue を読みに行かせるための安全弁)。
        以後は指数 backoff で再送する。これ以外の経路では Pane 0 へ何も直接書かない。
        critical だけでなく normal / low も age で昇格するため、Dispatcher が pull を
        忘れても永久に沈黙しない (SQUAD-226 critical 2)。
        queue 自体が読めないときは、件数の代わりに「queue 異常」を直送する
        (未 ack が見えない = 最も危険なので必ず鳴らす、SQUAD-226 critical 1)。
        """
        thresholds = {
            'critical': self.cfg.critical_fallback_seconds,
            'normal': self.cfg.normal_fallback_seconds,
            'low': self.cfg.low_fallback_seconds,
        }
        try:
            due = self.nq.due_fallback(int(now), thresholds)
        except QueueUnreadableError as e:
            self._queue_write_ok = False
            self._send_queue_alert(
                now,
                f'[QUEUE-ERROR] 通知 queue を読めません ({e})。未 ack 通知が見えない状態です。'
                f'queue/notifications/{self.cfg.session}/events.json を退避・復旧し、'
                f'各 project の reports/ を直接確認してください。',
            )
            return
        if not due:
            return
        self._send_queue_alert(
            now,
            f'[QUEUE] 未確認通知 {due["count"]}件 (最上位 {due["priority"]}, '
            f'最古 約{int(due["oldest_age"])}s 未 ack)。'
            f'queue/notifications/{self.cfg.session}/ を確認してください (squad notify pull)。',
        )

    def _send_queue_alert(self, now: float, msg: str) -> None:
        """Fallback / queue 異常の 1 行を Pane 0 へ送り、backoff を進める.

        Backoff は fallback.json (events とは別ファイル) だけで判定するので、queue 本体が
        破損していても指数 backoff は効く (毎サイクル鳴り続けない)。
        """
        if not self.nq.fallback_due(int(now)):
            return
        if self.notify_dispatcher(msg):
            self.nq.mark_fallback_sent(int(now))
        else:
            self.log('[WARN] queue fallback の送信に失敗 (次サイクルで再試行)')

    # ---- project 担当 ----

    def warn_missing_markers(self) -> None:
        """マーカー未設定 project の可視化 + 担当 project 0 件の可視化 (起動時 1 回のみ)."""
        for d in sorted(self.cfg.projects_dir.glob('*/')):
            if not (d / '.squad_session').is_file():
                self.log(
                    f'[WARN] project {d.name} has no .squad_session marker; '
                    f'falling back to default owner {self.cfg.default_owner}'
                )
        self.refresh_owned_projects()
        if not self.owned:
            msg = (
                f"[WARN] no projects owned by session '{self.cfg.session}'; watcher will idle. "
                'Check queue/projects/*/.squad_session markers or SQUAD_DEFAULT_OWNER'
            )
            self.log(msg)
            self.notify_dispatcher(msg)

    def refresh_owned_projects(self) -> None:
        """担当 project を毎サイクル再計算する (project 追加やマーカー変更に追従).

        担当を新しく得たときに既存 report を「配達済み」として seed する処理は持たない
        (SQUAD-216)。所有権の検出と report のスナップショットを原子的に揃えることはできず、
        その隙間に書かれた report を握り潰す TOCTOU になるため。処理済み report の再通知は
        共有 ledger の (project, report_id) が抑止し、ledger に無い report は 1 回だけ
        再通知される (永久沈黙より重複を選ぶ)。
        """
        owned: list[Path] = []
        if self.cfg.projects_dir.is_dir():
            for d in sorted(p for p in self.cfg.projects_dir.iterdir() if p.is_dir()):
                marker = d / '.squad_session'
                owner = self.cfg.default_owner
                if marker.is_file():
                    try:
                        first = marker.read_text(errors='replace').splitlines()[:1]
                        owner = ''.join(first[0].split()) if first else ''
                    except OSError:
                        owner = ''
                if owner == self.cfg.session:
                    owned.append(d)
        self.owned = owned

    def newest_mtime(self, pattern: str) -> str:
        """担当 project 内で pattern にマッチするファイルの最新 mtime (無ければ '')."""
        newest = ''
        for d in self.owned:
            for p in d.glob(pattern):
                try:
                    m = mtime_str(p)
                except OSError:
                    continue
                if mtime_gt(m, newest):
                    newest = m
        return newest

    def newest_file(self, pattern: str) -> Path | None:
        """担当 project 内で pattern にマッチする最新 mtime のファイル (無ければ None).

        newest_mtime() と違い、Step 6 の task/report 突き合わせで実ファイル
        (task_id 読み取り・project 特定) が要るときに使う。
        """
        newest_p: Path | None = None
        newest_m = ''
        for d in self.owned:
            for p in d.glob(pattern):
                try:
                    m = mtime_str(p)
                except OSError:
                    continue
                if mtime_gt(m, newest_m):
                    newest_m, newest_p = m, p
        return newest_p

    # ---- 1. report-bridge ----

    def report_bridge(self) -> None:
        """新規/更新された report を Dispatcher へ橋渡しする.

        通知するかどうかは共有 ledger の (project, report_id) だけで決める (project の担当が
        移っても、既に別の watcher が通知した report は再通知されない)。ledger が pending の
        まま残している report は next_attempt_at に達したサイクルで自動的に再送される
        (claim() が backoff を判定するので、別途スケジューラは持たない)。

        status: blocked (検証ゲート 3 回 fail) は [INBOX] 付きで人間判断に回す。
        report_id が無い / YAML を読めない report は握り潰さず [REPORT-INVALID] で通知する。
        """
        for project, path in find_reports(self.owned):
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue  # 走査中に消えた/読めない。次サイクルで再評価する
            sha, meta, parse_error = report_identity(data)
            report_id, invalid = delivery_key(meta, sha, parse_error, path)
            claim = self.ledger.claim(project, report_id, path, sha)
            if not claim.ok:
                continue
            self._deliver(project, report_id, path, sha, meta, invalid, claim)

    def _deliver(
        self,
        project: str,
        report_id: str,
        path: str,
        sha: str,
        meta: dict[str, str],
        invalid: str,
        claim: Claim,
    ) -> None:
        dedupe_key = f'report:{project}:{report_id}'
        if invalid:
            self.log(f'report 検知(invalid): {path} -> queue [REPORT-INVALID] 通知 ({invalid})')
            msg = (
                f'[REPORT-INVALID project={project} path={path} content_sha256={sha} error={invalid}] '
                'report_id が無いか YAML を解釈できません。担当 worker に schema 準拠 '
                '(report_id: UUIDv4) での再出力を指示してください。'
            )
            enqueued = self._enqueue_notification(
                project=project, source='invalid', priority='critical', message=msg, dedupe_key=dedupe_key
            )
        else:
            name = Path(path).name
            wm = WORKER_NUM_RE.search(name)
            wnum = wm.group(1) if wm else '?'
            kind = 'review' if '_review.yaml' in name else 'report'
            tag = (
                f'[REPORT project={project} worker={wnum} task_id={meta.get("task_id") or "unknown"} '
                f'report_id={report_id} content_sha256={sha} git_head={meta.get("git_head") or "unknown"} '
                f'attempt={claim.attempt}]'
            )
            if meta.get('status') == 'blocked':
                self.log(f'report 検知(blocked): {path} -> queue [INBOX] 通知')
                body = (
                    f'[INBOX] Worker{wnum} が blocked: 検証ゲート未通過。'
                    f'{path} の notes/verdict を確認し、ユーザーに優先報告してください。'
                )
                priority, source = 'critical', 'blocked'
            else:
                self.log(f'report 検知: {path} -> queue 通知 (attempt={claim.attempt})')
                body = f'Worker{wnum} {kind}: {path} を確認してください。(watcher 自動橋渡し)'
                priority, source = 'normal', 'report'
            # 1 行に収める: 本文を改行で分けると tmux send-keys が途中で確定してしまう
            # (直送は fallback だけだが、queue 本文としても同じ形式を踏襲する)。
            enqueued = self._enqueue_notification(
                project=project, source=source, priority=priority, message=f'{body} {tag}', dedupe_key=dedupe_key
            )

        # queue へ積めて初めて delivered に確定する。失敗したら pending のまま backoff を進め、
        # next_attempt_at に達したサイクルで再送する。token が空 = claim は fail-open した
        # (ledger に記録が無い) ので、実態と逆のログを出さない。
        token = claim.token
        if enqueued and self.ledger.commit(project, report_id, token):
            return
        if enqueued:
            self.log(f'[WARN] queue へ積んだが ledger を更新できず: {path} (再通知の可能性あり)')
        else:
            self.log(f'[WARN] 通知 queue への書込みに失敗: {path} (attempt={claim.attempt}, backoff 後に再送)')
        wait = self.ledger.fail(project, report_id, token)
        if not wait:
            self.log(f'[WARN] ledger に再送予定を記録できませんでした: {path} (lease 期限切れ後に再 claim)')

    # ---- 2 & 3. 承認オートアンサー + 停止検知 ----

    def check_workers(self) -> None:
        for n in (1, 2, 3, 4):
            pane = f'{self.cfg.session}:{PANE_SUFFIX[n]}'
            task_path = self.newest_file(f'tasks/worker{n}.yaml')
            task_m = mtime_str(task_path) if task_path else ''
            rep_m = self.newest_mtime(f'reports/worker{n}_report.yaml')
            review_m = self.newest_mtime(f'reports/worker{n}_review.yaml')
            if mtime_gt(review_m, rep_m):
                rep_m = review_m

            pending = bool(task_m) and (not rep_m or mtime_gt(task_m, rep_m))
            project, task_id = '', ''
            if task_path is not None:
                project = task_path.parent.parent.name
                try:
                    task_id = parse_scalars(task_path.read_text(errors='replace')).get('task_id', '')
                except OSError:
                    task_id = ''
            if pending and task_id and self._archived_report_delivered(task_path, n, project, task_id):
                self.log(f'Worker{n}: task {task_id} は archive 済み report で配達済み -> stall 対象外')
                pending = False

            if not pending:
                self.pane_stall[n] = 0
                self.resume_count[n] = 0
                continue

            # pane が存在しない場合 (例: SQUAD_ENABLE_CODEX=0 で Pane 6/W4 が無い) は
            # capture が空を返し続けて停止通報ループに入るため、このサイクルはスキップする。
            if not self.tmux.pane_exists(pane):
                continue

            cap = self.tmux.capture(pane)
            if APPROVAL_RE.search(cap):
                self.log(f'Worker{n}: 承認プロンプト検知 -> 自動受理')
                self.auto_answer(pane, cap)
                self.pane_stall[n] = 0
                self.pane_hash[n] = ''
                continue

            h = str(zlib.crc32(cap.encode()))
            if self.pane_hash.get(n) == h:
                self.pane_stall[n] = self.pane_stall.get(n, 0) + 1
                self.resume_count[n] = 0
            else:
                self.pane_hash[n] = h
                self.pane_stall[n] = 0
                # 通報済みタスクのみ再開カウントを進める
                if self.stall_notified.get(n) == task_m:
                    self.resume_count[n] = self.resume_count.get(n, 0) + 1
                    if self.resume_count[n] >= self.cfg.stall_resume_cycles:
                        self.stall_notified.pop(n, None)
                        self.resume_count[n] = 0
                        self.log(f'Worker{n}: 活動再開を検知 → 再停止時の再通報を有効化')

            if self.pane_stall.get(n, 0) >= self.cfg.stall_cycles and self.stall_notified.get(n) != task_m:
                self._notify_stall(n, pane, task_m, project or 'unknown', task_id)

    def auto_answer(self, pane: str, cap: str) -> None:
        """承認プロンプトに既定(Yes)で応答する。"(y/n)" 形式は y、それ以外は Enter."""
        if YN_RE.search(cap):
            self.tmux.send_keys(pane, 'y')
            self.sleep(0.3)
        self.tmux.send_keys(pane, 'Enter')
        self.sleep(0.3)

    def _archived_report_delivered(self, task_path: Path, n: int, project: str, task_id: str) -> bool:
        """Task と同じ task_id の archive 済み report が既に配達済みか (Step 6, 誤警報抑止).

        reports/ にはもう無い (archive 済み) が tasks/ に task YAML が残っているケースを
        対象にする。ledger で delivered と確認できたときだけ pending を打ち消す。本当に
        未処理/未検知の report は従来どおり pane hash による停止判定を行う。
        """
        project_dir = task_path.parent.parent
        archive_dir = project_dir / 'reports' / 'archive'
        if not archive_dir.is_dir():
            return False
        for pattern in (f'worker{n}_report*.yaml', f'worker{n}_review*.yaml'):
            for p in sorted(archive_dir.glob(pattern)):
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                sha, meta, parse_error = report_identity(data)
                if parse_error or meta.get('task_id') != task_id:
                    continue
                report_id, invalid = delivery_key(meta, sha, parse_error, str(p))
                if invalid:
                    continue
                if self.ledger.is_delivered(project, report_id):
                    return True
        return False

    def _notify_stall(self, n: int, pane: str, task_m: str, project: str, task_id: str) -> None:
        secs = self.cfg.interval * self.cfg.stall_cycles
        hook_event = self._recent_hook_event(n)
        pane_short = pane.split(':', 1)[1]
        if hook_event:
            self.log(f'Worker{n}: stall 検知だが hook={hook_event} のため完了通報に分類')
            msg = (
                f'Worker{n} は完了 (hook={hook_event}) していますが task が pending のままです '
                f'(約{secs}s 経過)。pane {pane_short} を確認し、report を書くよう促してください。'
            )
            source = 'stall-hook'
        else:
            self.log(f'Worker{n}: 約{secs}s 停止 (タスク未報告) -> queue 通報')
            msg = (
                f'Worker{n} が約{secs}s 停止しています (タスク割当済・report 未出力)。'
                f'pane {pane_short} を確認し、必要なら再送/clear してください。'
            )
            source = 'stall'
        dedupe_key = f'stall:{project}:{n}:{task_id or task_m}'
        # queue に積めたときだけ通報済みにする (失敗時は次サイクルで再試行)
        if self._enqueue_notification(
            project=project, source=source, priority='low', message=msg, dedupe_key=dedupe_key
        ):
            self.stall_notified[n] = task_m
        else:
            self.log(f'[WARN] Worker{n} の停止通報を queue へ書き込めず (次サイクルで再試行)')

    @staticmethod
    def _recent_hook_event(n: int) -> str:
        """Squad hook の直近イベント (5 分以内なら worker は応答可能 = 「停止」ではない).

        event 種別には依存しない (Claude Code バージョン間で field 名が揺れるため、
        鮮度ベースで判定する)。
        """
        state_file = ROOT / 'state' / f'w{n}.json'
        try:
            d = json.loads(state_file.read_text())
            ev, ts = d.get('last_event', ''), d.get('last_event_at', '')
            if not ev or not ts:
                return ''
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
        except (OSError, ValueError, json.JSONDecodeError):
            return ''
        return ev if 0 <= age <= HOOK_FRESH_SECONDS else ''

    # ---- 4. Discovery ----

    def _gh_json(self, args: list[str]) -> list[dict]:
        try:
            r = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode != 0:
            return []
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    def _add_candidate(self, state: dict, key: str, source: str, pj: str, desc: str) -> None:
        if key in state['seen']:
            return
        if state['baseline']:
            state['seen'].add(key)
            state['seen_lines'].append(key)
            return  # 既存 backlog は既知化のみ (通知しない)
        if state['added'] >= self.cfg.discovery_max:
            return
        state['seen'].add(key)
        state['seen_lines'].append(key)
        stamp = datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
        state['inbox_lines'].append(f'- [ ] {stamp} [{source}] {pj}: {desc}  `{key}`')
        state['added'] += 1

    def _disc_issues(self, state: dict, pj: str, gh_repo: str, labels: str) -> None:
        args = ['issue', 'list', '-R', gh_repo, '--state', 'open']
        for lb in [x for x in labels.split(',') if x]:
            args += ['--label', lb]
        args += ['--limit', '30', '--json', 'number,title']
        for i in self._gh_json(args):
            self._add_candidate(
                state, f'{pj}:issue:{gh_repo}#{i["number"]}', 'issue', pj, f'Issue #{i["number"]}: {i["title"]}'
            )

    def _disc_pr(self, state: dict, pj: str, gh_repo: str) -> None:
        args = [
            'pr',
            'list',
            '-R',
            gh_repo,
            '--state',
            'open',
            '--limit',
            '30',
            '--json',
            'number,title,reviewDecision,isDraft',
        ]
        for i in self._gh_json(args):
            if i.get('isDraft'):
                continue
            if i.get('reviewDecision') in ('APPROVED', 'CHANGES_REQUESTED'):
                continue  # レビュー未完のみ
            self._add_candidate(
                state,
                f'{pj}:pr:{gh_repo}#{i["number"]}:review',
                'pr',
                pj,
                f'PR #{i["number"]} レビュー待ち: {i["title"]}',
            )

    def _disc_ci(self, state: dict, pj: str, gh_repo: str) -> None:
        args = [
            'run',
            'list',
            '-R',
            gh_repo,
            '--status',
            'failure',
            '--limit',
            '10',
            '--json',
            'databaseId,workflowName,headBranch',
        ]
        for i in self._gh_json(args):
            self._add_candidate(
                state,
                f'{pj}:ci:{i["databaseId"]}',
                'ci',
                pj,
                f'CI 失敗: {i["workflowName"]} ({i.get("headBranch") or ""})',
            )

    def _disc_todo(self, state: dict, pj: str, repo: str, todo_paths: str) -> None:
        for rel in [x for x in todo_paths.split(',') if x]:
            hits = 0
            base = Path(repo) / rel
            for f in sorted(base.rglob('*')) if base.is_dir() else [base]:
                if hits >= 50:
                    break
                if not f.is_file() or any(part.startswith('.') for part in f.parts):
                    continue
                try:
                    lines = f.read_text(errors='replace').splitlines()
                except OSError:
                    continue
                for no, line in enumerate(lines, 1):
                    if hits >= 50:
                        break
                    if not TODO_RE.search(line):
                        continue
                    hits += 1
                    text = line.strip()
                    # 旧 watch.sh の `cksum` と同じ key を作る (.discovery_seen は
                    # 再起動・upgrade を跨ぐ永続フォーマットなので変えてはいけない)
                    h = posix_cksum(f'{f}|{text}'.encode())
                    self._add_candidate(state, f'{pj}:todo:{h}', 'todo', pj, f'{f}:{no} {text}')

    @staticmethod
    def _read_discovery_cfg(path: Path) -> dict[str, str]:
        """discovery.yaml の top-level `key: value` だけ拾う簡易リーダー."""
        out: dict[str, str] = {}
        try:
            text = path.read_text(errors='replace')
        except OSError:
            return out
        for line in text.splitlines():
            if not line or line[0].isspace() or line.startswith('#') or ':' not in line:
                continue
            key, _, val = line.partition(':')
            out.setdefault(key.strip(), val.strip().strip('"\''))
        return out

    def run_discovery(self) -> None:
        """仕事を発見 → triage inbox → Dispatcher 自動起票 nudge.

        watcher は「発見 + dedup + inbox 積み + Dispatcher nudge」まで。task YAML 生成と
        空き worker 割当は Dispatcher (Claude) が nudge を受けて自律処理する。
        """
        cfgs = [d / 'discovery.yaml' for d in self.owned if (d / 'discovery.yaml').is_file()]
        if not cfgs:
            self.log('discovery: 設定なし (担当 project に discovery.yaml を置くと有効化)')
            return
        self.cfg.queue_dir.mkdir(parents=True, exist_ok=True)
        seen_file, inbox_file = self.cfg.seen_file, self.cfg.inbox_file
        baseline = not seen_file.exists()  # 初回は既存 backlog を黙って既知化
        seen = set(seen_file.read_text(errors='replace').split('\n')) if seen_file.exists() else set()
        seen.discard('')
        seen_file.touch()  # 候補ゼロでも「seed 済み」を記録し、次回以降は baseline 扱いにしない
        if not inbox_file.exists():
            header = '# Discovery Triage Inbox\n\n'
            header += 'watcher が発見した未処理候補。Dispatcher が起票したら [x] にする。\n\n'
            inbox_file.write_text(header)
        state: dict = {'seen': seen, 'seen_lines': [], 'inbox_lines': [], 'added': 0, 'baseline': baseline}

        for cfg_path in cfgs:
            c = self._read_discovery_cfg(cfg_path)
            if c.get('enabled') == 'false':
                continue
            pj = cfg_path.parent.name
            repo, gh_repo = c.get('repo', ''), c.get('gh_repo', '')
            sources = c.get('sources') or 'issues,pr,ci,todo'
            srcs = [s.strip() for s in sources.split(',')]
            if 'issues' in srcs and gh_repo:
                self._disc_issues(state, pj, gh_repo, c.get('issue_labels', ''))
            if 'pr' in srcs and gh_repo:
                self._disc_pr(state, pj, gh_repo)
            if 'ci' in srcs and gh_repo:
                self._disc_ci(state, pj, gh_repo)
            if 'todo' in srcs and repo and c.get('todo_paths'):
                self._disc_todo(state, pj, repo, c['todo_paths'])

        if state['seen_lines']:
            with seen_file.open('a') as fh:
                fh.write('\n'.join(state['seen_lines']) + '\n')
        if state['inbox_lines']:
            with inbox_file.open('a') as fh:
                fh.write('\n'.join(state['inbox_lines']) + '\n')

        if baseline:
            self.log('discovery: baseline 完了 (既存 backlog を既知化、通知なし)')
            return
        if state['added'] > 0:
            self.log(f'discovery: 新規候補 {state["added"]} 件 -> inbox + queue 通知')
            # 同じ dedupe_key ('discovery') が未 ack のままなら、直近件数へ debounce merge
            # される (Pane 直送は廃止)。seen の既知化は queue 書込みの成否に関わらず取り消さない
            # (候補は inbox に記録済みで、queue 書込み失敗時に失われるのは nudge だけ)。
            nudge = f'[DISCOVERY] 新規候補 {state["added"]} 件を {inbox_file} に追加。'
            nudge += '空き worker に自動起票してください (task-yaml-author → 通知)。'
            nudge += 'merge gate は人間が維持。'
            if not self._enqueue_notification(
                project='*', source='discovery', priority='low', message=nudge, dedupe_key='discovery'
            ):
                self.log('[WARN] discovery 通知の queue 書込みに失敗 (次サイクルで再試行)')
            return

        # 新規ゼロ: idle を遊ばせず、throttle 付きで「一通りレビュー(sweep)」を投げる
        now = time.time()
        if now - self.last_sweep < self.cfg.sweep_interval:
            remain = int((self.cfg.sweep_interval - (now - self.last_sweep)) / 60)
            self.log(f'discovery: 新規なし (self-archive, 次 sweep まで約 {remain} 分)')
            return
        stamp = datetime.now().astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')
        sweep_line = f'- [ ] {stamp} [sweep] all: 新規タスクなし。'
        sweep_line += f'既存コード/open PR/backlog の一通りレビュー・監査  `sweep:{int(now)}`\n'
        with inbox_file.open('a') as fh:
            fh.write(sweep_line)
        self.log('discovery: 新規なし -> [SWEEP] 周回レビューを inbox 投入')
        sweep_msg = '[SWEEP] 新規タスクなし。空き worker がいれば既存コード/open PR/backlog の'
        sweep_msg += '一通りレビュー・監査を1件だけ割り当ててください (全員稼働中なら何もしない)。'
        if not self._enqueue_notification(
            project='*', source='sweep', priority='low', message=sweep_msg, dedupe_key='sweep'
        ):
            self.log('[WARN] sweep 通知の queue 書込みに失敗 (次サイクルで再試行)')
        self.last_sweep = now

    # ---- 5. worktree GC ----

    def _git(self, cwd: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['git', '-C', cwd, *args], capture_output=True, text=True, check=False)

    def gc_worktrees(self) -> None:
        """Merge 済みかつ clean な専用 worktree だけ自動掛除する.

        dirty (未コミット変更) / 未 merge / 判定不能 (fetch 失敗) は絶対に触らない。
        """
        removed = skipped = 0
        for raw in sorted(globmod.glob(self.cfg.worktree_glob)):
            wt = raw.rstrip('/')
            if not Path(wt).is_dir():
                continue
            if self._git(wt, ['rev-parse', '--git-dir']).returncode != 0:
                continue
            listing = self._git(wt, ['worktree', 'list', '--porcelain']).stdout
            main = next((ln.split(' ', 1)[1] for ln in listing.splitlines() if ln.startswith('worktree ')), '')
            if not main or os.path.realpath(wt) == os.path.realpath(main):
                continue  # main worktree は対象外
            if self._git(wt, ['status', '--porcelain']).stdout.strip():
                skipped += 1
                self.log(f'gc skip (dirty): {wt}')
                continue
            branch = self._git(wt, ['rev-parse', '--abbrev-ref', 'HEAD']).stdout.strip()
            if self._git(main, ['fetch', '-q', 'origin', 'main']).returncode != 0:
                skipped += 1
                self.log(f'gc skip (fetch fail): {wt}')
                continue
            merged = self._git(main, ['branch', '--merged', 'origin/main', '--format', '%(refname:short)']).stdout
            if branch not in merged.split():
                skipped += 1
                self.log(f'gc skip (not merged): {wt} [{branch}]')
                continue
            if self._git(main, ['worktree', 'remove', wt]).returncode == 0:
                removed += 1
                self.log(f'gc removed (merged+clean): {wt} [{branch}]')
            else:
                skipped += 1
                self.log(f'gc skip (remove failed): {wt}')
        if removed:
            self.log(f'gc: {removed} worktree を掛除 (skip {skipped})')

    # ---- ループ ----

    def prepare_ledger(self) -> None:
        """旧 schema の ledger を新 schema へ移行する.

        旧 ledger (path + mtime) の行は report_id を復元できないため引き継がない。移行後は
        reports/ に残る report が最大 1 回だけ再通知されるので、その旨を起動時に 1 度だけ
        Dispatcher にも伝える (重複を「異常」と誤解させないため)。

        ledger が最初から無い場合も seed はしない。「新規導入だから鳴らさない」を
        「ledger が (何らかの理由で) 存在しない」と区別できず、DB 消失や再作成のたびに
        report を delivered へ登録して永久沈黙させてしまうため。導入直後の一斉通知は、
        鳴らさないことより確実に鳴ることを優先するユーザー方針のもとでは許容する。
        """
        kind = self.ledger.migrate(self.cfg.legacy_ledger_path)
        if not kind:
            return
        msg = (
            f'[LEDGER] 配達 ledger を report_id ベースの新 schema へ移行しました '
            f'({self.cfg.ledger_path}, {kind})。旧記録 (path+mtime) からは report_id を '
            '復元できないため配達済みへは変換していません。既存 report が最大 1 回だけ '
            '再通知されます。report_id が既知のものと一致する通知は重複として無視してください。'
        )
        self.log(msg)
        self.notify_dispatcher(msg)

    def cycle(self) -> None:
        """1 サイクル分の監視処理."""
        self.refresh_owned_projects()
        self.report_bridge()
        self.check_workers()

        now = time.time()
        if now - self.last_discovery >= self.cfg.discovery_interval:
            self.run_discovery()
            self.last_discovery = now
        # worktree GC は glob ベースで project 単位に絞れないため、複数セッション並行時の
        # 重複実行を避けて既定セッションの watcher だけが担当する。
        if self.cfg.session == self.cfg.default_owner and now - self.last_gc >= self.cfg.gc_interval:
            self.gc_worktrees()
            self.last_gc = now

        # age-based fallback は flag の on/off に関わらず毎サイクル回す。flag on 時に
        # enqueue 済みの未 ack event は、flag off (rollback) 後もこの経路でしか Pane 0 へ
        # 届かないため、ここを flag でガードすると rollback 後に永久に沈黙する
        # (SQUAD-228, PR #29 事後 cross-review)。新規通知の enqueue 自体は
        # _enqueue_notification() 側で引き続き flag により直送/queue を切り替える。
        # age-based fallback は flag の on/off に関わらず毎サイクル回す。flag on 時に
        # enqueue 済みの未 ack event は、flag off (rollback) 後もこの経路でしか Pane 0 へ
        # 届かないため、ここを flag でガードすると rollback 後に永久に沈黙する
        # (SQUAD-228, PR #29 事後 cross-review)。新規通知の enqueue 自体は
        # _enqueue_notification() 側で引き続き flag により直送/queue を切り替える。
        self._process_queue_fallbacks(now)
        self.nq.write_health(owned_projects=len(self.owned), write_ok=self._queue_write_ok)
        self._queue_write_ok = True

    def run(self) -> int:
        c = self.cfg
        c.queue_dir.mkdir(parents=True, exist_ok=True)
        self.log(
            f'watcher start (session={c.session} interval={c.interval}s stall={c.stall_cycles} '
            f'stall_resume={c.stall_resume_cycles} discovery={c.discovery_interval}s '
            f'sweep={c.sweep_interval}s gc={c.gc_interval}s boot_delay={c.boot_delay}s '
            f'ledger={c.ledger_path})'
        )
        self.prepare_ledger()
        self.warn_missing_markers()
        self.sleep(c.boot_delay)
        while True:
            if not self.tmux.has_session():
                self.log(f"session '{c.session}' が無いので終了")
                return 0
            self.cycle()
            self.sleep(c.interval)


def main() -> int:
    return Watcher().run()


if __name__ == '__main__':
    raise SystemExit(main())
