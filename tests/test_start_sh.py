#!/usr/bin/env python3
# ruff: noqa: CPY001
"""start.sh の SQUAD_OWNED_PROJECTS マーカー自動整備テスト (SQUAD-210).

SQUAD_DRY_RUN=1 で settings/scaffold の pre-flight だけを実行させ、tmux には触れずに
.squad_session マーカーの書き込み/保護挙動を確認する。本番リポジトリの queue/ を巻き込ま
ないよう、start.sh + .claude/ を隔離した tmp ディレクトリへコピーして実行する
(tests/test_stop_sh.py と同じ手法)。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

REPO = Path(__file__).resolve().parent.parent


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / 'squad-root'
    root.mkdir()
    shutil.copy(REPO / 'start.sh', root / 'start.sh')
    shutil.copytree(REPO / '.claude', root / '.claude')
    return root


def _run_start(
    root: Path, workspace: Path, session: str, owned: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, 'SQUAD_SESSION': session, 'SQUAD_DRY_RUN': '1'}
    if owned is not None:
        env['SQUAD_OWNED_PROJECTS'] = owned
    else:
        env.pop('SQUAD_OWNED_PROJECTS', None)
    return subprocess.run(
        ['bash', str(root / 'start.sh'), str(workspace)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_no_owned_projects_env_leaves_markers_untouched(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess')

    assert r.returncode == 0, r.stderr
    assert not (pj / '.squad_session').exists()


def test_owned_projects_writes_marker_for_existing_project(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj')

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_skips_missing_project_dir(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='ghost')

    assert r.returncode == 0, r.stderr
    assert not (root / 'queue' / 'projects' / 'ghost').exists()
    assert '存在しない' in r.stdout


def test_owned_projects_does_not_overwrite_other_session_marker(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('othersess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj')

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'othersess'
    assert '既に' in r.stdout


def test_owned_projects_multiple_comma_separated(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    (root / 'queue' / 'projects' / 'a').mkdir(parents=True)
    (root / 'queue' / 'projects' / 'b').mkdir(parents=True)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='a, b')

    assert r.returncode == 0, r.stderr
    assert (root / 'queue' / 'projects' / 'a' / '.squad_session').read_text().strip() == 'testsess'
    assert (root / 'queue' / 'projects' / 'b' / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_same_value_marker_is_harmless(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('testsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj')

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'
