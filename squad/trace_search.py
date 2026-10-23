#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Worker の実行 trace (Claude Code transcript) を横断検索する (Issue #56).

新しいストレージは作らない。report の `session_id` から `~/.claude/projects/*/<session_id>.jsonl`
を特定し (exact)、無ければ scripts/measure_task_cost.py と同じ近似 (task の workspace の
transcript を task 発行〜report 完了の時間窓で切る) で探す (approximate)。

検索の単位は tool_result (どの tool を何の入力で呼び、何が返ったか) と assistant の text。
`trace_id = <session_id>:<行番号>.<block 番号>` (1 行に複数の tool_result が入るため)。出力は必ず mask.mask() を通す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import sys
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
from ledger import parse_scalars  # noqa: E402
from mask import mask  # noqa: E402
import measure_task_cost as mtc  # noqa: E402

SNIPPET = 120
DETAIL_LIMIT = 4000


@dataclass(frozen=True)
class Source:
    """1 task 分の transcript (approximate なら時間窓つき)."""

    project: str
    task_id: str
    worker: str
    path: Path
    exact: bool
    start: datetime | None = None
    end: datetime | None = None


@dataclass(frozen=True)
class Entry:
    trace_id: str
    source: Source
    timestamp: str
    tool: str
    is_error: bool
    text: str


def _read_scalars(path: Path) -> dict[str, str]:
    try:
        return parse_scalars(path.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        return {}


def sources(queue_dir: Path, project: str | None = None, task: str | None = None) -> tuple[list[Source], list[str]]:
    """Report から transcript を決める。(見つかった Source, 見つからなかった <project>/<task_id>)."""
    found: list[Source] = []
    missing: list[str] = []
    for pj_dir in sorted((queue_dir / 'projects').glob('*/')):
        if project and pj_dir.name != project:
            continue
        task_files: dict[str, Path] = {}  # task_id -> task YAML (tasks/ 直下を archive より優先)
        for path in sorted((pj_dir / 'tasks').rglob('*.yaml'), key=lambda p: p.parent == pj_dir / 'tasks'):
            if tid := _read_scalars(path).get('task_id'):
                task_files[tid] = path
        for report in sorted((pj_dir / 'reports').rglob('*.yaml')):
            meta = _read_scalars(report)
            tid = meta.get('task_id', '')
            if not tid or not meta.get('status') or (task and tid != task):
                continue
            worker = meta.get('worker', '')
            task_path = task_files.get(tid)
            start = datetime.fromtimestamp(task_path.stat().st_mtime, tz=timezone.utc) if task_path else None
            end = mtc.parse_iso8601(meta.get('completed_at', ''))
            # 1 session で複数 task をこなすことがあるので、exact でも時間窓で切る
            # (窓が作れなければ session 全体。他 task の分が混ざりうる)
            exact = mtc.find_session_transcript(meta.get('session_id', ''))
            if exact:
                found.append(Source(pj_dir.name, tid, worker, exact, exact=True, start=start, end=end))
                continue
            hits = []
            if task_path and start and end:
                task_text = task_path.read_text(encoding='utf-8', errors='replace')
                for d in mtc.session_dirs_for(task_text):
                    for jsonl in sorted(d.glob('*.jsonl')):
                        if jsonl.stat().st_mtime >= start.timestamp():
                            hits.append(Source(pj_dir.name, tid, worker, jsonl, exact=False, start=start, end=end))
            found.extend(hits)
            if not hits:
                missing.append(f'{pj_dir.name}/{tid}')
    return found, missing


def _text(content: object) -> str:
    """tool_result の content (文字列か block の list) を文字列にする."""
    if isinstance(content, list):
        return '\n'.join(_text(b.get('text', '')) if isinstance(b, dict) else str(b) for b in content)
    return '' if content is None else str(content)


def _lines(path: Path) -> Iterator[tuple[int, dict]]:
    try:
        with path.open(encoding='utf-8', errors='replace') as f:
            for n, raw in enumerate(f, 1):
                try:
                    d = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    yield n, d
    except OSError:
        return


def entries(src: Source) -> Iterator[Entry]:
    tools: dict[str, tuple[str, str]] = {}  # tool_use_id -> (tool 名, 入力)
    for n, d in _lines(src.path):
        message = d.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        ts = str(d.get('timestamp', ''))
        in_window = True
        if src.start and src.end:
            t = mtc.parse_iso8601(ts)
            in_window = t is not None and src.start <= t <= src.end
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'tool_use':
                tools[str(block.get('id'))] = (
                    str(block.get('name', '?')),
                    json.dumps(block.get('input'), ensure_ascii=False),
                )
                continue
            if not in_window:
                continue
            # 1 行 (1 message) に複数の tool_result が入ることがあるので block 番号まで付ける
            trace_id = f'{src.path.stem}:{n}.{i}'
            if kind == 'tool_result':
                name, args = tools.get(str(block.get('tool_use_id')), ('?', ''))
                text = f'{args}\n=> {_text(block.get("content"))}'
                yield Entry(trace_id, src, ts, name, bool(block.get('is_error')), mask(text))
            elif kind == 'text' and d.get('type') == 'assistant':
                yield Entry(trace_id, src, ts, 'text', False, mask(str(block.get('text', ''))))


def search(
    srcs: list[Source], query: str, tool: str | None = None, errors_only: bool = False, limit: int = 20
) -> list[Entry]:
    """マスク後の本文に query (大小文字無視) を含むものを新しい順に返す. 同じ trace_id は 1 回だけ."""
    q = query.lower()
    hits: dict[str, Entry] = {}
    # ponytail: 毎回 transcript を全部読む。遅くなったら FTS 索引を検討 (Issue #56 のスコープ外)
    for src in srcs:
        for e in entries(src):
            if (errors_only and not e.is_error) or (tool and e.tool != tool) or q not in e.text.lower():
                continue
            hits.setdefault(e.trace_id, e)
    return sorted(hits.values(), key=lambda e: e.timestamp, reverse=True)[:limit]


def fmt(e: Entry, query: str = '') -> str:
    flat = ' '.join(e.text.split())
    i = max(flat.lower().find(query.lower()) - 40, 0) if query else 0
    s = e.source
    # ~ = session_id が無く時間窓で近似した transcript。trace_id をそのまま get に渡せるよう ID の後ろに置く
    mark = ' ' if s.exact else '~'
    err = ' ERROR' if e.is_error else ''
    head = f'{e.trace_id} {mark} {s.project}/{s.task_id} {s.worker} {e.timestamp[:19]} {e.tool}{err}'
    return f'{head}  {flat[i : i + SNIPPET]}'


def get(trace_id: str, context: int = 0) -> list[str]:
    """trace_id (<session_id>:<行>.<block>) と前後 context 行を、tool の入力と結果つきで返す.

    `<session_id>:<行>` だけなら、その行の全 block を返す.
    """
    session_id, _, pos = trace_id.lstrip('~').rpartition(':')
    line, _, blk = pos.partition('.')
    path = mtc.find_session_transcript(session_id)
    if not path or not line.isdigit() or not (blk.isdigit() or not blk):
        return [f'{trace_id}: transcript が見つかりません']
    src = Source('', '', '', path, exact=True)
    center = int(line)
    lo, hi = center - context, center + context
    out = []
    for e in entries(src):
        n, _, b = e.trace_id.rpartition(':')[2].partition('.')
        n = int(n)
        if lo <= n <= hi and not (n == center and blk and b != blk):
            err = ' ERROR' if e.is_error else ''
            out.append(f'--- {e.trace_id} {e.timestamp[:19]} {e.tool}{err}\n{e.text[:DETAIL_LIMIT]}')
        elif n > hi:
            break
    return out or [f'{trace_id}: tool_result / text の行がありません']
