#!/usr/bin/env python3
# ruff: noqa: CPY001
"""watchd — tmux マルチエージェント worker 監視デーモン (stdlib only).

旧 watch.sh (bash) の全面移植 (Issue #26)。役割は同じ:
  1. report-bridge: worker が reports/*.yaml を書いたら Dispatcher へ自動通知。
     Codex が send-keys を忘れて/止まっても、report を書きさえすれば Dispatcher に届く。
  2. 承認オートアンサー: 残存する承認/権限プロンプトを自動受理 (bypass の保険)。
  3. 停止検知: タスク未報告かつ pane 無変化が続いたら Dispatcher へ通報。
  4. discovery / sweep: Issue/PR/CI/TODO を低頻度で発見して triage inbox + Dispatcher nudge。
  5. worktree GC: merged かつ clean な専用 worktree だけ削除。

起動: start.sh が nohup で `watch.sh` (このスクリプトの薄いラッパ) を叩く。手動: ./watch.sh &
設定 (env): WATCH_INTERVAL(s) / WATCH_STALL_CYCLES / WATCH_STALL_RESUME_CYCLES / WATCH_BOOT_DELAY(s)
            WATCH_DISCOVERY_INTERVAL / WATCH_DISCOVERY_MAX / WATCH_SWEEP_INTERVAL / WATCH_GC_INTERVAL
            WATCH_QUEUE_DIR / WATCH_LEDGER_FILE / WATCH_LEDGER_LEASE / WATCH_WORKTREE_GLOB
            SQUAD_SESSION / SQUAD_DEFAULT_OWNER
            WATCH_APPROVAL_DENY (自動承認しない操作の正規表現。既定は APPROVAL_DENY_DEFAULT)
            WATCH_NOTIFY_QUEUE (既定 0 = 従来どおり Dispatcher window 直送。1 で durable queue 経路)
            WATCH_CRITICAL_FALLBACK_SECONDS / WATCH_NORMAL_FALLBACK_SECONDS / WATCH_LOW_FALLBACK_SECONDS
            WATCH_TASK_EVAL (既定 1。0 で task 完了時の自動評価を止める) / WATCH_TASK_EVALS_FILE /
            WATCH_TASK_EVAL_TIMEOUT(s) / WATCH_TASK_EVAL_SINCE (再走査の起点, ISO8601)

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
import contextlib
import fcntl
import glob as globmod
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ctx  # noqa: E402
from ledger import ARCHIVE_VERIFY  # noqa: E402
from ledger import Claim  # noqa: E402
from ledger import delivery_key  # noqa: E402
from ledger import find_reports  # noqa: E402
from ledger import NEXT_KEYS  # noqa: E402
from ledger import next_rule_error  # noqa: E402
from ledger import parse_block  # noqa: E402
from ledger import parse_scalars  # noqa: E402
from ledger import report_identity  # noqa: E402
from ledger import ReportLedger  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import QueueUnreadableError  # noqa: E402
from squad import detect_status  # noqa: E402 (squad ls / squad web と同じ作業中判定, #79)
from squad import tmux_target  # noqa: E402
from squad import tmux_windows  # noqa: E402
import task_eval  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent

# 承認 / 権限プロンプト判定 (Claude permission / Codex approval / trust prompt)
APPROVAL_RE = re.compile(
    r'Do you want to proceed|Allow this|Approve|approve|\(y/n\)|press y|1\. Yes|'
    r'Yes, (and )?(proceed|allow|continue)|Trust (this|the)|allow command|Run command\?|Grant',
    re.IGNORECASE,
)
YN_RE = re.compile(r'\(y/n\)|press y', re.IGNORECASE)
# 承認プロンプトにこれが写っていたら自動受理せず Dispatcher に上げる (#54)。
# capture は末尾 40 行なので、直前に流れた出力に一致しても止まる (安全側に倒す)。
APPROVAL_DENY_DEFAULT = (
    r'\brm\s+-\w*r|git\s+push\b.*\s(--force|-f)\b|git\s+reset\s+--hard|git\s+clean\s+-\w*f|'
    r'gh\s+pr\s+(merge|close)|gh\s+repo\s+delete|gh\s+release\s+delete|\bdrop\s+(table|database)\b|'
    r'\bmkfs\b|\bdd\s+if='
)
TODO_RE = re.compile(r'TODO|FIXME|XXX')
WORKER_NUM_RE = re.compile(r'worker(\d+)')
# reports/archive/ の写しのファイル名に使えない文字 (task_id に / 等が混ざっても archive の外に書かない)
ARCHIVE_NAME_UNSAFE_RE = re.compile(r'[^\w.-]')
# verifier の verdict を attempt ごとに写す先 (queue/projects/<pj>/verdicts/, SQUAD-397)。reports/ の外に置くのは、
# graph sync が reports/ 以下の verdict を結果として数えるため (attempt 1 の fail を結果に混ぜない)
VERDICT_HISTORY_DIR = 'verdicts'
# verifier の verdict の result がとりうる値 (.claude/agents/verifier.md)。これ以外 (未記入を含む) は書きかけとみなす
VERDICT_RESULTS = frozenset({'pass', 'fail', 'inconclusive'})


def verdict_history_name(task_id: str, attempt: int, data: bytes) -> str:
    """verdicts/ の写しの名前 `<task_id>__attempt<N>__<sha256 先頭 16 桁>.yaml`.

    同じ attempt を書き直した verdict も内容が違えば別に残す。task_id は名前に使えない文字を _ にするので、
    読む側は中身の task_id で照合すること。
    """
    safe = ARCHIVE_NAME_UNSAFE_RE.sub('_', task_id)
    return f'{safe}__attempt{attempt}__{hashlib.sha256(data).hexdigest()[:16]}.yaml'


def _link_copy(dest: Path, data: bytes, mtime: float) -> None:
    """Write data to dest with mtime. dest が既にあれば何もしない. 失敗は OSError で返す.

    書きかけの写しを残さない: 一時ファイルに書いてから link で置く (link は既存を上書きしない)。
    """
    if dest.exists():
        return
    dest.parent.mkdir(exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.utime(tmp, (mtime, mtime))
        os.link(tmp, dest)
    except FileExistsError:
        pass  # 別の watcher が先に写した
    finally:
        os.unlink(tmp)


# この status の report を配達したら task YAML を tasks/archive/ へ退避する (SQUAD-355)。blocked や progress は残す
ARCHIVE_TASK_STATUSES = frozenset({'completed', 'failed'})
# 退避した task YAML への遅い書込み (退避前に開かれた fd から) を見張る秒数 (SQUAD-361 B2)
TASK_ARCHIVE_VERIFY_SECONDS = 300
WORKER_WINDOW = {1: 'w1', 2: 'w2', 3: 'w3', 4: 'w4'}
CAPTURE_TAIL_LINES = 40
HOOK_FRESH_SECONDS = 300

# タスク途中の区切り (ADR 0011 の 2. と 5.)。境界はすべて「以上」
BREAK_WORKERS = (1, 2, 3)  # Codex (w4) は /clear が無いので対象外
BREAK_CTX_FRESH_SECONDS = 600  # context_at がこれより古い ctx は不明として扱う
BREAK_QUIET_CYCLES = 2  # pane の hash がこのサイクル数続けて変わっていなければ待機中
BREAK_REPLY_TIMEOUT = 600  # BRIEF-REQUEST の返事待ち (monotonic)
BREAK_RETRY_SECONDS = 1800  # BRIEF-DEFER か返事なしの後、次に依頼するまで (monotonic)
BREAK_COOLDOWN_SECONDS = 1800  # 区切った後、次の区切りまで (wall clock, state に残す)
BREAK_LIMIT = 5  # 1 タスクで区切る回数の上限
BREAK_CLEAR_WAIT = 1.5  # /clear の後、RESUME を送るまで
BREAK_WAIT_EVENTS = ('Stop', 'idle_prompt')
# 依頼文の末尾 (空白を除いた形)。pane に写った依頼文の中の BRIEF-READY / BRIEF-DEFER を返事と読まないよう、
# これより後ろだけを返事として見る
BRIEF_REQUEST_TAIL = 'BRIEF-DEFER<理由>と返してください。'
BRIEF_REPLY_RE = re.compile(r'BRIEF-(READY|DEFER)')


def brief_request_text(task_id: str, reason: str, brief: Path) -> str:
    """BRIEF-REQUEST の文面 (ADR 0011 の 2.。instructions/worker.md と一字一句揃える)."""
    return (
        f'[BRIEF-REQUEST task_id={task_id} reason={reason}] コンテキストを区切ります。{brief} を '
        'instructions/worker.md の 4 項目で書き直し、書けたら BRIEF-READY とだけ返してください。'
        'バックグラウンド処理の完了待ちなど今は区切れないときは BRIEF-DEFER <理由> と返してください。'
    )


def resume_text(task_id: str, task_yaml: Path, brief: Path) -> str:
    """RESUME の文面 (ADR 0011 の 2.。instructions/worker.md と一字一句揃える)."""
    return (
        f'[RESUME task_id={task_id}] /clear 後の再開です。{task_yaml} と {brief} を読み、'
        'brief の「今の意図と次の一手」から続けてください。'
    )


# Dispatcher の区切り (ADR 0011 の 4.)
DISPATCHER = 'dispatcher'
WATCHD_SENT_KEEP = 20  # state/dispatcher.json の watchd_sent に残す件数 (hook が人間の入力と見分ける)
REPORT_NOTIFIED_KEEP = 200  # report_notified_at に残す件数 (最後の /clear からの配達数を数える)
WEB_INSTRUCTION_PREFIX = '[squad-web 指示]'
ASK_USER_TOOL = 'AskUserQuestion'
TRANSCRIPT_TAIL_BYTES = 256 * 1024
BRIEF_QUESTION_HEADING = '## 1. 人間への確認待ち'
BRIEF_NO_QUESTION = '- なし'


def dispatcher_brief_request_text(reason: str, session: str) -> str:
    """Dispatcher への BRIEF-REQUEST (ADR 0011 の 4.。instructions/dispatcher.md と一字一句揃える)."""
    return (
        f'[BRIEF-REQUEST dispatcher reason={reason}] コンテキストを区切ります。dashboards/{session}-brief.md を '
        '4 項目で書き直し、dashboards/<pj>.md も最新にしてから BRIEF-READY とだけ返してください。'
        '人間とのやりとりの途中など今は区切れないときは BRIEF-DEFER <理由> と返してください。'
    )


def dispatcher_resume_text(session: str) -> str:
    """Dispatcher への RESUME (ADR 0011 の 4.。instructions/dispatcher.md と一字一句揃える)."""
    return (
        f'[RESUME dispatcher] /clear 後の再開です。dashboards/{session}-brief.md を読み、'
        'instructions/dispatcher.md の「セッション開始時（状態復元）」の 0-5 を行ってから、'
        'brief の 2. と 3. を続けてください。'
    )


def brief_question_pending(path: Path) -> bool:
    """Dispatcher brief の「1. 人間への確認待ち」に「- なし」以外の行があるか.

    brief が無ければ確認待ちは無い。読めない・1. の見出しが無いときは確かめられないので、確認待ちとして
    扱う (区切らない側)。
    """
    try:
        lines = path.read_text(errors='replace').splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        start = [ln.strip() for ln in lines].index(BRIEF_QUESTION_HEADING) + 1
    except ValueError:
        return True
    for line in lines[start:]:
        text = line.strip()
        if text.startswith('## '):
            break
        if text and text != BRIEF_NO_QUESTION:
            return True
    return False


def _content_blocks(entry: dict) -> list[dict]:
    msg = entry.get('message')
    content = msg.get('content') if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def ask_user_pending(path: str) -> bool:
    """Transcript の最後の assistant メッセージが AskUserQuestion の tool_use で、tool_result がまだ無いか.

    path が無い・ファイルが無いときは答え待ちとは見なさない。読めないときは区切らない側 (True) に倒す。
    """
    if not path:
        return False
    try:
        with open(path, 'rb') as f:  # noqa: PTH123
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            f.seek(start)
            lines = f.read().splitlines()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if start > 0:
        lines = lines[1:]  # 途中から読んだ先頭行は欠けている
    asked: set[str] = set()
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        blocks = _content_blocks(entry)
        if entry.get('type') == 'assistant':
            asked = {
                str(b.get('id')) for b in blocks if b.get('type') == 'tool_use' and b.get('name') == ASK_USER_TOOL
            }
        elif entry.get('type') == 'user':
            asked -= {str(b.get('tool_use_id')) for b in blocks if b.get('type') == 'tool_result'}
    return bool(asked)


def brief_reply(cap: str) -> str:
    """Pane に出た依頼への返事 ('READY' / 'DEFER' / '')。依頼文より後ろの最後の 1 つを見る."""
    flat = ''.join(cap.split())
    pos = flat.rfind(BRIEF_REQUEST_TAIL)
    tail = flat[pos + len(BRIEF_REQUEST_TAIL) :] if pos >= 0 else flat
    found = BRIEF_REPLY_RE.findall(tail)
    return found[-1] if found else ''


def _parse_iso(value: object) -> float | None:
    """TZ 付き ISO8601 を epoch 秒に。TZ が無ければ UTC とみなす。読めなければ None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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


# web からの指示 (#85) を pane に打ち込む前に空白へ置き換える制御文字 (改行で途中確定、ESC で端末操作をさせない)
INSTRUCTION_CTRL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def _flat(text: str) -> str:
    """Pane に打ち込む文字列を 1 行にする (制御文字を空白にし、空白の並びを 1 つにまとめる)."""
    return ' '.join(INSTRUCTION_CTRL_RE.sub(' ', text).split())


# graph sync 失敗後の再試行間隔 (秒)。最後の値を上限として使い続ける
GRAPH_RETRY_BACKOFF = (5, 30, 300)
TASK_EVAL_MAX_RUNNING = 2  # 同時に走らせる評価の子プロセス数 (ledger を作り直した直後などに一斉に起動しない)
TASK_EVAL_MAX_WAITING = 20  # 起動待ちの上限。溢れた分は捨てて、次の再走査で reports/archive/ から拾い直す
TASK_EVAL_KILL_GRACE = 10  # 子が自分の時間上限で行を書けなかったとき、watchd が kill するまでの猶予 (秒)
TASK_EVAL_REAP_GRACE = 30  # kill した子がこの秒数 (monotonic) を過ぎても回収できなければ error 行を書いて見放す
TASK_EVAL_RESCAN_SECONDS = 300  # 評価漏れの再走査の間隔 (monotonic)。watchd 起動後の最初の見回りでは必ず走る
TASK_EVAL_STDERR_CHARS = 2000


@dataclass
class EvalJob:
    """評価の子プロセス 1 件 (task_eval.py)."""

    base: dict  # task_eval.base_row の値。子が行を書けなかったとき watchd が error 行に使う
    argv: list[str]
    proc: subprocess.Popen | None = None
    stderr: object = None  # 子の stderr を受ける一時ファイル
    started: float = 0.0  # monotonic
    killed_at: float | None = None  # kill した時刻 (monotonic)。回収は次の見回り以降に poll で行う


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
    # 段階導入 flag (SQUAD-226)。既定は従来動作 = Dispatcher window へ直送。WATCH_NOTIFY_QUEUE=1 で
    # durable queue 経路を opt-in する。flag を外せば (既定に戻せば) 即座に従来動作へ rollback。
    notify_queue_enabled: bool = field(default_factory=lambda: _bool_env('WATCH_NOTIFY_QUEUE', False))
    approval_deny: str = field(default_factory=lambda: os.environ.get('WATCH_APPROVAL_DENY') or APPROVAL_DENY_DEFAULT)
    # タスク途中の区切り (ADR 0011 の 2.)。どれも勘で決めた初期値
    break_ctx_pct: int = field(default_factory=lambda: _int_env('WATCH_BREAK_CTX_PCT', 50))
    break_stops: int = field(default_factory=lambda: _int_env('WATCH_BREAK_STOPS', 8))
    break_minutes: int = field(default_factory=lambda: _int_env('WATCH_BREAK_MINUTES', 60))
    # Dispatcher の区切り (ADR 0011 の 4.)。どれも勘で決めた初期値
    dispatcher_break_ctx_pct: int = field(default_factory=lambda: _int_env('WATCH_DISPATCHER_BREAK_CTX_PCT', 50))
    dispatcher_break_reports: int = field(default_factory=lambda: _int_env('WATCH_DISPATCHER_BREAK_REPORTS', 10))
    dispatcher_break_hours: int = field(default_factory=lambda: _int_env('WATCH_DISPATCHER_BREAK_HOURS', 4))
    dispatcher_human_quiet_min: int = field(default_factory=lambda: _int_env('WATCH_DISPATCHER_HUMAN_QUIET_MIN', 20))
    dashboards_dir: Path = field(default_factory=lambda: REPO_ROOT / 'dashboards')
    # task 完了時の自動評価 (Refs #130, #136)。出力先は measure_task_cost.py の task_costs.jsonl と同じ metrics/
    task_eval_enabled: bool = field(default_factory=lambda: _bool_env('WATCH_TASK_EVAL', True))
    task_evals_file: Path = field(
        default_factory=lambda: Path(
            os.environ.get('WATCH_TASK_EVALS_FILE') or REPO_ROOT / 'metrics' / 'task_evals.jsonl'
        )
    )
    task_eval_timeout: int = field(default_factory=lambda: _int_env('WATCH_TASK_EVAL_TIMEOUT', 300))
    # 再走査で拾い直す report の起点 (TZ 付き ISO8601)。空なら queue/.task_eval_since (初回に作る) を使う
    task_eval_since: str = field(default_factory=lambda: os.environ.get('WATCH_TASK_EVAL_SINCE', ''))
    # 評価の子プロセスの argv の先頭 (テストが偽の評価に差し替える)
    task_eval_cmd: list[str] = field(default_factory=lambda: [sys.executable, str(ROOT / 'task_eval.py')])

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
    def budget_notified_file(self) -> Path:
        """予算超過を通知済みの task (再起動しても重複通知しないため永続化する)."""
        return self.queue_dir / '.budget_notified'

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

    def target(self, name: str) -> str:
        """役割名 (dispatcher / w1 等) の宛先。旧構成 (window 0 に 7 pane) のセッションなら "<session>:0.<n>" (#95)."""
        return tmux_target(self.session, name, tmux_windows(self.session))

    def pane_exists(self, pane: str) -> bool:
        # 宛先は window 名 ("s:w1") と旧構成の pane 番号 ("s:0.1") の両方がありうる (#95)
        r = self._run(
            [
                'list-panes',
                '-s',
                '-t',
                f'={self.session}:',  # 末尾の : が無いと同名の window (別セッション) に当たる
                '-F',
                '#{session_name}:#{window_index}.#{pane_index} #{session_name}:#{window_name}',
            ]
        )
        if r.returncode != 0:
            return False
        return pane in r.stdout.split()

    def capture(self, pane: str, lines: int = CAPTURE_TAIL_LINES) -> str:
        r = self._run(['capture-pane', '-p', '-t', pane])
        if r.returncode != 0:
            return ''
        # 画面の下の空行を落としてから末尾を取る。/clear 直後など出力が画面の上にあると、空行だけの末尾に
        # なってスピナー行が範囲外に出て、作業中の pane を待機中と読む (#85 の real check で再現)
        return '\n'.join(r.stdout.rstrip().splitlines()[-lines:])

    def send_keys(self, pane: str, keys: str) -> bool:
        return self._run(['send-keys', '-t', pane, keys]).returncode == 0


def _state_dir() -> Path:
    """Squad hook が worker の状態を書くディレクトリ (呼び出しのたびに環境変数を見る).

    ROOT 固定にすると、テストが開発機の実 state/w*.json を読んでしまい、直近に
    hook が動いたかどうかで stall 判定テストの合否が変わる (実際に 3 件が flaky
    だった)。SQUAD_STATE_DIR があればそちらを見る。
    """
    return Path(os.environ.get('SQUAD_STATE_DIR') or (ROOT / 'state'))


def archived_task_text(task_path: Path, task_id: str) -> str | None:
    """tasks/archive/ に退避した task_id の task YAML の中身 (無ければ None, SQUAD-355).

    名前は worker{N}_<task_id>.yaml か、衝突時の worker{N}_<task_id>_<接尾辞>.yaml。名前だけでは別 task
    (T1 と T1_x) を区別できないので中身の task_id で確かめ、同じ task が複数あれば mtime の新しい方を採る。
    """
    if not task_id:
        return None
    prefix = f'{task_path.stem}_{ARCHIVE_NAME_UNSAFE_RE.sub("_", task_id)}'
    archive = task_path.parent / 'archive'
    found: list[tuple[float, str]] = []
    for p in [archive / f'{prefix}.yaml', *archive.glob(f'{globmod.escape(prefix)}_*.yaml')]:
        try:
            text = p.read_text(errors='replace')
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if parse_scalars(text).get('task_id') == task_id:
            found.append((mtime, text))
    return max(found)[1] if found else None


def resolve_next(task_path: Path, meta: dict[str, str]) -> str:
    """Report の task YAML から次手 `[NEXT ...]` を決める (#34). 付けるものが無ければ ''.

    task YAML に next_on_pass / next_on_fail が両方無ければ '' を返し、通知本文を変えない。
    tasks/ 直下に同じ task_id が無ければ tasks/archive/ の退避先を見る (SQUAD-355)。
    どちらにも無い・読めないときは推測せず `[NEXT unresolved reason=...]` にする。
    例外は投げない (遷移の解決失敗で通知を止めない)。
    """
    try:
        text = task_path.read_text(errors='replace')
    except OSError:
        text = None
    task_id = parse_scalars(text).get('task_id', '') if text is not None else ''
    if task_id != meta.get('task_id', '') or not task_id:
        # 配達時に退避済みか (SQUAD-355)、次の task で上書き済み。退避先に同じ task があればそれを使う
        archived = archived_task_text(task_path, meta.get('task_id', ''))
        if archived is not None:
            text, task_id = archived, meta.get('task_id', '')
    if text is None:
        return '[NEXT unresolved reason=task_yaml_missing]'
    if not task_id:
        return '[NEXT unresolved reason=task_yaml_unreadable]'
    if task_id != meta.get('task_id', ''):
        return '[NEXT unresolved reason=task_id_mismatch]'
    rules = {rule: parse_block(text, f'next_{rule}') for rule in ('on_pass', 'on_fail')}
    if rules['on_pass'] is None and rules['on_fail'] is None:
        return ''
    verify = meta.get('verify_status', '')
    rule = {'pass': 'on_pass', 'skipped': 'on_pass', 'fail': 'on_fail'}.get(verify)
    if rule is None:
        # report の値をそのまま載せると [NEXT] の区切りを壊せるので、語として読めるものだけ出す
        shown = verify if re.fullmatch(r'[\w-]+', verify) else ('missing' if not verify else 'other')
        return f'[NEXT unresolved reason=verify_status:{shown}]'
    block = rules[rule]
    if not block:
        return '[NEXT none]'
    if next_rule_error(block):
        return '[NEXT unresolved reason=invalid_rule]'
    # note は空白を含みうるので引用符で囲んで最後に置く
    fields = ' '.join(f'{k}="{block[k]}"' if k == 'note' else f'{k}={block[k]}' for k in NEXT_KEYS if block.get(k))
    return f'[NEXT rule={rule} {fields}]'


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
        self.approval_deny_re = re.compile(self.cfg.approval_deny, re.IGNORECASE)
        self.approval_held: dict[int, str] = {}
        self.last_discovery = 0.0
        self.last_sweep = 0.0
        # last_gc は「直近 GC 実行時刻」。0 のままだと起動直後の初サイクルで無条件に GC が
        # 走り、worktree が 20 件を超えると fetch/判定 (git 1 操作 ≈ 2s) がループを数十秒
        # ブロックする (実測: 30 件で ≈50s)。run() が boot_delay 経路で起動時刻を seed する。
        # (0 のままなので run() を通さない運用では従来どおり初サイクルで GC が走る。)
        self.last_gc = 0.0
        self._queue_write_ok = True
        self._queue_readable = True
        # report を配達したサイクルの最後に探索グラフを作り直す (#55)。起動直後に 1 回は作る
        self._graph_dirty = True
        self._archive_warned: set[str] = set()  # 退避の再試行で WARN を出し済みの task (同じ失敗を毎サイクル書かない)
        self._verdict_warned: set[str] = set()  # verdict の写しで WARN を出し済みの写し先
        # task 完了時の自動評価 (Refs #130, #136)。report_id -> 待ち / 実行中の子プロセス
        self._evals_waiting: dict[str, EvalJob] = {}
        self._evals_running: dict[str, EvalJob] = {}
        self._eval_rescan_at: float | None = None  # 次の再走査 (monotonic)。None = まだ一度も走っていない
        self._eval_overflow_warned = False
        # tasks/ 以下の task YAML の (path, mtime, ctime, size) 一覧。変わったら発注・書換・移動・削除があった (#84)
        self._tasks_sig: tuple | None = None
        # sync 失敗時は dirty を残し backoff で再試行する (一時的な失敗で永久に古いままにしない)
        self._graph_failures = 0
        self._graph_retry_at = 0.0
        # このサイクルで Dispatcher pane に何か送ったか (送った直後は待機中に見えても指示を重ねない, #85)
        self._sent_to_dispatcher = False
        self._instructions_sig: tuple[str, ...] | None = None
        # タスク途中の区切り (ADR 0011 の 2.)。check_workers() が pending の worker を残し、
        # check_context_breaks() が使う: n -> (task_path, task_m, project, task_id)
        self._pending: dict[int, tuple[Path, str, str, str]] = {}
        self._break_req: dict[int, dict] = {}  # 返事待ちの依頼 (watchd の中で完結するので state に残さない)
        self._break_retry_at: dict[int, float] = {}  # BRIEF-DEFER / 返事なしの後、次に依頼してよい monotonic 時刻
        self._worker_sent_at: dict[int, float] = {}  # watchd がこの worker に最後に送った wall clock
        # Dispatcher の区切り (ADR 0011 の 4.)
        self._disp_hash: str | None = None
        self._disp_quiet = 0  # Dispatcher pane の hash が続けて変わらなかったサイクル数
        self._disp_send_seq = 0  # watchd が Dispatcher pane に送った回数 (BRIEF-READY 後の送信を見つける)
        self._disp_req: dict | None = None  # 返事待ちの BRIEF-REQUEST
        self._disp_retry_at = float('-inf')  # BRIEF-DEFER / 返事なしの後、次に依頼してよい monotonic 時刻

    # ---- ログ / 通知 ----

    def log(self, msg: str) -> None:
        print(f'[{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wall(self) -> float:
        """Wall clock (テストで差し替える)。state に残して再起動をまたぐ時刻に使う."""
        return time.time()

    def mono(self) -> float:
        """Monotonic clock (テストで差し替える)。watchd の中で完結する待ちに使う."""
        return time.monotonic()

    def notify_dispatcher(self, msg: str, *, human_origin: bool = False) -> bool:
        """Dispatcher pane へ 1 メッセージ送る.

        本文と Enter を 1 回の send-keys にまとめない: tmux 側で Enter が届かず次の
        メッセージと連結される事象があるため、間に sleep を挟んで別々に送る。
        送る前に state/dispatcher.json の watchd_sent に残す (hook が受付時に照合する, ADR 0011 の 4.)。
        human_origin は web の指示 (deliver_instructions) だけ True にする。
        """
        pane = self.tmux.target('dispatcher')
        self._sent_to_dispatcher = True
        self._disp_send_seq += 1
        self._disp_quiet = 0  # 送った後の画面が落ち着くまで (2 サイクル) は区切りを依頼しない
        self._record_watchd_sent(msg, human_origin)
        if not self.tmux.send_keys(pane, msg):
            return False
        self.sleep(0.5)
        if not self.tmux.send_keys(pane, 'Enter'):
            return False
        self.sleep(0.3)
        return True

    def _update_dispatcher_state(self, fn: object) -> dict | None:
        """state/dispatcher.json を更新する。書けなくても送信や配達は止めない."""
        try:
            return ctx.update_state(_state_dir() / f'{DISPATCHER}.json', fn)  # type: ignore[arg-type]
        except (OSError, ValueError) as e:
            self.log(f'[WARN] state/{DISPATCHER}.json を更新できません: {e}')
            return None

    def _record_watchd_sent(self, msg: str, human_origin: bool) -> None:
        rec = {'crc32': ctx.prompt_crc32(msg), 'at': ctx.now_iso(), 'human_origin': human_origin}

        def fn(data: dict) -> None:
            prev = data.get('watchd_sent')
            data['watchd_sent'] = [*(prev if isinstance(prev, list) else []), rec][-WATCHD_SENT_KEEP:]

        self._update_dispatcher_state(fn)

    def _record_report_notified(self) -> None:
        """完了報告 ([REPORT) を Dispatcher に配達した時刻を残す (最後の /clear からの件数を数える)."""
        at = datetime.fromtimestamp(self.wall(), timezone.utc).isoformat(timespec='seconds')
        keep = max(REPORT_NOTIFIED_KEEP, self.cfg.dispatcher_break_reports)

        def fn(data: dict) -> None:
            prev = data.get('report_notified_at')
            data['report_notified_at'] = [*(prev if isinstance(prev, list) else []), at][-keep:]

        self._update_dispatcher_state(fn)

    def _enqueue_notification(
        self, *, project: str, source: str, priority: str, message: str, dedupe_key: str
    ) -> bool:
        """通知を session-local durable queue へ積む (flag off なら従来どおり Dispatcher window へ直送).

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
        """未 ack event が priority 別閾値を超えたら、Dispatcher window へ 1 行だけ fallback する.

        本文そのものではなく件数だけを送る (queue を読みに行かせるための安全弁)。
        以後は指数 backoff で再送する。これ以外の経路では Dispatcher window へ何も直接書かない。
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
            # events/ack が読めない = 未 ack の実態が一切見えない。write_health 側にも
            # 伝わらず queue_write_ok: true の健全な health が残り続けるため、
            # 「読取り健全性」をこちらのフラグで持つ (unacked_* の欠落と整合させる)。
            self._queue_readable = False
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
        """Fallback / queue 異常の 1 行を Dispatcher window へ送り、backoff を進める.

        Backoff は fallback.json (events とは別ファイル) だけで判定するので、queue 本体が
        破損していても指数 backoff は効く (毎サイクル鳴り続けない)。
        """
        if not self.nq.fallback_due(int(now)):
            return
        if self.notify_dispatcher(msg):
            try:
                self.nq.mark_fallback_sent(int(now))
            except OSError as e:
                # fallback.json への backoff 状態書込みが失敗しても、通知そのものは
                # 既に届いている。書けないと fallback_due() は毎回 True を返すため、
                # 次サイクルも再送されるだけで沈黙はしない (SQUAD-234)。
                self.log(f'[WARN] fallback backoff 状態の書込みに失敗 (次サイクルも送信される): {e}')
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
                with open(path, 'rb') as f:  # noqa: PTH123 (読んだ内容と同じ inode の mtime を取る)
                    data = f.read()
                    mtime = os.fstat(f.fileno()).st_mtime
            except OSError:
                continue  # 走査中に消えた/読めない。次サイクルで再評価する
            sha, meta, parse_error = report_identity(data)
            report_id, invalid = delivery_key(meta, sha, parse_error, path)
            claim = self.ledger.claim(project, report_id, path, sha)
            if not claim.ok:
                continue
            self._deliver(project, report_id, path, sha, meta, invalid, claim, data, mtime)
        self.reconcile_task_archives()

    def _deliver(
        self,
        project: str,
        report_id: str,
        path: str,
        sha: str,
        meta: dict[str, str],
        invalid: str,
        claim: Claim,
        data: bytes,
        mtime: float,
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
            # #34: 次手は watchd が task YAML から機械的に決める (LLM の解釈を挟まない)
            nxt = resolve_next(self.cfg.projects_dir / project / 'tasks' / f'worker{wnum}.yaml', meta)
            if nxt:
                tag = f'{tag} {nxt}'
            # 1 行に収める: 本文を改行で分けると tmux send-keys が途中で確定してしまう
            # (直送は fallback だけだが、queue 本文としても同じ形式を踏襲する)。
            enqueued = self._enqueue_notification(
                project=project, source=source, priority=priority, message=f'{body} {tag}', dedupe_key=dedupe_key
            )

        # queue へ積めて初めて delivered に確定する。失敗したら pending のまま backoff を進め、
        # next_attempt_at に達したサイクルで再送する。token が空 = claim は fail-open した
        # (ledger に記録が無い) ので、実態と逆のログを出さない。
        token = claim.token
        if enqueued and not invalid:
            self._archive_copy(path, report_id, sha, meta, data, mtime)
            self._record_report_notified()
        # 退避の予定は commit と同じトランザクションで ledger に残し、report_bridge() の最後に行う (SQUAD-361 B1)
        archive = None if invalid else self._task_archive_target(path, meta, mtime)
        if enqueued and self.ledger.commit(project, report_id, token, archive):
            self._graph_dirty = True
            if not invalid:
                self._schedule_task_eval(project, report_id, path, meta)
            return
        if enqueued:
            self.log(f'[WARN] queue へ積んだが ledger を更新できず: {path} (再通知の可能性あり)')
        else:
            self.log(f'[WARN] 通知 queue への書込みに失敗: {path} (attempt={claim.attempt}, backoff 後に再送)')
        wait = self.ledger.fail(project, report_id, token)
        if not wait:
            self.log(f'[WARN] ledger に再送予定を記録できませんでした: {path} (lease 期限切れ後に再 claim)')

    def _archive_copy(
        self, path: str, report_id: str, sha: str, meta: dict[str, str], data: bytes, mtime: float
    ) -> None:
        """配達した report の中身を reports/archive/<task_id>__<report_id>.yaml にそのまま写す (#78).

        workerN_report.yaml は次のタスクの report で上書きされるので、graph sync が結果を辿れるよう
        配達した bytes を残す。同じ名前があれば書かない (再配達で増えない)。report_id を持たない
        review は配達キーと同じく内容ハッシュで名前を決める。失敗しても配達は止めずログだけ残す。
        写しの mtime は元の report に揃える。graph sync は completed_at の無い report を mtime で新旧比較
        するので、写した時刻にすると watchd が遅れたとき古い report が再発注後の結果に見える。
        """
        task_id = ARCHIVE_NAME_UNSAFE_RE.sub('_', meta.get('task_id') or 'unknown')
        rid = f'review-{sha[:16]}' if report_id.startswith('review:') else ARCHIVE_NAME_UNSAFE_RE.sub('_', report_id)
        dest = Path(path).parent / 'archive' / f'{task_id}__{rid}.yaml'
        try:
            _link_copy(dest, data, mtime)
        except OSError as e:
            self.log(f'[WARN] report の写しを archive に残せません (配達は継続): {dest}: {e}')

    # ---- task 完了時の自動評価 (Refs #130, #136) ----

    def _schedule_task_eval(self, project: str, report_id: str, path: str, meta: dict[str, str]) -> None:
        """配達を確定した最終 report の評価を予約する. 起動は poll_task_evals() が行う.

        呼ぶのは ledger.commit() が成功した後だけ (配達を確定する前には評価しない)。評価するのは
        workerN_report.yaml で status が task_eval.FINAL_STATUSES (completed / blocked) のものだけ。
        progress などの途中の report と review は評価しない。配達・ledger 更新・通知は呼ぶ前に済んでいるので、
        ここで何が起きても配達は止めない (例外はログに残して捨てる)。待ち行列が一杯なら予約せず、
        次の再走査 (rescan_task_evals) が reports/archive/ の写しから拾い直す。
        """
        try:
            name = Path(path).name
            if '_review' in name or not name.endswith('_report.yaml'):
                return
            wm = WORKER_NUM_RE.search(name)
            fallback = f'worker{wm.group(1)}' if wm else ''
            self._enqueue_task_eval(
                project, report_id, meta, fallback, task_eval.evaluated_ids(self.cfg.task_evals_file)
            )
        except Exception as e:  # noqa: BLE001 (評価の失敗で配達を止めない)
            self.log(f'[WARN] task 評価を予約できません (配達は確定済み): {path}: {e}')

    def _enqueue_task_eval(
        self, project: str, report_id: str, meta: dict[str, str], worker_fallback: str, evaluated: set[str]
    ) -> bool:
        """評価を待ち行列に積む. 積んだら True. 対象外・評価済み・予約済み・待ち行列が一杯なら False."""
        status = meta.get('status', '')
        if not self.cfg.task_eval_enabled or status not in task_eval.FINAL_STATUSES:
            return False
        if report_id in self._evals_waiting or report_id in self._evals_running:
            return False
        if report_id in evaluated:
            return False  # 再配達・watchd の再起動後でも同じ report_id は 2 回評価しない
        if len(self._evals_waiting) >= TASK_EVAL_MAX_WAITING:
            if not self._eval_overflow_warned:
                self._eval_overflow_warned = True
                self.log(f'task 評価の待ちが {TASK_EVAL_MAX_WAITING} 件で一杯。溢れた分は次の再走査で拾い直す')
            return False
        worker = meta.get('worker') or worker_fallback
        task_id = meta.get('task_id') or 'unknown'
        agent = meta.get('agent', '')
        base = task_eval.base_row(project, task_id, report_id, worker, agent, status)
        argv = [
            *self.cfg.task_eval_cmd,
            *('--project', project, '--task-id', task_id, '--report-id', report_id),
            *('--worker', worker, '--agent', agent, '--status', status),
            *('--queue-dir', str(self.cfg.queue_dir), '--out', str(self.cfg.task_evals_file)),
            *('--timeout', str(self.cfg.task_eval_timeout)),
        ]
        self._evals_waiting[report_id] = EvalJob(base=base, argv=argv)
        return True

    @property
    def task_eval_since_file(self) -> Path:
        return self.cfg.queue_dir / '.task_eval_since'

    def _task_eval_since(self) -> float:
        """再走査で拾う report の起点 (epoch 秒). この時刻より前の report は再走査では評価しない.

        WATCH_TASK_EVAL_SINCE (ISO8601) があればそれ。無ければ queue/.task_eval_since。ファイルも無ければ
        今の時刻を書いて作る (= 自動評価付きの watchd が初めて再走査した時刻。導入前の report を一斉に評価しない)。
        全 session の watchd が同じファイルを使い、O_EXCL で作るので最初に作った値が残る。
        """
        if self.cfg.task_eval_since:
            ts = _parse_iso(self.cfg.task_eval_since)
            if ts is not None:
                return ts
            self.log(f'[WARN] WATCH_TASK_EVAL_SINCE を読めません ({self.cfg.task_eval_since!r})。既定の起点を使う')
        path = self.task_eval_since_file
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            ts = _parse_iso(path.read_text(encoding='utf-8').strip())
            if ts is None:
                raise ValueError(f'{path} を ISO8601 として読めません') from None
            return ts
        now = self.wall()
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(datetime.fromtimestamp(now).astimezone().isoformat(timespec='seconds') + '\n')
        self.log(f'task 評価の再走査の起点を記録: {path}')
        return float(int(now))

    def rescan_task_evals(self) -> None:
        """配達済みで task_evals.jsonl に行の無い最終 report を reports/archive/ から拾い直す (F1).

        待ち行列はメモリにしか無いので、watchd が再起動すると待っていた評価は消え、配達済みの report は
        再配達されない。配達した report は必ず reports/archive/<task_id>__<report_id>.yaml に写す (_archive_copy)
        ので、そこを正本として見直す。拾うのは次のすべてを満たすもの:
        写しの mtime (= 元の report の mtime) が起点 (_task_eval_since) 以降 / report_id が UUID /
        status が FINAL_STATUSES / review の書式 (verdict キー) でない / ledger で配達済み /
        task_evals.jsonl に同じ report_id の行が無い。古い順に、待ち行列の上限まで積む。
        """
        if not self.cfg.task_eval_enabled:
            return
        since = self._task_eval_since()
        evaluated = task_eval.evaluated_ids(self.cfg.task_evals_file)
        found: list[tuple[float, str, Path]] = []
        for d in self.owned:
            for p in (d / 'reports' / 'archive').glob('*.yaml'):
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= since:
                    found.append((mtime, d.name, p))
        added = 0
        for _mtime, project, p in sorted(found):
            if len(self._evals_waiting) >= TASK_EVAL_MAX_WAITING:
                break
            try:
                data = p.read_bytes()
            except OSError:
                continue
            sha, meta, err = report_identity(data)
            report_id, invalid = delivery_key(meta, sha, err, str(p))
            if invalid or 'verdict' in meta or report_id in evaluated:
                continue
            if meta.get('status', '') not in task_eval.FINAL_STATUSES:
                continue
            if not self.ledger.is_delivered(project, report_id):
                continue  # 配達を確定していない (commit に失敗した) report は評価しない
            if self._enqueue_task_eval(project, report_id, meta, '', evaluated):
                added += 1
        if added:
            self.log(f'task 評価の漏れを再走査で {added} 件拾い直した')

    def _task_eval_failed(self, report_id: str, job: EvalJob, error: str) -> None:
        """子が行を書けなかった評価を error 行で残す (同じ report_id の行があれば書かない)."""
        self.log(
            f'[WARN] task 評価に失敗 ({job.base["project"]}/{job.base["task_id"]} report_id={report_id}): {error}'
        )
        try:
            task_eval.append_row(self.cfg.task_evals_file, task_eval.error_row(job.base, error))
        except Exception as e:  # noqa: BLE001 (書けなくても watcher は止めない)
            self.log(f'[WARN] task 評価の失敗を {self.cfg.task_evals_file} に書けません: {e}')

    @staticmethod
    def _stderr_tail(job: EvalJob) -> str:
        try:
            job.stderr.seek(0)
            return job.stderr.read().decode('utf-8', errors='replace')[-TASK_EVAL_STDERR_CHARS:].strip()
        except (OSError, AttributeError, ValueError):
            return ''

    def poll_task_evals(self) -> None:
        """評価の子プロセスを見回る. 終わったものを回収し、時間切れは kill し、空きがあれば次を起動する.

        子 (task_eval.py) は自分で SIGALRM の時間上限 (task_eval_timeout) を持ち、時間切れ・例外・import 失敗を
        error 行で書く。子が行を書けずに終わった (異常終了) か、時間上限 + TASK_EVAL_KILL_GRACE を過ぎても
        終わらないときは watchd が error 行を書く。watchd は子の終了を待たない (サイクルを止めない)。
        kill した子も待たず、次の見回り以降に poll で回収する。kill から TASK_EVAL_REAP_GRACE 秒を過ぎても
        回収できなければ error 行と WARN を残して見放す。
        最初の見回りと TASK_EVAL_RESCAN_SECONDS ごとに rescan_task_evals() で評価漏れを拾い直す。
        """
        now = self.mono()
        if self._eval_rescan_at is None or now >= self._eval_rescan_at:
            self._eval_rescan_at = now + TASK_EVAL_RESCAN_SECONDS
            try:
                self.rescan_task_evals()
            except Exception as e:  # noqa: BLE001 (再走査の失敗で watcher を止めない。次の再走査で再試行)
                self.log(f'[WARN] task 評価の再走査に失敗 (次の再走査で再試行): {e}')
        for report_id, job in list(self._evals_running.items()):
            try:
                rc = job.proc.poll()
                if rc is None:
                    if job.killed_at is not None:
                        if self.mono() - job.killed_at < TASK_EVAL_REAP_GRACE:
                            continue
                        self._task_eval_failed(
                            report_id, job, f'timeout: kill 後 {TASK_EVAL_REAP_GRACE}s を過ぎても回収できない'
                        )
                    elif self.mono() - job.started < self.cfg.task_eval_timeout + TASK_EVAL_KILL_GRACE:
                        continue
                    else:
                        job.proc.kill()  # 待たない。次の見回りで poll して回収する
                        job.killed_at = self.mono()
                        continue
                elif job.killed_at is not None:
                    self._task_eval_failed(report_id, job, f'timeout: {self.cfg.task_eval_timeout}s を過ぎたので kill')
                elif rc not in (task_eval.EXIT_WRITTEN, task_eval.EXIT_DUPLICATE):
                    self._task_eval_failed(report_id, job, f'exit={rc}: {self._stderr_tail(job)}')
            except Exception as e:  # noqa: BLE001 (評価の見回りで watcher を止めない)
                self._task_eval_failed(report_id, job, f'watchd: {e}')
            with contextlib.suppress(Exception):
                job.stderr.close()
            del self._evals_running[report_id]
        if not self._evals_waiting:
            self._eval_overflow_warned = False
        while self._evals_waiting and len(self._evals_running) < TASK_EVAL_MAX_RUNNING:
            report_id = next(iter(self._evals_waiting))
            job = self._evals_waiting.pop(report_id)
            try:
                job.stderr = tempfile.TemporaryFile()  # noqa: SIM115 (子が終わるまで開いておく)
                job.proc = subprocess.Popen(  # noqa: S603 (argv は自前の task_eval.py と report の値)
                    job.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=job.stderr,
                    start_new_session=True,  # watchd への Ctrl-C / SIGTERM を子に巻き込まない
                )
            except Exception as e:  # noqa: BLE001 (起動できなくても watcher は止めない)
                with contextlib.suppress(Exception):
                    job.stderr.close()
                self._task_eval_failed(report_id, job, f'spawn: {e}')
                continue
            job.started = self.mono()
            self._evals_running[report_id] = job
            self.log(f'task 評価を開始: {job.base["project"]}/{job.base["task_id"]} report_id={report_id}')

    def archive_verdicts(self) -> None:
        """reports/worker{N}_verdict.yaml を verdicts/ に attempt ごとに写す (SQUAD-397).

        verdict は attempt ごと・task ごとに上書きされるので、eval_trajectory が最終の attempt を後から
        数えられるよう、見えた内容をそのまま残す。task_id か数字の attempt が無い verdict と、
        result が VERDICT_RESULTS に無い (未記入を含む) verdict は写さない
        (書きかけなら次のサイクルで見直す)。同じ名前があれば書かない。2 つの attempt の verdict が
        1 サイクル (既定 15 秒) の間に続けて書かれると前のものは残らないが、最後の attempt は残る。
        失敗しても他の処理は止めず、同じ写し先の WARN は 1 回だけ出す。
        """
        for d in self.owned:
            for path in sorted((d / 'reports').glob('worker*_verdict.yaml')):
                try:
                    with open(path, 'rb') as f:  # noqa: PTH123 (読んだ内容と同じ inode の mtime を取る)
                        data = f.read()
                        mtime = os.fstat(f.fileno()).st_mtime
                except OSError:
                    continue
                meta = parse_scalars(data.decode('utf-8', errors='replace'))
                task_id, attempt = meta.get('task_id', '').strip(), meta.get('attempt', '').strip()
                if not task_id or not attempt.isdigit() or meta.get('result', '').strip() not in VERDICT_RESULTS:
                    continue
                dest = d / VERDICT_HISTORY_DIR / verdict_history_name(task_id, int(attempt), data)
                try:
                    _link_copy(dest, data, mtime)
                except OSError as e:
                    if str(dest) not in self._verdict_warned:
                        self._verdict_warned.add(str(dest))
                        self.log(f'[WARN] verdict の履歴を残せません (次のサイクルで再試行): {dest}: {e}')
                    continue
                self._verdict_warned.discard(str(dest))

    @staticmethod
    def _task_archive_target(path: str, meta: dict[str, str], report_mtime: float) -> tuple[str, str, float] | None:
        """配達した report の task YAML を退避するなら (worker 番号, task_id, report の mtime) を返す (SQUAD-355).

        移すのは終わった report (status が completed / failed、または status の無い review) だけ。
        progress や blocked は続き・人間の判断待ちなので残す。
        """
        name = Path(path).name
        status = meta.get('status', '')
        done = status in ARCHIVE_TASK_STATUSES or ('_review' in name and not status)
        m = WORKER_NUM_RE.search(name)
        task_id = meta.get('task_id', '')
        if not (done and m and task_id):
            return None
        return m.group(1), task_id, report_mtime

    def reconcile_task_archives(self) -> None:
        """Ledger に残る task YAML 退避の予定を進める (SQUAD-361 / SQUAD-368).

        pending: 退避を試す。tasks/ の権限や容量の障害で失敗したら行を残し、次のサイクル (watchd の再起動後も)
        でまた試す。配達は確定済みなので通知は増えない。
        verify: 退避の前に task YAML を開いていた書き手 (追記の `cat >>` など、切り詰めずに開いた fd) が退避後に
        書くと、書込みは退避先の inode に入り tasks/worker{N}.yaml は消えたままになる。退避先の size / mtime が
        退避した時から変わっていたら tasks/ へ戻す。見る inode と退避時の size / mtime は rename の前に ledger に
        書く (書けなければ rename しない) ので、rename 後に ledger へ書けなくても見張りは失われない。
        見張るのは退避から TASK_ARCHIVE_VERIFY_SECONDS 秒 (wall clock) の後の最初のサイクルまで。
        """
        projects = [d.name for d in self.owned]
        for row in self.ledger.task_archives(projects):
            project, report_id = str(row['project']), str(row['report_id'])
            key = f'{project}:{row["worker"]}:{row["task_id"]}'
            if row['state'] == ARCHIVE_VERIFY:
                result = self._verify_archived_task(row)
                if result == 'done':
                    self.ledger.task_archive_done(project, report_id)
                elif result == 'retry':
                    self.ledger.task_archive_retry(project, report_id)
                continue
            result = self._archive_task(
                project, report_id, str(row['worker']), str(row['task_id']), float(row['report_mtime'])
            )
            if result == 'done':
                self.ledger.task_archive_done(project, report_id)
            if result == 'retry':
                self._archive_warned.add(key)
            else:
                self._archive_warned.discard(key)

    def _find_archived_inode(self, row: dict[str, object]) -> Path | None:
        """退避した inode を持つファイルを退避先から探す (ledger に dest を書けなかったときも見つける)."""
        archive = self.cfg.projects_dir / str(row['project']) / 'tasks' / 'archive'
        stem = globmod.escape(f'worker{row["worker"]}_{ARCHIVE_NAME_UNSAFE_RE.sub("_", str(row["task_id"]))}')
        dest = str(row['dest'])
        for p in [
            *([Path(dest)] if dest else []),
            *sorted(archive.glob(f'{stem}*.yaml')),
            *sorted(archive.glob(f'.moving-*-{stem}.yaml')),
        ]:
            try:
                if p.stat().st_ino == row['dest_ino']:
                    return p
            except OSError:
                continue
        return None

    def _verify_archived_task(self, row: dict[str, object]) -> str:
        """退避先への遅い書込みを見つけたら tasks/ へ戻す.

        Returns:
            'done' (見張りを終える)、'keep' (見張りを続ける)、'retry' (rename まで進んでいなかった。pending に戻す)。
        """
        src = self.cfg.projects_dir / str(row['project']) / 'tasks' / f'worker{row["worker"]}.yaml'
        try:
            if src.stat().st_ino == row['dest_ino']:
                return 'retry'  # ledger に書いた後、rename に失敗した (または途中で戻した)
        except OSError:
            pass
        expired = int(time.time()) >= int(row['until'])
        dest = self._find_archived_inode(row)
        if dest is None:
            return 'done'  # 手で動かされた・消された
        try:
            st = dest.stat()
        except OSError:
            return 'done' if expired else 'keep'
        if (st.st_size, st.st_mtime_ns) != (row['dest_size'], row['dest_mtime_ns']):
            try:
                os.link(dest, src)  # 既存を上書きしない
                dest.unlink()
            except FileExistsError:
                self.log(f'[WARN] 退避後に task YAML へ書き込まれましたが、新しい task があるので戻しません: {dest}')
                return 'done'
            except OSError as e:
                self.log(f'[WARN] 退避後に書き込まれた task YAML を戻せません (次サイクルで再試行): {dest}: {e}')
                return 'keep'
            self.log(f'[WARN] 退避後に task YAML へ書き込まれたので戻しました: {dest} -> {src}')
            return 'done'
        return 'done' if expired else 'keep'

    def _archive_task(self, project: str, report_id: str, worker: str, task_id: str, report_mtime: float) -> str:
        """配達した report と同じ task_id の tasks/worker{N}.yaml を tasks/archive/ へ移す (SQUAD-355).

        再起動した worker (とくに W4) が残った task YAML を読み、完了済みの task を再実行して report /
        review を上書きするのを防ぐ。task_id が違う (次の task が書かれた後) か、task YAML が report より
        新しい (同じ task_id で差し戻された後) なら動かさない。
        名前は Dispatcher の手作業の慣習に揃え、既にあれば worker{N}_<task_id>_<YYYYMMDDHHMMSS>.yaml にする。
        mtime は保つ (graph sync / trace が発注時刻に使う)。

        Returns:
            'verifying' (移した。ledger は verify)、'done' (動かすものが無い)、'retry' (失敗。次サイクルで再試行)。
        """
        key = f'{project}:{worker}:{task_id}'
        warned = key in self._archive_warned
        src = self.cfg.projects_dir / project / 'tasks' / f'worker{worker}.yaml'
        try:
            with open(src, 'rb') as f:  # noqa: PTH123 (読んだ内容と同じ inode の mtime を取る)
                data = f.read()
                st = os.fstat(f.fileno())
        except FileNotFoundError:
            return 'done'
        except OSError as e:
            if not warned:
                self.log(f'[WARN] task YAML を読めず退避しません (次サイクルで再試行): {src}: {e}')
            return 'retry'
        if parse_scalars(data.decode(errors='replace')).get('task_id') != task_id:
            return 'done'
        if st.st_mtime > report_mtime:
            return 'done'  # report の後に同じ task_id で差し戻された (停止検知と同じく実行中とみなす)
        # 見張りに要る情報は rename の前に残す。書けなければ動かさない (SQUAD-368 B2-STATE-LOSS)
        until = int(time.time()) + TASK_ARCHIVE_VERIFY_SECONDS
        if not self.ledger.task_archive_moving(project, report_id, st.st_ino, st.st_size, st.st_mtime_ns, until):
            if not warned:
                self.log(f'[WARN] ledger に退避の記録を書けないので退避しません (次サイクルで再試行): {src}')
            return 'retry'
        archive = src.parent / 'archive'
        stem = f'{src.stem}_{ARCHIVE_NAME_UNSAFE_RE.sub("_", task_id)}'
        moving = archive / f'.moving-{os.getpid()}-{stem}.yaml'
        try:
            archive.mkdir(exist_ok=True)
            # 確かめた後に Dispatcher が次の task を書いていたら、それを持って行かないよう rename の後に中身を比べる。
            # O_TRUNC で開いた書き手 (`>`, open(.., "w")) は rename より前に切り詰め、temp file + rename の書き手
            # (Claude Code の Write ツール) は別の inode を置くので、どちらもここで必ず差が出る
            os.rename(src, moving)
            if moving.read_bytes() != data:
                os.link(moving, src)
                moving.unlink()
                self.log(f'[WARN] 退避の途中で task YAML が書き換わったので戻しました: {src}')
                self.ledger.task_archive_retry(project, report_id)
                return 'retry'
            stamp = datetime.now().strftime('%Y%m%d%H%M%S')
            names = [f'{stem}.yaml', f'{stem}_{stamp}.yaml', *(f'{stem}_{stamp}-{k}.yaml' for k in range(2, 100))]
            for dest in (archive / n for n in names):
                if dest.exists() and dest.read_bytes() == data:
                    # 同じ中身が退避済み (手で cp したもの等)。重複を作らず、書き手が開いているかもしれない
                    # 移した inode の方を残す (退避後の書込みを見張れるように)
                    os.replace(moving, dest)
                    break
                try:
                    os.link(moving, dest)  # 既存を上書きしない
                except FileExistsError:
                    continue
                moving.unlink()
                break
            else:
                self.log(f'[WARN] 退避先の名前が尽きたので {moving} に残します')
                return 'verifying'
        except OSError as e:
            if not warned:
                self.log(f'[WARN] task YAML を退避できません (配達は確定済み、次サイクルで再試行): {src}: {e}')
            # rename の前に失敗したなら pending に戻す。書けなくても verify が source の inode を見て戻す
            self.ledger.task_archive_retry(project, report_id)
            return 'retry'
        self.log(f'task YAML を退避: {src} -> {dest}')
        if not self.ledger.task_archive_moved(project, report_id, str(dest)):
            self.log(f'[WARN] 退避先を ledger に書けません (見張りは inode で退避先を探して続ける): {dest}')
        return 'verifying'

    def deliver_instructions(self) -> None:
        """Squad web が queue/instructions/<session>/ に書いた指示を Dispatcher へ 1 件渡す (#85).

        Dispatcher (window dispatcher) が待機中のときだけ、古い順に 1 サイクル 1 件。このサイクルで既に Dispatcher へ
        何か送ったときは次のサイクルに回す (送った直後は作業中の表示がまだ出ていない)。
        二重に送らないよう、打ち込む前に delivered_at を書く (at-most-once)。書いた後に watchd が落ちても
        再起動後に送り直さない。送れなかったら delivered_at を消して failed_at を書き、活動タブに
        「送れず」と出す (中途半端に入力欄に残った文字列へ重ねて送らないため、自動では再送しない)。
        ログには本文を出さない (ファイル名だけ)。
        """
        d = self.cfg.queue_dir / 'instructions' / self.cfg.session
        files = sorted(d.glob('*.json'))
        sig = tuple(p.name for p in files)
        if sig != self._instructions_sig:  # 新しい指示は渡す前から活動タブに出す
            self._instructions_sig = sig
            self._graph_dirty = True
        if not files:
            return
        pane = self.tmux.target('dispatcher')
        if self._sent_to_dispatcher or not self.tmux.pane_exists(pane):
            return
        # 同じ session の watchd が 2 つ動いていても (再起動で旧プロセスが残る等)、印付けと送信を 1 つずつにする。
        # 取れなければこのサイクルは見送る。ロックの中で読み直すので、相手が印を付けた指示は送らない
        try:
            lock = os.open(d / '.lock', os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            self.log(f'[WARN] 指示のロックを開けません (今回は渡さない): {e}')
            return
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(lock)
            return
        try:
            self._deliver_one_instruction(files, pane)
        finally:
            os.close(lock)  # flock も外れる

    @staticmethod
    def _next_instruction(files: list[Path]) -> tuple[Path, dict] | None:
        """まだ渡していない (delivered_at も failed_at も無い) 最も古い web の指示."""
        for path in files:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get('body'), str) and data['body'].strip():
                if not data.get('delivered_at') and not data.get('failed_at'):
                    return path, data
        return None

    def _instruction_pending(self) -> bool:
        d = self.cfg.queue_dir / 'instructions' / self.cfg.session
        return self._next_instruction(sorted(d.glob('*.json'))) is not None

    def _deliver_one_instruction(self, files: list[Path], pane: str) -> None:
        found = self._next_instruction(files)
        if found is None:
            return
        path, data = found
        if detect_status(self.tmux.capture(pane))['status'] != 'idle':
            return
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        if not self._write_instruction(path, {**data, 'delivered_at': now}):
            return
        project = _flat(data['project']) if isinstance(data.get('project'), str) else ''
        # 先頭を固定のタグにする: 本文が - で始まると tmux が option と読み、Enter 等の名前だけなら key と読む。
        # 末尾も固定: 引数の末尾の ; は tmux がコマンドの区切りとして食べる
        tag = f' [project: {project}]' if project else ''
        msg = f'{WEB_INSTRUCTION_PREFIX}{tag} {_flat(data["body"])} [id={_flat(path.stem)}]'
        self._graph_dirty = True
        if self.notify_dispatcher(msg, human_origin=True):
            self.log(f'web からの指示を Dispatcher に渡しました: {path.name}')
            return
        self.log(f'[WARN] web からの指示を Dispatcher に送れませんでした (再送しない): {path.name}')
        self._write_instruction(path, {**data, 'delivered_at': '', 'failed_at': now})

    def _write_instruction(self, path: Path, data: dict) -> bool:
        """指示ファイルを丸ごと書き換える (一時ファイル + rename なので、読む側は書きかけを見ない)."""
        tmp = ''
        try:
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
            return True
        except OSError as e:
            self.log(f'[WARN] 指示ファイルを更新できません (今回は渡さない): {path.name}: {e}')
            if tmp:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False

    def _tasks_changed(self) -> bool:
        """担当 project の tasks/ 以下 (archive 含む) の YAML が前回から変わったか (#84).

        path 込みで比べるので、移動・削除も拾う。初回は比較元が無いので False
        (起動直後の sync は _graph_dirty の初期値 True が担う)。
        """
        sig = []
        for d in self.owned:
            for p in (d / 'tasks').rglob('*.y*ml'):
                try:
                    st = p.stat()
                    # ctime は utime / cp -p で巻き戻せないので、mtime を保った書き換えも拾う
                    sig.append((str(p), st.st_mtime_ns, st.st_ctime_ns, st.st_size))
                except OSError:
                    continue
        new = tuple(sorted(sig))
        changed = self._tasks_sig is not None and new != self._tasks_sig
        self._tasks_sig = new
        return changed

    def sync_graph(self) -> None:
        """探索グラフ (queue/.graph.db) を作り直す (#55).

        graph は PyYAML に依存するため、ここでだけ import する。PyYAML が無い・YAML が
        壊れている等で失敗しても、report 配達には影響させない (ログだけ残して次の配達で再試行)。
        """
        if self._tasks_changed():
            self._graph_dirty = True
            self._graph_retry_at = 0.0  # 新しい変化は backoff を待たず試す
        if not self._graph_dirty or time.monotonic() < self._graph_retry_at:
            return
        try:
            import graph  # noqa: PLC0415

            # review 指摘の category の候補 (#136) は session に依らず queue/_inbox.md に足す (印で重複を防ぐ)
            n_nodes, n_edges = graph.sync(self.cfg.queue_dir, inbox_path=self.cfg.queue_dir / '_inbox.md')
        except Exception as e:  # noqa: BLE001 (graph の失敗で watcher を止めない。ImportError = PyYAML 無しも含む)
            # dirty を残して backoff 付きで再試行する。ログは初回の失敗だけ (毎サイクルの洪水を避ける)
            wait = GRAPH_RETRY_BACKOFF[min(self._graph_failures, len(GRAPH_RETRY_BACKOFF) - 1)]
            if self._graph_failures == 0:
                self.log(f'[WARN] 探索グラフの更新に失敗 ({wait}s 後から再試行、PyYAML が必要な場合あり): {e}')
            self._graph_failures += 1
            self._graph_retry_at = time.monotonic() + wait
        else:
            self._graph_dirty = False
            if self._graph_failures:
                self.log('探索グラフの更新が回復しました')
            self._graph_failures = 0
            self.log(f'探索グラフを更新: {n_nodes} nodes, {n_edges} edges')

    # ---- 2 & 3. 承認オートアンサー + 停止検知 ----

    def check_workers(self) -> None:
        for n in (1, 2, 3, 4):
            self._pending.pop(n, None)
            pane = self.tmux.target(WORKER_WINDOW[n])
            task_path = self.newest_file(f'tasks/worker{n}.yaml')
            task_m = mtime_str(task_path) if task_path else ''
            rep_m = self.newest_mtime(f'reports/worker{n}_report.yaml')
            review_m = self.newest_mtime(f'reports/worker{n}_review.yaml')
            if mtime_gt(review_m, rep_m):
                rep_m = review_m

            pending = bool(task_m) and (not rep_m or mtime_gt(task_m, rep_m))
            project, task_meta = '', {}
            if task_path is not None:
                project = task_path.parent.parent.name
                try:
                    task_meta = parse_scalars(task_path.read_text(errors='replace'))
                except OSError:
                    task_meta = {}
            task_id = task_meta.get('task_id', '')
            if pending and task_id and self._archived_report_delivered(task_path, n, project, task_id):
                self.log(f'Worker{n}: task {task_id} は archive 済み report で配達済み -> stall 対象外')
                pending = False

            if not pending:
                self.pane_stall[n] = 0
                self.resume_count[n] = 0
                continue
            if task_path is not None:
                self._pending[n] = (task_path, task_m, project, task_id)

            self._check_budget(n, task_path, task_meta, project or 'unknown', task_id)

            # pane が存在しない場合 (例: SQUAD_ENABLE_CODEX=0 で w4 window が無い) は
            # capture が空を返し続けて停止通報ループに入るため、このサイクルはスキップする。
            if not self.tmux.pane_exists(pane):
                continue

            cap = self.tmux.capture(pane)
            if APPROVAL_RE.search(cap) and (deny := self.approval_deny_re.search(cap)):
                # 自動受理せず、停止判定にも回さない (人間の応答待ちであることが分かっているため)
                self._hold_approval(n, pane, cap, deny.group(0), project or 'unknown', task_id, task_m)
                continue
            if APPROVAL_RE.search(cap):
                self.log(f'Worker{n}: 承認プロンプト検知 -> 自動受理')
                self.auto_answer(pane, cap)
                self._worker_sent_at[n] = self.wall()
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

    def _hold_approval(
        self, n: int, pane: str, cap: str, matched: str, project: str, task_id: str, task_m: str
    ) -> None:
        """危険操作を含む承認プロンプトを critical で Dispatcher に上げる (同じ task の同じ画面には 1 回だけ).

        キーに task を含める。画面だけだと、次の task が同じ危険操作で止まったときに
        前の task の保留と見なして黙り、停止通報も抑止しているため無通知で止まり続ける。
        """
        h = f'{task_id}:{task_m}:{zlib.crc32(cap.encode())}'
        if self.approval_held.get(n) == h:
            return
        pane_short = pane.split(':', 1)[1]
        # 一致部分だけだと `rm -r` のように対象が分からないので、一致した行を見せる
        line = next((ln.strip() for ln in cap.splitlines() if matched in ln), matched)[:120]
        self.log(f'Worker{n}: 承認プロンプトに危険操作 ({line!r}) -> 自動受理せず保留')
        msg = (
            f'[APPROVAL-HOLD project={project} worker={n} task_id={task_id or "unknown"}] '
            f'Worker{n} の承認プロンプトに危険操作 ({line}) が含まれるため自動受理しませんでした。'
            f'pane {pane_short} を確認し、許可するなら手で応答してください。'
        )
        if self._enqueue_notification(
            project=project, source='approval-hold', priority='critical', message=msg, dedupe_key=f'approval-hold:{h}'
        ):
            self.approval_held[n] = h

    def _check_budget(self, n: int, task_path: Path | None, meta: dict[str, str], project: str, task_id: str) -> None:
        """Task の max_wall_clock_min を超えたら 1 回だけ通知する (kill はしない, #54).

        起点は task の created_at、読めなければ task YAML の mtime。
        """
        # parse_scalars は行末コメント (`30 # 目安`) を残すので、先頭の整数だけ読む
        m = re.match(r'\d+', meta.get('max_wall_clock_min', ''))
        limit_min = int(m.group(0)) if m else 0
        if limit_min <= 0 or task_path is None:
            return
        try:
            started = datetime.fromisoformat(meta.get('created_at', '').replace('Z', '+00:00')).timestamp()
        except ValueError:
            try:
                started = task_path.stat().st_mtime
            except OSError:
                return
        elapsed_min = int((time.time() - started) // 60)
        if elapsed_min < limit_min:
            return
        key = f'budget:{project}:{n}:{task_id or mtime_str(task_path)}'
        path = self.cfg.budget_notified_file
        try:
            if key in path.read_text().splitlines():
                return
        except FileNotFoundError:
            pass
        except OSError as e:
            self.log(f'[WARN] {path} を読めず予算超過の判定を見送り: {e}')
            return
        msg = (
            f'[BUDGET project={project} worker={n} task_id={task_id or "unknown"}] '
            f'Worker{n} が実行時間の予算 {limit_min} 分を超えました (経過 約{elapsed_min} 分)。'
            f'続行・打ち切り・再割当を判断してください (watcher は止めません)。'
        )
        if not self._enqueue_notification(
            project=project, source='budget', priority='normal', message=msg, dedupe_key=key
        ):
            return
        try:
            with path.open('a') as f:
                f.write(key + '\n')
        except OSError as e:
            self.log(f'[WARN] {path} に通知済みを記録できず (再起動後に 1 回再通知され得る): {e}')

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
        if hook_event == 'Notification' and YN_RE.search(self.tmux.capture(pane)):
            self.log(f'Worker{n}: stall 検知、hook=Notification は permission prompt 待ちの疑い')
            msg = (
                f'Worker{n} は応答待ちの可能性があります (hook=Notification, 約{secs}s 経過)。'
                f'pane {pane_short} を確認してください。'
            )
            source = 'stall-hook'
        elif hook_event:
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
        state_file = _state_dir() / f'w{n}.json'
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

    # ---- 3b. タスク途中の区切り (ADR 0011 の 2. と 5.) ----

    def _break_eligible(self, n: int | str) -> bool:
        """途中の /clear を当ててよい worker か (ADR 0011 の 5.。判定はここ 1 か所に置く).

        start.sh が書く agents-<session>.json で `claude` の w1-w3 と Dispatcher だけ。ファイルが無い・
        読めない・値が不明・claude-local・opencode、および Codex (w4) は区切らない。Dispatcher は
        ファイルが読めるときだけ対象にする (start.sh を通らずに動いている session には当てない)。
        """
        if n != DISPATCHER and n not in BREAK_WORKERS:
            return False
        if ARCHIVE_NAME_UNSAFE_RE.search(self.cfg.session):
            return False
        path = _state_dir() / f'agents-{self.cfg.session}.json'
        try:
            agents = json.loads(path.read_text())
        except (OSError, ValueError):
            return False
        if not isinstance(agents, dict):
            return False
        return n == DISPATCHER or agents.get(f'w{n}') == 'claude'

    def _load_worker_state(self, n: int) -> dict:
        try:
            data = json.loads((_state_dir() / f'w{n}.json').read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _update_break(self, n: int, values: dict) -> dict:
        """state/w<n>.json の break を values で更新する (hook と同じロックを取る)."""

        def fn(data: dict) -> None:
            brk = data.get('break')
            data['break'] = {**(brk if isinstance(brk, dict) else {}), **values}

        return ctx.update_state(_state_dir() / f'w{n}.json', fn)['break']

    @staticmethod
    def _stop_count(state: dict) -> int:
        v = state.get('stop_count')
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    def _break_state(self, n: int, state: dict, task_key: str) -> dict:
        """このタスクの break を返す。別のタスクのものなら作り直す (stop_base はタスク開始時の stop_count)."""
        brk = state.get('break')
        if isinstance(brk, dict) and brk.get('task') == task_key:
            return brk
        return self._update_break(
            n,
            {
                'task': task_key,
                'stop_base': self._stop_count(state),
                'break_count': 0,
                'last_break_at': None,
                'limit_notified': False,
            },
        )

    def _break_reason(self, state: dict, brk: dict, task_meta_created: str, task_path: Path) -> str:
        """区切る理由 (ctx:52% / stops:8 / minutes:60)。成立しなければ ''. 上から最初に成立したものを返す.

        ctx は state の取得元 (statusline / transcript) だけで決め、pane は渡さない。pane の読み取りは
        画面に写った依頼文の `reason=ctx:52%` も ctx と読むので、自分の送った文で区切りを繰り返しうる。
        """
        now = self.wall()
        r = ctx.resolve(state, self.cfg.session)
        pct = r.get('context_pct')
        at = _parse_iso(r.get('context_at'))
        if isinstance(pct, int) and at is not None and now - at <= BREAK_CTX_FRESH_SECONDS:
            return f'ctx:{pct}%' if pct >= self.cfg.break_ctx_pct else ''
        # ctx が不明: Stop の回数と経過時間で決める
        stops = self._stop_count(state) - (brk.get('stop_base') or 0)
        if stops >= self.cfg.break_stops:
            return f'stops:{stops}'
        base = _parse_iso(brk.get('last_break_at')) or _parse_iso(task_meta_created)
        if base is None:
            try:
                base = task_path.stat().st_mtime
            except OSError:
                return ''
        minutes = int((now - base) // 60)
        if minutes >= self.cfg.break_minutes:
            return f'minutes:{minutes}'
        return ''

    def _worker_waiting(self, n: int, state: dict, cap: str, task_id: str, task_m: str) -> bool:
        """待機中か (ADR 0011 の 2.。すべて満たすとき)."""
        if detect_status(cap)['status'] != 'idle':
            return False
        if self.pane_stall.get(n, 0) < BREAK_QUIET_CYCLES:
            return False
        if state.get('last_event') not in BREAK_WAIT_EVENTS:
            return False
        ev_at = _parse_iso(state.get('last_event_at'))
        sent = self._worker_sent_at.get(n)
        if ev_at is None or (sent is not None and ev_at <= sent):
            return False
        # 保留中の承認プロンプト (_hold_approval のキーは task_id:task_m:画面)
        return not self.approval_held.get(n, '').startswith(f'{task_id}:{task_m}:')

    def _send_line(self, pane: str, text: str) -> bool:
        """本文と Enter を別々に送る (notify_dispatcher と同じ理由)."""
        if not self.tmux.send_keys(pane, text):
            return False
        self.sleep(0.5)
        return self.tmux.send_keys(pane, 'Enter')

    def check_context_breaks(self, *, dispatcher: bool = False) -> None:
        """Pending の worker の ctx が溜まっていたら、brief を書かせて /clear し、brief から再開させる.

        dispatcher=True では Dispatcher 分 (ADR 0011 の 4.) だけを回す。cycle() はこちらを report_bridge()
        より前に呼び、/clear と再開文を送ったサイクルの通知をその後に届ける。
        """
        if dispatcher:
            self._check_dispatcher_break()
            return
        for n in (1, 2, 3, 4):
            info = self._pending.get(n)
            req = self._break_req.get(n)
            if info is None or not self._break_eligible(n):
                if req is not None:
                    self.log(f'Worker{n}: report が来たか対象外になったので区切りの依頼を捨てる')
                    self._break_req.pop(n, None)
                continue
            task_path, task_m, project, task_id = info
            if not task_id or ARCHIVE_NAME_UNSAFE_RE.search(task_id):
                continue
            if req is not None and req['task_m'] != task_m:
                self.log(f'Worker{n}: task YAML が変わったので区切りの依頼を捨てる')
                self._break_req.pop(n, None)
                req = None
            pane = self.tmux.target(WORKER_WINDOW[n])
            if not self.tmux.pane_exists(pane):
                continue
            state = self._load_worker_state(n)
            brk = self._break_state(n, state, f'{project}/{task_id}')
            brief = self.cfg.projects_dir / project / 'briefs' / f'{task_id}.md'
            if req is not None:
                self._advance_break(n, pane, state, brief, task_path, task_id, task_m)
                continue
            count = brk.get('break_count') or 0
            if count >= BREAK_LIMIT:
                if not brk.get('limit_notified'):
                    self._notify_break_limit(n, project, task_id, brief)
                continue
            if self.mono() < self._break_retry_at.get(n, float('-inf')):
                continue
            last = _parse_iso(brk.get('last_break_at'))
            if last is not None and self.wall() - last < BREAK_COOLDOWN_SECONDS:
                continue
            cap = self.tmux.capture(pane)
            if not self._worker_waiting(n, state, cap, task_id, task_m):
                continue
            created = parse_scalars(task_path.read_text(errors='replace')).get('created_at', '')
            reason = self._break_reason(state, brk, created, task_path)
            if reason:
                self._request_brief(n, pane, brief, task_id, task_m, reason)

    def _request_brief(self, n: int, pane: str, brief: Path, task_id: str, task_m: str, reason: str) -> None:
        with contextlib.suppress(OSError):
            brief.parent.mkdir(parents=True, exist_ok=True)
        self.log(f'Worker{n}: コンテキストを区切る (task_id={task_id} reason={reason}) -> BRIEF-REQUEST')
        requested = self.wall()
        self._worker_sent_at[n] = requested
        self._break_req[n] = {'task_m': task_m, 'wall': requested, 'mono': self.mono()}
        if not self._send_line(pane, brief_request_text(task_id, reason, brief)):
            self.log(f'[WARN] Worker{n} に BRIEF-REQUEST を送れず (30 分後にやり直す)')
            self._break_req.pop(n, None)
            self._break_retry_at[n] = self.mono() + BREAK_RETRY_SECONDS

    def _advance_break(
        self, n: int, pane: str, state: dict, brief: Path, task_path: Path, task_id: str, task_m: str
    ) -> None:
        """返事待ちの依頼を進める: READY なら /clear と RESUME、DEFER か 10 分経過なら 30 分後にやり直す."""
        req = self._break_req[n]
        if self.mono() - req['mono'] >= BREAK_REPLY_TIMEOUT:
            self.log(f'Worker{n}: BRIEF-REQUEST に 10 分返事が無い -> /clear せず 30 分後にやり直す')
            self._break_req.pop(n, None)
            self._break_retry_at[n] = self.mono() + BREAK_RETRY_SECONDS
            return
        cap = self.tmux.capture(pane)
        reply = brief_reply(cap)
        if reply == 'DEFER':
            self.log(f'Worker{n}: BRIEF-DEFER -> 30 分は依頼しない')
            self._break_req.pop(n, None)
            self._break_retry_at[n] = self.mono() + BREAK_RETRY_SECONDS
            return
        if reply != 'READY' or not self._worker_waiting(n, state, cap, task_id, task_m):
            return
        try:
            fresh = brief.stat().st_mtime > req['wall']
        except OSError:
            fresh = False
        if not fresh:
            return  # brief が書き直されていなければ /clear しない (10 分で諦める)
        self._break_req.pop(n, None)
        self._worker_sent_at[n] = self.wall()
        if not self._send_line(pane, '/clear'):
            self.log(f'[WARN] Worker{n} に /clear を送れず (30 分後にやり直す)')
            self._break_retry_at[n] = self.mono() + BREAK_RETRY_SECONDS
            return
        self.sleep(BREAK_CLEAR_WAIT)
        sent = self._send_line(pane, resume_text(task_id, task_path, brief))
        cur = self._load_worker_state(n)
        prev = cur.get('break') if isinstance(cur.get('break'), dict) else {}
        brk = self._update_break(
            n,
            {
                'break_count': (prev.get('break_count') or 0) + 1,
                'last_break_at': datetime.fromtimestamp(self.wall(), timezone.utc).isoformat(timespec='seconds'),
                'stop_base': self._stop_count(cur),
            },
        )
        self._worker_sent_at[n] = self.wall()
        if not sent:
            self.log(f'[WARN] Worker{n} に /clear の後の RESUME を送れず。pane を確認してください')
        self.log(f'Worker{n}: /clear して brief から再開させた (break_count={brk.get("break_count")})')

    # ---- 3c. Dispatcher の区切り (ADR 0011 の 4.) ----

    def _dispatcher_brief(self) -> Path:
        return self.cfg.dashboards_dir / f'{self.cfg.session}-brief.md'

    def _load_dispatcher_state(self) -> dict:
        try:
            data = json.loads((_state_dir() / f'{DISPATCHER}.json').read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _dispatcher_break_reason(self, state: dict) -> str:
        """区切る理由 (ctx:52% / reports:10 / hours:4)。成立しなければ ''. 上から最初に成立したものを返す.

        「最後の /clear」は statusline が session_id の変化で書く last_clear_at。無ければ ctx 以外では区切らない。
        """
        now = self.wall()
        r = ctx.resolve(state, self.cfg.session)
        pct = r.get('context_pct')
        at = _parse_iso(r.get('context_at'))
        if isinstance(pct, int) and at is not None and now - at <= BREAK_CTX_FRESH_SECONDS:
            return f'ctx:{pct}%' if pct >= self.cfg.dispatcher_break_ctx_pct else ''
        cleared = _parse_iso(state.get('last_clear_at'))
        if cleared is None:
            return ''
        notified = state.get('report_notified_at')
        times = [_parse_iso(t) for t in notified] if isinstance(notified, list) else []
        reports = sum(1 for t in times if t is not None and t >= cleared)
        if reports >= self.cfg.dispatcher_break_reports:
            return f'reports:{reports}'
        hours = int((now - cleared) // 3600)
        if now - cleared >= self.cfg.dispatcher_break_hours * 3600:
            return f'hours:{hours}'
        return ''

    def _dispatcher_blocker(self, state: dict, cap: str) -> str:
        """割り込まない理由 (ADR 0011 の 4.)。どれも当てはまらなければ ''."""
        if detect_status(cap)['status'] != 'idle':
            return 'busy'
        if self._disp_quiet < BREAK_QUIET_CYCLES:
            return 'pane-changing'
        if self._sent_to_dispatcher:
            return 'sent-this-cycle'
        if self._instruction_pending():
            return 'web-instruction'
        human = _parse_iso(state.get('last_human_prompt_at'))
        if human is not None and self.wall() - human < self.cfg.dispatcher_human_quiet_min * 60:
            return 'human-input'
        if brief_question_pending(self._dispatcher_brief()):
            return 'question-in-brief'
        path = state.get('transcript_path')
        if ask_user_pending(path if isinstance(path, str) else ''):
            return 'ask-user'
        return ''

    def _check_dispatcher_break(self) -> None:
        if not self._break_eligible(DISPATCHER):
            if self._disp_req is not None:
                self.log('Dispatcher: 対象外になったので区切りの依頼を捨てる')
                self._disp_req = None
            return
        pane = self.tmux.target(DISPATCHER)
        if not self.tmux.pane_exists(pane):
            return
        cap = self.tmux.capture(pane)
        h = hex(zlib.crc32(cap.encode('utf-8', 'replace')))
        self._disp_quiet = self._disp_quiet + 1 if h == self._disp_hash else 0
        self._disp_hash = h
        state = self._load_dispatcher_state()
        if self._disp_req is not None:
            self._advance_dispatcher_break(pane, state, cap)
            return
        if self.mono() < self._disp_retry_at:
            return
        brk = state.get('break') if isinstance(state.get('break'), dict) else {}
        last = _parse_iso(brk.get('last_break_at'))
        if last is not None and self.wall() - last < BREAK_COOLDOWN_SECONDS:
            return
        reason = self._dispatcher_break_reason(state)
        if not reason or self._dispatcher_blocker(state, cap):
            return
        self.log(f'Dispatcher: コンテキストを区切る (reason={reason}) -> BRIEF-REQUEST')
        requested = self.wall()
        if not self.notify_dispatcher(dispatcher_brief_request_text(reason, self.cfg.session)):
            self.log('[WARN] Dispatcher に BRIEF-REQUEST を送れず (30 分後にやり直す)')
            self._disp_retry_at = self.mono() + BREAK_RETRY_SECONDS
            return
        self._disp_req = {'wall': requested, 'mono': self.mono(), 'seq': self._disp_send_seq}

    def _advance_dispatcher_break(self, pane: str, state: dict, cap: str) -> None:
        """返事待ちの依頼を進める (流れは worker と同じ。BRIEF-READY 後に何か送っていたらやり直す)."""
        req = self._disp_req
        assert req is not None
        if self.mono() - req['mono'] >= BREAK_REPLY_TIMEOUT:
            self.log('Dispatcher: BRIEF-REQUEST に 10 分返事が無い -> /clear せず 30 分後にやり直す')
            self._disp_req = None
            self._disp_retry_at = self.mono() + BREAK_RETRY_SECONDS
            return
        reply = brief_reply(cap)
        if reply == 'DEFER':
            self.log('Dispatcher: BRIEF-DEFER -> 30 分は依頼しない')
            self._disp_req = None
            self._disp_retry_at = self.mono() + BREAK_RETRY_SECONDS
            return
        if reply != 'READY':
            # BRIEF-READY が出ていない画面を見た。ここまでの送信は BRIEF-READY より前なので brief に入りうる
            req['seq'] = self._disp_send_seq
            return
        if self._disp_send_seq != req['seq']:
            # BRIEF-READY が出た後に送ったかもしれない通知が /clear で文脈から消えるので、この回はやり直す
            self.log('Dispatcher: BRIEF-READY の後に watchd が送ったので /clear せずやり直す')
            self._disp_req = None
            return
        blocker = self._dispatcher_blocker(state, cap)
        if blocker:
            return
        try:
            fresh = self._dispatcher_brief().stat().st_mtime > req['wall']
        except OSError:
            fresh = False
        if not fresh:
            return  # brief が書き直されていなければ /clear しない (10 分で諦める)
        self._disp_req = None
        if not self.notify_dispatcher('/clear'):
            self.log('[WARN] Dispatcher に /clear を送れず (30 分後にやり直す)')
            self._disp_retry_at = self.mono() + BREAK_RETRY_SECONDS
            return
        self.sleep(BREAK_CLEAR_WAIT)
        sent = self.notify_dispatcher(dispatcher_resume_text(self.cfg.session))
        at = datetime.fromtimestamp(self.wall(), timezone.utc).isoformat(timespec='seconds')

        def fn(data: dict) -> None:
            prev = data.get('break') if isinstance(data.get('break'), dict) else {}
            data['break'] = {**prev, 'break_count': (prev.get('break_count') or 0) + 1, 'last_break_at': at}

        self._update_dispatcher_state(fn)
        if not sent:
            self.log('[WARN] Dispatcher に /clear の後の RESUME を送れず。pane を確認してください')
        self.log('Dispatcher: /clear して brief から再開させた')

    def _notify_break_limit(self, n: int, project: str, task_id: str, brief: Path) -> None:
        msg = (
            f'[BREAK-LIMIT project={project} worker={n} task_id={task_id}] '
            f'Worker{n} はこのタスクで {BREAK_LIMIT} 回区切りました。以後このタスクでは途中の区切りをしません。'
            f'pane と brief ({brief}) を確認してください。'
        )
        if self._enqueue_notification(
            project=project,
            source='break-limit',
            priority='low',
            message=msg,
            dedupe_key=f'break-limit:{project}:{n}:{task_id}',
        ):
            self._update_break(n, {'limit_notified': True})
            self.log(f'Worker{n}: 区切りが上限 ({BREAK_LIMIT} 回) に達した -> [BREAK-LIMIT] 通知')

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
        """Merge 済みかつ clean な専用 worktree だけ自動削除する.

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
            self.log(f'gc: {removed} worktree を削除 (skip {skipped})')

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
        self._sent_to_dispatcher = False
        self.refresh_owned_projects()
        # Dispatcher の区切りは report_bridge() より前に回す。/clear と再開文を送ったサイクルの通知は後に届く
        try:
            self.check_context_breaks(dispatcher=True)
        except Exception as e:  # noqa: BLE001 (区切りの失敗で watcher を止めない。区切らない側に倒す)
            self.log(f'[WARN] Dispatcher の区切りの判定に失敗 (今回は区切らない): {e}')
        self.archive_verdicts()
        self.report_bridge()
        try:
            self.poll_task_evals()
        except Exception as e:  # noqa: BLE001 (評価の失敗で watcher を止めない)
            self.log(f'[WARN] task 評価の見回りに失敗: {e}')
        self.deliver_instructions()
        self.sync_graph()
        self.check_workers()
        try:
            self.check_context_breaks()
        except Exception as e:  # noqa: BLE001 (区切りの失敗で watcher を止めない。区切らない側に倒す)
            self.log(f'[WARN] タスク途中の区切りの判定に失敗 (今回は区切らない): {e}')

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
        # enqueue 済みの未 ack event は、flag off (rollback) 後もこの経路でしか Dispatcher window へ
        # 届かないため、ここを flag でガードすると rollback 後に永久に沈黙する
        # (SQUAD-228, PR #29 事後 cross-review)。新規通知の enqueue 自体は
        # _enqueue_notification() 側で引き続き flag により直送/queue を切り替える。
        self._process_queue_fallbacks(now)
        # write_health() は既定環境 (flag off かつ notification dir 未使用) では呼ばない。
        # ここを無条件にすると、queue を一度も opt-in していない環境でも health.json が
        # 新規作成され続けてしまう (SQUAD-230, PR #31 cross-review blocking 指摘)。
        # dir.exists()/write_health() の I/O 失敗 (PermissionError 等) は cycle() 全体を
        # 落とさない。run() は cycle() の例外を捕捉しないため、ここで拾わないと watcher
        # プロセスごと停止し、以後の fallback 通知も含め恒久的に沈黙する
        # (SQUAD-231, PR #31 re-review blocking 指摘)。_process_queue_fallbacks() は
        # 既にこの手前で実行済みなので、ここで例外を吸収しても当該サイクルの fallback
        # 通知は失われない。
        try:
            if self.cfg.notify_queue_enabled or self.nq.dir.exists():
                self.nq.write_health(
                    owned_projects=len(self.owned),
                    write_ok=self._queue_write_ok and self._queue_readable,
                )
        except OSError as e:
            self.log(f'[WARN] health.json 更新に失敗 (次サイクルで再試行): {e}')
        self._queue_write_ok = True
        self._queue_readable = True

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
        # GC を起動直後に走らせない。last_gc=0 初期化だと最初の cycle() で無条件に
        # gc_worktrees() に入り、worktree が 20 件を超えると fetch/判定 (git 1 操作 ≈ 2s)
        # がループを数十秒ブロックする (実測: 30 件で ≈50s)。report-bridge の応答性を
        # 優先し、初回は gc_interval 分待ってから実行する。boot_delay の sleep を
        # monkeypatch する時間テストがあるため sleep 後の現在時刻を基にする。
        self.last_gc = time.time()
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
