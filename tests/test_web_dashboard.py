#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/web.py (読み取り専用 Web ダッシュボード, Issue #76) のテスト.

queue は tmp に実ファイルの YAML で作り、本物の `graph.sync` で `.graph.db` を作ってから YAML を消す。
web はその DB と tmux だけで描けることを、実際に HTTP サーバーを立てて叩いて確かめる。
tmux だけは subprocess.run を差し替える (テスト環境の tmux に依存しない)。
"""

from __future__ import annotations

import ast
from datetime import datetime
from datetime import timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
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
import graph  # noqa: E402
import squad as squad_cli  # noqa: E402
import web  # noqa: E402

TOKEN = 'test-token-123'
SECRET = 'ghp_' + 'A' * 36
NOW = int(time.time())
# report の summary は Markdown で書かれ、信用できない入力でもある (実 report と同じ block scalar の形)
XSS_LINES = [
    '## 結果',
    f'- 権限が無い `{SECRET}`',
    '- <script>alert(1)</script> <img src=x onerror=alert(1)>',
    '- [x](javascript:alert(1)) <a href="javascript:alert(2)">raw</a>',
]
XSS_SUMMARY = ''.join(f'  {line}\n' for line in XSS_LINES)
DAY = 86_400


def _write(path: Path, text: str, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """実 queue と同じ形の YAML から sync した DB を作り、YAML (projects/) を消した queue.

    pj (session sq1 が担当): SQUAD-1 (archive, 今日 done) / SQUAD-1-fix1 (w1 が実行中) /
    SQUAD-2 (w2, blocked) / OLD-1 (60 日前に done)。pj2 は別 session の担当。.ruff_cache は project ではない。
    """
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(pj / '.squad_session', 'sq1\n', NOW)
    _write(pj / 'tasks' / 'archive' / 'worker1_SQUAD-1.yaml', 'task_id: SQUAD-1\ntitle: "画面を作る"\n', NOW - 600)
    _write(
        pj / 'reports' / 'archive' / 'worker1_SQUAD-1.yaml',
        'task_id: SQUAD-1\nstatus: completed\nsummary: |\n  作った\n  二行目\n',
        NOW - 500,
    )
    _write(
        pj / 'tasks' / 'worker1.yaml',
        'task_id: SQUAD-1-fix1\ntitle: "見づらいので作り直す"\nassigned_to: worker1\n'
        f'description: |\n  token {SECRET} を使う\n',
        NOW - 400,
    )
    _write(pj / 'tasks' / 'worker2.yaml', 'task_id: SQUAD-2\ntitle: "詰まる"\nassigned_to: worker2\n', NOW - 300)
    _write(
        pj / 'reports' / 'worker2_report.yaml',
        f'task_id: SQUAD-2\nstatus: blocked\nsummary: |\n{XSS_SUMMARY}',
        NOW - 200,
    )
    old = NOW - 60 * DAY
    _write(pj / 'tasks' / 'archive' / 'worker3_OLD-1.yaml', 'task_id: OLD-1\ntitle: "昔の"\n', old)
    _write(pj / 'reports' / 'archive' / 'worker3_OLD-1.yaml', 'task_id: OLD-1\nstatus: completed\n', old + 10)
    _write(q / 'projects' / 'pj2' / '.squad_session', 'other\n', NOW)
    _write(q / 'projects' / 'pj2' / 'tasks' / 'worker1.yaml', 'task_id: P2-1\nassigned_to: worker1\n', NOW - 100)
    (q / 'projects' / '.ruff_cache' / '0.1').mkdir(parents=True)
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'workers': {f'w{i}': {'agent': 'claude', 'pane': f'w{i}'} for i in (1, 2, 3)}}))
    graph.sync(q, config_path=cfg)
    # web は YAML も config.json も読まない (worker 定義は sync が DB に写す) ので、消しても同じ画面が出る
    shutil.rmtree(q / 'projects')
    cfg.unlink()
    return tmp_path


# start.sh が作る window 構成 (#95)。list-panes -s -F '#{window_name}\t#{pane_title}' の出力
SQ1_PANES = ''.join(
    f'{w}\t{t}\n'
    for w, t in (('dispatcher', 'Dispatcher'), ('w1', 'Worker1'), ('w2', 'Worker2'), ('w3', 'Worker3'))
    + (('w4', 'Worker4'), ('term', 'Terminal'), ('aux', 'Aux-Shell'))
)
# window に分ける前の旧構成: window 0 に 7 pane。window 名はプロセス名
LEGACY_PANES = ''.join(f'claude\t{t}\n' for t in ('Dispatcher', 'W1', 'W2', 'W3', 'Terminal', 'Aux-Shell', 'W4'))


def _fake_tmux(calls: list[list[str]], layout: str = SQ1_PANES, prefix: str = 'sq1:w') -> object:
    screens = {
        f'{prefix}1': '✶ Thinking… (12s · esc to interrupt)\n',
        f'{prefix}2': f'export GH={SECRET}\nDo you want to proceed?\n 1. Yes\n',
    }

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[:2] == ['tmux', 'list-sessions']:
            return subprocess.CompletedProcess(cmd, 0, 'sq1\nother\n', '')
        if cmd[:2] == ['tmux', 'list-panes']:
            # other は aux / w1 window を持つが start.sh が付けるタイトルが無い無関係なセッション
            out = layout if '=sq1:' in cmd else 'aux\tbash\nw1\tbash\n'
            return subprocess.CompletedProcess(cmd, 0, out, '')
        if cmd[:2] == ['tmux', 'capture-pane']:
            target = cmd[cmd.index('-t') + 1]
            if target == f'{prefix}3':
                return subprocess.CompletedProcess(cmd, 1, '', "can't find pane")
            return subprocess.CompletedProcess(
                cmd, 0, screens.get(target, f'idle\nAuthorization: Bearer {SECRET}\n'), ''
            )
        return subprocess.CompletedProcess(cmd, 1, '', 'unexpected')

    return run


@pytest.fixture
def server(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, list[list[str]]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, 'run', _fake_tmux(calls))  # web と squad.probe_worker の両方
    srv = web.make_server('127.0.0.1', 0, TOKEN, root / 'queue' / '.graph.db')
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


def _get_json(base: str, path: str) -> dict:
    code, body = _req(f'{base}{path}', headers={'Authorization': f'Bearer {TOKEN}'})
    assert code == 200, body
    return json.loads(body)


PATHS = [
    '/',
    '/api/sessions',
    '/api/pane?target=sq1:0.1',
    '/api/projects',
    '/api/goals',
    '/api/graph?goal=x',
    '/api/activity',
]


def test_refuses_to_start_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('SQUAD_WEB_TOKEN', raising=False)
    with pytest.raises(SystemExit) as e:
        squad_cli.main(['web', '--port', '0'])
    assert e.value.code != 0
    with pytest.raises(ValueError):
        web.make_server('127.0.0.1', 0, '', Path('g.db'))


@pytest.mark.parametrize('path', PATHS)
def test_token_required(server: tuple[str, list], path: str) -> None:
    base, _ = server
    assert _req(base + path)[0] == 401
    assert _req(base + path, headers={'Authorization': 'Bearer wrong'})[0] == 401
    sep = '&' if '?' in path else '?'
    assert _req(f'{base}{path}{sep}token=wrong')[0] == 401
    assert _req(f'{base}{path}{sep}token={TOKEN}')[0] == 200
    assert _req(base + path, headers={'Authorization': f'Bearer {TOKEN}'})[0] == 200


def test_projects_come_from_db_and_match_graph_status(server: tuple[str, list], root: Path) -> None:
    base, _ = server
    data = _get_json(base, '/api/projects')
    assert data['error'] is None
    assert data['synced_at'].endswith('+00:00')  # 画面に出す DB の最終 sync 時刻
    rows = {p['name']: p for p in data['projects']}
    assert set(rows) == {'pj', 'pj2'}  # .ruff_cache は出ない
    pj = rows['pj']
    assert (pj['session'], pj['active'], pj['blocked'], pj['done_today']) == ('sq1', 1, 1, 1)
    assert pj['latest_result']['task_id'] == 'SQUAD-2'
    assert pj['latest_result']['status'] == 'blocked'
    assert SECRET not in json.dumps(data)
    # squad graph status と同じ DB 値: running = active、blocked = blocked
    status = graph.status(root / 'queue' / '.graph.db')
    for name, row in rows.items():
        goals = status[name]['goals'].values()
        assert row['active'] == sum(g['running'] for g in goals)
        assert row['blocked'] == sum(g['blocked'] for g in goals)


def test_sessions_reuse_squad_ls_states(server: tuple[str, list]) -> None:
    base, calls = server
    data = _get_json(base, '/api/sessions')
    (sess,) = data['sessions']  # aux window があってもタイトル Aux-Shell の無い other は squad セッションではない
    assert sess['name'] == 'sq1'
    assert sess['projects'] == ['pj']  # pj2 は other の担当
    w = {r['worker']: r for r in sess['workers']}
    assert [(k, v['status'], v['label']) for k, v in w.items()] == [
        ('w1', 'busy', '作業中'),
        ('w2', 'permission_wait', '許可待ち'),
        ('w3', 'unreachable', '停止'),
    ]
    assert [(t['task_id'], t['state']) for t in w['w1']['tasks']] == [('SQUAD-1-fix1', 'running')]
    assert [(t['task_id'], t['state']) for t in w['w2']['tasks']] == [('SQUAD-2', 'blocked')]
    assert w['w3']['tasks'] == []
    assert all('last_line' not in r for r in sess['workers'])  # 可視範囲だけのマスクは返さない
    assert SECRET not in json.dumps(data)
    assert {c[1] for c in calls} <= {'list-sessions', 'list-panes', 'capture-pane'}


def test_sessions_show_ctx_source_and_age(root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Status line が書いた state の ctx を、取得元と取得時刻と一緒に返す。取れない worker は null (#105)."""
    import ctx  # noqa: PLC0415

    state = tmp_path / 'state'
    state.mkdir()
    st: dict = {}
    ctx.set_slot(st, 'statusline', session='sq1', pct=12, tokens=24000, window=200000, model='Opus 5.5')
    (state / 'w1.json').write_text(json.dumps(st))
    other: dict = {}
    ctx.set_slot(other, 'statusline', session='other', pct=80, tokens=1, window=2, model='Sonnet 5.5')
    (state / 'w2.json').write_text(json.dumps(other))
    disp: dict = {}
    ctx.set_slot(disp, 'statusline', session='sq1', pct=30, tokens=3, window=10, model='Opus 5.5')
    (state / 'dispatcher.json').write_text(json.dumps(disp))
    monkeypatch.setenv('SQUAD_STATE_DIR', str(state))
    monkeypatch.setattr(subprocess, 'run', _fake_tmux([]))

    (sess,) = web.sessions_data(root / 'queue' / '.graph.db')['sessions']

    w = {r['worker']: r for r in sess['workers']}
    assert (w['w1']['model'], w['w1']['context_pct'], w['w1']['context_source']) == ('Opus 5.5', 12, 'statusline')
    assert w['w1']['context_at'] == st['ctx_sources']['statusline']['at']
    assert (w['w2']['context_pct'], w['w2']['context_source']) == (None, None)  # 別セッションの値は使わない
    assert w['w3']['context_pct'] is None
    assert (sess['dispatcher']['context_pct'], sess['dispatcher']['context_source']) == (30, 'statusline')
    assert 'ctxCell(s.dispatcher)' in web.PAGE
    assert 'ctxCell(w)' in web.PAGE
    assert '不明' in web.PAGE


def test_legacy_pane_layout_session_still_shows(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Window に分ける前の旧構成 (window 0 に 7 pane、pane タイトル Aux-Shell) も稼働状況と pane 表示が出る (#95)."""
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, 'run', _fake_tmux(calls, LEGACY_PANES, 'sq1:0.'))
    (sess,) = web.sessions_data(root / 'queue' / '.graph.db')['sessions']
    assert [(r['pane'], r['status']) for r in sess['workers']] == [
        ('sq1:0.1', 'busy'),
        ('sq1:0.2', 'permission_wait'),
        ('sq1:0.3', 'unreachable'),
    ]
    assert web.pane_data(root / 'queue' / '.graph.db', 'sq1:0.2')['ok'] is True
    assert web.pane_data(root / 'queue' / '.graph.db', 'sq1:w2')['ok'] is False


def test_pane_opens_only_squad_worker_panes(server: tuple[str, list]) -> None:
    base, calls = server
    data = _get_json(base, '/api/pane?target=sq1:w2')
    assert data['ok'] is True
    assert 'Do you want to proceed?' in data['text']
    assert SECRET not in data['text']
    calls.clear()
    for target in ('other:w1', 'sq1:dispatcher', 'sq1:aux', '3:0.0'):
        refused = _get_json(base, f'/api/pane?target={target}')
        assert refused['ok'] is False
    assert not [c for c in calls if c[1] == 'capture-pane']


def test_goals_default_30_days_newest_first(server: tuple[str, list]) -> None:
    base, _ = server
    goals = _get_json(base, '/api/goals')['goals']
    ids = [g['node_id'] for g in goals]
    assert ids == ['goal:pj2/P2-1', 'goal:pj/SQUAD-2', 'goal:pj/SQUAD-1']  # OLD-1 は 30 日より前
    g1 = goals[2]
    assert (g1['state'], g1['tasks'], g1['title']) == ('running', 2, 'SQUAD-1 画面を作る')
    assert 'goal:pj/OLD-1' in [g['node_id'] for g in _get_json(base, '/api/goals?days=0')['goals']]


def test_graph_is_one_goal_with_detail(server: tuple[str, list]) -> None:
    base, _ = server
    data = _get_json(base, '/api/graph?goal=goal:pj/SQUAD-1')
    nodes = {n['node_id']: n for n in data['nodes']}
    assert {n['kind'] for n in nodes.values()} == {'goal', 'intent', 'result'}
    assert {k for k in nodes if k.startswith('intent:')} == {'intent:pj/SQUAD-1', 'intent:pj/SQUAD-1-fix1'}
    done = nodes['intent:pj/SQUAD-1']
    assert done['state'] == 'done'
    assert done['detail']['result_summary'] == '作った\n二行目\n'
    fix = nodes['intent:pj/SQUAD-1-fix1']
    assert fix['state'] == 'running'
    assert 'ghp_<redacted>' in fix['detail']['description']
    assert SECRET not in json.dumps(data)
    assert {(e['src'], e['rel']) for e in data['edges']} == {
        ('goal:pj/SQUAD-1', 'spawns'),
        ('intent:pj/SQUAD-1', 'yields'),
    }
    assert _get_json(base, '/api/graph?goal=none')['nodes'] == []


def test_without_db_or_with_old_schema(tmp_path: Path) -> None:
    missing = tmp_path / 'missing.db'
    data = web.projects_data(missing)
    assert data['synced_at'] is None
    assert 'sync' in data['error']
    assert not missing.exists()  # mode=ro なので作らない
    old = tmp_path / 'old.db'
    conn = sqlite3.connect(old)
    conn.execute('CREATE TABLE nodes (node_id TEXT)')
    conn.commit()
    conn.close()
    assert 'schema' in web.goals_data(old, 30)['error']


def _snapshot(root: Path) -> dict[str, tuple[float, str]]:
    return {
        str(p): (p.stat().st_mtime, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob('*'))
        if p.is_file()
    }


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS'])
def test_write_methods_rejected_and_db_unchanged(server: tuple[str, list], root: Path, method: str) -> None:
    base, _ = server
    before = _snapshot(root)
    for path in PATHS:
        code, _ = _req(base + path, method=method, headers={'Authorization': f'Bearer {TOKEN}'})
        assert code == 405
    for path in PATHS:
        _req(f'{base}{path}', headers={'Authorization': f'Bearer {TOKEN}'})
    assert _snapshot(root) == before  # .graph.db にも書かない


def test_web_reads_no_yaml_and_sends_no_keys() -> None:
    """web.py は YAML を読まず (yaml / yaml_shallow を使わない)、tmux には読み取り系だけを渡す."""
    src = (SQUAD_DIR / 'web.py').read_text()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    name = lambda c: ast.unparse(c.func)  # noqa: E731
    imported = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    assert not {'yaml', 'graph', 'yaml_shallow'} & imported
    assert 'yaml_shallow' not in src
    assert {c.args[0].value for c in calls if name(c) == '_tmux_lines'} == {
        'list-sessions',
        'list-panes',
        'capture-pane',
    }
    assert [ast.unparse(c) for c in calls if name(c).startswith('subprocess.')] == [
        "subprocess.run(['tmux', *args], capture_output=True, text=True)"
    ]
    assert 'send-keys' not in src
    assert "'?mode=ro'" in src or '?mode=ro' in src


def test_pane_masked_before_tail(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """末尾 PANE_LINES 行に BEGIN 行が入らない長い鍵でも本文が出ない (切ってからマスクしない)."""
    body = [f'KEYBODY{i:03d}' for i in range(web.PANE_LINES + 10)]
    screen = '\n'.join(['-----BEGIN RSA PRIVATE KEY-----', *body])  # pragma: allowlist secret
    calls: list[list[str]] = []

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        out = {'list-sessions': 's\n', 'list-panes': 'w1\tbash\naux\tAux-Shell\n', 'capture-pane': screen + '\n\n'}[
            cmd[1]
        ]
        return subprocess.CompletedProcess(cmd, 0, out, '')

    monkeypatch.setattr(subprocess, 'run', run)
    pane = web.pane_data(root / 'queue' / '.graph.db', 's:w1')
    (capture,) = [c for c in calls if c[1] == 'capture-pane']
    assert capture[capture.index('-S') + 1] == '-'  # 可視範囲でなく履歴全体を取る
    assert 'KEYBODY' not in pane['text']
    assert pane['text'].split('\n')[-1] == '[REDACTED PRIVATE KEY LINE]'  # 末尾の空行は落とす
    assert len(pane['text'].split('\n')) == web.PANE_LINES


def test_page_pins_cytoscape_version(server: tuple[str, list]) -> None:
    base, _ = server
    _code, body = _req(f'{base}/?token={TOKEN}')
    assert b'https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js" integrity="sha384-' in body
    assert b"name: 'preset'" in body


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


# ---------- Markdown 表示 (SQUAD-284-md) ----------


def test_body_is_masked_on_server_and_sent_as_json_data(server: tuple[str, list]) -> None:
    """本文は全文 (改行つき) をマスクしてから JSON で渡す。HTML としては返さない (nosniff)."""
    base, _ = server
    req = urllib.request.Request(f'{base}/api/projects', headers={'Authorization': f'Bearer {TOKEN}'})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers['Content-Type'] == 'application/json; charset=utf-8'
        assert r.headers['X-Content-Type-Options'] == 'nosniff'
        data = json.loads(r.read())
    summary = {p['name']: p for p in data['projects']}['pj']['latest_result']['summary']
    expected = '\n'.join(XSS_LINES).replace(SECRET, 'ghp_<redacted>') + '\n'
    assert summary == expected  # 1 行に潰さない (Markdown のリストが崩れない)、マスク済み
    graph_data = _get_json(base, '/api/graph?goal=goal:pj/SQUAD-2')
    intent = {n['node_id']: n for n in graph_data['nodes']}['intent:pj/SQUAD-2']
    assert intent['detail']['result_summary'] == expected
    assert SECRET not in json.dumps(graph_data)


def _md_script() -> str:
    """PAGE から Markdown 描画の部分 (// md:begin .. // md:end) を取り出す."""
    begin, end = web.PAGE.index('// md:begin'), web.PAGE.index('// md:end')
    return web.PAGE[begin:end]


@pytest.mark.parametrize(
    ('libs', 'expected'),
    [
        ('both', {'html': 'SAN(MD(**b**))', 'text': None}),
        ('inline', {'html': 'SAN(INLINE(**b**))', 'text': None}),
        ('no_marked', {'html': None, 'text': '**b**'}),
        ('no_purify', {'html': None, 'text': '**b**'}),
        ('throws', {'html': None, 'text': '**b**'}),
    ],
)
def test_render_markdown_falls_back_to_plain_text(libs: str, expected: dict) -> None:
    """Marked と DOMPurify が両方あるときだけ innerHTML。欠けたり例外なら textContent (今のプレーンテキスト)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node が無い')
    stubs = {
        'marked': "{parse: t => 'MD(' + t + ')', parseInline: t => 'INLINE(' + t + ')'}",
        'purify': "{sanitize: h => 'SAN(' + h + ')', addHook: () => {}}",
    }
    setup = {
        'both': f'window.marked = {stubs["marked"]}; window.DOMPurify = {stubs["purify"]};',
        'inline': f'window.marked = {stubs["marked"]}; window.DOMPurify = {stubs["purify"]};',
        'no_marked': f'window.DOMPurify = {stubs["purify"]};',
        'no_purify': f'window.marked = {stubs["marked"]};',
        'throws': f'window.marked = {{parse: () => {{ throw new Error() }}}}; window.DOMPurify = {stubs["purify"]};',
    }[libs]
    js = (
        'globalThis.window = globalThis;\n'
        + setup
        + '\n'
        + _md_script()
        + '\nconst e = {innerHTML: null, textContent: null, classList: {add() {}}, style: {}};\n'
        + f'renderMarkdown(e, "**b**", {"true" if libs == "inline" else "false"});\n'
        + 'console.log(JSON.stringify({html: e.innerHTML, text: e.textContent}));\n'
    )
    out = subprocess.run([node, '-e', js], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == expected


def test_page_loads_markdown_libs_pinned_with_sri() -> None:
    page = web.PAGE
    for url, sri in ((web.MARKED_URL, web.MARKED_SRI), (web.DOMPURIFY_URL, web.DOMPURIFY_SRI)):
        assert '@latest' not in url
        assert re.search(r'@\d+\.\d+\.\d+/', url)  # 版を URL に固定
        assert re.fullmatch(r'sha384-[A-Za-z0-9+/]{64}', sri)
        assert f'<script src="{url}" integrity="{sri}" crossorigin="anonymous"></script>' in page


def test_every_body_field_goes_through_render_markdown() -> None:
    """本文 (題名・要約・内容) を textContent で直接入れている箇所が無い. innerHTML は renderMarkdown だけ."""
    script = web.PAGE[web.PAGE.index('<script>') :]
    for expr in ('t.title', 'r.summary', 'g.title', 'n.title', 'e.title'):
        assert not re.search(r"el\('[a-z0-9]+', '[^']*', " + re.escape(expr), script), expr
    # 横パネルの本文 4 種は renderMarkdown、日付や worker 名など短い値だけ textContent
    assert "const BODY = ['result_summary', 'description', 'summary', 'evidence'];" in script
    assert "BODY.includes(k) ? renderMarkdown(el('div'), v) : el('div', '', v)" in script
    assert script.count('innerHTML') == 1
    assert 'DOMPurify.sanitize(html, PURIFY)' in _md_script()
    for call in ("renderMarkdown(el('span', ''), t.title, true)", 'r.summary', 'g.title', 'n.title, true'):
        assert call in script, call


def test_purify_forbids_remote_loads_and_dom_clobbering() -> None:
    """本文から画像等で外へ GET させない、id / name で画面の要素を乗っ取らせない、リンクは http(s) / mailto だけ."""
    md = _md_script()
    config = md[md.index('const PURIFY = {') : md.index('};', md.index('const PURIFY = {'))]
    for tag in ('img', 'svg', 'video', 'audio', 'iframe', 'object', 'embed', 'picture', 'source', 'style'):
        assert f"'{tag}'" in config.split('FORBID_ATTR')[0], tag
    for attr in ('id', 'name', 'src', 'srcset', 'style'):
        assert f"'{attr}'" in config.split('FORBID_ATTR')[1], attr
    assert 'SANITIZE_NAMED_PROPS: true' in config
    assert 'ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i' in config


def test_closed_task_is_gray_and_goal_counts_as_done(tmp_path: Path) -> None:
    """#78: archive 済みで結果の無い task は「完了 (報告なし)」として灰色、配下がそれと done だけのゴールは done."""
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(pj / 'tasks' / 'archive' / 'a.yaml', 'task_id: SQUAD-9\ntitle: "上書きで消えた"\n', NOW - 300)
    _write(pj / 'tasks' / 'archive' / 'b.yaml', 'task_id: SQUAD-9-fix1\n', NOW - 200)
    _write(pj / 'reports' / 'archive' / 'b.yaml', 'task_id: SQUAD-9-fix1\nstatus: completed\n', NOW - 100)
    _write(pj / 'tasks' / 'archive' / 'c.yaml', 'task_id: SQUAD-8\n', NOW - 300)
    graph.sync(q, config_path=tmp_path / 'none.json')
    db = q / '.graph.db'
    goals = {g['node_id']: g['state'] for g in web.goals_data(db, 0)['goals']}
    assert goals == {'goal:pj/SQUAD-9': 'done', 'goal:pj/SQUAD-8': 'closed'}
    nodes = {n['node_id']: n['state'] for n in web.graph_data(db, 'goal:pj/SQUAD-9')['nodes']}
    assert nodes['intent:pj/SQUAD-9'] == 'closed'
    # 灰色: バッジは unknown と同じ灰色の class、グラフの枠線も灰色。表示名は「完了 (報告なし)」
    assert '.closed,.unknown{background:#eaeef2;color:#57606a}' in web.PAGE
    assert "closed: '#8c959f'" in web.PAGE
    assert "STATE_LABEL = {closed: '完了 (報告なし)'}" in web.PAGE


# ---------- 活動タブ (#82) ----------


def _utc(sec: int) -> str:
    return datetime.fromtimestamp(sec, timezone.utc).isoformat(timespec='seconds')


RID = '3f2b1c8e-1111-4222-8333-444455556666'


@pytest.fixture
def activity_db(tmp_path: Path) -> Path:
    """実 queue と同じ形で、配達済みの report が reports/ と reports/archive/ の両方にある queue.

    watchd (#78) は bytes をそのまま写す。report_id を持つもの (SQUAD-5) と持たないもの (SQUAD-6) の両方。
    pj2 は別セッション・3 日前.
    """
    q = tmp_path / 'queue'
    pj = q / 'projects' / 'pj'
    _write(pj / '.squad_session', 'sq1\n', NOW)
    _write(
        pj / 'tasks' / 'archive' / 'worker1.yaml',
        f'task_id: SQUAD-5\ntitle: "画面を作る"\nassigned_to: worker1\ncreated_at: "{_utc(NOW - 3 * 3600)}"\n'
        'context:\n  parent_issue: 82\n',
        NOW - 3 * 3600,
    )
    done = (
        f'report_id: "{RID}"\ntask_id: SQUAD-5\nworker: worker1\nstatus: completed\nverify_status: pass\n'
        f'completed_at: "{_utc(NOW - 2 * 3600)}"\nsummary: |\n\n  作った {SECRET}\n  二行目\n'
    )
    _write(pj / 'reports' / 'worker1_report.yaml', done, NOW - 2 * 3600)
    _write(pj / 'reports' / 'archive' / f'SQUAD-5__{RID}.yaml', done, NOW - 2 * 3600)
    _write(
        pj / 'tasks' / 'worker2.yaml',
        f'task_id: SQUAD-6\ntitle: "詰まる"\nassigned_to: worker2\ncreated_at: "{_utc(NOW - 90 * 60)}"\n',
        NOW - 90 * 60,
    )
    blocked = (
        f'task_id: SQUAD-6\nworker: worker2\nstatus: blocked\nverify_status: fail\n'
        f'completed_at: "{_utc(NOW - 30 * 60)}"\nsummary: "権限が無い"\n'
    )
    _write(pj / 'reports' / 'worker2_report.yaml', blocked, NOW - 30 * 60)
    _write(pj / 'reports' / 'archive' / 'SQUAD-6__sha.yaml', blocked, NOW - 30 * 60)
    pj2 = q / 'projects' / 'pj2'
    _write(pj2 / '.squad_session', 'other\n', NOW)
    _write(pj2 / 'tasks' / 'archive' / 'w.yaml', 'task_id: P2-1\ntitle: "昔の"\n', NOW - 3 * DAY)
    _write(pj2 / 'reports' / 'archive' / 'w.yaml', 'task_id: P2-1\nstatus: completed\n', NOW - 3 * DAY + 60)
    graph.sync(q, config_path=tmp_path / 'none.json')
    shutil.rmtree(q / 'projects')  # web は YAML を読まない
    return q / '.graph.db'


def _events(data: dict) -> list[tuple[str, str, str]]:
    return [(e['project'], e['task_id'], e['event']) for e in data['events']]


def test_activity_newest_first_one_row_per_event(activity_db: Path) -> None:
    data = web.activity_data(activity_db, 24)
    assert data['error'] is None
    assert _events(data) == [
        ('pj', 'SQUAD-6', 'blocked'),
        ('pj', 'SQUAD-6', 'order'),
        ('pj', '', 'goal_done'),  # Issue 82 の配下 (SQUAD-5) が全部終わった時刻 = 最後の結果の時刻
        ('pj', 'SQUAD-5', 'result'),
        ('pj', 'SQUAD-5', 'order'),
    ]  # reports/ と reports/archive/ の両方にある report も 1 行。pj2 は 24 時間より前
    ats = [e['at'] for e in data['events']]
    assert ats == sorted(ats, reverse=True)
    ev = {(e['task_id'], e['event']): e for e in data['events']}
    result = ev['SQUAD-5', 'result']
    assert (result['at'], result['verify'], result['worker']) == (_utc(NOW - 2 * 3600), 'pass', 'worker1')
    assert result['session'] == 'sq1'
    assert result['title'] == '作った ghp_<redacted>'  # 要約の 1 行目 (空行は飛ばす)、マスク済み
    assert ev['SQUAD-6', 'blocked']['verify'] == 'fail'
    order = ev['SQUAD-5', 'order']
    assert (order['at'], order['worker'], order['title']) == (_utc(NOW - 3 * 3600), 'worker1', '画面を作る')
    assert ev['', 'goal_done']['at'] == _utc(NOW - 2 * 3600)
    assert SECRET not in json.dumps(data)


def test_activity_rows_point_to_graph_goal_and_node(activity_db: Path) -> None:
    for e in web.activity_data(activity_db, 0)['events']:
        nodes = {n['node_id'] for n in web.graph_data(activity_db, e['goal'])['nodes']}
        assert e['node_id'] in nodes, e  # Graph でそのゴールを開くと、そのノードがある
    ev = {(e['task_id'], e['event']): e for e in web.activity_data(activity_db, 24)['events']}
    assert (ev['SQUAD-5', 'result']['goal'], ev['SQUAD-5', 'result']['node_id']) == ('goal:pj#82', f'result:{RID}')
    assert ev['SQUAD-6', 'order']['node_id'] == 'intent:pj/SQUAD-6'


def test_activity_filters_and_goes_back_further(activity_db: Path) -> None:
    every = web.activity_data(activity_db, 0)
    assert ('pj2', 'P2-1', 'result') in _events(every)  # 「もっと見る」で遡ると出る
    assert ('pj2', 'P2-1', 'result') in _events(web.activity_data(activity_db, 168))
    assert (every['projects'], every['sessions']) == (['pj', 'pj2'], ['other', 'sq1'])
    assert {e[0] for e in _events(web.activity_data(activity_db, 0, project='pj2'))} == {'pj2'}
    by_sess = web.activity_data(activity_db, 0, session='sq1')
    assert {(e['project'], e['session']) for e in by_sess['events']} == {('pj', 'sq1')}
    assert web.activity_data(activity_db, 0, session='nobody')['events'] == []


def test_activity_api_needs_token_and_takes_filters(activity_db: Path) -> None:
    srv = web.make_server('127.0.0.1', 0, TOKEN, activity_db)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_address[1]}'
    try:
        assert _req(base + '/api/activity')[0] == 401
        data = _get_json(base, '/api/activity?hours=0&project=pj2&session=other')
        assert (data['hours'], _events(data)) == (
            0,
            [('pj2', '', 'goal_done'), ('pj2', 'P2-1', 'result'), ('pj2', 'P2-1', 'order')],
        )
        assert _get_json(base, '/api/activity?hours=x')['hours'] == 24  # 既定は直近 24 時間
        assert SECRET not in json.dumps(_get_json(base, '/api/activity?hours=0'))
    finally:
        srv.shutdown()
        srv.server_close()


def test_activity_tab_page() -> None:
    """活動タブ: 行クリックで Graph のゴールとノードを開く、5 秒更新は Graph 以外のタブと同じ、題名は Markdown."""
    page = web.PAGE
    assert '<button data-v="activity">活動</button>' in page
    assert 'tr.onclick = () => openGoal(e.goal, e.node_id)' in page
    assert 'function openGoal(g, n) { goal = g; pickNode = n; if (cy) { cy.destroy(); cy = null; }' in page
    assert 'const pick = pickNode;' in page  # drawGraph はこのノードを選ぶ
    assert "if (view === 'activity') await activity();" in page
    assert "setInterval(() => { if (view !== 'graph') refresh(); }, 5000);" in page
    assert 'const HOURS = [24, 168, 720, 0]' in page
    assert "renderMarkdown(el('span', 'sum'), e.title, true)" in page


def test_page_script_parses(tmp_path: Path) -> None:
    """PAGE は Python の文字列なので、エスケープの書き損じで JS が構文エラーになり画面ごと止まりうる."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node が無い')
    js = tmp_path / 'page.js'
    js.write_text(web.PAGE[web.PAGE.index('<script>\n') + 9 : web.PAGE.rindex('</script>')])
    subprocess.run([node, '--check', str(js)], check=True)


def _node(node_id: str, kind: str, issued: str = '') -> tuple[dict, dict]:
    return {'node_id': node_id, 'kind': kind, 'detail': {}}, {'issued_at': issued}


def test_place_orders_columns_by_derived_from_and_rows_by_issue_time() -> None:
    """実装 -> レビュー -> 修正 -> 再レビューが左から右、同じ列は発注時刻の順 (#93)."""
    found = {
        'goal:p#1': _node('goal:p#1', 'goal'),
        'intent:p/impl': _node('intent:p/impl', 'intent', '2026-01-01T00:00:00+00:00'),
        'intent:p/review': _node('intent:p/review', 'intent', '2026-01-01T01:00:00+00:00'),
        'intent:p/fix': _node('intent:p/fix', 'intent', '2026-01-01T02:00:00+00:00'),
        'intent:p/other': _node('intent:p/other', 'intent', '2026-01-01T00:30:00+00:00'),
        'result:r1': _node('result:r1', 'result'),
        'fact:f1': _node('fact:f1', 'fact'),
    }
    edges = [
        {'src': 'goal:p#1', 'rel': 'spawns', 'dst': 'intent:p/impl'},
        {'src': 'intent:p/impl', 'rel': 'yields', 'dst': 'result:r1'},
        {'src': 'result:r1', 'rel': 'yields', 'dst': 'fact:f1'},
        {'src': 'intent:p/review', 'rel': 'derived_from', 'dst': 'intent:p/impl'},
        {'src': 'intent:p/fix', 'rel': 'derived_from', 'dst': 'intent:p/review'},
    ]
    web.place_nodes(found, edges, 'goal:p#1')
    pos = {k: (v[0]['col'], v[0]['row']) for k, v in found.items()}
    assert pos['goal:p#1'] == (0, 0)
    assert [pos[k][0] for k in ('intent:p/impl', 'intent:p/review', 'intent:p/fix')] == [1, 2, 3]
    assert pos['result:r1'] == (1, 1)
    assert pos['fact:f1'] == (1, 2)  # fact は goal と重ならず result の下
    assert pos['intent:p/other'] == (1, 3)


def test_place_survives_derived_from_cycle() -> None:
    found = {k: _node(k, 'intent', k) for k in ('intent:a', 'intent:b')}
    edges = [{'src': 'intent:a', 'rel': 'derived_from', 'dst': 'intent:b'}] * 1 + [
        {'src': 'intent:b', 'rel': 'derived_from', 'dst': 'intent:a'}
    ]
    web.place_nodes(found, edges, 'goal:x')
    assert all('col' in v[0] for v in found.values())


def test_graph_gives_positions_and_one_task_goal_is_a_card(server: tuple[str, list]) -> None:
    base, _ = server
    multi = _get_json(base, '/api/graph?goal=goal:pj/SQUAD-1')
    assert multi['card'] is None
    assert all('col' in n and 'row' in n for n in multi['nodes'])
    single = _get_json(base, '/api/graph?goal=goal:pj/SQUAD-2')
    assert single['card']['node_id'] == 'intent:pj/SQUAD-2'
    assert single['card']['detail']['result_summary']  # 結果の要約もカードに入る
    assert SECRET not in json.dumps(single)


def test_page_has_readable_labels_and_card_renderer() -> None:
    page = web.PAGE
    assert "layout: {name: 'preset'" in page and 'drawCard' in page
    assert "'font-size': 17" in page


# ---------- 指示の投入 (#85) ----------

ORIGIN = 'http://127.0.0.1'
INSTR = '/api/instructions'


@pytest.fixture
def write_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """--allow-write の server。queue は pj (sq1 の担当) と pj2 (other の担当)。YAML は残す (sync し直すため)."""
    q = tmp_path / 'queue'
    _write(q / 'projects' / 'pj' / '.squad_session', 'sq1\n', NOW)
    _write(q / 'projects' / 'pj' / 'tasks' / 'worker1.yaml', 'task_id: SQUAD-1\nassigned_to: worker1\n', NOW - 60)
    _write(q / 'projects' / 'pj2' / '.squad_session', 'other\n', NOW)
    _write(q / 'projects' / 'pj2' / 'tasks' / 'worker1.yaml', 'task_id: P2-1\n', NOW - 60)
    graph.sync(q, config_path=tmp_path / 'none.json')
    monkeypatch.setattr(subprocess, 'run', _fake_tmux([]))  # squad セッションは sq1 だけ
    srv = web.make_server('127.0.0.1', 0, TOKEN, q / '.graph.db', allow_write=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_address[1]}', q
    srv.shutdown()
    srv.server_close()


def _post(base: str, payload: object, headers: dict | None = None, path: str = INSTR) -> tuple[int, bytes]:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    port = base.rsplit(':', 1)[1]
    h = {'Authorization': f'Bearer {TOKEN}', 'Origin': f'{ORIGIN}:{port}', 'Content-Type': 'application/json'}
    h.update(headers or {})
    req = urllib.request.Request(base + path, method='POST', headers={k: v for k, v in h.items() if v}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _instruction_files(q: Path) -> list[Path]:
    return sorted((q / 'instructions').rglob('*')) if (q / 'instructions').exists() else []


def test_read_only_server_rejects_instructions_and_has_no_form(server: tuple[str, list], root: Path) -> None:
    """--allow-write 無しは今と同じ読み取り専用: 正しいトークン・Origin でも POST は 405、入力欄もコードも無い."""
    base, _ = server
    assert _post(base, {'session': 'sq1', 'body': 'x'})[0] == 405
    assert not (root / 'queue' / 'instructions').exists()
    _, page = _req(base + '/', headers={'Authorization': f'Bearer {TOKEN}'})
    assert b'id="instr"' not in page and b'loadTargets' not in page and b'/api/instructions' not in page
    assert _req(base + '/api/targets', headers={'Authorization': f'Bearer {TOKEN}'})[0] == 404


def test_post_writes_one_file_per_instruction(write_server: tuple[str, Path]) -> None:
    base, q = write_server
    before = {p: p.read_bytes() for p in (q / 'projects').rglob('*') if p.is_file()}
    code, body = _post(base, {'session': 'sq1', 'project': 'pj', 'body': '  PR #9 を見て\n2 行目  '})
    assert code == 201, body
    iid = json.loads(body)['id']
    assert _post(base, {'session': 'sq1', 'body': 'project なし'})[0] == 201
    files = sorted((q / 'instructions' / 'sq1').iterdir())
    assert [p.name for p in files][0] == f'{iid}.json' and len(files) == 2  # 一時ファイルは残らない
    data = json.loads(files[0].read_text())
    assert data['session'] == 'sq1' and data['project'] == 'pj' and data['delivered_at'] == ''
    assert data['body'] == 'PR #9 を見て\n2 行目'
    assert re.fullmatch(r'\d{8}T\d{12}Z-[0-9a-f]{8}', iid)  # ファイル名は自前で作る (入力から作らない)
    # tasks/ reports/ には書かない
    assert {p: p.read_bytes() for p in (q / 'projects').rglob('*') if p.is_file()} == before


def test_post_token_only_in_authorization_header(write_server: tuple[str, Path]) -> None:
    base, q = write_server
    ok = {'session': 'sq1', 'body': 'x'}
    assert _post(base, ok, {'Authorization': ''})[0] == 401
    assert _post(base, ok, {'Authorization': 'Bearer wrong'})[0] == 401
    assert _post(base, ok, {'Authorization': ''}, path=f'/api/instructions?token={TOKEN}')[0] == 401
    assert _post(base, ok, {'Authorization': '', 'Cookie': f'token={TOKEN}'})[0] == 401
    assert _instruction_files(q) == []


def test_post_checks_origin(write_server: tuple[str, Path]) -> None:
    base, q = write_server
    ok = {'session': 'sq1', 'body': 'x'}
    assert _post(base, ok, {'Origin': 'http://evil.example'})[0] == 403
    assert _post(base, ok, {'Origin': 'http://127.0.0.1:1'})[0] == 403  # port 違い
    assert _post(base, ok, {'Origin': ''})[0] == 403  # Origin 無し
    assert _instruction_files(q) == []


def test_post_rejects_bad_input_without_echoing_it(write_server: tuple[str, Path]) -> None:
    base, q = write_server
    cases = [
        ({'session': 'sq1', 'body': 'あ' * (web.MAX_BODY_CHARS + 1)}, 413),
        (b'{"session": "sq1", "body": "' + b'x' * web.MAX_REQUEST_BYTES + b'"}', 413),
        ({'session': 'sq1', 'body': '   '}, 400),
        ({'session': 'sq1'}, 400),
        ({'session': 'other', 'body': SECRET}, 400),  # squad セッションでない
        ({'session': '../sq1', 'body': SECRET}, 400),
        ({'session': 'sq1', 'project': 'pj2', 'body': SECRET}, 400),  # 別セッションの担当
        ({'session': 'sq1', 'project': '../../projects/pj/tasks', 'body': SECRET}, 400),
        ({'session': ['sq1'], 'body': 'x'}, 400),
        (b'not json', 400),
        (b'[1]', 400),
    ]
    for payload, expected in cases:
        code, body = _post(base, payload)
        assert code == expected, (payload, body)
        assert SECRET.encode() not in body and TOKEN.encode() not in body
    assert _post(base, {'session': 'sq1', 'body': 'あ' * web.MAX_BODY_CHARS})[0] == 201  # 上限ちょうどは通す
    assert len(_instruction_files(q)) == 2  # sq1/ と 1 件だけ


def test_targets_and_form_only_with_allow_write(write_server: tuple[str, Path]) -> None:
    base, _ = write_server
    assert _get_json(base, '/api/targets')['sessions'] == {'sq1': ['pj']}
    _, page = _req(base + '/', headers={'Authorization': f'Bearer {TOKEN}'})
    assert b'<form id="instr">' in page and b"fetch('/api/instructions'" in page


def test_sent_instruction_shows_in_activity(write_server: tuple[str, Path]) -> None:
    """Graph sync が指示を読み込み、活動タブに「指示」として時系列で出る (配達済みかどうかも)."""
    base, q = write_server
    _, body = _post(base, {'session': 'sq1', 'project': 'pj', 'body': f'見て {SECRET}'})
    path = q / 'instructions' / 'sq1' / f'{json.loads(body)["id"]}.json'
    graph.sync(q, config_path=q / 'none.json')
    ev = [e for e in _get_json(base, '/api/activity?hours=24')['events'] if e['event'] == 'instruction']
    assert len(ev) == 1 and ev[0]['state'] == 'pending' and ev[0]['project'] == 'pj' and ev[0]['session'] == 'sq1'
    assert SECRET not in ev[0]['title'] and ev[0]['title'].startswith('見て ')  # 画面に出す前にマスク
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, 'delivered_at': '2099-01-01T00:00:00+00:00'}))  # watchd が渡した
    graph.sync(q, config_path=q / 'none.json')
    data = _get_json(base, '/api/activity?hours=24&session=sq1&project=pj')
    ev = [e for e in data['events'] if e['event'] == 'instruction']
    assert ev[0]['state'] == 'delivered' and ev[0]['delivered_at'].startswith('2099')
    assert [e['event'] for e in data['events']] == ['instruction', 'order']  # 新しい順
    assert _get_json(base, '/api/activity?hours=24&project=pj2')['events'][0]['event'] == 'order'


def test_activity_works_on_db_without_instructions_table(activity_db: Path) -> None:
    conn = sqlite3.connect(activity_db)
    conn.execute('DROP TABLE instructions')
    conn.commit()
    conn.close()
    assert web.activity_data(activity_db, 0)['error'] is None


def test_write_page_script_parses(tmp_path: Path) -> None:
    node = shutil.which('node')
    if not node:
        pytest.skip('node が無い')
    page = web.PAGE_WRITE
    assert not re.search(r'<!--WRITE_|/\*WRITE_', page)
    js = tmp_path / 'page.js'
    js.write_text(page[page.index('<script>\n') + 9 : page.rindex('</script>')])
    subprocess.run([node, '--check', str(js)], check=True)


def test_does_not_write_through_symlinked_instructions_dir(write_server: tuple[str, Path]) -> None:
    """instructions/<session> が tasks/ を指す symlink でも、そこには書かない (500)."""
    base, q = write_server
    (q / 'instructions').mkdir()
    (q / 'instructions' / 'sq1').symlink_to(q / 'projects' / 'pj' / 'tasks')
    before = sorted((q / 'projects' / 'pj' / 'tasks').iterdir())
    code, body = _post(base, {'session': 'sq1', 'body': SECRET})
    assert code == 500 and SECRET.encode() not in body
    assert sorted((q / 'projects' / 'pj' / 'tasks').iterdir()) == before
