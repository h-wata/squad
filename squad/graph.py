#!/usr/bin/env python3
# ruff: noqa: CPY001
"""探索グラフ — task / report YAML から Goal / Intent / Result / Fact を索引する (Issue #55).

正本は YAML のまま。`queue/.graph.db` (sqlite3) は YAML から何度でも作り直せる派生索引で、
sync のたびに全件を作り直す (差分更新しない)。消えた・移動した YAML の扱いを考えずに済み、
DB を消しても `squad graph sync` で同じ内容に戻る。report 配達 ledger
(`queue/.report_ledger.db`) とは責務もファイルも分ける。

ノード (node_id は YAML から決定的に決まるので、何度 sync しても重複しない):
  goal    task の context.parent_issue          goal:<project>#<issue>
  intent  task YAML (tasks/ と tasks/archive/)   intent:<project>/<task_id>
  result  report YAML (status と task_id を持つ) result:<report_id> (無ければ result:sha:<sha256 先頭16桁>)
  fact    report の decision_bearing_claims[i]   fact:<result の id 部分>#<i>

エッジ: goal -spawns-> intent、intent -yields-> result、result -yields-> fact、
        intent -derived_from-> intent|result (task YAML の任意フィールド derived_from)

YAML のネスト (context / decision_bearing_claims / derived_from) を読むため PyYAML を使う。
squad.py / watchd.py の stdlib only を崩さないよう、import はこのモジュールの中だけで行い、
呼び出し側は ImportError を受けて graph 機能だけを諦める。
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

import yaml

# 派生索引なので migration は持たない。schema を変えたら番号を上げ、古い DB は作り直す
SCHEMA_VERSION = 1
SCHEMA = (
    'CREATE TABLE nodes ('
    'node_id TEXT PRIMARY KEY, '
    "kind TEXT NOT NULL CHECK (kind IN ('goal', 'intent', 'result', 'fact')), "
    'project TEXT NOT NULL, '
    'state TEXT NOT NULL, '
    'title TEXT NOT NULL, '
    'ref TEXT NOT NULL, '
    'payload_json TEXT NOT NULL, '
    'updated_at TEXT NOT NULL)',
    'CREATE TABLE edges ('
    'src_id TEXT NOT NULL, '
    "rel TEXT NOT NULL CHECK (rel IN ('spawns', 'yields', 'derived_from')), "
    'dst_id TEXT NOT NULL, '
    'PRIMARY KEY (src_id, rel, dst_id))',
    'CREATE INDEX idx_edges_dst ON edges (dst_id)',
)
# libyaml があれば C 実装を使う (pure Python の SafeLoader だと実 queue 約 700 件で 18 秒かかった)
_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
_ISSUE_RE = re.compile(r'^#?(\d+)$')
_ISSUE_URL_RE = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def default_db_path(queue_dir: Path) -> Path:
    return queue_dir / '.graph.db'


def _load(path: Path) -> dict[str, Any] | None:
    """YAML を dict で読む。読めない・dict でないものは索引しない (正本の検査は check_*.py の仕事)."""
    try:
        data = yaml.load(path.read_bytes(), Loader=_LOADER)  # noqa: S506 (SafeLoader 系のみ)
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def _s(value: object) -> str:
    return '' if value is None else str(value)


def _goal_id(project: str, parent_issue: object) -> str:
    """番号だけなら project の Issue、URL なら owner/repo の Issue (別 repo の Issue を取り違えない)."""
    ref = _s(parent_issue).strip()
    if m := _ISSUE_URL_RE.search(ref):
        return f'goal:{m.group(1)}#{m.group(2)}'
    m = _ISSUE_RE.match(ref)
    return f'goal:{project}#{m.group(1)}' if m else ''


def _line(value: object, limit: int = 200) -> str:
    """表示用に 1 行へ潰す (summary は block scalar で複数行のことが多い)."""
    return ' '.join(_s(value).split())[:limit]


def _result_key(data: dict[str, Any], raw: bytes) -> str:
    report_id = _s(data.get('report_id')).strip()
    if _UUID_RE.match(report_id) and report_id != '00000000-0000-4000-8000-000000000000':
        return report_id.lower()
    return 'sha:' + hashlib.sha256(raw).hexdigest()[:16]


def _derived_target(project: str, ref: str) -> str:
    """derived_from の 1 要素を node_id にする (UUID は report、それ以外は同じ project の task_id)."""
    ref = ref.strip()
    if _UUID_RE.match(ref):
        return f'result:{ref.lower()}'
    return f'intent:{project}/{ref}'


def collect(queue_dir: Path) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str, str]]]:
    """queue/projects/*/ の YAML からノードとエッジを集める (DB には触れない)."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()

    def add(
        node_id: str, kind: str, project: str, state: str, title: str, ref: str, updated_at: float, **payload: object
    ) -> None:
        # 同じ node_id が複数ファイルにある (task の差し戻しで再発行等) ときは新しい方を採る
        old = nodes.get(node_id)
        if old and old['_mtime'] > updated_at:
            return
        nodes[node_id] = {
            'node_id': node_id,
            'kind': kind,
            'project': project,
            'state': state,
            'title': title,
            'ref': ref,
            'payload': payload,
            '_mtime': updated_at,
        }

    for pj_dir in sorted((queue_dir / 'projects').glob('*/')):
        project = pj_dir.name
        task_files: dict[str, tuple[float, bool]] = {}  # intent id -> (mtime, tasks/ 直下か)
        for path in sorted((pj_dir / 'tasks').rglob('*.yaml')):
            data = _load(path)
            task_id = _s(data.get('task_id')).strip() if data else ''
            if not task_id:
                continue
            mtime = path.stat().st_mtime
            intent = f'intent:{project}/{task_id}'
            current = path.parent == pj_dir / 'tasks'
            if intent not in task_files or task_files[intent][0] <= mtime:
                task_files[intent] = (mtime, current)
            add(
                intent,
                'intent',
                project,
                'unreported',
                _line(data.get('title')),
                str(path),
                mtime,
                assigned_to=_s(data.get('assigned_to')),
                created_at=_s(data.get('created_at')),
            )
            context = data.get('context') if isinstance(data.get('context'), dict) else {}
            goal = _goal_id(project, context.get('parent_issue'))
            if goal:
                add(
                    goal,
                    'goal',
                    project,
                    'unknown',
                    f'Issue {goal.rsplit("#", 1)[1]}',
                    _s(context.get('parent_issue')),
                    0,
                )
                edges.add((goal, 'spawns', intent))
            derived = data.get('derived_from') or []
            for ref in derived if isinstance(derived, list) else [derived]:
                if _s(ref).strip():
                    edges.add((intent, 'derived_from', _derived_target(project, _s(ref))))

        results: dict[str, list[tuple[float, str]]] = {}  # intent id -> [(mtime, status)]
        for path in sorted((pj_dir / 'reports').rglob('*.yaml')):
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            data = _load(path)
            if not data or not _s(data.get('task_id')).strip() or not _s(data.get('status')).strip():
                continue  # verdict 等、report 以外の YAML
            key = _result_key(data, raw)
            result = f'result:{key}'
            intent = f'intent:{project}/{_s(data["task_id"]).strip()}'
            status = _s(data['status']).strip()
            mtime = path.stat().st_mtime
            add(
                result,
                'result',
                project,
                status,
                _line(data.get('summary')),
                str(path),
                mtime,
                task_id=_s(data['task_id']).strip(),
                verify_status=_s(data.get('verify_status')),
                pr_url=_s(data.get('pr_url')),
                worker=_s(data.get('worker')),
                review='_review' in path.name,
            )
            edges.add((intent, 'yields', result))
            results.setdefault(intent, []).append((mtime, status))
            claims = data.get('decision_bearing_claims')
            for i, c in enumerate(claims if isinstance(claims, list) else []):
                if isinstance(c, dict) and _s(c.get('claim')).strip():
                    fact = f'fact:{key}#{i}'
                    add(
                        fact,
                        'fact',
                        project,
                        _s(c.get('status')) or 'uncertain',
                        _line(c.get('claim')),
                        str(path),
                        mtime,
                        evidence=_s(c.get('evidence')),
                    )
                    edges.add((result, 'yields', fact))

        # intent の状態: 最新 result の status が正。ただし tasks/ 直下の task が result より
        # 新しければ差し戻し後の再実行中。result が無く archive にしか無い task は、報告されずに
        # 外されたか、report が上書きされて残っていない (unreported のまま。実 queue では大半が
        # 後者)。archive への cp で mtime が進むことがあるので、archive の task は mtime を比べない
        for intent, (mtime, current) in task_files.items():
            reported = sorted(results.get(intent, []))
            if current and (not reported or reported[-1][0] < mtime):
                nodes[intent]['state'] = 'running'
            elif reported:
                nodes[intent]['state'] = 'done' if reported[-1][1] in ('completed', 'complete') else 'blocked'
    return nodes, edges


def connect(db_path: Path) -> sqlite3.Connection:
    """DB を開く。schema が古ければ作り直す (派生索引なので消してよい)."""
    conn = sqlite3.connect(db_path, timeout=30)
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        conn.executescript('DROP TABLE IF EXISTS edges; DROP TABLE IF EXISTS nodes;')
        for stmt in SCHEMA:
            conn.execute(stmt)
        conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    return conn


def sync(queue_dir: Path, db_path: Path | None = None) -> tuple[int, int]:
    """YAML から索引を作り直す。(ノード数, エッジ数) を返す. YAML は一切書き換えない."""
    nodes, edges = collect(queue_dir)
    conn = connect(db_path or default_db_path(queue_dir))
    try:
        with conn:
            conn.execute('DELETE FROM edges')
            conn.execute('DELETE FROM nodes')
            conn.executemany(
                'INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                [
                    (
                        n['node_id'],
                        n['kind'],
                        n['project'],
                        n['state'],
                        n['title'],
                        n['ref'],
                        json.dumps(n['payload'], ensure_ascii=False),
                        _iso(n['_mtime']),
                    )
                    for n in nodes.values()
                ],
            )
            conn.executemany('INSERT INTO edges VALUES (?, ?, ?)', sorted(edges))
    finally:
        conn.close()
    return len(nodes), len(edges)


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec='seconds') if mtime else ''


def _node(conn: sqlite3.Connection, node_id: str) -> tuple | None:
    return conn.execute('SELECT node_id, kind, state, title, ref FROM nodes WHERE node_id = ?', (node_id,)).fetchone()


def _fmt(row: tuple | None, node_id: str) -> str:
    if row is None:
        return f'{node_id} (索引に無い)'
    nid, kind, state, title, ref = row
    return f'{nid} [{kind}/{state}] {title}  ({ref})'


def why(db_path: Path, node_id: str) -> list[str]:
    """node_id から導出元 (goal 方向) へ遡った経路を、根から近い順にインデントして返す."""
    conn = connect(db_path)
    try:
        lines: list[str] = []
        seen: set[str] = set()

        def walk(nid: str, depth: int) -> None:
            lines.append('  ' * depth + _fmt(_node(conn, nid), nid))
            if nid in seen:  # derived_from の循環
                return
            seen.add(nid)
            for src, rel in conn.execute('SELECT src_id, rel FROM edges WHERE dst_id = ? ORDER BY src_id', (nid,)):
                lines.append('  ' * (depth + 1) + f'<- {rel}')
                walk(src, depth + 1)
            for dst in conn.execute(
                "SELECT dst_id FROM edges WHERE src_id = ? AND rel = 'derived_from' ORDER BY dst_id", (nid,)
            ):
                lines.append('  ' * (depth + 1) + '-> derived_from')
                walk(dst[0], depth + 1)

        walk(node_id, 0)
        return lines
    finally:
        conn.close()


def show(db_path: Path, node_id: str) -> list[str]:
    """1 ノードと、その前後のエッジを返す."""
    conn = connect(db_path)
    try:
        row = conn.execute('SELECT payload_json FROM nodes WHERE node_id = ?', (node_id,)).fetchone()
        lines = [_fmt(_node(conn, node_id), node_id)]
        if row:
            lines.append(f'  payload: {row[0]}')
        for src, rel in conn.execute('SELECT src_id, rel FROM edges WHERE dst_id = ? ORDER BY src_id', (node_id,)):
            lines.append(f'  <- {rel} {_fmt(_node(conn, src), src)}')
        for rel, dst in conn.execute('SELECT rel, dst_id FROM edges WHERE src_id = ? ORDER BY dst_id', (node_id,)):
            lines.append(f'  -> {rel} {_fmt(_node(conn, dst), dst)}')
        return lines
    finally:
        conn.close()
