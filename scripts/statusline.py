#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Squad 用の Claude Code status line (#105, stdlib only).

stdin に Claude Code の status line JSON を受け取り、1 行を出す:
    w1 | Opus 5.5 | ctx 12% | SQUAD-301
同じ値を squad/state/<worker>.json の ctx_sources.statusline に書く (取得元 statusline と時刻つき)。
JSON の項目は docs/statusline-stdin-json.md (実測)。ctx は context_window.used_percentage (使用済みの割合)。

worker ID は env SQUAD_WORKER_ID (start.sh が worker の pane に設定する)。Dispatcher には無いので
`--name dispatcher` で渡す。どちらも無ければ表示だけして state には書かない。
task_id は queue/projects/*/tasks/worker<N>.yaml のうち、project の .squad_session が自分のセッション
(env SQUAD_SESSION) のもので最新のもの。同じ task_id の report の方が新しければ済んだものとして出さない。
何があっても status line は止めない (例外は握りつぶして 1 行は出す)。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / 'squad'))
import ctx  # noqa: E402

_WORKER_NUM = re.compile(r'^w(\d+)$')
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_-]+$')  # state のファイル名に使うので、パスになる文字は通さない
_TASK_ID = re.compile(r'^task_id\s*:\s*["\']?([^"\'#\s]+)', re.MULTILINE)


def queue_dir() -> Path:
    return Path(os.environ.get('SQUAD_QUEUE_DIR') or (REPO_ROOT / 'queue'))


def _task_id(path: Path) -> str:
    try:
        m = _TASK_ID.search(path.read_text(errors='replace'))
    except OSError:
        return ''
    return m.group(1) if m else ''


def current_task(worker: str, session: str) -> str:
    m = _WORKER_NUM.match(worker)
    if not m:
        return ''
    n = m.group(1)
    best: tuple[float, Path] | None = None
    for task in queue_dir().glob(f'projects/*/tasks/worker{n}.yaml'):
        pj = task.parent.parent
        try:
            owner = (pj / '.squad_session').read_text().strip()
            mtime = task.stat().st_mtime
        except OSError:
            continue
        if session and owner != session:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, task)
    if best is None:
        return ''
    task_id = _task_id(best[1])
    report = best[1].parent.parent / 'reports' / f'worker{n}_report.yaml'
    with contextlib.suppress(OSError):
        if report.stat().st_mtime >= best[0] and _task_id(report) == task_id:
            return ''
    return task_id


def parse(data: object) -> dict:
    data = data if isinstance(data, dict) else {}
    model = data.get('model') if isinstance(data.get('model'), dict) else {}
    cw = data.get('context_window') if isinstance(data.get('context_window'), dict) else {}
    pct = cw.get('used_percentage')
    return {
        'model': model.get('display_name') or model.get('id') or None,
        'pct': None if isinstance(pct, bool) or not isinstance(pct, (int, float)) else round(pct),
        'tokens': ctx.usage_tokens(cw.get('current_usage')),
        'window': ctx.as_int(cw.get('context_window_size')),
    }


def render(worker: str, values: dict, task_id: str) -> str:
    ctx_text = f'ctx {values["pct"]}%' if values['pct'] is not None else 'ctx 不明'
    parts = [worker, values['model'] or '-', ctx_text, task_id]
    return ' | '.join(p for p in parts if p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--name', default='', help='SQUAD_WORKER_ID が無いときの名前 (Dispatcher 用)')
    args = ap.parse_args(argv)
    worker = os.environ.get('SQUAD_WORKER_ID') or args.name
    session = os.environ.get('SQUAD_SESSION', '')
    try:
        data = json.loads(sys.stdin.read() or 'null')
    except ValueError:
        data = None
    values = parse(data)
    task_id = ''
    with contextlib.suppress(Exception):
        task_id = current_task(worker, session) if worker else ''
    print(render(worker, values, task_id))
    if worker and _SAFE_NAME.match(worker) and isinstance(data, dict):
        with contextlib.suppress(Exception):

            def write(st: dict) -> None:
                st['worker'] = worker
                ctx.set_slot(st, 'statusline', session=session, **values)
                ctx.apply(st, session)

            ctx.update_state(ctx.state_dir() / f'{worker}.json', write)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
