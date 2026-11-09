#!/usr/bin/env python3
# ruff: noqa: CPY001
"""全 project の進捗を 1 画面で見る読み取り専用 Web ダッシュボード (Issue #76, stdlib only).

読むのは `queue/.graph.db` と tmux だけ。task / report YAML は読まない:
  - 状態 (running / blocked / done ...)、ゴールの組み分け、project 一覧は `squad graph sync` が DB に
    入れたものをそのまま出す。`squad graph status` と同じ DB の値なので、2 つの判定は一致する
  - DB は `mode=ro` で開く (無ければ作らない、書かない)。sync は今どおり watchd の仕事
  - tmux は list-sessions / list-panes / capture-pane だけを呼ぶ (キー入力は送らない)。worker の状態は
    `squad ls` と同じ squad.probe_worker で判定する
  - GET 以外は 405。全リクエストでトークンを検査する (未設定なら起動しない)
  - 例外は `--allow-write` で起動したときの `POST /api/instructions` だけ (#85)。Dispatcher への指示を
    `queue/instructions/<session>/` に 1 件 1 ファイルで書く (tmux には送らない。打ち込むのは watchd)。
    トークンは Authorization ヘッダだけ、Origin は自分自身だけ、本文は MAX_BODY_CHARS まで
  - pane の中身と DB 由来の文字列は squad/mask.py を通してから返す

squad セッションの判定: start.sh が作る window "aux" があり、その pane タイトルが "Aux-Shell" のセッション (#95)。
window に分ける前の旧構成 (aux window が無い) は pane タイトル "Aux-Shell" だけで見分ける。
worker の pane は squad/config.json の window 名 (例 "w1") をセッション名に付けたもの (squad ls と同じ)。
旧構成のセッションでは squad.tmux_target が "0.1" 等に読み替える。
config.json は graph sync が DB (meta.workers) に写したものを読む (web 自身はファイルを開かない)。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hmac
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlsplit

from mask import mask
import squad

SQUAD_WINDOW = 'aux'
PANE_TITLE = 'Aux-Shell'  # start.sh が aux window (旧構成は pane 0.5) に付けるタイトル
PANE_LINES = 300
STATUS_LABEL = {'busy': '作業中', 'idle': '待機中', 'permission_wait': '許可待ち', 'unreachable': '停止'}
DONE = ('done', 'verified')
EVENT_ORDER = {'instruction': -1, 'order': 0, 'result': 1, 'blocked': 1, 'goal_done': 2}
# 指示 (#85): 本文の上限と、リクエスト全体 (JSON) の上限。日本語 1 文字は UTF-8 で 3 byte、JSON の
# エスケープ (\uXXXX) なら 6 byte になるので、本文の上限の 6 倍 + 余裕を取る
MAX_BODY_CHARS = 4000
MAX_REQUEST_BYTES = MAX_BODY_CHARS * 6 + 1024
# 書き込み先のディレクトリ名になるので、tmux のセッション一覧と完全一致したうえでこの形に限る
SESSION_RE = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}')
DETAIL_KEYS = (
    'assigned_to',
    'created_at',
    'finished_at',
    'description',
    'verify_status',
    'review',
    'summary',
    'pr_url',
    'worker',
    'evidence',
)


def _masked(obj: Any) -> Any:  # noqa: ANN401
    if isinstance(obj, str):
        return mask(obj)
    if isinstance(obj, dict):
        return {k: _masked(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_masked(v) for v in obj]
    return obj


# ---------- graph DB (read-only) ----------


class NotSyncedError(Exception):
    """DB が無い、または schema が古い (squad graph sync がまだ)."""


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise NotSyncedError(f'{db_path.name} がありません (squad graph sync が必要)')
    conn = sqlite3.connect(f'{db_path.resolve().as_uri()}?mode=ro', uri=True)
    try:
        conn.execute('SELECT 1 FROM projects, meta LIMIT 0')
    except sqlite3.Error as e:
        conn.close()
        raise NotSyncedError(f'{db_path.name} の schema が古い (squad graph sync が必要): {e}') from e
    return conn


def _query(db_path: Path, fn: Callable[[sqlite3.Connection], dict[str, Any]]) -> dict[str, Any]:
    """DB を読み、synced_at を添えてマスクして返す。DB が使えなければ error を返す (画面に出す)."""
    try:
        conn = _open(db_path)
    except NotSyncedError as e:
        return {'synced_at': None, 'error': str(e)}
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'synced_at'").fetchone()
        return _masked({'synced_at': row[0] if row else None, 'error': None, **fn(conn)})
    finally:
        conn.close()


def _payload(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except ValueError:
        return {}


def _worker_key(name: str) -> str:
    """'w1' / 'worker1' / 'W1' を同じ worker として比べる."""
    m = re.search(r'(\d+)$', name or '')
    return f'w{m.group(1)}' if m else (name or '').lower()


def _is_today(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso).astimezone().date() == datetime.now().astimezone().date()
    except ValueError:
        return False


def projects_data(db_path: Path) -> dict[str, Any]:
    """1 project 1 行: active / blocked / 今日終わった件数、最新の結果、最終更新."""

    def fn(conn: sqlite3.Connection) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {}
        for pj, state, payload in conn.execute("SELECT project, state, payload_json FROM nodes WHERE kind = 'intent'"):
            c = counts.setdefault(pj, {'active': 0, 'blocked': 0, 'done_today': 0})
            if state == 'running':
                c['active'] += 1
            elif state == 'blocked':
                c['blocked'] += 1
            elif state in DONE and _is_today(_payload(payload).get('finished_at', '')):
                c['done_today'] += 1
        out = []
        for pj, session, latest, updated_at in conn.execute(
            'SELECT project, session, latest_result, updated_at FROM projects ORDER BY updated_at DESC, project'
        ):
            row = conn.execute('SELECT state, title, payload_json FROM nodes WHERE node_id = ?', (latest,)).fetchone()
            result = None
            if row:
                p = _payload(row[2])
                result = {
                    'node_id': latest,
                    'task_id': p.get('task_id', ''),
                    'status': row[0],
                    'summary': p.get('summary') or row[1],  # 全文 (Markdown)。title は 1 行に潰した版
                    'at': p.get('reported_at', ''),
                }
            out.append(
                {
                    'name': pj,
                    'session': session,
                    **counts.get(pj, {'active': 0, 'blocked': 0, 'done_today': 0}),
                    'latest_result': result,
                    'updated_at': updated_at,
                }
            )
        return {'projects': out}

    return _query(db_path, fn)


def goals_data(db_path: Path, days: int) -> dict[str, Any]:
    """ゴール一覧 (新しい順)。days > 0 なら最終更新がその日数以内のものだけ."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds') if days > 0 else ''

    def fn(conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(
            'SELECT node_id, project, state, title, payload_json, updated_at FROM nodes '
            "WHERE kind = 'goal' AND updated_at >= ? ORDER BY updated_at DESC, node_id",
            (since,),
        )
        goals = [
            {
                'node_id': node_id,
                'project': project,
                'state': state,
                'title': title,
                'tasks': len(_payload(payload).get('members', [])),
                'updated_at': updated_at,
            }
            for node_id, project, state, title, payload, updated_at in rows
        ]
        return {'days': days, 'goals': goals}

    return _query(db_path, fn)


def graph_data(db_path: Path, goal: str) -> dict[str, Any]:
    """ゴール 1 つ分: goal -> intent -> result -> fact。ノードはクリック時の横パネル用の中身を持つ."""

    def fn(conn: sqlite3.Connection) -> dict[str, Any]:
        ids, frontier = {goal}, {goal}
        edges: set[tuple[str, str, str]] = set()
        while frontier:  # spawns / yields を下る。derived_from は辿らない (他のゴールへ広がるため)
            marks = ','.join('?' * len(frontier))
            rows = conn.execute(
                f"SELECT src_id, rel, dst_id FROM edges WHERE rel != 'derived_from' AND src_id IN ({marks})",  # noqa: S608
                sorted(frontier),
            ).fetchall()
            edges.update(rows)
            frontier = {r[2] for r in rows} - ids
            ids |= frontier
        marks = ','.join('?' * len(ids))
        found: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}  # node_id -> (画面用, payload)
        for node_id, kind, state, title, payload, updated_at in conn.execute(
            f'SELECT node_id, kind, state, title, payload_json, updated_at FROM nodes WHERE node_id IN ({marks})',  # noqa: S608
            sorted(ids),
        ):
            p = _payload(payload)
            node = {'node_id': node_id, 'kind': kind, 'state': state, 'title': title, 'updated_at': updated_at}
            found[node_id] = (node | {'detail': {k: p[k] for k in DETAIL_KEYS if p.get(k)}}, p)
        for node, p in found.values():  # intent の横パネルに最新 result の要約も出す
            latest = found.get(p.get('latest_result', ''))
            if node['kind'] == 'intent' and latest and latest[1].get('summary'):
                node['detail']['result_summary'] = latest[1]['summary']
        edges.update(
            conn.execute(
                f"SELECT src_id, rel, dst_id FROM edges WHERE rel = 'derived_from' AND src_id IN ({marks})",  # noqa: S608
                sorted(ids),
            ).fetchall()
        )
        out_edges = [{'src': s, 'rel': r, 'dst': d} for s, r, d in sorted(edges) if s in found and d in found]
        place_nodes(found, out_edges, goal)
        intents = [n for n, _ in found.values() if n['kind'] == 'intent']
        # task が 1 件のゴールは図にせずカードで出す (内容 + 結果の要約は detail にある)
        card = intents[0] if len(intents) == 1 else None
        return {'goal': goal, 'nodes': [found[k][0] for k in sorted(found)], 'edges': out_edges, 'card': card}

    return _query(db_path, fn)


def place_nodes(
    found: dict[str, tuple[dict[str, Any], dict[str, Any]]], edges: list[dict[str, str]], goal: str
) -> None:
    """Graph の位置 (col = 左から何列目, row = 上から何段目) を node に入れる (#93).

    列は derived_from の深さ (親より必ず右)、同じ列の中は発注時刻の順。ゴールは左端 (col 0)、
    result / fact は持ち主の task の真下に積む。発注時刻は issued_at (UTC)、無ければ created_at。
    """
    intents = sorted(
        (n for n, _ in found.values() if n['kind'] == 'intent'),
        key=lambda n: (found[n['node_id']][1].get('issued_at') or n['detail'].get('created_at') or '', n['node_id']),
    )
    parents: dict[str, list[str]] = {}
    for e in edges:
        if e['rel'] == 'derived_from' and e['src'] in found and e['dst'] in found:
            parents.setdefault(e['src'], []).append(e['dst'])
    col = {n['node_id']: 1 for n in intents}
    for _ in range(len(intents)):  # 深さが落ち着くまで (循環があっても件数回で止める)
        changed = False
        for n in intents:
            want = max([col[p] + 1 for p in parents.get(n['node_id'], []) if p in col], default=1)
            if want > col[n['node_id']] and want <= len(intents) + 1:
                col[n['node_id']], changed = want, True
        if not changed:
            break
    below: dict[str, list[str]] = {}
    for e in edges:
        if e['rel'] == 'yields':
            below.setdefault(e['src'], []).append(e['dst'])
    cursor: dict[int, int] = {}
    for n in intents:
        c = col[n['node_id']]
        stack, seen = [n['node_id']], {n['node_id']}  # intent の下に result、その下に fact を順に積む
        while stack:
            cur = stack.pop(0)
            found[cur][0]['col'], found[cur][0]['row'] = c, cursor.get(c, 0)
            cursor[c] = cursor.get(c, 0) + 1
            for nxt in sorted(below.get(cur, [])):
                if nxt in found and nxt not in seen:
                    seen.add(nxt)
                    stack.insert(0, nxt)
    if goal in found:
        found[goal][0]['col'], found[goal][0]['row'] = 0, 0
    for node, _ in found.values():
        node.setdefault('col', 0)
        node.setdefault('row', 0)


def _first_line(text: str) -> str:
    return next((line.strip() for line in (text or '').splitlines() if line.strip()), '')


def activity_data(db_path: Path, hours: int, project: str = '', session: str = '') -> dict[str, Any]:
    """全 project の出来事 (発注 / 結果 / blocked / ゴール完了) を新しい順に (#82). hours > 0 ならその時間以内.

    出来事はノード 1 つに 1 行: result は report_id (無ければ中身のハッシュ) で決まる node_id なので、
    reports/archive/ の写し (#78) があっても増えない。時刻は graph sync が payload に入れた UTC の ISO8601。
    goal / node_id は行から Graph を開くためのもの。
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds') if hours > 0 else ''

    def fn(conn: sqlite3.Connection) -> dict[str, Any]:
        owners = dict(conn.execute('SELECT project, session FROM projects'))
        goal_of = dict(conn.execute("SELECT dst_id, src_id FROM edges WHERE rel = 'spawns'"))
        intent_of = dict(
            conn.execute("SELECT dst_id, src_id FROM edges WHERE rel = 'yields' AND src_id LIKE 'intent:%'")
        )
        events = []
        for node_id, kind, pj, state, title, payload in conn.execute(
            "SELECT node_id, kind, project, state, title, payload_json FROM nodes WHERE kind != 'fact'"
        ):
            p = _payload(payload)
            if kind == 'intent':
                at, ev, intent, worker = p.get('issued_at', ''), 'order', node_id, p.get('assigned_to', '')
            elif kind == 'result':
                intent, worker = intent_of.get(node_id, ''), p.get('worker', '')
                at, ev = p.get('reported_at', ''), 'result' if state in ('completed', 'complete') else 'blocked'
                title = _first_line(p.get('summary', '')) or title
            else:
                at, ev, intent, worker = p.get('finished_at', ''), 'goal_done', '', ''
            sess = owners.get(pj, '')
            if not at or at < since or (project and pj != project) or (session and sess != session):
                continue
            events.append(
                {
                    'at': at,
                    'project': pj,
                    'session': sess,
                    'worker': worker,
                    'task_id': intent.split('/', 1)[-1] if intent else '',
                    'event': ev,
                    'state': state,
                    'verify': p.get('verify_status', '') if kind == 'result' else '',
                    'title': title,
                    'goal': node_id if kind == 'goal' else goal_of.get(intent, ''),
                    'node_id': node_id,
                }
            )
        try:
            instructions = conn.execute(
                'SELECT session, id, project, body, created_at, delivered_at, failed_at FROM instructions'
            ).fetchall()
        except sqlite3.OperationalError:  # instructions 表の無い古い DB (次の sync で作り直される)
            instructions = []
        for sess, iid, pj, body, at, delivered, failed in instructions:
            if not at or at < since or (project and pj != project) or (session and sess != session):
                continue
            events.append(
                {
                    'at': at,
                    'project': pj,
                    'session': sess,
                    'worker': '',
                    'task_id': '',
                    'event': 'instruction',
                    'state': 'delivered' if delivered else 'failed' if failed else 'pending',
                    'verify': '',
                    'title': body,
                    'goal': '',
                    'node_id': f'instruction:{sess}/{iid}',
                    'delivered_at': delivered,
                }
            )
        # 同じ時刻なら指示 -> 発注 -> 結果 -> ゴール完了の順に起きたものとして、新しい順の上にゴール完了を出す
        events.sort(key=lambda e: (e['at'], EVENT_ORDER[e['event']], e['node_id']), reverse=True)
        return {
            'hours': hours,
            'events': events,
            'projects': sorted(owners),
            'sessions': sorted({s for s in owners.values() if s} | {i[0] for i in instructions}),
        }

    return _query(db_path, fn)


def targets_data(db_path: Path) -> dict[str, Any]:
    """指示を出せる宛先: 起動中の squad セッションと、そのセッションが担当する project (#85)."""
    owners = _query(db_path, lambda conn: {'owners': conn.execute('SELECT project, session FROM projects').fetchall()})
    pairs = owners.get('owners', [])
    return {
        'sessions': {s: sorted(pj for pj, o in pairs if o == s) for s in squad_sessions() if SESSION_RE.fullmatch(s)}
    }


def write_instruction(queue_dir: Path, session: str, project: str, body: str) -> str:
    """指示を queue/instructions/<session>/<id>.json に書き、id を返す。session は検証済みのものだけ渡す.

    ファイル名は入力から作らない (時刻 + 乱数)。一時ファイルに書いてから link で置くので、watchd や graph
    sync が書きかけを読まず、同名があれば上書きしない (FileExistsError)。
    """
    d = queue_dir / 'instructions' / session
    d.mkdir(parents=True, exist_ok=True)
    # instructions/ か <session>/ が symlink で他の場所 (tasks/ 等) を指していたら書かない
    if d.resolve() != queue_dir.resolve() / 'instructions' / session:
        raise OSError(f'instructions の置き場所が {queue_dir} の外を指しています')
    now = datetime.now(timezone.utc)
    iid = f'{now.strftime("%Y%m%dT%H%M%S%fZ")}-{secrets.token_hex(4)}'  # 名前順 = 時刻順
    data = {
        'id': iid,
        'session': session,
        'project': project,
        'body': body,
        'created_at': now.isoformat(timespec='seconds'),
        'delivered_at': '',
    }
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.link(tmp, d / f'{iid}.json')
    finally:
        os.unlink(tmp)
    return iid


# ---------- tmux ----------


def _tmux_lines(*args: str) -> list[str] | None:
    """Tmux の出力行。失敗したら None."""
    r = subprocess.run(['tmux', *args], capture_output=True, text=True)
    return r.stdout.splitlines() if r.returncode == 0 else None


def _workers(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """squad/config.json の worker 定義 (graph sync が meta.workers に写したもの)."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'workers'").fetchone()
    return _payload(row[0]) if row else {}


def squad_sessions() -> dict[str, list[str]]:
    """Squad セッション名 -> その window 名一覧."""
    out = {}
    for name in _tmux_lines('list-sessions', '-F', '#{session_name}') or []:
        rows = [
            r.split('\t', 1)
            for r in _tmux_lines('list-panes', '-s', '-t', f'={name}:', '-F', '#{window_name}\t#{pane_title}') or []
        ]
        windows = [r[0] for r in rows]
        titles = [r[-1] for r in rows]
        # -t の末尾 : はセッション指定の印。無いと、attach 中のセッションにある同名の window (例: rmf) に当たる
        # window 名だけだと、aux や w1 という window を持つ無関係なセッションの pane まで開けてしまう。
        # start.sh が aux window に付けるタイトルも揃っていることを求める。旧構成は window 名が無いのでタイトルだけ
        if [SQUAD_WINDOW, PANE_TITLE] in rows or (SQUAD_WINDOW not in windows and PANE_TITLE in titles):
            out[name] = list(dict.fromkeys(windows))
    return out


def _pane(session: str, suffix: str, windows: list[str]) -> str:
    return suffix if ':' in suffix else squad.tmux_target(session, suffix, windows)


def sessions_data(db_path: Path) -> dict[str, Any]:
    """起動中の squad セッションごとに、担当 project と各 worker の状態・担当タスク."""

    def fn(conn: sqlite3.Connection) -> dict[str, Any]:
        return {
            'workers': _workers(conn),
            'owners': conn.execute('SELECT project, session FROM projects').fetchall(),
            'tasks': conn.execute(
                "SELECT node_id, project, state, title, payload_json FROM nodes WHERE kind = 'intent'"
            ).fetchall(),
        }

    db = _query(db_path, fn)
    current = {}  # (project, worker) -> tasks/ 直下にある task (= その worker が今持っている task)
    for node_id, project, state, title, payload in db.pop('tasks', []):
        p = _payload(payload)
        if p.get('current'):
            current[project, _worker_key(p.get('assigned_to', ''))] = {
                'node_id': node_id,
                'task_id': node_id.split('/', 1)[-1],
                'project': project,
                'state': state,
                'title': title,
            }
    owners = db.pop('owners', [])
    workers = db.pop('workers', {})
    sessions = []
    for name, windows in squad_sessions().items():
        projects = sorted(pj for pj, s in owners if s == name)
        rows = []
        for w, meta in workers.items():
            pane = _pane(name, meta.get('pane', ''), windows)
            st = squad.probe_worker(pane)
            rows.append(
                {
                    'worker': w,
                    'pane': pane,
                    'agent': meta.get('agent', ''),
                    'status': st['status'],
                    'label': STATUS_LABEL.get(st['status'], st['status']),
                    'model': st.get('model'),
                    'context_pct': st.get('context_pct'),
                    # last_line は返さない: 可視範囲だけをマスクした行なので、BEGIN 行が画面外に出た
                    # PRIVATE KEY の本文が素通しになりうる。中身は pane_data (履歴全体をマスク) で見る
                    'tasks': [current[pj, _worker_key(w)] for pj in projects if (pj, _worker_key(w)) in current],
                }
            )
        sessions.append({'name': name, 'projects': projects, 'workers': rows})
    return _masked({**db, 'sessions': sessions})


def pane_data(db_path: Path, target: str) -> dict[str, Any]:
    """Squad セッションの worker pane 1 つの中身 (クリックで広く開く用)。それ以外の pane は返さない."""
    workers = _query(db_path, lambda conn: {'workers': _workers(conn)}).get('workers', {})
    allowed = {_pane(s, m.get('pane', ''), ws) for s, ws in squad_sessions().items() for m in workers.values()}
    if target not in allowed:
        return {'target': target, 'ok': False, 'text': '(squad の worker pane ではありません)'}
    # 履歴全体 (-S -) をマスクしてから末尾を切る。可視範囲や末尾だけをマスクすると、PRIVATE KEY の
    # BEGIN 行が範囲外に出て本文が素通しになる。ponytail: history-limit を超えて捨てられた BEGIN 行は救えない
    lines = _tmux_lines('capture-pane', '-p', '-S', '-', '-t', target)
    text = mask('\n'.join(lines or []).rstrip('\n'))
    return {'target': target, 'ok': lines is not None, 'text': '\n'.join(text.split('\n')[-PANE_LINES:])}


# ---------- HTTP ----------


class Handler(BaseHTTPRequestHandler):
    """GET のみ受け付ける (--allow-write なら POST /api/instructions も)。設定は make_server が server 属性に載せる."""

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
        db = self.server.db_path

        def arg(key: str, default: str = '') -> str:
            return (query.get(key) or [default])[0]

        if url.path == '/':
            self._send(200, self.server.page.encode(), 'text/html; charset=utf-8')
            return
        if url.path == '/api/sessions':
            data = sessions_data(db)
        elif url.path == '/api/pane':
            data = pane_data(db, arg('target'))
        elif url.path == '/api/projects':
            data = projects_data(db)
        elif url.path == '/api/goals':
            days = arg('days', '30')
            data = goals_data(db, int(days) if days.isdigit() else 30)
        elif url.path == '/api/graph':
            data = graph_data(db, arg('goal'))
        elif url.path == '/api/activity':
            hours = arg('hours', '24')
            data = activity_data(db, int(hours) if hours.isdigit() else 24, arg('project'), arg('session'))
        elif url.path == '/api/targets' and self.server.allow_write:
            data = targets_data(db)
        else:
            self._send(404, b'{"error": "not found"}')
            return
        self._send(200, json.dumps(data, ensure_ascii=False).encode())

    def _reject(self) -> None:
        self._send(405, b'{"error": "read only"}')

    def _error(self, code: int, reason: str) -> None:
        """エラー応答は固定の文言だけ (入力の本文やトークンを返さない)."""
        self._send(code, json.dumps({'error': reason}).encode())

    def do_POST(self) -> None:  # noqa: N802
        """指示を 1 件受け付ける (#85)。--allow-write で起動したときだけ."""
        if not self.server.allow_write:
            self._reject()
            return
        if urlsplit(self.path).path != '/api/instructions':
            self._error(404, 'not found')
            return
        # トークンは Authorization ヘッダだけ (クエリ・Cookie は見ない)。他サイトのページはこのヘッダを
        # 付けられない (付けると preflight の OPTIONS になり 405 で止まる) ので CSRF を防げる
        header = self.headers.get('Authorization', '')
        given = header[7:] if header.startswith('Bearer ') else ''
        if not hmac.compare_digest(given.encode(), self.server.token.encode()):
            self._error(401, 'unauthorized')
            return
        host = self.headers.get('Host', '')
        if not host or self.headers.get('Origin') != f'http://{host}':
            self._error(403, 'bad origin')
            return
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self._error(411, 'length required')
            return
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._error(413, 'too large')
            return
        try:
            req = json.loads(self.rfile.read(length))
        except ValueError:  # UnicodeDecodeError も含む
            self._error(400, 'bad json')
            return
        req = req if isinstance(req, dict) else {}
        session, project, body = req.get('session'), req.get('project'), req.get('body')
        project = project or ''
        if not (isinstance(session, str) and isinstance(project, str) and isinstance(body, str) and body.strip()):
            self._error(400, 'session and body are required')
            return
        if len(body) > MAX_BODY_CHARS:
            self._error(413, 'body too long')
            return
        # session / project は既知の一覧と完全一致したものだけ (パスに使うので入力の形は信用しない)
        targets = targets_data(self.server.db_path)['sessions']
        if session not in targets or (project and project not in targets[session]):
            self._error(400, 'unknown session or project')
            return
        try:
            iid = write_instruction(self.server.db_path.parent, session, project, body.strip())
        except OSError:
            self.log_error('指示を書けませんでした (session=%s)', session)
            self._error(500, 'write failed')
            return
        self._send(201, json.dumps({'id': iid}).encode())

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _reject  # noqa: N815

    def log_message(self, fmt: str, *args: object) -> None:
        """クエリ (token を含みうる) をログに出さない."""
        if args and isinstance(args[0], str):
            args = (args[0].split('?', 1)[0], *args[1:])
        super().log_message(fmt, *args)


def make_server(host: str, port: int, token: str, db_path: Path, allow_write: bool = False) -> ThreadingHTTPServer:
    """allow_write=True のときだけ指示の投入 (POST /api/instructions) と入力欄を有効にする (#85).

    指示は db_path と同じディレクトリ (queue/) の instructions/ に書く。
    """
    if not token:
        raise ValueError('token is required')
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.token = token
    srv.db_path = db_path
    srv.allow_write = allow_write
    srv.page = PAGE_WRITE if allow_write else PAGE
    return srv


# Cytoscape.js は版を URL に固定し、SRI で中身も固定する (ページはトークンを持つので、CDN 側で差し替え
# られた script を実行しない)。階層レイアウトは同梱の breadthfirst を使う (拡張を足さない)。SRI は公開値
CYTOSCAPE_URL = 'https://cdn.jsdelivr.net/npm/cytoscape@3.30.2/dist/cytoscape.min.js'
CYTOSCAPE_SRI = 'sha384-IWROdLKRsN1UuJywMlWl7/blXQ8GEooN2n7dzTxfEPd7ybYIKCUJ2Ol/1Gpf3YV4'  # pragma: allowlist secret
# report 本文の Markdown 表示。marked で HTML にし、DOMPurify で script / on* 属性 / javascript: を落とす。
# どちらかが読めなければプレーンテキストで出す (renderMarkdown)。SRI は実ファイルから計算した値
MARKED_URL = 'https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js'
MARKED_SRI = 'sha384-fGlgqlm/GUgiVEyV6lvBJbxjprBPT1g0VoE9o/FjCWntEwOQYGqANQdFeFlqbObP'  # pragma: allowlist secret
DOMPURIFY_URL = 'https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js'
DOMPURIFY_SRI = 'sha384-JEyTNhjM6R1ElGoJns4U2Ln4ofPcqzSsynQkmEc/KGy6336qAZl70tDLufbkla+3'  # pragma: allowlist secret

PAGE = (
    (
        """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>squad dashboard</title>
<script src="CYTOSCAPE_URL" integrity="CYTOSCAPE_SRI" crossorigin="anonymous"></script>
<script src="MARKED_URL" integrity="MARKED_SRI" crossorigin="anonymous"></script>
<script src="DOMPURIFY_URL" integrity="DOMPURIFY_SRI" crossorigin="anonymous"></script>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1f2328;font-size:14px}
nav{display:flex;align-items:center;gap:.4em;background:#24292f;padding:.5em .8em;color:#fff}
nav button{background:none;border:1px solid transparent;color:#d0d7de;padding:.35em .9em;border-radius:6px;
  cursor:pointer;font-size:14px}
nav button.on{background:#fff;color:#24292f}
#sync{margin-left:auto;font-size:12px;color:#d0d7de}
main{padding:1em}
.err{background:#ffebe9;color:#82071e;padding:.5em .8em;border-radius:6px;margin-bottom:1em}
.sess{background:#fff;border:1px solid #d0d7de;border-radius:8px;margin-bottom:1em;overflow:hidden}
.sess h2{font-size:16px;margin:0;padding:.6em .8em;border-bottom:1px solid #d0d7de}
.sess h2 small{font-weight:normal;color:#57606a;margin-left:.8em}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{text-align:left;padding:.45em .7em;border-bottom:1px solid #eaeef2;vertical-align:top}
th{font-size:12px;color:#57606a;font-weight:600;background:#f6f8fa}
tr.click{cursor:pointer}tr.click:hover{background:#f6f8fa}
.badge{display:inline-block;padding:.1em .6em;border-radius:10px;font-size:12px;white-space:nowrap}
.busy,.running{background:#ddf4ff;color:#0550ae}.idle{background:#eaeef2;color:#57606a}
.permission_wait{background:#fff8c5;color:#7d4e00}.unreachable,.blocked{background:#ffebe9;color:#82071e}
.done,.completed{background:#dafbe1;color:#116329}.verified{background:#116329;color:#fff}
.closed,.unknown{background:#eaeef2;color:#57606a}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:#57606a}
.num{text-align:right;font-variant-numeric:tabular-nums}
.sum{max-width:48em;color:#424a53}
#modal{position:fixed;inset:3%;background:#0d1117;color:#e6edf3;border-radius:8px;display:none;flex-direction:column}
#modal header{display:flex;justify-content:space-between;padding:.5em .8em;border-bottom:1px solid #30363d}
#modal pre{flex:1;margin:0;padding:.8em;overflow:auto;font-size:12px;white-space:pre-wrap}
#graph:not([hidden]){display:flex;gap:1em;height:calc(100vh - 5em)}
#goals{width:320px;overflow:auto;background:#fff;border:1px solid #d0d7de;border-radius:8px}
#goals .g{padding:.5em .7em;border-bottom:1px solid #eaeef2;cursor:pointer}
#goals .g:hover{background:#f6f8fa}#goals .g.on{background:#ddf4ff}
#goals .g .t{margin:.2em 0}
#gmain{flex:1;min-width:0;display:flex;flex-direction:column;gap:1em}
#cy{flex:1;min-height:0;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:auto}
#cy .card{padding:1em;max-width:60em}
#side{height:38%;overflow:auto;background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:.8em}
#side pre{white-space:pre-wrap;font-size:12px;background:#f6f8fa;padding:.5em;border-radius:6px}
.md p,.md ul,.md ol,.md pre{margin:.3em 0}.md ul,.md ol{padding-left:1.4em}
.md h1,.md h2,.md h3,.md h4{font-size:1em;margin:.4em 0 .2em}
.md code{font-family:ui-monospace,monospace;font-size:12px;background:#eff1f3;padding:0 .2em;border-radius:3px}
.md pre code{background:none;padding:0}.md pre{overflow:auto}
.sum .md{max-height:14em;overflow:auto}.plain{white-space:pre-wrap}
#actbar,#pjbar{margin-bottom:.6em}#actbar select,#pjbar select{margin:0 .8em 0 .3em}#actmore{margin-top:.6em}
#instr{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:.6em .8em;margin-bottom:.8em}
#instr select{margin:0 .8em 0 .3em}#instr textarea{display:block;width:100%;margin:.4em 0;font:inherit}
</style></head><body>
<nav><button data-v="sessions" class="on">稼働状況</button><button data-v="projects">Projects</button>
<button data-v="graph">Graph</button><button data-v="activity">活動</button><span id="sync"></span></nav>
<main><div id="err" class="err" hidden></div>
<section id="sessions"></section>
<section id="projects" hidden><div id="pjbar">project<select id="pjsel"></select>期間<select id="pjdays">
<option value="30">直近 30 日</option>
<option value="90">直近 90 日</option><option value="0">すべて</option></select></div>
<div id="pjlist"></div></section>
<section id="graph" hidden><div id="goals"></div><div id="gmain"><div id="cy"></div>
<div id="side"><p class="mono">ノードをクリックすると内容が出ます</p></div></div></section>
<section id="activity" hidden><!--WRITE_FORM-->
<div id="actbar">project<select id="actpj"></select>セッション<select id="actsess">
</select><span id="actrange" class="mono"></span></div><div id="actlist"></div>
<button id="actmore">もっと見る</button></section></main>
<div id="modal"><header><span id="mtitle" class="mono"></span><button onclick="closeModal()">閉じる</button></header>
<pre id="mtext"></pre></div>
<script>
const params = new URLSearchParams(location.search), token = params.get('token') || '';
const api = p => fetch(p, {headers: {Authorization: 'Bearer ' + token}}).then(r => r.json());
const el = (tag, cls, text) => { const e = document.createElement(tag);
  if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; };
const STATE_LABEL = {closed: '完了 (報告なし)'};  // archive 済みで結果が残っていない task (#78)
const badge = (s, label) => el('span', 'badge ' + s, label || STATE_LABEL[s] || s);
// md:begin
// 本文 (report の summary 等) を Markdown で描く。中身はサーバーでマスク済みだが信用はしない:
// marked で HTML にしたものを必ず DOMPurify に通す。どちらかが無い・例外のときはプレーンテキスト
if (window.DOMPurify && window.DOMPurify.addHook) window.DOMPurify.addHook('afterSanitizeAttributes', n => {
  if (n.tagName === 'A') { n.setAttribute('target', '_blank'); n.setAttribute('rel', 'noopener noreferrer'); } });
// 画像・media・埋め込みは落とす (report の中身でブラウザに内部 URL を GET させない)。id / name は DOM
// clobbering (document.getElementById('side') 等の乗っ取り) を防ぐため落とす。リンクは http(s) / mailto だけ
const PURIFY = {
  FORBID_TAGS: ['style', 'form', 'input', 'img', 'picture', 'source', 'video', 'audio', 'track', 'svg', 'math',
    'iframe', 'object', 'embed'],
  FORBID_ATTR: ['id', 'name', 'style', 'src', 'srcset', 'poster'], SANITIZE_NAMED_PROPS: true,
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i};
function renderMarkdown(e, text, inline) {
  text = text === undefined || text === null ? '' : String(text);
  try {
    if (window.marked && window.DOMPurify) {
      const html = inline ? window.marked.parseInline(text) : window.marked.parse(text, {breaks: true});
      e.innerHTML = window.DOMPurify.sanitize(html, PURIFY);
      e.classList.add('md'); return e;
    }
  } catch (err) { /* 下のプレーンテキストに落とす */ }
  e.textContent = text; if (!inline) e.classList.add('plain'); return e;
}
// md:end
const local = iso => iso ? new Date(iso).toLocaleString('ja-JP', {hour12: false}) : '-';
const row = cells => { const tr = el('tr'); for (const c of cells) { const td = el('td');
  if (c instanceof Node) td.append(c); else td.textContent = c ?? ''; tr.append(td); } return tr; };
const table = (heads, rows) => { const t = el('table'), h = el('tr');
  for (const x of heads) h.append(el('th', '', x)); t.append(h); for (const r of rows) t.append(r); return t; };
let view = params.get('view') || 'sessions', goal = params.get('goal') || '', goalDays = '30', goalPj = '', cy = null;
let pickNode = params.get('node') || '';  // Graph で選ぶノード (活動タブの行から来たとき等)
function show(v) { view = v;
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x.dataset.v === v));
  for (const s of document.querySelectorAll('section')) s.hidden = s.id !== v; refresh(); }
document.querySelectorAll('nav button').forEach(b => b.onclick = () => show(b.dataset.v));
function meta(d) {
  document.getElementById('sync').textContent = 'DB 最終 sync: ' + local(d.synced_at);
  const e = document.getElementById('err'); e.hidden = !d.error; e.textContent = d.error || '';
}
function closeModal() { document.getElementById('modal').style.display = 'none'; }
async function openPane(target) {
  const d = await api('/api/pane?target=' + encodeURIComponent(target));
  document.getElementById('mtitle').textContent = target;
  const pre = document.getElementById('mtext'); pre.textContent = d.ok ? d.text : '(capture-pane に失敗) ' + d.text;
  document.getElementById('modal').style.display = 'flex'; pre.scrollTop = pre.scrollHeight;
}

async function sessions() {
  const d = await api('/api/sessions'), root = document.getElementById('sessions'); meta(d);
  root.replaceChildren();
  if (!d.sessions.length) root.append(el('p', '', 'squad セッションが見つかりません'));
  for (const s of d.sessions) {
    const box = el('div', 'sess'), h = el('h2', '', s.name);
    h.append(el('small', '', '担当 project: ' + (s.projects.join(', ') || 'なし'))); box.append(h);
    box.append(table(['worker', '状態', '担当タスク', 'model / ctx'], s.workers.map(w => {
      const task = el('div');
      for (const t of w.tasks) { const x = el('div'); x.append(badge(t.state), ' ',
        el('span', 'mono', t.task_id + ' '), renderMarkdown(el('span', ''), t.title, true)); task.append(x); }
      if (!w.tasks.length) task.textContent = '-';
      const tr = row([el('span', '', w.worker + ' (' + w.agent + ')'), badge(w.status, w.label), task,
        (w.model || '-') + ' / ' + (w.context_pct == null ? '-' : w.context_pct + '%')]);
      tr.className = 'click'; tr.title = 'クリックで pane を開く'; tr.onclick = () => openPane(w.pane); return tr;
    })));
    root.append(box);
  }
}

async function projects() {
  const d = await api('/api/projects'), sel = document.getElementById('pjsel'); meta(d);
  // 期間内に更新があった project だけ選択肢と表に出す (古いものは「すべて」で見える)。選んだ project の行だけ出す
  const days = +document.getElementById('pjdays').value, since = Date.now() - days * 864e5;
  const pjs = (d.projects || []).filter(p => !days || new Date(p.updated_at) >= since);
  options(sel, pjs.map(p => p.name));
  document.getElementById('pjlist').replaceChildren(table(
    ['project', 'session', 'active', 'blocked', '今日終わった', '最新の結果', '最終更新'],
    pjs.filter(p => !sel.value || p.name === sel.value).map(p => { const r = p.latest_result, res = el('div', 'sum');
      if (r) res.append(badge(r.status), ' ', el('span', 'mono', r.task_id + ' '),
        renderMarkdown(el('div'), r.summary));
      else res.textContent = '-';
      const tr = row([el('b', '', p.name), p.session || '-', el('span', 'num', String(p.active)),
        el('span', 'num', String(p.blocked)), el('span', 'num', String(p.done_today)), res,
        local(p.updated_at)]);
      tr.className = 'click'; tr.title = 'クリックでこの project のゴールを Graph で開く';
      tr.onclick = () => { goalPj = p.name; openGoal('', ''); }; return tr; })));
}

async function goals() {
  const d = await api('/api/goals?days=' + goalDays), root = document.getElementById('goals'); meta(d);
  root.replaceChildren();
  const sel = el('select'); for (const [v, t] of [['30', '直近 30 日'], ['90', '直近 90 日'], ['0', 'すべて']])
    sel.append(new Option(t, v, false, v === goalDays));
  sel.onchange = () => { goalDays = sel.value; goals(); };
  const pj = el('select'); pj.append(new Option('project: すべて', ''));
  for (const v of [...new Set((d.goals || []).map(g => g.project))].sort())
    pj.append(new Option(v, v, false, v === goalPj));
  pj.onchange = () => { goalPj = pj.value; goal = ''; goals().then(drawGraph); };
  const gs = (d.goals || []).filter(g => !goalPj || g.project === goalPj);
  const head = el('div', 'g'); head.append(sel, ' ', pj, el('span', 'mono', ' ' + gs.length + ' 件'));
  root.append(head);
  if (!goal && gs.length) goal = gs[0].node_id;
  for (const g of gs) {
    const x = el('div', 'g' + (g.node_id === goal ? ' on' : '')); x.append(badge(g.state), ' ',
      el('span', 'mono', g.project + ' / ' + g.tasks + ' task / ' + local(g.updated_at)),
      renderMarkdown(el('div', 't'), g.title, true));
    x.onclick = () => { goal = g.node_id; goals(); drawGraph(); }; root.append(x);
  }
}

const COLOR = {running: '#0969da', blocked: '#cf222e', done: '#1a7f37', verified: '#116329', completed: '#1a7f37',
  confirmed: '#1a7f37', falsified: '#cf222e', closed: '#8c959f'};
const SHAPE = {goal: 'round-rectangle', intent: 'round-rectangle', result: 'ellipse', fact: 'diamond'};
function label(n) {
  const id = n.kind === 'intent' ? n.node_id.split('/').pop() : n.kind === 'goal' ? '' : n.kind;
  const t = n.title.length > 16 ? n.title.slice(0, 16) + '…' : n.title;
  return id ? id + '\\n' + t : t;
}
const CELL_X = 250, CELL_Y = 84;  // 列幅 / 段の高さ (ノードは 210 x 62)
function drawCard(d) {  // task が 1 件のゴール: 図にせず、内容と結果の要約を出す
  const root = document.getElementById('cy'), n = d.card; root.replaceChildren();
  const box = el('div', 'card'); box.append(el('div', 'mono', n.node_id));
  const h = el('h3', '', ''); h.append(badge(n.state), ' ', renderMarkdown(el('span', ''), n.title, true));
  box.append(h);
  for (const k of ['result_summary', 'description']) {
    const v = n.detail[k]; if (!v) continue;
    box.append(el('h4', '', NAMES[k])); box.append(renderMarkdown(el('div'), v));
  }
  root.append(box); document.getElementById('side').hidden = true;  // カードに同じ内容が出ているので下の詳細は隠す
}
async function drawGraph() {
  if (!goal) return;
  const d = await api('/api/graph?goal=' + encodeURIComponent(goal)); meta(d);
  const pick = pickNode;
  const first = d.nodes.find(n => n.node_id === pick) || d.nodes.find(n => n.node_id === goal);
  if (cy) { cy.destroy(); cy = null; }
  document.getElementById('cy').replaceChildren(); document.getElementById('side').hidden = false;
  if (d.card) { drawCard(d); return; }
  if (typeof cytoscape === 'undefined') { const e = document.getElementById('err'); e.hidden = false;
    e.textContent = 'Cytoscape.js を CDN から読めませんでした (右の詳細だけ出します)'; side(first); return; }
  const els = d.nodes.map(n => ({data: {id: n.node_id, label: label(n), kind: n.kind,
    color: COLOR[n.state] || '#8c959f', n}, position: {x: n.col * CELL_X, y: n.row * CELL_Y}}));
  const derivedTo = new Set(d.edges.filter(e => e.rel === 'derived_from').map(e => e.src));
  // ゴールからの spawns 線は、他の task から派生していない task (左端の列) にだけ引く
  for (const e of d.edges) if (!(e.rel === 'spawns' && derivedTo.has(e.dst)))
    els.push({data: {id: e.src + '>' + e.dst, rel: e.rel,  // 派生は親 -> 子 (時間の向き) に描く
      source: e.rel === 'derived_from' ? e.dst : e.src, target: e.rel === 'derived_from' ? e.src : e.dst}});
  cy = cytoscape({container: document.getElementById('cy'), elements: els, wheelSensitivity: 0.3,
    layout: {name: 'preset', fit: true, padding: 30},
    style: [
      {selector: 'node', style: {label: 'data(label)', 'text-wrap': 'wrap', 'text-valign': 'center',
        'font-size': 17, shape: e => SHAPE[e.data('kind')], width: 210, height: 62,
        'background-color': '#fff', 'border-width': 3, 'border-color': 'data(color)', color: '#1f2328'}},
      {selector: 'node[kind = "goal"]', style: {'background-color': '#24292f', color: '#fff', width: 200}},
      {selector: 'node[kind = "result"]', style: {width: 210, height: 40, 'font-size': 14}},
      {selector: 'node[kind = "fact"]', style: {width: 120, height: 50}},
      {selector: 'node:selected', style: {'border-width': 5, 'border-color': '#bf8700'}},
      {selector: 'edge', style: {width: 2, 'line-color': '#afb8c1', 'target-arrow-color': '#afb8c1',
        'target-arrow-shape': 'triangle', 'curve-style': 'bezier'}},
      {selector: 'edge[rel = "derived_from"]', style: {'line-style': 'dashed'}}]});
  cy.on('tap', 'node', ev => side(ev.target.data('n')));
  if (pick && cy.getElementById(pick).length) cy.getElementById(pick).select();
  side(first);
}
const NAMES = {result_summary: '結果の要約', description: '内容', summary: '要約', evidence: '根拠',
  assigned_to: '担当', created_at: '作成', finished_at: '終了', verify_status: 'verify', review: 'review',
  pr_url: 'PR', worker: 'worker'};
const BODY = ['result_summary', 'description', 'summary', 'evidence'];  // Markdown で書かれる本文
function side(n) {
  const root = document.getElementById('side'); root.replaceChildren();
  if (!n) return;
  root.append(el('div', 'mono', n.node_id)); const h = el('h3', '', '');
  h.append(badge(n.state), ' ', renderMarkdown(el('span', ''), n.title, true));
  root.append(h, el('div', 'mono', '更新: ' + local(n.updated_at)));
  for (const k in NAMES) {
    const v = n.detail[k]; if (!v) continue; root.append(el('h4', '', NAMES[k]));
    root.append(BODY.includes(k) ? renderMarkdown(el('div'), v) : el('div', '', v));
  }
}

// 活動タブ (#82): 既定は直近 24 時間、「もっと見る」で 7 日 -> 30 日 -> すべてと遡る
const HOURS = [24, 168, 720, 0], HOURS_LABEL = {24: '直近 24 時間', 168: '直近 7 日', 720: '直近 30 日', 0: 'すべて'};
const EVENT_LABEL = {order: '発注', result: '結果', blocked: 'blocked', goal_done: 'ゴール完了', instruction: '指示'};
const INSTR_BADGE = {delivered: 'done', pending: 'permission_wait', failed: 'blocked'};  // 指示の配達状態 (#85)
let actHours = 24;
// 5 秒ごとの更新で開いている select を閉じないよう、中身が変わったときだけ作り直す。blank が null なら空の選択肢なし
function options(sel, values, blank = 'すべて') {
  const key = JSON.stringify(values); if (sel.dataset.v === key) return; sel.dataset.v = key; const cur = sel.value;
  sel.replaceChildren(...(blank === null ? [] : [new Option(blank, '')]),
    ...values.map(v => new Option(v, v, false, v === cur)));
}
function openGoal(g, n) { goal = g; pickNode = n; if (cy) { cy.destroy(); cy = null; } show('graph'); }
async function activity() {
  const pj = document.getElementById('actpj'), sess = document.getElementById('actsess');
  const q = new URLSearchParams({hours: actHours, project: pj.value, session: sess.value});
  /*WRITE_HOOK*/
  const d = await api('/api/activity?' + q); meta(d);
  options(pj, d.projects || []); options(sess, d.sessions || []);
  const evs = d.events || [];
  document.getElementById('actrange').textContent = HOURS_LABEL[actHours] + ' ' + evs.length + ' 件';
  document.getElementById('actmore').hidden = actHours === 0;
  document.getElementById('actlist').replaceChildren(table(
    ['時刻', 'project', 'セッション', 'worker', 'task_id', '種類', '題名 / 要約'], evs.map(e => {
      const kind = el('span'); kind.append(badge(e.event === 'order' ? 'running' : e.event === 'goal_done' ? 'done'
        : e.event === 'instruction' ? INSTR_BADGE[e.state] : e.state, EVENT_LABEL[e.event]));
      if (e.verify) kind.append(' ', el('span', 'mono', 'verify ' + e.verify));
      if (e.event === 'instruction') kind.append(' ', el('span', 'mono', e.state === 'delivered'
        ? '渡した ' + local(e.delivered_at) : e.state === 'failed' ? '送れず (再送して下さい)'
        : '待機中になったら渡す'));
      const tr = row([local(e.at), e.project, e.session || '-', e.worker || '-', el('span', 'mono', e.task_id || '-'),
        kind, renderMarkdown(el('span', 'sum'), e.title, true)]);
      if (e.goal) { tr.className = 'click'; tr.title = 'クリックで Graph を開く';
        tr.onclick = () => openGoal(e.goal, e.node_id); }
      return tr; })));
}
for (const id of ['pjsel', 'pjdays']) document.getElementById(id).onchange = () => projects();
for (const id of ['actpj', 'actsess']) document.getElementById(id).onchange = () => activity();
document.getElementById('actmore').onclick = () => { actHours = HOURS[HOURS.indexOf(actHours) + 1]; activity(); };
/*WRITE_JS*/
async function refresh() {
  try { if (view === 'sessions') await sessions(); if (view === 'projects') await projects();
    if (view === 'graph') { await goals(); if (!cy) await drawGraph(); } if (view === 'activity') await activity();
  } catch (e) { const x = document.getElementById('err'); x.hidden = false; x.textContent = 'error: ' + e; }
}
show(view); setInterval(() => { if (view !== 'graph') refresh(); }, 5000);
</script></body></html>
"""
    )
    .replace('CYTOSCAPE_URL', CYTOSCAPE_URL)
    .replace('CYTOSCAPE_SRI', CYTOSCAPE_SRI)
    .replace('MARKED_URL', MARKED_URL)
    .replace('MARKED_SRI', MARKED_SRI)
    .replace('DOMPURIFY_URL', DOMPURIFY_URL)
    .replace('DOMPURIFY_SRI', DOMPURIFY_SRI)
)
# 指示の入力欄 (#85)。--allow-write のときのページにだけ入れる (読み取り専用のページには欄もコードも無い)
WRITE_FORM = """<form id="instr"><b>Dispatcher への指示</b> セッション<select id="insess"></select>project
<select id="inpj"></select><textarea id="intext" rows="3" maxlength="MAX_BODY_CHARS"
placeholder="Dispatcher が待機中になったら watchd が打ち込みます (改行は空白になります)"></textarea>
<button>送る</button> <span id="inmsg" class="mono"></span></form>""".replace('MAX_BODY_CHARS', str(MAX_BODY_CHARS))
WRITE_JS = """// 指示の投入 (#85): 宛先は起動中の squad セッションと、そのセッションが担当する project
async function loadTargets() {
  const d = await api('/api/targets'), t = d.sessions || {}, s = document.getElementById('insess');
  options(s, Object.keys(t), null); options(document.getElementById('inpj'), t[s.value] || [], '(指定しない)');
}
document.getElementById('insess').onchange = () => loadTargets();
document.getElementById('instr').onsubmit = async ev => {
  ev.preventDefault(); const msg = document.getElementById('inmsg'), text = document.getElementById('intext');
  const r = await fetch('/api/instructions', {method: 'POST', headers: {Authorization: 'Bearer ' + token,
    'Content-Type': 'application/json'}, body: JSON.stringify({session: document.getElementById('insess').value,
    project: document.getElementById('inpj').value, body: text.value})});
  const d = await r.json().catch(() => ({}));
  msg.textContent = r.ok ? '受け付けました (' + d.id + ')' : '送れませんでした: ' + (d.error || r.status);
  if (r.ok) { text.value = ''; activity(); }
};
"""
PAGE_WRITE = (
    PAGE.replace('<!--WRITE_FORM-->', WRITE_FORM)
    .replace('/*WRITE_JS*/', WRITE_JS)
    .replace('/*WRITE_HOOK*/', 'await loadTargets();')
)
