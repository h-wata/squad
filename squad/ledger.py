#!/usr/bin/env python3
# ruff: noqa: CPY001
"""report 配達 ledger (sqlite3, stdlib only).

report を Dispatcher へ橋渡ししたかどうかを、全 watcher (全セッション) で共有する
永続ストアで管理する。旧 watch.sh の「awk + flock + タブ区切りテキストを read-modify-
rename」実装を sqlite3 の BEGIN IMMEDIATE トランザクションに置き換えたもの
(Issue #26)。排他は DB に任せ、自前のロックファイルは持たない。

配達は 2 段階:
  claim   配達権 (期限付き lease) を取る。取れた watcher だけが送信する。
  commit  送信成功後に「配達済み」(lease = '0') へ確定する。
  release 送信失敗時に claim 前の記録へ戻す (行を消すのではなく戻す)。

claim を即「配達済み」にしないのは、送信に失敗した report や送信前に落ちた watcher の
report が二度と橋渡しされなくなるため。lease にしておけば期限切れ後に誰か (別セッション
の watcher でもよい) が再び claim して配達をやり直せる。

commit / release は claim 時に発行した token で所有者を照合する。mtime だけで照合すると、
A の lease が切れた後に B が同じ mtime を再 claim した状況で、遅れて戻ってきた A が B の
claim を勝手に commit したり release で壊したりできてしまう。

異常時 (DB を開けない・書けない) は「未配達」側に倒す (fail-open)。握り潰し (気づけない)
より再通知 (煩いが気づける) を選ぶ、という watcher 全体の方針に合わせる。この場合 claim は
成功扱いだが token は返さない (呼び出し側が「claim 未記録」を WARN できるようにするため)。

mtime は「秒.ナノ秒 (9 桁ゼロ詰め)」の文字列として記録・比較する。float に落とすと同一秒内に
書き直された report (例: in_progress -> blocked) や下位桁だけ違う mtime を同じ版とみなして
恒久的に握り潰す。同一版かどうかは文字列一致、新旧の判定だけ mtime_gt() で行う。
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import random
import sqlite3
import time
from typing import NamedTuple

DELIVERED = '0'
SQLITE_MAGIC = b'SQLite format 3\x00'
SCHEMA = 'CREATE TABLE IF NOT EXISTS reports (path TEXT PRIMARY KEY, mtime TEXT NOT NULL, lease TEXT NOT NULL)'

# 監視対象 report のファイル名パターン (queue/projects 配下)
REPORT_GLOBS = ('**/reports/worker*_report.yaml', '**/reports/worker*_review.yaml')


class Claim(NamedTuple):
    """claim の結果.

    Attributes:
        status: 'claim' (配達権を取れた) / 'seen' (配達済みか他が配達中) /
            'stale' (記録より古い mtime なので通知しない)。
        token: 'claim' 時の所有権 token。None は ledger に記録できなかったことを表す
            (fail-open。呼び出し側は commit / release を呼んでも空振りする)。
        prev_mtime: claim 前に記録されていた mtime (未登録なら '')。
        prev_lease: claim 前に記録されていた lease (未登録なら '')。
    """

    status: str
    token: str | None
    prev_mtime: str
    prev_lease: str

    @property
    def ok(self) -> bool:
        return self.status == 'claim'


def mtime_str(path: os.PathLike[str] | str) -> str:
    """Stat の mtime を「秒.ナノ秒(9桁)」文字列で返す (float 化による桁落ちを避ける)."""
    ns = os.stat(path).st_mtime_ns
    return f'{ns // 10**9}.{ns % 10**9:09d}'


def mtime_gt(a: str, b: str) -> bool:
    """Mtime 文字列 a が b より真に新しいか.

    整数部は数値で、小数部はゼロ詰めした固定長文字列の辞書順で比較する
    (find %T@ の 20 桁 mtime を float にすると下位桁が落ちて誤判定するため)。
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


def _lease_deadline(lease: str) -> int:
    """Lease 値 '<期限 epoch 秒>:<nonce>' から期限だけ取り出す (壊れていれば 0)."""
    head = lease.split(':', 1)[0]
    try:
        return int(head)
    except ValueError:
        return 0


def find_reports(dirs: Iterable[Path]) -> list[tuple[str, str]]:
    """担当 project 配下の report / review を (path, mtime) で列挙する."""
    out: list[tuple[str, str]] = []
    for d in dirs:
        for pattern in REPORT_GLOBS:
            for p in sorted(d.glob(pattern)):
                try:
                    out.append((str(p), mtime_str(p)))
                except OSError:
                    continue
    return out


class ReportLedger:
    """report 配達状態の永続ストア (sqlite3)."""

    def __init__(self, db_path: Path | str, lease_seconds: int = 60, timeout: float = 5.0) -> None:
        self.path = Path(db_path)
        self.lease_seconds = lease_seconds
        self.timeout = timeout

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
    def _lookup(conn: sqlite3.Connection, path: str) -> tuple[str, str] | None:
        row = conn.execute('SELECT mtime, lease FROM reports WHERE path = ?', (path,)).fetchone()
        return (row[0], row[1]) if row else None

    def _new_token(self, now: int) -> str:
        # 期限値だけでは一意にならない (新しい mtime の claim は既存 lease を待たずに成立
        # するため、同じ秒に 2 watcher が claim すると期限が一致する)。PID と乱数を混ぜる。
        return f'{now + self.lease_seconds}:{os.getpid()}-{random.randrange(1 << 30)}'

    # ---- 公開 API ----

    def claim(self, path: str, mtime: str) -> Claim:
        """配達権を取る."""
        now = int(time.time())
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, path)
                prev_mt, prev_ut = rec if rec else ('', '')
                if rec:
                    if mtime == prev_mt:
                        # 同じ版。配達済み、または他 watcher が配達中なら触らない。
                        if prev_ut == DELIVERED or now < _lease_deadline(prev_ut):
                            return Claim('seen', None, prev_mt, prev_ut)
                    elif not mtime_gt(mtime, prev_mt):
                        # 記録より古い版は lease の状態によらず常に skip。claim を許すと
                        # 記録の mtime と呼び出し側の mtime が食い違い、commit が空振りして
                        # lease 切れ後に二重通知される。
                        return Claim('stale', None, prev_mt, prev_ut)
                token = self._new_token(now)
                conn.execute(
                    'INSERT INTO reports(path, mtime, lease) VALUES(?, ?, ?) '
                    'ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, lease = excluded.lease',
                    (path, mtime, token),
                )
                return Claim('claim', token, prev_mt, prev_ut)
        except sqlite3.Error:
            return Claim('claim', None, '', '')

    def commit(self, path: str, mtime: str, token: str | None) -> bool:
        """送信成功後に配達済みへ確定する.

        自分の claim でなくなっていれば何もしない (成功扱い)。ledger を操作できなければ
        False を返す (lease 期限切れ後に再 claim されるので通知は消えない)。
        """
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, path)
                if rec is None:
                    return False
                mt, ut = rec
                if mt != mtime or ut == DELIVERED or ut != token:
                    return True
                conn.execute('UPDATE reports SET lease = ? WHERE path = ?', (DELIVERED, path))
                return True
        except sqlite3.Error:
            return False

    def release(self, path: str, token: str | None, prev_mtime: str, prev_lease: str) -> bool:
        """送信失敗時に claim を取り消し、claim 前の記録へ戻す.

        行を消すのではなく戻すのが重要。単に消すと直前に配達済みだった古い版の記録まで
        失われ、更新前の mtime を掴んでいた別 watcher がその古い版を再 claim できてしまう。
        """
        try:
            with self._tx() as conn:
                rec = self._lookup(conn, path)
                if rec is None:
                    return True
                if rec[1] != token:
                    return True  # lease 切れ後に別 watcher が再 claim している
                if prev_mtime:
                    conn.execute(
                        'UPDATE reports SET mtime = ?, lease = ? WHERE path = ?', (prev_mtime, prev_lease, path)
                    )
                else:
                    conn.execute('DELETE FROM reports WHERE path = ?', (path,))
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

    def _build(self, rows: Iterable[tuple[str, str, str]], replace: bool = False) -> int:
        """一時 DB を作って rows を入れ、DB がまだ無ければ atomic に据える.

        Args:
            rows: (path, mtime, lease) の並び。
            replace: True なら既存ファイルを上書きする (旧テキスト ledger の in-place 移行用)。

        Returns:
            登録件数。既に他プロセスが作っていた場合は -1 (先着優先で何もしない)。
        """
        tmp = self.path.with_name(f'{self.path.name}.new.{os.getpid()}')
        tmp.unlink(missing_ok=True)
        try:
            conn = self._connect(tmp)
            try:
                conn.executemany('INSERT OR REPLACE INTO reports(path, mtime, lease) VALUES(?, ?, ?)', rows)
                count = conn.execute('SELECT count(*) FROM reports').fetchone()[0]
            finally:
                conn.close()
            if replace:
                os.replace(tmp, self.path)
                return int(count)
            try:
                # link は宛先が既にあれば必ず失敗する = 先着優先の atomic な据え付け
                os.link(tmp, self.path)
            except FileExistsError:
                return -1
            return int(count)
        finally:
            tmp.unlink(missing_ok=True)

    def baseline_seed(self, projects_dir: Path) -> int:
        """既存 report を「通知済み」として一括登録する (導入時の一斉通知を防ぐ).

        対象は担当 project ではなく queue/projects 配下の全 report。後から起動した別
        セッションの watcher は「ledger がある = seed 済み」としか判断しないため、担当分
        しか seed しないとその watcher が過去 report を一斉通知してしまう。

        Returns:
            登録件数。既に ledger がある場合は -1、失敗した場合は -2。
        """
        if self.path.exists():
            return -1
        try:
            rows = [(p, m, DELIVERED) for p, m in find_reports([projects_dir])]
            return self._build(rows)
        except (OSError, sqlite3.Error):
            return -2

    def migrate_legacy(self, legacy_path: Path, replace: bool = False) -> int:
        r"""旧タブ区切り ledger (`<mtime>\t<lease>\t<path>`) を sqlite3 へ取り込む.

        Args:
            legacy_path: 旧テキスト ledger。replace=True なら自分自身 (ledger_path) でもよい。
            replace: True なら既存の ledger_path を上書きする (WATCH_LEDGER_FILE が
                旧テキスト ledger を直接指している場合の in-place 移行)。

        Returns:
            取り込み件数。既に ledger がある場合は -1、失敗した場合は -2。
        """
        if self.path.exists() and not replace:
            return -1
        try:
            rows: list[tuple[str, str, str]] = []
            for line in legacy_path.read_text(errors='replace').splitlines():
                cols = line.split('\t')
                if len(cols) < 3:
                    continue  # 壊れた行 / 旧中間形式は無視する
                mt, ut, path = cols[0], cols[1], '\t'.join(cols[2:])
                rows.append((path, mt, ut))
            return self._build(rows, replace=replace)
        except (OSError, sqlite3.Error):
            return -2
