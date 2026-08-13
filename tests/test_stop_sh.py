#!/usr/bin/env python3
# ruff: noqa: CPY001
"""stop.sh の watcher 停止契約テスト (Issue #26 / cross-review F2).

watch.sh が `exec python3 squad/watchd.py` になったため、watcher プロセスの cmdline は
watch.sh を含まない。pidfile 不在時の fallback と複数 watcher 誤爆防止 guard が
watchd.py を検出できることを、実プロセスを起動して確認する。

本番 watcher を巻き込まないよう、stop.sh は tmp_path へコピーしたものを使う
(stop.sh の pgrep パターンは自身の SCRIPT_DIR 起点なので tmp 配下しか一致しない)。
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

REPO = Path(__file__).resolve().parent.parent
SLEEPER = 'import time; time.sleep(60)\n'


def _alive(p: subprocess.Popen) -> bool:
    return p.poll() is None


def _wait_dead(p: subprocess.Popen, timeout: float = 5.0) -> bool:
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


@pytest.fixture
def fake_squad(tmp_path: Path) -> Iterator[tuple[Path, Callable[..., subprocess.Popen]]]:
    """stop.sh + 偽 watcher (watchd.py / 旧 watch.sh) を持つ隔離 squad ディレクトリ."""
    shutil.copy(REPO / 'stop.sh', tmp_path / 'stop.sh')
    (tmp_path / 'squad').mkdir()
    (tmp_path / 'squad' / 'watchd.py').write_text(SLEEPER)
    # 旧 watch.sh 相当 (exec しないので cmdline に watch.sh が残る)
    (tmp_path / 'watch.sh').write_text('#!/bin/bash\nwhile true; do sleep 1; done\n')
    procs: list[subprocess.Popen] = []

    def spawn(session: str, legacy: bool = False) -> subprocess.Popen:
        env = {**os.environ, 'SQUAD_SESSION': session}
        cmd = ['bash', str(tmp_path / 'watch.sh')] if legacy else ['python3', str(tmp_path / 'squad' / 'watchd.py')]
        p = subprocess.Popen(cmd, env=env)
        procs.append(p)
        return p

    yield tmp_path, spawn

    for p in procs:
        p.kill()
        p.wait()


def _run_stop(root: Path, session: str | None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != 'SQUAD_SESSION'}
    args = ['bash', str(root / 'stop.sh')] + ([session] if session else [])
    return subprocess.run(args, env=env, capture_output=True, text=True, check=False, timeout=60)


def test_fallback_kills_only_matching_session_watchd(fake_squad: tuple) -> None:
    """Pidfile が無くても watchd.py プロセスを SQUAD_SESSION 単位で停止できる."""
    root, spawn = fake_squad
    sess_a = f'squadtest-{os.getpid()}-a'
    sess_b = f'squadtest-{os.getpid()}-b'
    a, b = spawn(sess_a), spawn(sess_b)
    legacy = spawn(sess_a, legacy=True)  # 移行期間の旧 watch.sh プロセスも対象
    time.sleep(0.5)
    assert not Path(f'/tmp/{sess_a}-watch.pid').exists()

    _run_stop(root, sess_a)

    assert _wait_dead(a)
    assert _wait_dead(legacy)
    assert _alive(b)  # 別セッションの watcher は巻き込まない


def test_no_arg_guard_detects_multiple_watchers(fake_squad: tuple) -> None:
    """引数も env も無い場合、複数 watcher 稼働中なら誤爆せず終了する."""
    root, spawn = fake_squad
    a = spawn(f'squadtest-{os.getpid()}-a')
    b = spawn(f'squadtest-{os.getpid()}-b')
    time.sleep(0.5)

    r = _run_stop(root, None)

    assert r.returncode == 1
    assert '複数 Squad' in r.stdout
    assert _alive(a)
    assert _alive(b)
