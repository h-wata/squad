#!/usr/bin/env python3
# ruff: noqa: CPY001
"""1 task 分の transcript から実行過程の指標を数え、`metrics/task_evals.jsonl` に 1 行追記する (SQUAD-354).

transcript の特定・読み込み・secret マスクは squad/trace_search.py をそのまま使う。承認保留の
禁止パターンは squad/watchd.py の APPROVAL_DENY_DEFAULT。時間窓は task YAML の mtime 以上、report の
`completed_at` 以下 (両端を含む)。同じ task を測り直すと追記が増えるが、読む側 (bench_report.py)
は最後の 1 件を使う。

指標 `deny_pattern_matches` は、承認保留の禁止パターンに一致した Bash 入力の試行数で、実際に実行された
(deny・承認保留をすり抜けた) かどうかは示さない。実行されたかは transcript で確認が要る。

`writes_outside_workspace` の許可 workspace は task YAML の context.workspace と、同じ task の report の
source_worktree (top-level と source_tree_clean 配下の全部)。1 つも取れない task は 0 ではなく null。

`verify_attempt` / `verify_result` は最後の verdict の attempt と result。watchd が attempt ごとに写した
`queue/projects/<pj>/verdicts/` と今の verdict から、task_id が一致するものだけを読む。どちらにも無ければ null。
再試行で pass したかは `verify_attempt >= 2` かつ `verify_result == "pass"` で分かる。

バッチ評価 (`--backfill`): 全 project の `reports/archive/` にある最終 report (status が completed / blocked、
`report_id` あり、ファイル名に review を含まないもの) のうち、`--out` にその report_id の error の無い行がまだ
無いものだけを評価して追記する。再実行しても評価済みの report_id の行は増えない。transcript が見つからない
report は行を書かず stderr に出す。それ以外の評価失敗は `error` に理由を入れた行で残し、次回やり直す
(bench_report.py は error 行を集計に使わない)。transcript は 30 日で消えるので、
それより短い間隔 (週 1 回) で回す。手順は docs/benchmark.md。

使い方:
  eval_trajectory.py --project <pj> --task-id <id> [--queue-dir <dir>] [--out <jsonl>]
  eval_trajectory.py --backfill [--queue-dir <dir>] [--out <jsonl>]
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import glob as globmod
import json
import os
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'squad'))
sys.path.insert(0, str(ROOT / 'scripts'))
from ledger import parse_scalars  # noqa: E402
import trace_search  # noqa: E402
from watchd import APPROVAL_DENY_DEFAULT  # noqa: E402
from watchd import ARCHIVE_NAME_UNSAFE_RE  # noqa: E402
from watchd import VERDICT_HISTORY_DIR  # noqa: E402
from watchd import VERDICT_RESULTS  # noqa: E402
from watchd import verdict_history_name  # noqa: E402,F401 (テストが写しの名前を作るのに使う)

DENY_RE = re.compile(APPROVAL_DENY_DEFAULT, re.IGNORECASE)
EXTERNAL_WRITE_RE = re.compile(r'\bgh\s+(pr|issue)\s+(create|comment|merge|close)\b|\bgit\s+push\b')
WORKSPACE_RE = re.compile(r'^[ \t]+workspace:[ \t]*["\']?([^\s"\']+)', re.MULTILINE)
REVIEW_CONTENT_KEYS = frozenset({'verdict', 'reviewed_pr'})
WRITE_TOOLS = {'Edit': 'file_path', 'Write': 'file_path', 'MultiEdit': 'file_path', 'NotebookEdit': 'notebook_path'}
FINAL_STATUSES = frozenset({'completed', 'blocked'})
ERROR_CHARS = 2000
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


def is_review_report(path: Path, doc: object) -> bool:
    """Review report か. ファイル名に review を含む、または内容に verdict / reviewed_pr を持つ.

    reviewed_head_sha は author の report にも出るので判定に使わない。doc が dict でない (parse できない) ときは
    ファイル名だけで判定する。
    """
    return 'review' in path.name or (isinstance(doc, dict) and bool(REVIEW_CONTENT_KEYS & doc.keys()))


def report_worktrees(queue_dir: Path, project: str, task_id: str) -> list[str]:
    """同じ task の author の report (reports/ と reports/archive/) の source_worktree を集める.

    review report (is_review_report) は除く。
    YAML を parse し、トップレベルの source_worktree と source_tree_clean 配下
    (マッピング単一 / リスト複数) の source_worktree だけを採る。parse できない report は無視する。
    """
    reports = queue_dir / 'projects' / project / 'reports'
    found: list[str] = []
    for path in sorted([*reports.glob('*.yaml'), *reports.glob('archive/*.yaml')]):
        try:
            doc = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace'))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict) or str(doc.get('task_id', '')).strip() != task_id:
            continue
        if is_review_report(path, doc):
            continue
        clean = doc.get('source_tree_clean')
        entries = [doc, *(clean if isinstance(clean, list) else [clean])]
        for entry in entries:
            w = entry.get('source_worktree') if isinstance(entry, dict) else None
            if isinstance(w, str) and os.path.isabs(w.strip()):
                found.append(os.path.normpath(w.strip()))
    return list(dict.fromkeys(found))


def _allowed(path: str, workspace: str | list[str], project: str) -> bool:
    p = os.path.normpath(path)
    for ws in [workspace] if isinstance(workspace, str) else workspace:
        if ws and (p == ws or p.startswith(ws.rstrip('/') + '/')):
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


def final_verdict(queue_dir: Path, project: str, task_id: str, worker: str) -> tuple[int | None, str | None]:
    """Task の最後の verdict の (attempt, result). 見つからなければ (None, None) (SQUAD-397).

    候補は watchd が attempt ごとに写した verdicts/ と、今の reports/<worker>_verdict.yaml。
    中身の task_id が一致し、result が VERDICT_RESULTS のどれかのものだけを採り
    (別 task や書きかけの verdict を採らない)、mtime が最も新しいものを最後とみなす。差し戻しで
    attempt が 1 から数え直されても、古い回の大きな attempt を採らないため。
    """
    pj = queue_dir / 'projects' / project
    hist = pj / VERDICT_HISTORY_DIR
    prefix = globmod.escape(ARCHIVE_NAME_UNSAFE_RE.sub('_', task_id))
    paths = sorted(hist.glob(f'{prefix}__attempt*.yaml')) if hist.is_dir() else []
    paths.append(pj / 'reports' / f'{worker}_verdict.yaml')
    found: list[tuple[float, int, str | None]] = []
    for p in paths:
        try:
            text = p.read_text(encoding='utf-8', errors='replace')
            mtime = p.stat().st_mtime
        except OSError:
            continue
        s = parse_scalars(text)
        attempt, result = s.get('attempt', '').strip(), s.get('result', '').strip()
        # result の無い (書きかけの) verdict は最後の verdict として採らない
        if s.get('task_id', '').strip() != task_id or not attempt.isdigit() or result not in VERDICT_RESULTS:
            continue
        found.append((mtime, int(attempt), result))
    if not found:
        return None, None
    _, attempt, result = max(found)
    return attempt, result


def evaluate(queue_dir: Path, project: str, task_id: str) -> dict | None:
    srcs, _ = trace_search.sources(queue_dir, project=project, task=task_id)
    if not srcs:
        return None
    task_path = find_task_yaml(queue_dir, project, task_id)
    m = WORKSPACE_RE.search(task_path.read_text(encoding='utf-8', errors='replace')) if task_path else None
    # 許可する workspace は task YAML の context.workspace と report の source_worktree 全部
    workspaces = list(
        dict.fromkeys(([os.path.normpath(m.group(1))] if m else []) + report_worktrees(queue_dir, project, task_id))
    )
    worker = srcs[0].worker

    seen: set[Path] = set()
    calls: list[tuple[str, str, dict, bool]] = []
    for src in srcs:
        if src.path in seen:
            continue
        seen.add(src.path)
        calls += [(e.tool, *_split(e.text), e.is_error) for e in trace_search.entries(src) if e.tool != 'text']

    keys = Counter((tool, head) for tool, head, _, _ in calls)
    # 外部書き込みは「試行」を数える。is_error の呼び出しは実行されていないので数えない
    ext = Counter(
        head for tool, head, _, err in calls if tool == 'Bash' and not err and EXTERNAL_WRITE_RE.search(head)
    )
    denied = [head for tool, head, _, _ in calls if tool == 'Bash' and DENY_RE.search(head)]
    outside = 0
    for tool, _, inp, _ in calls:
        field = WRITE_TOOLS.get(tool)
        target = inp.get(field) if field else None
        if isinstance(target, str) and os.path.isabs(target) and not _allowed(target, workspaces, project):
            outside += 1

    attempt, result = final_verdict(queue_dir, project, task_id, worker)
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
        'verify_attempt': attempt,
        'verify_result': result,
        'memory_calls': sum(tool.startswith('mcp__kioku_mesh__') for tool, *_ in calls),
        'trace_search_calls': sum(tool == 'Bash' and 'squad trace' in head for tool, head, *_ in calls),
        # workspace が 1 つも取れないときは 0 件ではなく判定不能 (null)
        'writes_outside_workspace': outside if workspaces else None,
        'deny_pattern_matches': len(denied),
        'denied_samples': [h[:SAMPLE_CHARS] for h in denied[:SAMPLE_LIMIT]],
        'external_write_attempts': sum(ext.values()),
        'external_write_attempt_dups': sum(n - 1 for n in ext.values()),
        'attribution': 'exact' if all(s.exact for s in srcs) else 'approximate',
        'measured_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }


def evaluated_ids(out: Path) -> set[str]:
    """既に out に評価済みの行 (error が空) がある report_id の集合. ファイルが無ければ空.

    error 行は評価済みに数えない (一時的に読めなかっただけの report を次回やり直すため)。
    """
    ids: set[str] = set()
    if not out.is_file():
        return ids
    for line in out.read_text(encoding='utf-8', errors='replace').splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(rec, dict)
            and isinstance(rec.get('report_id'), str)
            and rec['report_id']
            and not rec.get('error')
        ):
            ids.add(rec['report_id'])
    return ids


def backfill(queue_dir: Path, out: Path) -> int:
    """未評価の最終 report だけを評価して out に追記する. 追記した行数を返す."""
    done = evaluated_ids(out)
    rows: list[dict] = []
    for path in sorted((queue_dir / 'projects').glob('*/reports/archive/*.yaml')):
        text = path.read_text(encoding='utf-8', errors='replace')
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            doc = None  # parse できない report はファイル名だけで判定する
        if is_review_report(path, doc):
            continue
        s = parse_scalars(text)
        report_id, task_id = s.get('report_id', '').strip(), s.get('task_id', '').strip()
        if not report_id or not task_id or s.get('status', '').strip() not in FINAL_STATUSES or report_id in done:
            continue
        done.add(report_id)
        project = path.parents[2].name
        base = {'report_id': report_id, 'status': s['status'].strip(), 'source': 'backfill', 'error': None}
        try:
            rec = evaluate(queue_dir, project, task_id)
            if rec is None:  # 行を書くと毎回 error 行が増えるので stderr にだけ出す
                print(f'{project}/{task_id} ({report_id}): report か transcript が見つかりません', file=sys.stderr)
                continue
            row = {**rec, **base}
        except Exception as e:  # noqa: BLE001 (1 件の失敗で残りを止めず、error 行に残す)
            row = {'project': project, 'task_id': task_id, **base, 'error': f'{type(e).__name__}: {e}'[-ERROR_CHARS:]}
        row.setdefault('measured_at', datetime.now().astimezone().isoformat(timespec='seconds'))
        rows.append(row)
    if rows:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('a', encoding='utf-8') as f:
            if out.stat().st_size > 0 and out.read_bytes()[-1:] != b'\n':  # 末尾改行なしの既存行と結合させない
                f.write('\n')
            f.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    mtc = trace_search.mtc
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project')
    parser.add_argument('--task-id')
    parser.add_argument('--backfill', action='store_true', help='未評価の最終 report だけを評価して追記する')
    parser.add_argument('--queue-dir', type=Path, default=mtc.QUEUE_ROOT / 'queue')
    parser.add_argument('--out', type=Path, default=mtc.METRICS_PATH.parent / 'task_evals.jsonl')
    args = parser.parse_args(argv)
    if args.backfill:
        print(f'{backfill(args.queue_dir, args.out)} 件を追記しました')
        return 0
    if not (args.project and args.task_id):
        parser.error('--project と --task-id が必要です (または --backfill)')
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
