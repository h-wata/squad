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


def _fake_tmux(tmp_path: Path, *, alive: bool) -> Path:
    """has-session が alive の生死を返す偽 tmux バイナリを作り、その bin dir を返す."""
    fake_bin = tmp_path / 'fake-tmux-bin'
    fake_bin.mkdir(exist_ok=True)
    fake_tmux = fake_bin / 'tmux'
    exit_code = 0 if alive else 1
    fake_tmux.write_text(f'#!/usr/bin/env bash\nexit {exit_code}\n')
    fake_tmux.chmod(0o755)
    return fake_bin


def _path_without_tmux(tmp_path: Path, path_value: str) -> str:
    """PATH 上の全実行ファイルを tmux だけ除いてシンボリックリンクした bin dir を作る.

    ディレクトリ単位で PATH から除外すると mkdir/dirname 等の必須コマンドも
    巻き添えで消えるため、ファイル単位で tmux だけを取り除く。
    """
    fake_bin = tmp_path / 'no-tmux-bin'
    fake_bin.mkdir(exist_ok=True)
    for entry in path_value.split(os.pathsep):
        d = Path(entry)
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.name == 'tmux' or (fake_bin / f.name).exists():
                continue
            try:
                (fake_bin / f.name).symlink_to(f)
            except OSError:
                continue
    return str(fake_bin)


def _run_start(
    root: Path,
    workspace: Path,
    session: str,
    owned: str | None = None,
    *,
    tmux_alive: bool | None = None,
    strip_tmux_from_path: bool = False,
    tmp_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, 'SQUAD_SESSION': session, 'SQUAD_DRY_RUN': '1'}
    if owned is not None:
        env['SQUAD_OWNED_PROJECTS'] = owned
    else:
        env.pop('SQUAD_OWNED_PROJECTS', None)
    if tmux_alive is not None:
        assert tmp_path is not None
        fake_bin = _fake_tmux(tmp_path, alive=tmux_alive)
        env['PATH'] = f'{fake_bin}:{env["PATH"]}'
    bash_bin = shutil.which('bash') or 'bash'
    if strip_tmux_from_path:
        assert tmp_path is not None
        env['PATH'] = _path_without_tmux(tmp_path, env['PATH'])
    return subprocess.run(
        [bash_bin, str(root / 'start.sh'), str(workspace)],
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


def test_owned_projects_creates_missing_project_dir(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='ghost')

    assert r.returncode == 0, r.stderr
    ghost = root / 'queue' / 'projects' / 'ghost'
    assert (ghost / 'tasks').is_dir()
    assert (ghost / 'reports').is_dir()
    assert (ghost / '.squad_session').read_text().strip() == 'testsess'


def test_owned_projects_rejects_path_like_names(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='../escape, .hidden')

    assert r.returncode == 0, r.stderr
    assert not (root / 'queue' / 'escape').exists()
    assert not (root / 'queue' / 'projects' / '.hidden').exists()
    assert r.stdout.count('不正です') == 2


def test_owned_projects_does_not_overwrite_alive_session_marker(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('othersess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', tmux_alive=True, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'othersess'
    assert '既に' in r.stdout


def test_owned_projects_takes_over_stale_session_marker(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('deadsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', tmux_alive=False, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'testsess'
    assert '引き継ぎました' in r.stdout


def test_owned_projects_no_tmux_in_path_is_conservative(tmp_path: Path) -> None:
    root = _fake_root(tmp_path)
    pj = root / 'queue' / 'projects' / 'pj'
    pj.mkdir(parents=True)
    (pj / '.squad_session').write_text('deadsess\n')
    workspace = tmp_path / 'ws'
    workspace.mkdir()

    r = _run_start(root, workspace, 'testsess', owned='pj', strip_tmux_from_path=True, tmp_path=tmp_path)

    assert r.returncode == 0, r.stderr
    assert (pj / '.squad_session').read_text().strip() == 'deadsess'
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
