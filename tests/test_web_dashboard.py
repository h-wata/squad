#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/web.py (読み取り専用 Web ダッシュボード, Issue #76) のテスト.

queue / dashboards / graph DB は tmp に実ファイルで作り、実際に HTTP サーバーを立てて叩く。
tmux だけは subprocess.run を差し替える (テスト環境の tmux に依存しない)。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

SQUAD_DIR = Path(__file__).resolve().parent.parent / 'squad'
sys.path.insert(0, str(SQUAD_DIR))
import squad as squad_cli  # noqa: E402
import web  # noqa: E402

TOKEN = 'test-token-123'
SECRET = 'ghp_' + 'A' * 36
RID = '11111111-1111-4111-8111-111111111111'


def _write(path: Path, text: str, mtime: int = 1_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Pj に active (T2) / blocked (T3) / 完了済み (T1) のタスクと dashboard を持つ queue."""
    q = tmp_path / 'queue' / 'projects' / 'pj'
    _write(q / 'tasks' / 'worker1.yaml', 'task_id: T1\ntitle: "done task"\nassigned_to: worker1\n')
    _write(q / 'tasks' / 'worker2.yaml', 'task_id: T2\ntitle: "running task"\nassigned_to: worker2\n')
    _write(q / 'tasks' / 'worker3.yaml', 'task_id: T3\ntitle: "stuck task"\nassigned_to: worker3\n')
    _write(q / 'tasks' / 'archive' / 'worker1_T0.yaml', 'task_id: T0\n')
    _write(q / 'reports' / 'worker1_report.yaml', 'task_id: T1\nstatus: completed\nsummary: "ok"\n', 2_000)
    _write(q / 'reports' / 'worker2_report.yaml', 'task_id: T_OLD\nstatus: completed\nsummary: "old"\n', 1_500)
    _write(
        q / 'reports' / 'worker3_report.yaml',
        f'task_id: T3\nstatus: blocked\nsummary: "token {SECRET}"\n',
        3_000,
    )
    _write(tmp_path / 'dashboards' / 'pj.md', '# pj\n', 4_000)
    _make_graph_db(tmp_path / 'queue' / '.graph.db')
    return tmp_path


def _make_graph_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        'CREATE TABLE nodes (node_id TEXT PRIMARY KEY, kind TEXT, project TEXT, state TEXT, title TEXT, '
        'ref TEXT, payload_json TEXT, updated_at TEXT)'
    )
    conn.execute('CREATE TABLE edges (src_id TEXT, rel TEXT, dst_id TEXT)')
    rows = [
        ('goal:pj#1', 'goal', 'pj', 'open', 'goal one'),
        ('intent:pj/T1', 'intent', 'pj', 'done', 'done task'),
        (f'result:{RID}', 'result', 'pj', 'completed', 'ok'),
        (f'fact:{RID}#0', 'fact', 'pj', 'confirmed', 'a fact'),
        ('intent:other/X', 'intent', 'other', 'running', 'other pj'),
    ]
    conn.executemany("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, '', '{}', '')", rows)
    conn.executemany(
        'INSERT INTO edges VALUES (?, ?, ?)',
        [
            ('goal:pj#1', 'spawns', 'intent:pj/T1'),
            ('intent:pj/T1', 'yields', f'result:{RID}'),
            (f'result:{RID}', 'yields', f'fact:{RID}#0'),
        ],
    )
    conn.commit()
    conn.close()


def _fake_tmux(calls: list[list[str]]) -> object:
    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        out = ''
        if cmd[:2] == ['tmux', 'list-sessions']:
            out = 'squad\nother\n'
        elif cmd[:2] == ['tmux', 'list-panes']:
            out = 'squad:0.0\tDispatcher\nsquad:0.5\tAux-Shell\n' if 'squad' in cmd else 'other:0.0\tbash\n'
        elif cmd[:2] == ['tmux', 'capture-pane']:
            out = f'working...\nAuthorization: Bearer {SECRET}\nexport GH={SECRET}\n'
        return subprocess.CompletedProcess(cmd, 0, out, '')

    return run


@pytest.fixture
def server(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[list[str]]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(web.subprocess, 'run', _fake_tmux(calls))
    monkeypatch.setattr(squad_cli.subprocess, 'run', _fake_tmux(calls))
    srv = web.make_server('127.0.0.1', 0, TOKEN, root / 'queue', root / 'dashboards')
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_address[1]}', calls
    srv.shutdown()
    srv.server_close()


def _req(url: str, method: str = 'GET', headers: dict | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=b'x' if method != 'GET' else None)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _get_json(base: str, path: str) -> object:
    code, body = _req(f'{base}{path}', headers={'Authorization': f'Bearer {TOKEN}'})
    assert code == 200, body
    return json.loads(body)


def test_refuses_to_start_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SQUAD_WEB_TOKEN', raising=False)
    with pytest.raises(SystemExit) as e:
        squad_cli.main(['web', '--port', '0'])
    assert e.value.code != 0
    with pytest.raises(ValueError):
        web.make_server('127.0.0.1', 0, '', Path('q'), Path('d'))


@pytest.mark.parametrize('path', ['/', '/api/projects', '/api/panes', '/api/graph?project=pj'])
def test_token_required(server: tuple[str, list], path: str) -> None:
    base, _ = server
    assert _req(base + path)[0] == 401
    assert _req(base + path, headers={'Authorization': 'Bearer wrong'})[0] == 401
    sep = '&' if '?' in path else '?'
    assert _req(f'{base}{path}{sep}token=wrong')[0] == 401
    assert _req(f'{base}{path}{sep}token={TOKEN}')[0] == 200
    assert _req(base + path, headers={'Authorization': f'Bearer {TOKEN}'})[0] == 200


def test_projects(server: tuple[str, list]) -> None:
    base, _ = server
    data = _get_json(base, '/api/projects')
    (pj,) = data['projects']
    assert pj['name'] == 'pj'
    by_id = {t['task_id']: t for t in pj['tasks']}
    assert by_id['T1']['state'] == 'done'
    assert by_id['T2']['state'] == 'active'  # report は別 task のもの
    assert by_id['T2']['worker'] == 'worker2'
    assert by_id['T3']['state'] == 'blocked'
    assert 'T0' not in by_id  # archive は対象外
    assert pj['latest_report']['task_id'] == 'T3'
    assert pj['latest_report']['status'] == 'blocked'
    assert SECRET not in json.dumps(data)  # summary もマスク済み
    assert pj['dashboard_mtime'].startswith('1970-01-01T')
    assert pj['dashboard_mtime'] == web.iso(4_000)


def test_panes_are_masked_and_only_squad_sessions(server: tuple[str, list]) -> None:
    base, calls = server
    data = _get_json(base, '/api/panes')
    assert [s['name'] for s in data['sessions']] == ['squad']
    panes = data['sessions'][0]['panes']
    assert [p['target'] for p in panes] == ['squad:0.0', 'squad:0.5']
    assert all(p['ok'] for p in panes)
    text = json.dumps(data)
    assert SECRET not in text
    assert 'working...' in text
    assert {c[1] for c in calls} <= {'list-sessions', 'list-panes', 'capture-pane'}


def test_graph(server: tuple[str, list]) -> None:
    base, _ = server
    data = _get_json(base, '/api/graph?project=pj')
    kinds = {n['node_id']: n['kind'] for n in data['nodes']}
    assert kinds == {
        'goal:pj#1': 'goal',
        'intent:pj/T1': 'intent',
        f'result:{RID}': 'result',
        f'fact:{RID}#0': 'fact',
    }
    assert len(data['edges']) == 3
    assert _get_json(base, '/api/graph?project=none') == {'nodes': [], 'edges': []}


def test_graph_without_db(tmp_path: Path) -> None:
    assert web.graph_data(tmp_path / 'missing.db', 'pj') == {'nodes': [], 'edges': []}
    assert not (tmp_path / 'missing.db').exists()  # mode=ro なので作らない


def _snapshot(root: Path) -> dict[str, tuple[float, str]]:
    return {
        str(p): (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob('*'))
        if p.is_file()
    }


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def test_write_methods_rejected_and_files_unchanged(server: tuple[str, list], root: Path, method: str) -> None:
    base, _ = server
    before = _snapshot(root)
    for path in ('/', '/api/projects', '/api/panes', '/api/graph?project=pj'):
        code, _ = _req(base + path, method=method, headers={'Authorization': f'Bearer {TOKEN}'})
        assert code == 405
    for path in ('/', '/api/projects', '/api/panes', '/api/graph?project=pj'):
        _req(f'{base}{path}', headers={'Authorization': f'Bearer {TOKEN}'})
    assert _snapshot(root) == before


def test_no_send_keys_in_source() -> None:
    """web.py が tmux に渡すサブコマンドは読み取り系だけ (send-keys 等が無い)."""
    src = (SQUAD_DIR / 'web.py').read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    name = lambda c: ast.unparse(c.func)  # noqa: E731
    subcommands = {c.args[0].value for c in calls if name(c) == '_tmux_lines'}
    assert subcommands == {'list-sessions', 'list-panes', 'capture-pane'}
    # subprocess を呼ぶのは _tmux_lines の中の 1 箇所だけ
    assert [ast.unparse(c) for c in calls if name(c).startswith('subprocess.')] == [
        "subprocess.run(['tmux', *args], capture_output=True, text=True)"
    ]
    assert 'send-keys' not in src


def test_pane_masked_before_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    """末尾 PANE_LINES 行に BEGIN 行が入らない長い鍵でも本文が出ない (切ってからマスクしない)."""
    body = [f'KEYBODY{i:03d}' for i in range(web.PANE_LINES + 10)]
    screen = '\n'.join(['-----BEGIN RSA PRIVATE KEY-----', *body])  # pragma: allowlist secret

    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        out = {'list-sessions': 's\n', 'list-panes': 's:0.5\tAux-Shell\n', 'capture-pane': screen + '\n\n'}[cmd[1]]
        return subprocess.CompletedProcess(cmd, 0, out, '')

    monkeypatch.setattr(web.subprocess, 'run', run)
    (pane,) = web.panes_data()['sessions'][0]['panes']
    (capture,) = [c for c in calls if c[1] == 'capture-pane']
    assert capture[capture.index('-S') + 1] == '-'  # 可視範囲でなく履歴全体を取る
    assert 'KEYBODY' not in pane['text']
    assert pane['text'].split('\n')[-1] == '[REDACTED PRIVATE KEY LINE]'  # 末尾の空行は落とす
    assert len(pane['text'].split('\n')) == web.PANE_LINES


def test_pane_capture_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        out = {'list-sessions': 's\n', 'list-panes': 's:0.5\tAux-Shell\n', 'capture-pane': ''}[cmd[1]]
        return subprocess.CompletedProcess(cmd, 1 if cmd[1] == 'capture-pane' else 0, out, 'no pane')

    monkeypatch.setattr(web.subprocess, 'run', run)
    (pane,) = web.panes_data()['sessions'][0]['panes']
    assert pane['ok'] is False
    assert pane['text'] == ''


def test_yaml_shallow_reads_block_scalar(tmp_path: Path) -> None:
    """`summary: |` の本文を 1 行に畳んで返す (dashboard の表に入れても崩れない). 他の key は従来どおり."""
    p = tmp_path / 'r.yaml'
    p.write_text(
        'task_id: X\nsummary: |\n  1 行目\n\n  2 行目: 値\nnotes: >-\n  folded\nissues:\n  - a\nstatus: completed\n'
    )
    meta = squad_cli.yaml_shallow(p)
    assert meta['summary'] == '1 行目 2 行目: 値'
    assert meta['notes'] == 'folded'
    assert meta['status'] == 'completed'
    assert meta['task_id'] == 'X'
    assert 'issues' not in meta


def test_projects_match_results_by_task_id(tmp_path: Path) -> None:
    """実 queue と同じ名前の結果ファイル (worker4_review*.yaml / workerN_verdict.yaml) を task_id で突き合わせる."""
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    t, r = pj / 'tasks', pj / 'reports'
    # cross-review: 結果は worker4_review_<id>.yaml と worker4_review.yaml にだけある
    _write(t / 'worker4.yaml', 'task_id: X-1\nassigned_to: worker4\n', 1_000)
    _write(r / 'worker4_review_X-1.yaml', 'task_id: X-1\nverdict: approve\n', 1_100)
    _write(r / 'worker4_report.yaml', 'task_id: OLD-4\nstatus: completed\n', 900)
    # report は別 task のまま、verdict だけが pass (SQUAD-281 と同じ形)
    _write(t / 'worker2.yaml', 'task_id: X-2\nassigned_to: worker2\n', 1_000)
    _write(r / 'worker2_report.yaml', 'task_id: OLD-2\nstatus: completed\n', 900)
    _write(r / 'worker2_verdict.yaml', 'task_id: X-2\nresult: pass\n', 1_200)
    # verdict は fail でも report が completed なら report を採る
    _write(t / 'worker3.yaml', 'task_id: X-3\n', 1_000)
    _write(r / 'worker3_report.yaml', 'task_id: X-3\nstatus: completed\nsummary: |\n  直した\n  2 行目\n', 1_600)
    _write(r / 'worker3_verdict.yaml', 'task_id: X-3\nresult: fail\n', 1_250)
    # 同じ task_id で再発注された task: 発注より古い完了報告では done にしない
    _write(t / 'worker1.yaml', 'task_id: X-4\n', 2_000)
    _write(r / 'worker1_report.yaml', 'task_id: X-4\nstatus: completed\n', 1_500)
    # verdict が fail だけ
    _write(t / 'worker5.yaml', 'task_id: X-5\n', 1_000)
    _write(r / 'worker5_verdict.yaml', 'task_id: X-5\nresult: fail\n', 1_100)

    (data,) = web.projects_data(tmp_path / 'queue', tmp_path / 'dashboards')['projects']
    states = {x['task_id']: x['state'] for x in data['tasks']}
    assert states == {'X-1': 'done', 'X-2': 'done', 'X-3': 'done', 'X-4': 'active', 'X-5': 'blocked'}
    assert data['latest_report']['file'] == 'worker3_report.yaml'
    assert data['latest_report']['summary'] == '直した 2 行目'


@pytest.fixture
def tokyo_tz() -> None:
    """Naive 時刻のローカル解釈を固定する (UTC と読むと 9 時間ずれるのを検出できるように)."""
    old = os.environ.get('TZ')
    os.environ['TZ'] = 'Asia/Tokyo'
    time.tzset()
    yield
    if old is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = old
    time.tzset()


def _states(tmp_path: Path) -> dict[str, str]:
    (data,) = web.projects_data(tmp_path / 'queue', tmp_path / 'dashboards')['projects']
    return {x['task_id']: x['state'] for x in data['tasks']}


def test_freshness_uses_created_at_and_completed_at(tmp_path: Path) -> None:
    """mesh-mem と同じ形: task の mtime だけが後から一括更新され、report より新しく見える."""
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    t, r = pj / 'tasks', pj / 'reports'
    _write(t / 'worker3.yaml', 'task_id: Y-1\ncreated_at: "2026-05-22T17:18:00+09:00"\n', 2_000_000_000)
    _write(
        r / 'worker3_report.yaml',
        'task_id: Y-1\nstatus: completed\ncompleted_at: "2026-05-22T17:25:00+09:00"\n',
        3_000,
    )
    # 再発注: created_at が completed_at より新しければ done にしない
    _write(t / 'worker2.yaml', 'task_id: Y-2\ncreated_at: "2026-05-23T09:00:00+09:00"\n', 2_000_000_000)
    _write(
        r / 'worker2_report.yaml',
        'task_id: Y-2\nstatus: completed\ncompleted_at: "2026-05-22T21:25:00+09:00"\n',
        3_000,
    )
    # created_at はあるが completed_at が無い review: report 側は mtime で比べる
    _write(t / 'worker4.yaml', 'task_id: Y-3\ncreated_at: "1970-01-01T00:30:00+00:00"\n', 9_000)
    _write(r / 'worker4_review.yaml', 'task_id: Y-3\nverdict: approve\n', 3_000)  # 00:50Z
    # Dispatcher が created_at を丸めて先の時刻を書いた (SQUAD-257 と同じ形): mtime の方が早ければそちらで比べる
    _write(t / 'worker5.yaml', 'task_id: Y-4\ncreated_at: "1970-01-01T01:00:00+00:00"\n', 3_000)  # 00:50Z
    _write(r / 'worker5_review.yaml', 'task_id: Y-4\nverdict: approve\nchecked_at: "x"\n', 3_300)  # 00:55Z
    assert _states(tmp_path) == {'Y-1': 'done', 'Y-2': 'active', 'Y-3': 'done', 'Y-4': 'done'}


def test_freshness_falls_back_to_mtime_without_created_at(tmp_path: Path) -> None:
    """created_at が無ければ task / report とも mtime (report の completed_at も使わない)."""
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    t, r = pj / 'tasks', pj / 'reports'
    _write(t / 'worker1.yaml', 'task_id: Z-1\n', 2_000)
    _write(r / 'worker1_report.yaml', 'task_id: Z-1\nstatus: completed\ncompleted_at: "2099-01-01T00:00:00Z"\n', 1_000)
    _write(t / 'worker2.yaml', 'task_id: Z-2\n', 2_000)
    _write(r / 'worker2_report.yaml', 'task_id: Z-2\nstatus: completed\ncompleted_at: "1970-01-01T00:00:00Z"\n', 3_000)
    assert _states(tmp_path) == {'Z-1': 'active', 'Z-2': 'done'}


@pytest.mark.usefixtures('tokyo_tz')
def test_freshness_naive_is_local_time(tmp_path: Path) -> None:
    """Naive はローカル時刻 (ここでは JST)。UTC と読むと 4 件とも逆の判定になる組み合わせ."""
    pj = tmp_path / 'queue' / 'projects' / 'pj'
    t, r = pj / 'tasks', pj / 'reports'
    cases = {
        # task naive 17:18 JST = 08:18Z < report 12:00Z
        'N-1': ('"2026-05-22T17:18:00"', '"2026-05-22T12:00:00Z"'),
        # task naive 17:18 JST = 08:18Z < report 08:30Z
        'N-2': ('"2026-05-22T17:18:00"', '"2026-05-22T08:30:00+00:00"'),
        # task 12:00Z > report naive 17:25 JST = 08:25Z
        'N-3': ('"2026-05-22T12:00:00Z"', '"2026-05-22T17:25:00"'),
        # task 08:20Z > report naive 08:25 JST = 前日 23:25Z
        'N-4': ('"2026-05-22T08:20:00Z"', '"2026-05-22T08:25:00"'),
    }
    for i, (tid, (created, completed)) in enumerate(cases.items()):
        _write(t / f'worker{i}.yaml', f'task_id: {tid}\ncreated_at: {created}\n', 2_000_000_000)
        _write(r / f'worker{i}_report.yaml', f'task_id: {tid}\nstatus: completed\ncompleted_at: {completed}\n', 2_000)
    assert _states(tmp_path) == {'N-1': 'done', 'N-2': 'done', 'N-3': 'active', 'N-4': 'active'}
