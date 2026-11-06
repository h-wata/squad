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

intent の state (#57): running (tasks/ 直下にあり、それより新しい結果が無い) / blocked (最新 report の
status が completed 以外) / done / verified (done のうち verify_status が pass で、最新の cross-review が
approve) / closed (完了 (報告なし): archive にしか無く結果も無い。Dispatcher が閉じたが結果が残っていない, #78)。
`squad graph status` と `squad web` (#76) は
どちらもこの DB の値を読むので、2 つの画面の判定は一致する。

DB には他に projects (project ごとの担当 session・最新の結果・最終更新) と meta (synced_at と、
squad/config.json の worker 定義 workers) を持つ。squad web は DB と tmux だけを読むため。

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
SCHEMA_VERSION = 2
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
    'CREATE TABLE projects ('
    'project TEXT PRIMARY KEY, '
    'session TEXT NOT NULL, '
    'latest_result TEXT NOT NULL, '
    'updated_at TEXT NOT NULL)',
    'CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
)
TABLES = ('edges', 'nodes', 'projects', 'meta')
# 同じ task に複数の結果があるときの優先順: worker の report > cross-review の verdict > verifier の verdict
_RANK_REPORT, _RANK_REVIEW, _RANK_VERIFIER = 2, 1, 0
# libyaml があれば C 実装を使う (pure Python の SafeLoader だと実 queue 約 700 件で 18 秒かかった)
_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
_ISSUE_RE = re.compile(r'^#?(\d+)$')
_ISSUE_URL_RE = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)')
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


CONFIG_PATH = Path(__file__).resolve().parent / 'config.json'  # squad ls と同じ worker 定義


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


def parse_iso8601(value: object) -> datetime | None:
    """ISO8601 を aware datetime にする. scripts/measure_task_cost.py の同名関数と同じ扱い.

    Z 終端は +00:00、オフセット無し (naive) は worker が手元の時計で書いた値なのでローカル時刻として読む。
    """
    text = _s(value).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _epoch(value: object) -> float | None:
    dt = parse_iso8601(value)
    return dt.timestamp() if dt else None


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


def collect(
    queue_dir: Path,
) -> tuple[dict[str, dict[str, Any]], set[tuple[str, str, str]], dict[str, dict[str, Any]]]:
    """queue/projects/*/ の YAML からノード・エッジ・project 情報を集める (DB には触れない)."""
    nodes: dict[str, dict[str, Any]] = {}
    edges: set[tuple[str, str, str]] = set()
    projects: dict[str, dict[str, Any]] = {}
    goal_members: dict[str, tuple[str, str, str, list[str]]] = {}  # goal -> (project, title, ref, [intent])

    def add(
        node_id: str,
        kind: str,
        project: str,
        state: str,
        title: str,
        ref: str,
        updated_at: float,
        current: bool = False,
        **payload: object,
    ) -> None:
        # 同じ node_id が複数ファイルにある (task の差し戻しで再発行等) ときは tasks/ 直下を優先し、
        # 次に新しい方を採る (archive への cp で archive 側の mtime が進むことがある)
        old = nodes.get(node_id)
        if old and old['_rank'] > (current, updated_at):
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
            '_rank': (current, updated_at),
        }

    for pj_dir in sorted((queue_dir / 'projects').glob('*/')):
        project = pj_dir.name
        # .ruff_cache 等の隠しディレクトリと、tasks/ も reports/ も無いディレクトリは project ではない
        if project.startswith('.') or not ((pj_dir / 'tasks').is_dir() or (pj_dir / 'reports').is_dir()):
            continue
        try:
            session = (pj_dir / '.squad_session').read_text().strip()
        except OSError:
            session = ''
        times: list[float] = []  # project の最終更新 (task の発行と結果の時刻の最大)
        # intent id -> (tasks/ 直下か, mtime, 発注時刻 or None)。発注時刻は created_at と mtime の早い方
        task_files: dict[str, tuple[bool, float, float | None]] = {}
        issues: dict[str, tuple[str, str]] = {}  # intent id -> (task_id, parent_issue)
        for path in sorted((pj_dir / 'tasks').rglob('*.yaml')):
            data = _load(path)
            task_id = _s(data.get('task_id')).strip() if data else ''
            if not task_id:
                continue
            mtime = path.stat().st_mtime
            times.append(mtime)
            intent = f'intent:{project}/{task_id}'
            current = path.parent == pj_dir / 'tasks'
            created = _epoch(data.get('created_at'))
            issued = min(created, mtime) if created is not None else None
            if (current, mtime) > task_files.get(intent, (False, 0.0, None))[:2]:
                task_files[intent] = (current, mtime, issued)
            add(
                intent,
                'intent',
                project,
                'closed',
                _line(data.get('title')),
                str(path),
                mtime,
                current,
                assigned_to=_s(data.get('assigned_to')),
                created_at=_s(data.get('created_at')),
                routing_reason=_s(data.get('routing_reason')),
                replan_reason=_s(data.get('replan_reason')),
                description=_s(data.get('description'))[:4000],
            )
            context = data.get('context') if isinstance(data.get('context'), dict) else {}
            if nodes[intent]['ref'] == str(path):  # 採用された方のファイルの parent_issue を使う
                issues[intent] = (task_id, _s(context.get('parent_issue')).strip())
            derived = data.get('derived_from') or []
            for ref in derived if isinstance(derived, list) else [derived]:
                if _s(ref).strip():
                    edges.add((intent, 'derived_from', _derived_target(project, _s(ref))))

        # intent -> [(時刻, mtime, 優先度, status, verify, result, pr)]。時刻は completed_at (無ければ mtime)
        results: dict[str, list[tuple[float, float, int, str, str, str, str]]] = {}
        # cross-review の verdict。review は別 task_id で発行されることが多い (実 queue で 156 件中 142 件) ので、
        # task_id に加えて pr_url でも実装側と突き合わせる
        reviews: dict[str, list[tuple[float, str]]] = {}  # intent id か pr_url -> [(時刻, verdict)]
        for path in sorted((pj_dir / 'reports').rglob('*.yaml')):
            try:
                raw = path.read_bytes()
                mtime = path.stat().st_mtime
            except OSError:
                continue
            data = _load(path)
            if not data or not _s(data.get('task_id')).strip():
                continue
            intent = f'intent:{project}/{_s(data["task_id"]).strip()}'
            at = _epoch(data.get('completed_at')) or mtime
            times.append(at)
            status = _s(data.get('status')).strip()
            if _s(data.get('verdict')).strip():  # cross-review の report (status を持たないことが多い)
                verdict = (at, _s(data['verdict']).strip())
                reviews.setdefault(intent, []).append(verdict)
                if pr := _s(data.get('pr_url')).strip().rstrip('/'):
                    reviews.setdefault(pr, []).append(verdict)
                if not status:  # review は approve でも request_changes でも、書いた時点で review task は終わり
                    results.setdefault(intent, []).append((at, mtime, _RANK_REVIEW, 'completed', '', '', ''))
            elif not status and _s(data.get('result')).strip():  # verifier の verdict (workerN_verdict.yaml)
                ok = _s(data['result']).strip() == 'pass'
                results.setdefault(intent, []).append(
                    (at, mtime, _RANK_VERIFIER, 'completed' if ok else 'blocked', '', '', '')
                )
            if not status:
                continue  # verdict 等、report 以外の YAML
            key = _result_key(data, raw)
            result = f'result:{key}'
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
                # reports/archive/ の写し (#78) はファイル名から review と分からないので verdict でも見る
                review='_review' in path.name or bool(_s(data.get('verdict')).strip()),
                summary=_s(data.get('summary'))[:2000],
                reported_at=_iso(at),
            )
            nodes[result]['_at'] = at
            edges.add((intent, 'yields', result))
            verify = _s(data.get('verify_status')).strip()
            pr = _s(data.get('pr_url')).strip().rstrip('/')
            results.setdefault(intent, []).append((at, mtime, _RANK_REPORT, status, verify, result, pr))
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

        # intent の状態: task 以後の結果のうち、優先度 (report > review > verifier) と時刻が最大のものが正。
        # tasks/ 直下の task より新しい結果が無ければ実行中 (差し戻し後の再実行を含む)。新旧は task の発注時刻と
        # 結果の completed_at (無ければ mtime) で比べる。発注時刻は created_at と mtime の早い方: どちらも実際の
        # 発注より後ろにしかずれない (mtime は一括更新されることがあり、mesh-mem の tasks/ は 2026-07-02 に全部
        # 更新された。created_at は Dispatcher が丸めて書くので発注より後になることがある: SQUAD-257 は
        # created_at 13:10、発注 13:04、review 13:09)。created_at が無いときは task / 結果とも mtime で比べる。
        # 結果が無く archive にしか無い task は、Dispatcher が閉じたが report が上書きされて残っていない
        # (closed = 完了 (報告なし), #78。watchd が reports/archive/ に写すようになる前の分)。archive への cp で
        # mtime が進むことがあるので、archive の task は新旧を比べない。
        # verified (検証済み) は done のうち、report の verify_status が pass で、その report 以後の
        # cross-review が approve のもの (#57)。どれも YAML から機械的に決まる
        for intent, (current, mtime, issued) in task_files.items():
            nodes[intent]['payload']['current'] = current
            fresh = [
                r
                for r in results.get(intent, [])
                if not current or (r[0] >= issued if issued is not None else r[1] >= mtime)
            ]
            if not fresh:
                nodes[intent]['state'] = 'running' if current else 'closed'
                continue
            at, _mtime, rank, status, verify, result, pr = max(fresh, key=lambda r: (r[2], r[0]))
            # report より前の review は、やり直す前の変更へのものなので数えない
            candidates = [*reviews.get(intent, []), *(reviews.get(pr, []) if pr else [])]
            review = max([(0.0, ''), *(r for r in candidates if r[0] >= at)])[1] if rank == _RANK_REPORT else ''
            if status not in ('completed', 'complete'):
                nodes[intent]['state'] = 'blocked'
            # approve / approve_continues (再レビュー, dispatcher.md) / approve_with_nits 等を承認とみなす
            elif verify in ('pass', 'passed') and review.startswith('approve'):
                nodes[intent]['state'] = 'verified'
            else:
                nodes[intent]['state'] = 'done'
            nodes[intent]['_finished'] = at
            nodes[intent]['payload'].update(
                latest_result=result, verify_status=verify, review=review, finished_at=_iso(at)
            )
        for intent in task_files:
            nodes[intent]['payload'].setdefault('finished_at', '')
        for goal, (pj, title, ref, members) in _group_goals(project, issues, nodes).items():
            # 同じ Issue の URL を別 project の task も指すことがあるので、上書きせず配下を足す
            goal_members.setdefault(goal, (pj, title, ref, []))[3].extend(members)
        latest = max(
            (n for n in nodes.values() if n['kind'] == 'result' and n['project'] == project),
            key=lambda n: (n['_at'], n['node_id']),
            default=None,
        )
        projects[project] = {
            'session': session,
            'latest_result': latest['node_id'] if latest else '',
            'updated_at': _iso(max(times, default=0)),
        }
    for goal, (project, title, ref, members) in goal_members.items():
        members = sorted(set(members))
        states = {nodes[m]['state'] for m in members}
        # 配下の task が全部 done / verified / closed なら完了 (GitHub は見に行かない)。それ以外は未完了の中で
        # 一番重い状態。配下が closed だけのゴールは closed (結果が 1 件も残っていない) のまま灰色で出す
        state = 'closed' if states == {'closed'} else next((s for s in ('running', 'blocked') if s in states), 'done')
        times = [t for m in members for t in (nodes[m]['_mtime'], nodes[m].get('_finished', 0.0))]
        add(goal, 'goal', project, state, title, ref, max(times), members=members)
        edges.update((goal, 'spawns', m) for m in members)
    return nodes, edges, projects


# task_id の頭 (SQUAD-284-fix1 -> SQUAD-284)。この形でない task_id は 1 件で 1 ゴール
_BASE_RE = re.compile(r'^([A-Za-z][A-Za-z0-9]*-\d+)(?:-.+)?$')


def _group_goals(
    project: str, issues: dict[str, tuple[str, str]], nodes: dict[str, dict[str, Any]]
) -> dict[str, tuple[str, str, str, list[str]]]:
    """Intent をゴールに組み分ける (#76 addendum).

    context.parent_issue があればその Issue がゴール。無い task は task_id の頭が同じ派生 task
    (SQUAD-284 / SQUAD-284-fix1 / SQUAD-016-review) を、元の task を根にした 1 ゴールにまとめる。
    元の task が Issue を持っていれば、派生 task もその Issue のゴールに入れる。
    """
    base = {i: (m.group(1) if (m := _BASE_RE.match(tid)) else tid) for i, (tid, _) in issues.items()}
    root_issue = {base[i]: _goal_id(project, ref) for i, (tid, ref) in issues.items() if tid == base[i] and ref}
    out: dict[str, tuple[str, str, str, list[str]]] = {}
    # 古い順に入れて、ゴールの題名は根 (無ければ一番古い task) の題名にする
    for intent in sorted(issues, key=lambda i: (issues[i][0] != base[i], nodes[i]['_mtime'])):
        ref = issues[intent][1]
        goal = _goal_id(project, ref) or root_issue.get(base[intent], '')
        if goal:
            label, gref = f'Issue {goal.rsplit("#", 1)[1]}', ref or base[intent]
        else:
            goal, label, gref = f'goal:{project}/{base[intent]}', base[intent], base[intent]
        if goal not in out:
            out[goal] = (project, f'{label} {nodes[intent]["title"]}'.strip(), gref, [])
        out[goal][3].append(intent)
    return out


def connect(db_path: Path) -> sqlite3.Connection:
    """DB を開く。schema が古ければ作り直す (派生索引なので消してよい)."""
    conn = sqlite3.connect(db_path, timeout=30)
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        # 複数の watcher が同時に初期化しうるので、書き込みロックを取ってから version を読み直す
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            for table in TABLES:
                conn.execute(f'DROP TABLE IF EXISTS {table}')
            for stmt in SCHEMA:
                conn.execute(stmt)
            conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        conn.commit()
    return conn


def sync(queue_dir: Path, db_path: Path | None = None, config_path: Path = CONFIG_PATH) -> tuple[int, int]:
    """YAML から索引を作り直す。(ノード数, エッジ数) を返す. YAML は一切書き換えない."""
    nodes, edges, projects = collect(queue_dir)
    try:
        workers = json.loads(config_path.read_text()).get('workers', {})
    except (OSError, ValueError):
        workers = {}
    conn = connect(db_path or default_db_path(queue_dir))
    try:
        with conn:
            for table in TABLES:
                conn.execute(f'DELETE FROM {table}')  # noqa: S608 (固定のテーブル名)
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
            conn.executemany(
                'INSERT INTO projects VALUES (?, ?, ?, ?)',
                [(pj, p['session'], p['latest_result'], p['updated_at']) for pj, p in projects.items()],
            )
            conn.executemany(
                'INSERT INTO meta VALUES (?, ?)',
                [
                    ('synced_at', datetime.now(timezone.utc).isoformat(timespec='seconds')),
                    ('workers', json.dumps(workers, ensure_ascii=False)),
                ],
            )
    finally:
        conn.close()
    return len(nodes), len(edges)


STATES = ('running', 'blocked', 'done', 'verified', 'closed')
UNFINISHED = ('running', 'blocked')


def status(db_path: Path, project: str | None = None) -> dict[str, Any]:
    """Project ごとに、goal 別の intent 件数と未完了 (running / blocked) の intent 一覧を返す (読み取りのみ)."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT n.project, n.node_id, n.state, n.title, n.payload_json, n.updated_at, COALESCE(e.src_id, '-') "
            "FROM nodes n LEFT JOIN edges e ON e.dst_id = n.node_id AND e.rel = 'spawns' "
            "WHERE n.kind = 'intent' AND (? IS NULL OR n.project = ?) ORDER BY n.project, n.node_id",
            (project, project),
        ).fetchall()
        results = {
            r[0]: r[1:] for r in conn.execute("SELECT node_id, title, updated_at FROM nodes WHERE kind = 'result'")
        }
    finally:
        conn.close()
    out: dict[str, Any] = {}
    for pj, node_id, state, title, payload_json, updated_at, goal in rows:
        # status は Issue のゴールだけで集計する。task_id の頭で組んだゴール (goal:<pj>/<task_id>) は
        # 1 件ずつの行になって表が埋まるので、従来どおり '-' にまとめる
        goal = goal if '#' in goal else '-'
        pj_out = out.setdefault(pj, {'goals': {}, 'unfinished': []})
        counts = pj_out['goals'].setdefault(goal, dict.fromkeys(STATES, 0))
        counts[state] = counts.get(state, 0) + 1
        if state not in UNFINISHED:
            continue
        payload = json.loads(payload_json)
        result = payload.get('latest_result', '') if state == 'blocked' else ''
        reason, reported_at = results.get(result, ('', ''))
        pj_out['unfinished'].append(
            {
                'node_id': node_id,
                'state': state,
                'title': title,
                'goal': goal,
                'assigned_to': payload.get('assigned_to', ''),
                # running は task の発行 (mtime)、blocked は report の日付。古いものは放置の可能性がある
                'since': (reported_at if result else updated_at)[:10],
                'reason_id': result,
                'reason': reason,
            }
        )
    return out


def render_status(data: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for pj, pj_out in data.items():
        lines += [f'## {pj}', '', '| goal | ' + ' | '.join(STATES) + ' |', '|---' * (len(STATES) + 1) + '|']
        only_closed = 0
        for goal, c in sorted(pj_out['goals'].items()):
            if goal != '-' and sum(c.values()) == c['closed']:
                only_closed += 1  # report が上書きされて追えない過去分だけの goal は畳む
                continue
            lines.append(f'| {goal} | ' + ' | '.join(str(c[k]) for k in STATES) + ' |')
        if only_closed:
            lines.append(f'| (closed (完了・報告なし) だけの goal {only_closed} 件) |' + ' |' * len(STATES))
        lines += ['', '未完了:' if pj_out['unfinished'] else '未完了: なし']
        for u in pj_out['unfinished']:
            lines.append(
                f'- [{u["state"]} {u["since"]}] {u["node_id"]} {u["title"]} ({u["assigned_to"] or "-"}, {u["goal"]})'
            )
            if u['reason_id']:
                lines.append(f'  理由: {u["reason"][:100]} ({u["reason_id"]})')
        lines.append('')
    return lines


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
