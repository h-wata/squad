#!/usr/bin/env python3
# ruff: noqa: CPY001
"""report 配達 ledger (sqlite3, stdlib only).

report を Dispatcher へ橋渡ししたかどうかを、全 watcher (全セッション) で共有する
永続ストアで管理する。旧 watch.sh の「awk + flock + タブ区切りテキスト」実装を sqlite3 の
BEGIN IMMEDIATE トランザクションに置き換えたもの (Issue #26)。排他は DB に任せ、自前の
ロックファイルは持たない。

配達の主キーは **(project, report_id)** である (SQUAD-215/216)。report_id は writer が
report を新規作成するときに一度だけ発番する UUIDv4 で、同じ報告の修正・再出力・archive
からの復帰では変わらない。別の報告は内容が同じでも別 ID になる。

mtime は配達の同一性・到着順序・新旧判定のどれにも使わない。mtime は「変更時刻」であって
「到着時刻」ではなく、mv/cp/archive 復帰で過去の値を保てるうえ、時計後退や分解能でも壊れる。
比較すべきなのは到着時刻ではなく「その identity をもう配達したか」だけである。

配達は 2 段階:
  claim   配達権 (期限付き lease) を取る。取れた watcher だけが送信する。
  commit  送信成功後に delivered へ確定する。以後 同じ DB からは再送しない。
  fail    送信/commit 失敗時に pending のまま次回試行時刻 (next_attempt_at) を後ろへずらす。

claim を即 delivered にしないのは、送信に失敗した report や送信前に落ちた watcher の report が
二度と橋渡しされなくなるため。lease にしておけば期限切れ後に誰か (別セッションの watcher でも
よい) が再び claim して配達をやり直せる。commit / fail は claim 時に発行した token で所有者を
照合する。照合しないと、A の lease が切れた後に B が claim した状況で、遅れて戻ってきた A が
B の claim を勝手に commit したり壊したりできてしまう。

再試行間隔は 15秒 -> 60秒 -> 5分 -> 30分 -> 以降 30分ごと。**試行回数の上限は設けない**。
有限上限は通信不能時に report を永久沈黙させるため禁止する。30分 cap なら障害が長引いても
可視性を保ちつつ、通常時に鳴り続けることもない。

異常時 (DB を開けない・書けない) は「未配達」側に倒す (fail-open)。握り潰し (気づけない) より
再通知 (煩いが気づける) を選ぶ。この場合 claim は成功扱いだが token は返さない (呼び出し側が
「claim 未記録」を WARN できるようにするため)。再送間隔だけはプロセス内のメモリで同じ backoff を
適用する (再起動でリセットされるのは許容する = 重複側に倒す)。
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import random
import re
import sqlite3
import time
from typing import NamedTuple
import uuid

PENDING = 'pending'
DELIVERED = 'delivered'
SQLITE_MAGIC = b'SQLite format 3\x00'
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS deliveries ('
    'project TEXT NOT NULL, '
    'report_id TEXT NOT NULL, '
    'path TEXT NOT NULL, '
    'content_sha256 TEXT NOT NULL, '
    'state TEXT NOT NULL, '
    'lease TEXT NOT NULL, '
    'attempt_count INTEGER NOT NULL, '
    'next_attempt_at INTEGER NOT NULL, '
    'PRIMARY KEY (project, report_id))'
)
LEGACY_TABLE = 'reports'

# 送信/commit 失敗時の再試行間隔。末尾の値が cap で、以降はその間隔で無限に再送する。
BACKOFF_SECONDS = (15, 60, 300, 1800)

# 監視対象 report のファイル名パターン (queue/projects/<project> 配下)
REPORT_GLOBS = ('**/reports/worker*_report.yaml', '**/reports/worker*_review.yaml')

# report_id が無い / 壊れている report の配達キーに使う接頭辞。UUID を後付けで推測せず、
# 内容が変わらない限り 1 回だけ [REPORT-INVALID] を通知するための決定的なキー。
INVALID_PREFIX = 'INVALID:'

_SCALAR_RE = re.compile(r'^([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*$')
_BLOCK_MARKERS = ('|', '>', '|-', '>-', '|+', '>+')


def backoff_seconds(attempt: int) -> int:
    """Attempt 回目の失敗のあと、次に再送するまで待つ秒数 (最後の値が cap)."""
    return BACKOFF_SECONDS[min(max(attempt, 1), len(BACKOFF_SECONDS)) - 1]


def parse_scalars(text: str) -> dict[str, str]:
    """top-level の `key: value` だけ拾う簡易 YAML リーダー (block scalar 本文は読まない)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line[0].isspace() or line.startswith('#'):
            continue
        m = _SCALAR_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val in _BLOCK_MARKERS:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in '"\'':
            val = val[1:-1]
        out.setdefault(key, val)
    return out


def normalize_report_id(raw: str) -> str:
    """Report_id を UUID の正準形へ正規化する (UUID でなければ空文字).

    大文字小文字やハイフン省略の揺れで同じ報告が二重に配達されないよう正準化する。
    UUID として読めない値 ('TBD' 等) を通してしまうと、その文字列を一度配達した時点で
    以後同じ値を書く report がすべて握り潰されるため、無効として扱う。
    """
    try:
        return str(uuid.UUID(raw.strip()))
    except (ValueError, AttributeError):
        return ''


def report_identity(data: bytes) -> tuple[str, dict[str, str], str]:
    """Report の生 bytes から (content_sha256, top-level scalars, parse error) を返す."""
    sha = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode()
    except UnicodeDecodeError as e:
        return sha, {}, f'decode error: {e}'
    return sha, parse_scalars(text), ''


def delivery_key(meta: dict[str, str], sha: str, parse_error: str, path: str) -> tuple[str, str]:
    """配達キーとして使う report_id と、無効な場合の理由を返す.

    Returns:
        (report_id, invalid_reason)。invalid_reason が空でなければ [REPORT-INVALID] 扱い。
        その場合の配達キーは path + 内容ハッシュ由来の決定的なキーになる。path を含めるのは、
        同じ project 内で report_id 欠落の別 worker が偶然同じ内容 (例: 同じテンプレートの
        コピペ漏れ) の report を書いたとき、sha だけをキーにすると片方が「配達済み」を共有して
        もう片方が永久に通知されなくなるため。内容が直るまで path ごとに 1 回だけ通知され、
        直せば別キー (正規の report_id) として改めて通知される。
    """
    if parse_error:
        return f'{INVALID_PREFIX}{path}:{sha}', parse_error
    raw = meta.get('report_id', '')
    if not raw:
        return f'{INVALID_PREFIX}{path}:{sha}', 'report_id 欠落'
    rid = normalize_report_id(raw)
    if not rid:
        return f'{INVALID_PREFIX}{path}:{sha}', f'report_id が UUID ではありません: {raw!r}'
    return rid, ''


class Claim(NamedTuple):
    """claim の結果.

    Attributes:
        status: 'claim' (配達権を取れた) / 'seen' (配達済み) /
            'held' (他 watcher が配達中、または backoff 待ちで再送時刻に達していない)。
        token: 'claim' 時の所有権 token。None は ledger に記録できなかったことを表す
            (fail-open。commit / fail はメモリ上の backoff だけを更新する)。
        attempt: この送信が当該 report_id にとって何回目か (1 始まり)。
    """

    status: str
    token: str | None
    attempt: int

    @property
    def ok(self) -> bool:
        return self.status == 'claim'


def _lease_deadline(lease: str) -> int:
    """Lease 値 '<期限 epoch 秒>:<nonce>' から期限だけ取り出す (空/壊れていれば 0)."""
    head = lease.split(':', 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def find_reports(dirs: Iterable[Path]) -> list[tuple[str, str]]:
    """担当 project 配下の report / review を (project, path) で列挙する.

    project は project ディレクトリ名。配達キー (project, report_id) の前半に使う。
    """
    out: list[tuple[str, str]] = []
    for d in dirs:
        for pattern in REPORT_GLOBS:
            for p in sorted(d.glob(pattern)):
                out.append((d.name, str(p)))
    return out


class ReportLedger:
    """report 配達状態の永続ストア (sqlite3)."""

    def __init__(self, db_path: Path | str, lease_seconds: int = 60, timeout: float = 5.0) -> None:
        self.path = Path(db_path)
        self.lease_seconds = lease_seconds
        self.timeout = timeout
        # DB を使えないときの backoff だけを保持する (再起動でリセットされてよい)
        self._mem: dict[tuple[str, str], tuple[int, int]] = {}

    # ---- 内部 ----

    def _connect(self, path: Path | None = None) -> sqlite3.Connection:
        conn = sqlite3.connect(path or self.path, timeout=self.timeout, isolation_level=None)
        conn.execute(f'PRAGMA busy_timeout = {int(self.timeout * 1000)}')
        conn.execute(SCHEMA)
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE で直列化した書き込みトランザクション."""
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
                conn.execute('COMMIT')
            except BaseException:
                conn.execute('ROLLBACK')
                raise
        finally:
            conn.close()

    @staticmethod
    def _lookup(conn: sqlite3.Connection, project: str, report_id: str) -> tuple[str, str, int, int] | None:
        row = conn.execute(
            'SELECT state, lease, attempt_count, next_attempt_at FROM deliveries WHERE project = ? AND report_id = ?',
            (project, report_id),
        ).fetchone()
        return (row[0], row[1], int(row[2]), int(row[3])) if row else None

    def _new_token(self, now: int) -> str:
        # 期限値だけでは一意にならない (同じ秒に 2 watcher が claim すると期限が一致する)。
        return f'{now + self.lease_seconds}:{os.getpid()}-{random.randrange(1 << 30)}'

    # ---- 公開 API ----

    def claim(self, project: str, report_id: str, path: str, content_sha256: str) -> Claim:
        """配達権を取る (未配達で、再送時刻に達していて、他 watcher が配達中でないとき)."""
        now = int(time.time())
        key = (project, report_id)
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, project, report_id)
                attempts = 0
                if rec:
                    state, lease, attempts, next_at = rec
                    if state == DELIVERED:
                        return Claim('seen', None, attempts)
                    if now < next_at or now < _lease_deadline(lease):
                        # backoff 待ち、または他 watcher が配達中
                        return Claim('held', None, attempts)
                token = self._new_token(now)
                conn.execute(
                    'INSERT INTO deliveries'
                    '(project, report_id, path, content_sha256, state, lease, attempt_count, next_attempt_at) '
                    'VALUES(?, ?, ?, ?, ?, ?, ?, 0) '
                    'ON CONFLICT(project, report_id) DO UPDATE SET '
                    'path = excluded.path, content_sha256 = excluded.content_sha256, lease = excluded.lease',
                    (project, report_id, path, content_sha256, PENDING, token, attempts),
                )
                self._mem.pop(key, None)
                return Claim('claim', token, attempts + 1)
        except sqlite3.Error:
            return self._mem_claim(key, now)

    def _mem_claim(self, key: tuple[str, str], now: int) -> Claim:
        """DB を使えないときの fail-open claim (backoff だけメモリで守る)."""
        attempts, next_at = self._mem.get(key, (0, 0))
        if now < next_at:
            return Claim('held', None, attempts)
        return Claim('claim', None, attempts + 1)

    def commit(self, project: str, report_id: str, token: str | None) -> bool:
        """送信成功後に delivered へ確定する.

        自分の claim でなくなっていれば何もしない (成功扱い)。ledger を操作できなければ
        False を返す (呼び出し側が fail() で backoff を進め、いずれ再通知される)。
        """
        if token is None:
            return False
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, project, report_id)
                if rec is None:
                    return False
                state, lease, _attempts, _next_at = rec
                if state == DELIVERED or lease != token:
                    return True
                conn.execute(
                    'UPDATE deliveries SET state = ?, lease = ?, next_attempt_at = 0 '
                    'WHERE project = ? AND report_id = ?',
                    (DELIVERED, '', project, report_id),
                )
                return True
        except sqlite3.Error:
            return False

    def fail(self, project: str, report_id: str, token: str | None) -> bool:
        """送信/commit 失敗時に pending のまま次回試行時刻を backoff だけ後ろへずらす.

        行は消さない。消すと「未登録 = 初回」に戻り、backoff が効かず失敗のたびに
        全速力で再送してしまう。試行回数の上限は設けない (永久沈黙を作らないため)。
        """
        now = int(time.time())
        key = (project, report_id)
        if token is None:
            attempts = self._mem.get(key, (0, 0))[0] + 1
            self._mem[key] = (attempts, now + backoff_seconds(attempts))
            return True
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, project, report_id)
                if rec is None:
                    return True
                state, lease, attempts, _next_at = rec
                if state == DELIVERED or lease != token:
                    return True  # lease 切れ後に別 watcher が再 claim している
                attempts += 1
                conn.execute(
                    'UPDATE deliveries SET lease = ?, attempt_count = ?, next_attempt_at = ? '
                    'WHERE project = ? AND report_id = ?',
                    ('', attempts, now + backoff_seconds(attempts), project, report_id),
                )
                return True
        except sqlite3.Error:
            return False

    def exists(self) -> bool:
        return self.path.exists()

    def is_sqlite(self) -> bool:
        """Ledger のファイル実体が sqlite3 DB か (旧テキスト ledger と区別する)."""
        try:
            with self.path.open('rb') as fh:
                return fh.read(16) == SQLITE_MAGIC
        except OSError:
            return False

    def _build(self, rows: Iterable[tuple[str, str, str, str]], replace: bool = False) -> int:
        """一時 DB を作って rows を配達済みとして入れ、DB がまだ無ければ atomic に据える.

        Args:
            rows: (project, report_id, path, content_sha256) の並び。
            replace: True なら既存ファイルを上書きする (旧テキスト ledger の in-place 移行用)。

        Returns:
            登録件数。既に他プロセスが作っていた場合は -1 (先着優先で何もしない)。
        """
        tmp = self.path.with_name(f'{self.path.name}.new.{os.getpid()}')
        tmp.unlink(missing_ok=True)
        try:
            conn = self._connect(tmp)
            try:
                conn.executemany(
                    'INSERT OR REPLACE INTO deliveries'
                    '(project, report_id, path, content_sha256, state, lease, attempt_count, next_attempt_at) '
                    f"VALUES(?, ?, ?, ?, '{DELIVERED}', '', 0, 0)",
                    rows,
                )
                count = conn.execute('SELECT count(*) FROM deliveries').fetchone()[0]
            finally:
                conn.close()
            if replace:
                # 2 watcher が同時に旧形式と判定した場合の TOCTOU 対策: 読み取り〜置換の
                # 間に別プロセスが先に sqlite3 化していたら、その移行済み DB を空 DB で
                # 上書きしないよう、lock 保持中に宛先の現在状態を再チェックする。
                lock_path = self.path.with_name(f'{self.path.name}.lock')
                with lock_path.open('a+') as lock_fh:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                    try:
                        if self.is_sqlite():
                            return -1
                        os.replace(tmp, self.path)
                        return int(count)
                    finally:
                        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            try:
                # link は宛先が既にあれば必ず失敗する = 先着優先の atomic な据え付け
                os.link(tmp, self.path)
            except FileExistsError:
                return -1
            return int(count)
        finally:
            tmp.unlink(missing_ok=True)

    def migrate(self, legacy_text: Path | None = None) -> str:
        """旧 ledger を report_id ベースの新 schema へ移行する.

        旧 ledger の行は `path + mtime` しか持たず、そこから report_id を安全に復元する
        手段が無い。推測で delivered として登録すると「まだ配達していない report を配達済み
        にする」= 永久沈黙を作り得るため、**旧行は一切引き継がない**。空の配達表へ移行し、
        reports/ に残る legacy report は最大 1 回だけ再通知されるほうに倒す。

        Args:
            legacy_text: 別パスにある旧タブ区切り ledger (queue/.report_ledger)。参照用に
                `.legacy` へ退避し、新 schema は空の配達表から始める。

        Returns:
            'legacy-text' (別パスの旧テキストを退避) / 'text' (ledger_path 自体が旧テキスト
            だったので置換) / 'legacy-table' (旧 sqlite schema を破棄) /
            '' (移行不要、または移行できなかった)。
        """
        if legacy_text is not None and legacy_text.is_file() and not self.path.exists():
            legacy_text.replace(legacy_text.with_name(f'{legacy_text.name}.legacy'))
            return 'legacy-text' if self._build([]) >= 0 else ''
        if not self.path.exists():
            return ''
        if not self.is_sqlite():
            return 'text' if self._build([], replace=True) >= 0 else ''
        try:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (LEGACY_TABLE,)
                ).fetchone()
                if not row:
                    return ''
                conn.execute(f'DROP TABLE {LEGACY_TABLE}')
            return 'legacy-table'
        except sqlite3.Error:
            return ''
