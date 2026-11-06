#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/web.py (読み取り専用 Web ダッシュボード, Issue #76) のテスト.

queue は tmp に実ファイルの YAML で作り、本物の `graph.sync` で `.graph.db` を作ってから YAML を消す。
web はその DB と tmux だけで描けることを、実際に HTTP サーバーを立てて叩いて確かめる。
tmux だけは subprocess.run を差し替える (テスト環境の tmux に依存しない)。
"""

from __future__ import annotations

import ast
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
    cfg.write_text(json.dumps({'workers': {f'w{i}': {'agent': 'claude', 'pane': f'0.{i}'} for i in (1, 2, 3)}}))
    graph.sync(q, config_path=cfg)
    # web は YAML も config.json も読まない (worker 定義は sync が DB に写す) ので、消しても同じ画面が出る
    shutil.rmtree(q / 'projects')
    cfg.unlink()
    return tmp_path


def _fake_tmux(calls: list[list[str]]) -> object:
    screens = {
        'sq1:0.1': 'Thinking…\n',
        'sq1:0.2': f'export GH={SECRET}\nDo you want to proceed?\n 1. Yes\n',
    }

    def run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if cmd[:2] == ['tmux', 'list-sessions']:
            return subprocess.CompletedProcess(cmd, 0, 'sq1\nother\n', '')
        if cmd[:2] == ['tmux', 'list-panes']:
            out = 'Dispatcher\nAux-Shell\n' if 'sq1' in cmd else 'bash\n'
            return subprocess.CompletedProcess(cmd, 0, out, '')
        if cmd[:2] == ['tmux', 'capture-pane']:
            target = cmd[cmd.index('-t') + 1]
            if target == 'sq1:0.3':
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


PATHS = ['/', '/api/sessions', '/api/pane?target=sq1:0.1', '/api/projects', '/api/goals', '/api/graph?goal=x']


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
    (sess,) = data['sessions']  # Aux-Shell の無い other は squad セッションではない
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


def test_pane_opens_only_squad_worker_panes(server: tuple[str, list]) -> None:
    base, calls = server
    data = _get_json(base, '/api/pane?target=sq1:0.2')
    assert data['ok'] is True
    assert 'Do you want to proceed?' in data['text']
    assert SECRET not in data['text']
    calls.clear()
    for target in ('other:0.1', 'sq1:0.0', '3:0.0'):
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
        out = {'list-sessions': 's\n', 'list-panes': 'Aux-Shell\n', 'capture-pane': screen + '\n\n'}[cmd[1]]
        return subprocess.CompletedProcess(cmd, 0, out, '')

    monkeypatch.setattr(subprocess, 'run', run)
    pane = web.pane_data(root / 'queue' / '.graph.db', 's:0.1')
    (capture,) = [c for c in calls if c[1] == 'capture-pane']
    assert capture[capture.index('-S') + 1] == '-'  # 可視範囲でなく履歴全体を取る
    assert 'KEYBODY' not in pane['text']
    assert pane['text'].split('\n')[-1] == '[REDACTED PRIVATE KEY LINE]'  # 末尾の空行は落とす
    assert len(pane['text'].split('\n')) == web.PANE_LINES


def test_page_pins_cytoscape_version(server: tuple[str, list]) -> None:
    base, _ = server
    _code, body = _req(f'{base}/?token={TOKEN}')
    assert b'https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js" integrity="sha384-' in body
    assert b"name: 'breadthfirst'" in body


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
    for expr in ('t.title', 'r.summary', 'g.title', 'n.title'):
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
