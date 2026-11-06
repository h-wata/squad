#!/usr/bin/env python3
# ruff: noqa: CPY001
"""全 project の進捗を 1 画面で見る読み取り専用 Web ダッシュボード (Issue #76, stdlib only).

正本は YAML (ADR 0007) なので、画面は読むだけ:
  - YAML / dashboards/*.md は読むだけ、graph DB は `mode=ro` で開く (無ければ作らない)
  - tmux は list-sessions / list-panes / capture-pane だけを呼ぶ (キー入力は送らない)
  - GET 以外は 405。全リクエストでトークンを検査する (未設定なら起動しない)
  - pane の中身と YAML 由来の文字列は squad/mask.py を通してから返す

squad セッションの判定: start.sh が pane 0.5 に付けるタイトル "Aux-Shell" を持つセッション。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import hmac
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from mask import mask
from squad import yaml_shallow

SQUAD_PANE_TITLE = 'Aux-Shell'
PANE_LINES = 40


def iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds')


def _masked(obj: Any) -> Any:  # noqa: ANN401
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {k: _masked(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_masked(v) for v in obj]
    return obj


# ---------- project 一覧 ----------


def parse_iso8601(value: str) -> datetime | None:
    """ISO8601 を UTC の aware datetime にする. scripts/measure_task_cost.py の同名関数と同じ扱い.

    Z 終端は +00:00、オフセット無し (naive) は worker が手元の時計で書いた値なのでローカル時刻として読む。
    """
    text = (value or '').strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


# 同じ task_id に複数の結果があるときの優先順: worker の report > cross-review > verifier の verdict
_RESULT_RANK = {'status': 2, 'verdict': 1, 'result': 0}


def _result_state(meta: dict[str, str]) -> tuple[int, str] | None:
    """結果ファイル 1 つから (優先度, state)。report / review / verdict のどれでもなければ None."""
    if 'status' in meta:
        return _RESULT_RANK['status'], 'done' if meta['status'] == 'completed' else 'blocked'
    if 'verdict' in meta:  # cross-review は approve でも request_changes でも、書いた時点で task は終わり
        return _RESULT_RANK['verdict'], 'done'
    if 'result' in meta:
        return _RESULT_RANK['result'], 'done' if meta['result'] == 'pass' else 'blocked'
    return None


def projects_data(queue_dir: Path, dashboards_dir: Path) -> dict[str, Any]:
    """tasks/ 直下の task と reports/ の結果を task_id で突き合わせて active / blocked / done を決める.

    結果は `<task>_report.yaml` に限らず worker4_review*.yaml や workerN_verdict.yaml にもあるので、
    reports/*.yaml を全部読む。task より古い結果は数えない (同じ task_id の再発注を done にしない)。
    新旧は task の発注時刻と report の completed_at (無ければ report の mtime) で比べる。発注時刻は task の
    created_at と mtime の早い方。どちらも実際の発注より後ろにしかずれないため: mtime は一括更新される
    ことがあり (mesh-mem の tasks/ は 2026-07-02 に全部更新された)、created_at は Dispatcher が丸めて
    書くので実際の発注より後になることがある (SQUAD-257 は created_at 13:10、発注 13:04、review 13:09)。
    created_at が無いときは task / report とも mtime で比べる。
    """
    out = []
    for pj_dir in sorted(p for p in (queue_dir / 'projects').glob('*') if p.is_dir()):
        reports = {p.name: p for p in (pj_dir / 'reports').glob('*.yaml')}
        results: dict[str, list[tuple[datetime, datetime, int, str]]] = {}
        for rep_path in reports.values():
            rep = yaml_shallow(rep_path)
            if rep.get('task_id') and (st := _result_state(rep)):
                mtime = datetime.fromtimestamp(rep_path.stat().st_mtime, timezone.utc)
                completed = parse_iso8601(rep.get('completed_at', '')) or mtime
                results.setdefault(rep['task_id'], []).append((mtime, completed, *st))
        tasks = []
        for task in sorted((pj_dir / 'tasks').glob('*.yaml')):
            meta = yaml_shallow(task)
            worker = meta.get('assigned_to') or task.stem
            issued = task.stat().st_mtime
            task_mtime = datetime.fromtimestamp(issued, timezone.utc)
            created = parse_iso8601(meta.get('created_at', ''))
            created = min(created, task_mtime) if created else None
            found = [
                (rank, mtime, st)
                for mtime, completed, rank, st in results.get(meta.get('task_id', ''), [])
                if (completed >= created if created else mtime >= task_mtime)
            ]
            state = max(found)[2] if found else 'active'
            tasks.append(
                {
                    'task_id': meta.get('task_id', ''),
                    'title': meta.get('title', ''),
                    'worker': worker,
                    'state': state,
                    'issued_at': iso(issued),
                }
            )
        latest = max(reports.values(), key=lambda p: p.stat().st_mtime, default=None)
        latest_report = None
        if latest:
            rep = yaml_shallow(latest)
            latest_report = {
                'file': latest.name,
                'task_id': rep.get('task_id', ''),
                'status': rep.get('status', ''),
                'summary': mask(rep.get('summary', ''))[:200],  # 切ってからだと token の一部が残る
                'mtime': iso(latest.stat().st_mtime),
            }
        dash = dashboards_dir / f'{pj_dir.name}.md'
        out.append(
            {
                'name': pj_dir.name,
                'tasks': tasks,
                'latest_report': latest_report,
                'dashboard_mtime': iso(dash.stat().st_mtime) if dash.exists() else None,
            }
        )
    return _masked({'projects': out})


# ---------- tmux pane ----------


def _tmux_lines(*args: str) -> list[str] | None:
    """Tmux の出力行。失敗したら None."""
    r = subprocess.run(['tmux', *args], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else None


def panes_data() -> dict[str, Any]:
    sessions = []
    for name in _tmux_lines('list-sessions', '-F', '#{session_name}') or []:
        rows = [
            ln.split('\t', 1)
            for ln in _tmux_lines(
                'list-panes', '-s', '-t', name, '-F', '#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}'
            )
            or []
        ]
        if not any(len(r) == 2 and r[1] == SQUAD_PANE_TITLE for r in rows):
            continue
        panes = []
        for target, title in (r for r in rows if len(r) == 2):
            # 履歴全体 (-S -) をマスクしてから末尾を切る。可視範囲や末尾だけをマスクすると、PRIVATE KEY の
            # BEGIN 行が範囲外に出て本文が素通しになる。ponytail: history-limit を超えて捨てられた BEGIN 行は救えない
            lines = _tmux_lines('capture-pane', '-p', '-S', '-', '-t', target)
            text = mask('\n'.join(lines or []).rstrip('\n'))
            panes.append(
                {
                    'target': target,
                    'title': title,
                    'ok': lines is not None,
                    'text': '\n'.join(text.split('\n')[-PANE_LINES:]),
                }
            )
        sessions.append({'name': name, 'panes': panes})
    return _masked({'sessions': sessions})


# ---------- 探索グラフ ----------


def graph_data(db_path: Path, project: str) -> dict[str, Any]:
    if not db_path.exists():
        return {'nodes': [], 'edges': []}
    conn = sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)
    try:
        nodes = [
            dict(zip(('node_id', 'kind', 'state', 'title', 'updated_at'), r))
            for r in conn.execute(
                'SELECT node_id, kind, state, title, updated_at FROM nodes WHERE project = ? ORDER BY node_id',
                (project,),
            )
        ]
        ids = {n['node_id'] for n in nodes}
        edges = [
            dict(zip(('src', 'rel', 'dst'), r))
            for r in conn.execute('SELECT src_id, rel, dst_id FROM edges ORDER BY src_id, dst_id')
            if r[0] in ids and r[2] in ids
        ]
    except sqlite3.Error:
        return {'nodes': [], 'edges': []}
    finally:
        conn.close()
    return _masked({'nodes': nodes, 'edges': edges})


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    """GET のみ受け付ける。設定は make_server が server 属性に載せる."""

    def _send(self, code: int, body: bytes, ctype: str = 'application/json; charset=utf-8') -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        header = self.headers.get('Authorization', '')
        given = header[7:] if header.startswith('Bearer ') else (query.get('token') or [''])[0]
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        if not self._authorized(query):
            self._send(401, b'{"error": "unauthorized"}')
            return
        srv = self.server
        if url.path == '/':
            self._send(200, PAGE.encode(), 'text/html; charset=utf-8')
            return
        if url.path == '/api/projects':
            data = projects_data(srv.queue_dir, srv.dashboards_dir)
        elif url.path == '/api/panes':
            data = panes_data()
        elif url.path == '/api/graph':
            data = graph_data(srv.queue_dir / '.graph.db', (query.get('project') or [''])[0])
        else:
            self._send(404, b'{"error": "not found"}')
            return
        self._send(200, json.dumps(data, ensure_ascii=False).encode())

    def _reject(self) -> None:
        self._send(405, b'{"error": "read only"}')

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _reject  # noqa: N815

    def log_message(self, fmt: str, *args: object) -> None:
        """クエリ (token を含みうる) をログに出さない."""
        if args and isinstance(args[0], str):
            args = (args[0].split('?', 1)[0], *args[1:])
        super().log_message(fmt, *args)


def make_server(host: str, port: int, token: str, queue_dir: Path, dashboards_dir: Path) -> ThreadingHTTPServer:
    if not token:
        raise ValueError('token is required')
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.token = token
    srv.queue_dir = queue_dir
    srv.dashboards_dir = dashboards_dir
    return srv


PAGE = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>squad dashboard</title>
<style>
body{font-family:sans-serif;margin:0;background:#f4f4f4;color:#222}
nav{background:#222;padding:.4em}nav button{margin-right:.3em}
section{padding:.6em}.row{display:flex;gap:.6em;overflow-x:auto;align-items:flex-start}
.card{background:#fff;border-radius:6px;padding:.6em;min-width:260px;max-width:340px;flex:0 0 auto}
.card h3{margin:.2em 0}.small{font-size:.8em;color:#666}
.active{color:#06c}.blocked{color:#c00;font-weight:bold}.done{color:#888}
pre{background:#111;color:#ddd;padding:.4em;font-size:11px;overflow:auto;max-height:22em;white-space:pre}
svg text{font-size:11px}
</style></head><body>
<nav><button data-v="projects">Projects</button><button data-v="panes">Workers</button>
<button data-v="graph">Graph</button> <select id="pj"></select> <span id="ts" class="small"></span></nav>
<section id="projects"></section><section id="panes" hidden></section><section id="graph" hidden></section>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const H = {headers: {Authorization: 'Bearer ' + token}};
const api = p => fetch(p, H).then(r => r.json());
const el = (tag, cls, text) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
let view = 'projects';
document.querySelectorAll('nav button').forEach(b => b.onclick = () => { view = b.dataset.v;
  for (const s of document.querySelectorAll('section')) s.hidden = s.id !== view; refresh(); });
document.getElementById('pj').onchange = () => refresh();

async function projects() {
  const d = await api('/api/projects'), root = document.getElementById('projects');
  const sel = document.getElementById('pj');
  const row = el('div', 'row');
  const cur = sel.value; sel.replaceChildren();
  for (const p of d.projects) {
    sel.append(new Option(p.name, p.name, false, p.name === cur));
    const c = el('div', 'card'); c.append(el('h3', '', p.name));
    for (const t of p.tasks.filter(t => t.state !== 'done'))
      c.append(el('div', t.state, `[${t.state}] ${t.worker} ${t.task_id} ${t.title}`));
    const r = p.latest_report;
    if (r) c.append(el('div', 'small', `report: ${r.task_id} ${r.status} (${r.mtime}) ${r.summary}`));
    c.append(el('div', 'small', `dashboard: ${p.dashboard_mtime || '-'}`));
    row.append(c);
  }
  root.replaceChildren(row);
}

async function panes() {
  const d = await api('/api/panes'), root = document.getElementById('panes');
  root.replaceChildren();
  if (!d.sessions.length) root.append(el('p', '', 'squad セッションが見つかりません'));
  for (const s of d.sessions) {
    root.append(el('h3', '', s.name)); const row = el('div', 'row');
    for (const p of s.panes) { const c = el('div', 'card'); c.append(el('div', 'small', `${p.target} ${p.title}`));
      c.append(el('pre', '', p.ok ? p.text : '(capture-pane に失敗)')); row.append(c); }
    root.append(row);
  }
}

const KINDS = ['goal', 'intent', 'result', 'fact'];
async function graph() {
  const pj = document.getElementById('pj').value, root = document.getElementById('graph');
  if (!pj) return;
  const d = await api('/api/graph?project=' + encodeURIComponent(pj));
  const NS = 'http://www.w3.org/2000/svg', W = 260, RH = 18, pos = {}, cnt = [0, 0, 0, 0];
  for (const n of d.nodes) { const k = KINDS.indexOf(n.kind); if (k < 0) continue;
    pos[n.node_id] = [k * W + 10, cnt[k]++ * RH + 30, n]; }
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('width', KINDS.length * W); svg.setAttribute('height', Math.max(...cnt) * RH + 40);
  const add = (tag, attrs, text) => { const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; svg.append(e); return e; };
  KINDS.forEach((k, i) => add('text', {x: i * W + 10, y: 14, 'font-weight': 'bold'}, k));
  for (const e of d.edges) { const a = pos[e.src], b = pos[e.dst]; if (!a || !b) continue;
    add('line', {x1: a[0] + W - 20, y1: a[1] - 4, x2: b[0], y2: b[1] - 4, stroke: '#aaa'}); }
  for (const id in pos) { const [x, y, n] = pos[id];
    add('text', {x, y, class: n.state}, `${id.split(/[:/]/).pop().slice(0, 24)} ${n.state}`)
      .append(Object.assign(document.createElementNS(NS, 'title'), {textContent: `${id}\\n${n.title}`})); }
  root.replaceChildren(svg);
}

async function refresh() {
  try { if (view === 'projects' || !document.getElementById('pj').value) await projects();
    if (view === 'panes') await panes(); if (view === 'graph') await graph();
    document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  } catch (e) { document.getElementById('ts').textContent = 'error: ' + e; }
}
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""
