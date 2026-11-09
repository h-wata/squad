#!/usr/bin/env python3
# ruff: noqa: CPY001
"""bin/squad が Python のサブコマンドを squad/squad.py に渡す (#99).

本物の squad.py や tmux を触らないよう、bin/squad を tmp_path にコピーし、
引数と SQUAD_SESSION を JSON で書き出すだけの偽 squad.py を隣に置いて叩く。
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

import pytest

REPO = Path(__file__).resolve().parent.parent
FAKE_SQUAD_PY = """import json, os, sys
print(json.dumps({'argv': sys.argv[1:], 'session': os.environ.get('SQUAD_SESSION')}))
"""
DELEGATED = ['ls', 'assign', 'dashboard', 'ledger', 'notify', 'graph', 'trace', 'web']


@pytest.fixture
def fake_root(tmp_path: Path) -> Path:
    root = tmp_path / 'repo'
    (root / 'bin').mkdir(parents=True)
    (root / 'squad').mkdir()
    shutil.copy2(REPO / 'bin' / 'squad', root / 'bin' / 'squad')
    (root / 'squad' / 'squad.py').write_text(FAKE_SQUAD_PY)
    return root


def _run(
    cmd: Path, *args: str, cwd: Path, session: str | None = None, piddir: Path | None = None
) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != 'SQUAD_SESSION'}
    if session is not None:
        env['SQUAD_SESSION'] = session
    # 本物の tmux server を見ないよう socket の置き場所を cwd に向ける
    env['TMUX_TMPDIR'] = str(cwd)
    env.pop('TMUX', None)
    if piddir is not None:
        env['SQUAD_PIDFILE_DIR'] = str(piddir)
    return subprocess.run([str(cmd), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('sub', DELEGATED)
def test_python_subcommands_are_delegated(fake_root: Path, tmp_path: Path, sub: str) -> None:
    cwd = tmp_path / 'elsewhere'
    cwd.mkdir()
    r = _run(fake_root / 'bin' / 'squad', sub, 'a b', '--flag', cwd=cwd, session='scratch-s')
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {'argv': [sub, 'a b', '--flag'], 'session': 'scratch-s'}


def test_delegation_works_through_symlink(fake_root: Path, tmp_path: Path) -> None:
    link_dir = tmp_path / 'home' / '.local' / 'bin'
    link_dir.mkdir(parents=True)
    (link_dir / 'squad').symlink_to(fake_root / 'bin' / 'squad')
    empty = tmp_path / 'nopids'
    empty.mkdir()
    r = _run(link_dir / 'squad', 'graph', 'status', cwd=tmp_path, piddir=empty)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {'argv': ['graph', 'status'], 'session': None}


def test_status_stays_in_bash(fake_root: Path, tmp_path: Path) -> None:
    r = _run(fake_root / 'bin' / 'squad', 'status', cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert '"argv"' not in r.stdout
    assert f'SQUAD_ROOT: {fake_root}' in r.stdout
    assert 'WATCHER' in r.stdout


class PidDir:
    """一意名の pidfile を置く専用ディレクトリ。生きた pid (sleep) を pidfile に書く."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.procs: list[subprocess.Popen] = []

    def make(self, name: str) -> None:
        p = subprocess.Popen(['sleep', '30'])
        self.procs.append(p)
        (self.path / f'{name}-watch.pid').write_text(str(p.pid))


@pytest.fixture
def piddir(tmp_path: Path) -> Iterator[PidDir]:
    d = tmp_path / 'pids'
    d.mkdir()
    pd = PidDir(d)
    yield pd
    for p in pd.procs:
        p.terminate()
        p.wait()
    for f in d.glob('*-watch.pid'):
        f.unlink()


def _name() -> str:
    return f'sq101test-{uuid.uuid4().hex[:8]}'


def test_single_running_session_is_auto_selected(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    name = _name()
    piddir.make(name)
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, piddir=piddir.path)
    assert json.loads(r.stdout)['session'] == name
    # log も同じセッションのログを見に行く
    r = _run(fake_root / 'bin' / 'squad', 'log', cwd=tmp_path, piddir=piddir.path)
    assert f'/tmp/{name}-watch.log' in r.stderr


def test_explicit_session_wins(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    piddir.make(_name())
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, session='explicit-s', piddir=piddir.path)
    assert json.loads(r.stdout)['session'] == 'explicit-s'


def test_two_sessions_keep_default_and_warn(fake_root: Path, tmp_path: Path, piddir: PidDir) -> None:
    a, b = _name(), _name()
    piddir.make(a)
    piddir.make(b)
    r = _run(fake_root / 'bin' / 'squad', 'ls', cwd=tmp_path, piddir=piddir.path)
    assert json.loads(r.stdout)['session'] is None
    assert a in r.stderr and b in r.stderr
    assert 'SQUAD_SESSION=<name>' in r.stderr
    assert len(r.stderr.strip().splitlines()) == 1
