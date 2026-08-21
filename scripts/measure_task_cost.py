#!/usr/bin/env python3
# ruff: noqa: CPY001
"""task 単位でモデル別の token / usage quota を集計する (SQUAD-258 Phase 0).

下位モデル / local-coder への委譲判断を「勘」ではなく実測に基づかせるための計測基盤。
task YAML の mtime (発注時刻) 〜 report YAML の `completed_at` (完了時刻) を区間として、
その区間に含まれる `~/.claude/projects/<encoded-cwd>/*.jsonl` 内の assistant message の
usage を合算し、`metrics/task_costs.jsonl` に追記する。

**セッション対応の限界 (重要)**: Claude Code の session transcript は「セッション起動時の
cwd」でディレクトリが決まり、`cd` で worktree に移動しても同じディレクトリに記録され続ける
(実測: worker が dedicated worktree で作業しても専用の `~/.claude/projects/` ディレクトリは
作られない)。かつ task/report YAML には sessionId が記録されていない。そのため同じ cwd で
複数 worker / Dispatcher が同時に活動していると、時間窓だけでは対象セッションを一意に
特定できない場合がある。本スクリプトは時間窓に重なる全 jsonl ファイルの assistant usage を
合算し、複数の異なる sessionId が窓に含まれていた場合は `notes` にその旨を記録する
(集計値が他セッション分を含み過大になっている可能性がある、という限界を可視化する)。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from ledger import parse_scalars  # noqa: E402


def _resolve_queue_root() -> Path:
    """`queue/` を実際に持つ主 checkout を指す (worktree 構成でどこから実行されても).

    `queue/` は gitignore 対象の実行時状態で、`git worktree add` で作られた副 worktree には
    存在しない (実測: `/home/gisen/work/squad` にしかない)。`git rev-parse --git-common-dir`
    はどの worktree から実行しても全 worktree 共通の主 `.git` を指すため、その親を辿れば
    スクリプト自身がどの worktree に置かれていても主 checkout の `queue/` に届く。
    git が使えない場合はスクリプトの置き場所を代わりに使う (フォールバック)。
    """
    script_dir = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ['git', '-C', str(script_dir), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return Path(result.stdout.strip()).parent
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return script_dir.parent


# REPO_ROOT: metrics/task_costs.jsonl の出力先。git 追跡対象なので、実行中の worktree
# (= このスクリプト自身の置き場所) に書く。QUEUE_ROOT と混同すると、副 worktree から実行
# したときに主 checkout の作業ツリー (別ブランチ) を汚してしまう。
REPO_ROOT = Path(__file__).resolve().parent.parent
QUEUE_ROOT = _resolve_queue_root()
CLAUDE_PROJECTS = Path.home() / '.claude' / 'projects'
METRICS_PATH = REPO_ROOT / 'metrics' / 'task_costs.jsonl'
USAGE_FIELDS = (
    ('input_tokens', 'input_tokens'),
    ('output_tokens', 'output_tokens'),
    ('cache_read_input_tokens', 'cache_read_tokens'),
    ('cache_creation_input_tokens', 'cache_creation_tokens'),
)


def find_task_yaml(project: str, task_id: str) -> Path | None:
    """Task YAML を現役 (worker{N}.yaml) → archive の順に探す."""
    tasks_dir = QUEUE_ROOT / 'queue' / 'projects' / project / 'tasks'
    for path in sorted(tasks_dir.glob('worker*.yaml')):
        meta = parse_scalars(path.read_text(encoding='utf-8', errors='replace'))
        if meta.get('task_id') == task_id:
            return path
    archive_dir = tasks_dir / 'archive'
    candidates = sorted(archive_dir.glob(f'worker*_{task_id}.yaml'))
    candidates += sorted(archive_dir.glob(f'worker*_{task_id}_*.yaml'))
    return candidates[0] if candidates else None


def find_report_yaml(project: str, task_id: str) -> Path | None:
    """Report YAML を現役 (worker{N}_report.yaml) → archive の順に探す."""
    reports_dir = QUEUE_ROOT / 'queue' / 'projects' / project / 'reports'
    for path in sorted(reports_dir.glob('worker*_report.yaml')):
        meta = parse_scalars(path.read_text(encoding='utf-8', errors='replace'))
        if meta.get('task_id') == task_id:
            return path
    archive_dir = reports_dir / 'archive'
    candidates = sorted(archive_dir.glob(f'worker*_report_{task_id}.yaml'))
    return candidates[0] if candidates else None


def extract_workspace(task_text: str) -> str | None:
    """`context.workspace:` の値を取り出す (parse_scalars はネストを読まないため専用の正規表現)."""
    for line in task_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('workspace:'):
            value = stripped.split(':', 1)[1].strip().strip('"\'')
            return value or None
    return None


def parse_iso8601(value: str) -> datetime | None:
    """ISO8601 (Z 終端 / +HH:MM オフセット / オフセット無し) を UTC の aware datetime にする."""
    if not value:
        return None
    text = value.strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def session_dirs_for(task_text: str) -> list[Path]:
    """候補となる session transcript ディレクトリ.

    dedicated worktree (`context.workspace`) を優先し、無ければ主 checkout (QUEUE_ROOT) に
    フォールバックする。dedicated workspace を持たない worker は主 checkout の cwd のまま
    セッションを開始することが実測で確認できたため (SQUAD-250 の worker1 が該当)。
    """
    candidates: list[str] = []
    workspace = extract_workspace(task_text)
    if workspace:
        candidates.append(workspace)
    candidates.append(str(QUEUE_ROOT))
    dirs: list[Path] = []
    seen: set[Path] = set()
    for cwd in candidates:
        encoded = cwd.rstrip('/').replace('/', '-')
        d = CLAUDE_PROJECTS / encoded
        if d.is_dir() and d not in seen:
            seen.add(d)
            dirs.append(d)
    return dirs


def collect_usage(session_dirs: list[Path], start: datetime | None, end: datetime | None) -> dict:
    """時間窓内の assistant message usage を合算する. どのファイルにも触れなくても例外を出さない."""
    totals = {field: 0 for _, field in USAGE_FIELDS}
    session_ids: set[str] = set()
    matched = 0
    corrupt_lines = 0
    usage_missing = 0
    for session_dir in session_dirs:
        for jsonl_path in sorted(session_dir.glob('*.jsonl')):
            try:
                lines = jsonl_path.read_text(encoding='utf-8', errors='replace').splitlines()
            except OSError:
                continue
            for line in lines:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    corrupt_lines += 1
                    continue
                if entry.get('type') != 'assistant':
                    continue
                ts = parse_iso8601(entry.get('timestamp', ''))
                if ts is None:
                    continue
                if start is not None and ts < start:
                    continue
                if end is not None and ts > end:
                    continue
                message = entry.get('message')
                if not isinstance(message, dict):
                    continue
                usage = message.get('usage')
                if not isinstance(usage, dict):
                    usage_missing += 1
                    continue
                matched += 1
                session_ids.add(entry.get('sessionId', ''))
                for src_key, dst_key in USAGE_FIELDS:
                    val = usage.get(src_key)
                    if isinstance(val, int):
                        totals[dst_key] += val
    return {
        'totals': totals,
        'matched_messages': matched,
        'corrupt_lines': corrupt_lines,
        'usage_missing': usage_missing,
        'session_ids': session_ids,
    }


def extract_attempts(report_meta: dict[str, str]) -> int | None:
    """verdict_path があれば `attempt:` を読む. 無ければ None (=不明)."""
    verdict_path = report_meta.get('verdict_path', '')
    if not verdict_path:
        return None
    path = Path(verdict_path)
    if not path.is_file():
        return None
    meta = parse_scalars(path.read_text(encoding='utf-8', errors='replace'))
    raw = meta.get('attempt')
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def measure(project: str, task_id: str) -> dict:
    """1 task 分の cost レコードを組み立てる (欠損は 0 ではなく None として記録する)."""
    notes: list[str] = []

    task_path = find_task_yaml(project, task_id)
    report_path = find_report_yaml(project, task_id)

    task_meta: dict[str, str] = {}
    task_text = ''
    start: datetime | None = None
    if task_path is None:
        notes.append('task YAML が見つからず集計区間の開始時刻が不明 (unknown)')
    else:
        task_text = task_path.read_text(encoding='utf-8', errors='replace')
        task_meta = parse_scalars(task_text)
        start = datetime.fromtimestamp(task_path.stat().st_mtime, tz=timezone.utc)

    report_meta: dict[str, str] = {}
    end: datetime | None = None
    if report_path is None:
        notes.append('report YAML が見つからず集計区間の終了時刻が不明 (unknown)')
    else:
        report_meta = parse_scalars(report_path.read_text(encoding='utf-8', errors='replace'))
        end = parse_iso8601(report_meta.get('completed_at', ''))
        if end is None:
            notes.append('report の completed_at が ISO8601 として解釈できず終了時刻が不明 (unknown)')

    session_dirs = session_dirs_for(task_text)
    if not session_dirs:
        notes.append('session transcript ディレクトリ (~/.claude/projects/...) が見つからない')

    if start is None or end is None:
        # 区間の片側が不明なまま集計すると、際限なく過去まで遡って無関係な session まで
        # 合算してしまい (実測: Dispatcher の長期セッション全体を拾って桁違いの値になった)、
        # 0 と見分けのつかない不正確な数値よりも unknown の方が安全なため集計自体を行わない。
        notes.append('集計区間が不完全 (開始/終了のいずれかが不明) なため usage 集計をスキップ (unknown)')
        usage = {'corrupt_lines': 0, 'usage_missing': 0, 'session_ids': set(), 'matched_messages': 0}
        totals = dict.fromkeys(('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'), 0)
        has_usage = False
    else:
        usage = collect_usage(session_dirs, start, end)
        if usage['corrupt_lines']:
            notes.append(f'JSONL 破損行を {usage["corrupt_lines"]} 件スキップ')
        if usage['usage_missing']:
            notes.append(f'usage フィールド欠損の assistant message を {usage["usage_missing"]} 件スキップ')
        if len(usage['session_ids']) > 1:
            notes.append(
                f'時間窓に {len(usage["session_ids"])} 個の異なる sessionId が含まれていた '
                '(同一 cwd での並行セッションと区別できず、他セッション分を含み過大の可能性)',
            )
        if usage['matched_messages'] == 0:
            notes.append('時間窓に一致する assistant message が 0 件 (usage は unknown)')
        has_usage = usage['matched_messages'] > 0
        totals = usage['totals']

    wall_clock_sec = (end - start).total_seconds() if (start is not None and end is not None) else None

    return {
        'task_id': task_id,
        'project': project,
        'worker': report_meta.get('worker', task_meta.get('assigned_to', 'unknown')),
        'agent': report_meta.get('agent', task_meta.get('agent', 'unknown')),
        'model': task_meta.get('model', 'unknown'),
        'status': report_meta.get('status', 'unknown'),
        'verify_status': report_meta.get('verify_status', 'unknown'),
        'attempts': extract_attempts(report_meta),
        'input_tokens': totals['input_tokens'] if has_usage else None,
        'output_tokens': totals['output_tokens'] if has_usage else None,
        'cache_read_tokens': totals['cache_read_tokens'] if has_usage else None,
        'cache_creation_tokens': totals['cache_creation_tokens'] if has_usage else None,
        'wall_clock_sec': wall_clock_sec,
        'measured_at': datetime.now(tz=timezone.utc).isoformat(),
        'notes': '; '.join(notes) if notes else '',
    }


def summarize(record: dict) -> str:
    return (
        f'{record["task_id"]} [{record["project"]}/{record["worker"]}] model={record["model"]} '
        f'status={record["status"]}/{record["verify_status"]} '
        f'in={record["input_tokens"]} out={record["output_tokens"]} '
        f'cache_read={record["cache_read_tokens"]} cache_creation={record["cache_creation_tokens"]} '
        f'wall_clock_sec={record["wall_clock_sec"]}'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--task-id', required=True)
    args = parser.parse_args()

    record = measure(args.project, args.task_id)

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    print(summarize(record))


if __name__ == '__main__':
    main()
