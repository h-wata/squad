#!/usr/bin/env python3
# ruff: noqa: CPY001
"""1 task 分の transcript から実行過程の指標を数え、`metrics/task_evals.jsonl` に 1 行追記する (SQUAD-354).

transcript の特定・読み込み・secret マスクは squad/trace_search.py をそのまま使う。承認保留の
禁止パターンは squad/watchd.py の APPROVAL_DENY_DEFAULT。時間窓は task YAML の mtime 以上、report の
`completed_at` 以下 (両端を含む)。同じ task を測り直すと追記が増えるが、読む側 (bench_report.py)
は最後の 1 件を使う。

使い方:
  eval_trajectory.py --project <pj> --task-id <id> [--queue-dir <dir>] [--out <jsonl>]
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'squad'))
sys.path.insert(0, str(ROOT / 'scripts'))
from ledger import parse_scalars  # noqa: E402
import trace_search  # noqa: E402
from watchd import APPROVAL_DENY_DEFAULT  # noqa: E402

DENY_RE = re.compile(APPROVAL_DENY_DEFAULT, re.IGNORECASE)
EXTERNAL_WRITE_RE = re.compile(r'\bgh\s+(pr|issue)\s+(create|comment|merge|close)\b|\bgit\s+push\b')
WORKSPACE_RE = re.compile(r'^[ \t]+workspace:[ \t]*["\']?([^\s"\']+)', re.MULTILINE)
WRITE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path'}
SAMPLE_LIMIT = 3
SAMPLE_CHARS = 120


def find_task_yaml(queue_dir: Path, project: str, task_id: str) -> Path | None:
    """tasks/ 直下を先に、無ければ tasks/archive/ 以下から task_id を持つ task YAML を探す."""
    tasks = queue_dir / 'projects' / project / 'tasks'
    for path in sorted(tasks.rglob('*.yaml'), key=lambda p: p.parent != tasks):
        try:
            if parse_scalars(path.read_text(encoding='utf-8', errors='replace')).get('task_id') == task_id:
                return path
        except OSError:
            continue
    return None


def _allowed(path: str, workspace: str, project: str) -> bool:
    p = os.path.normpath(path)
    if workspace and (p == workspace or p.startswith(workspace.rstrip('/') + '/')):
        return True
    return '/scratchpad/' in p or re.search(rf'/queue/projects/{re.escape(project)}/(reports|briefs)/', p) is not None


def _split(text: str) -> tuple[str, dict]:
    """Entry.text (`<入力 JSON>` の改行 `=> <結果>`、マスク済み) から入力の行と dict を取り出す."""
    head = text.split('\n=> ', 1)[0]
    try:
        d = json.loads(head)
    except ValueError:
        d = None
    return head, d if isinstance(d, dict) else {}


def evaluate(queue_dir: Path, project: str, task_id: str) -> dict | None:
    srcs, _ = trace_search.sources(queue_dir, project=project, task=task_id)
    if not srcs:
        return None
    task_path = find_task_yaml(queue_dir, project, task_id)
    m = WORKSPACE_RE.search(task_path.read_text(encoding='utf-8', errors='replace')) if task_path else None
    workspace = os.path.normpath(m.group(1)) if m else ''
    worker = srcs[0].worker

    seen: set[Path] = set()
    calls: list[tuple[str, str, dict, bool]] = []
    for src in srcs:
        if src.path in seen:
            continue
        seen.add(src.path)
        calls += [(e.tool, *_split(e.text), e.is_error) for e in trace_search.entries(src) if e.tool != 'text']

    keys = Counter((tool, head) for tool, head, _, _ in calls)
    ext = Counter(head for tool, head, _, _ in calls if tool == 'Bash' and EXTERNAL_WRITE_RE.search(head))
    denied = [head for tool, head, _, _ in calls if tool == 'Bash' and DENY_RE.search(head)]
    outside = 0
    for tool, _, inp, _ in calls:
        field = WRITE_TOOLS.get(tool)
        target = inp.get(field) if field else None
        if isinstance(target, str) and os.path.isabs(target) and not _allowed(target, workspace, project):
            outside += 1

    verdict = queue_dir / 'projects' / project / 'reports' / f'{worker}_verdict.yaml'
    attempt = (
        parse_scalars(verdict.read_text(encoding='utf-8', errors='replace')).get('attempt')
        if verdict.is_file()
        else None
    )
    return {
        'project': project,
        'task_id': task_id,
        'worker': worker,
        'tool_calls': len(calls),
        'tool_errors': sum(err for *_, err in calls),
        'retries': sum(n - 1 for n in keys.values()),
        'verify_ran': any(
            (tool in ('Agent', 'Task') and inp.get('subagent_type') == 'verifier')
            or (tool == 'Bash' and 'verify-task.sh' in head)
            for tool, head, inp, _ in calls
        ),
        'verify_attempt': int(attempt) if attempt and attempt.isdigit() else None,
        'memory_calls': sum(tool.startswith('mcp__kioku_mesh__') for tool, *_ in calls),
        'trace_search_calls': sum(tool == 'Bash' and 'squad trace' in head for tool, head, *_ in calls),
        'writes_outside_workspace': outside,
        'denied_commands': len(denied),
        'denied_samples': [h[:SAMPLE_CHARS] for h in denied[:SAMPLE_LIMIT]],
        'external_writes': sum(ext.values()),
        'external_write_dups': sum(n - 1 for n in ext.values()),
        'attribution': 'exact' if all(s.exact for s in srcs) else 'approximate',
        'measured_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }


def main(argv: list[str] | None = None) -> int:
    mtc = trace_search.mtc
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--queue-dir', type=Path, default=mtc.QUEUE_ROOT / 'queue')
    parser.add_argument('--out', type=Path, default=mtc.METRICS_PATH.parent / 'task_evals.jsonl')
    args = parser.parse_args(argv)
    rec = evaluate(args.queue_dir, args.project, args.task_id)
    if rec is None:
        print(f'{args.project}/{args.task_id}: report か transcript が見つかりません', file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    print(json.dumps(rec, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
