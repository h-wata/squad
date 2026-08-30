#!/usr/bin/env python3
# ruff: noqa: CPY001
"""task 単位でモデル別の token / usage quota を集計する (SQUAD-258 Phase 0 / SQUAD-261 / SQUAD-265).

下位モデル / local-coder への委譲判断を「勘」ではなく実測に基づかせるための計測基盤。
task YAML の mtime (発注時刻) 〜 report YAML の `completed_at` (完了時刻) を区間として、
その区間に含まれる `~/.claude/projects/<encoded-cwd>/*.jsonl` 内の assistant message の
usage を合算し、`metrics/task_costs.jsonl` に追記する。

**セッション単位の一意特定 (SQUAD-261)**: report YAML に `session_id` (worker 実行時の
`$CLAUDE_CODE_SESSION_ID`) が記録されていれば、`~/.claude/projects/*/<session_id>.jsonl` を
直接特定して集計する (`attribution: "exact"`)。session_id が無い古い report は、cwd 推測 +
時間窓で重なる全 jsonl を合算する従来の近似にフォールバックする (`attribution:
"approximate"`)。近似モードでは、Claude Code の session transcript が「セッション起動時の
cwd」で固定され (dedicated worktree に `cd` しても専用ディレクトリは作られない)、同じ cwd で
複数 worker / Dispatcher が同時に活動していると時間窓だけでは対象セッションを分離できない
という限界がある (複数の異なる sessionId が窓に含まれていた場合は `notes` に明記する)。

**quota (5h/7d リミット) 消費の計測 (SQUAD-265)**: token 数とは別に、squad の律速である
5h/7d usage limit の実消費率 (%) を扱う。データ源は `/tmp/claude_usage_cache.json`
(Claude Code のカスタム statusLine スクリプトが公式 OAuth usage API から取得しキャッシュした
もの。本スクリプトは credential にはアクセスせず、既にキャッシュされたこのファイルを読むのみ)、
無ければ自 pane の `tmux capture-pane`（`$TMUX_PANE` の自分自身のみ、他 pane は読まない）で
ステータス行の `5h:NN%` / `7d:NN%` 表示を読む。`--quota-snapshot` でこの値を JSON 出力でき、
worker が task 着手前後に呼んで report YAML の `quota_5h_before_pct` 等に記録する運用を
想定する (2 点が揃わない限り delta は計算できないため、記録は worker の運用に依存する)。

**重要な限界 (実測で確認)**: この 5h/7d 使用率は **アカウント全体で共有される値**であり、
同時に開いている全 pane (Dispatcher・W1〜W3 のどれか) で同一の値を示す。並行して他 worker が
稼働していると、単一 task の消費だけを分離することはできない。また表示は整数 % の丸め値で
粒度が粗く、`resets_at` を跨ぐと差分が負になり得る。モデル別 (Sonnet:Haiku 等) の重み比は、
観測できた API レスポンスに Sonnet/Haiku 個別の scoped entry が存在しなかったため実測できて
おらず「不明」として扱う (詳細は SQUAD-265 report 参照)。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import re
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
QUOTA_CACHE_PATH = Path('/tmp/claude_usage_cache.json')
_TMUX_PCT_RE = re.compile(r'(5h|7d):(\d+)%')
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


def find_session_transcript(session_id: str) -> Path | None:
    """`session_id` から transcript ファイルを横断的に特定する (exact attribution 用).

    cwd 推測に頼らず `~/.claude/projects/*/<session_id>.jsonl` を直接探す。ファイル名は
    sessionId と一致する (実測で確認済み: `$CLAUDE_CODE_SESSION_ID` == transcript ファイル名
    == 各行の `sessionId` フィールド)。
    """
    if not session_id or not CLAUDE_PROJECTS.is_dir():
        return None
    matches = sorted(CLAUDE_PROJECTS.glob(f'*/{session_id}.jsonl'))
    return matches[0] if matches else None


def read_quota_cache(path: Path) -> dict | None:
    """`/tmp/claude_usage_cache.json` から 5h/7d 使用率 (%) を読む. credential にはアクセスしない."""
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    data = raw.get('data')
    if not isinstance(data, dict):
        return None
    try:
        five_hour = data.get('five_hour', {}).get('utilization')
        seven_day = data.get('seven_day', {}).get('utilization')
    except AttributeError:
        return None
    if not isinstance(five_hour, (int, float)) or not isinstance(seven_day, (int, float)):
        return None
    return {'five_hour_pct': int(five_hour), 'seven_day_pct': int(seven_day)}


def parse_tmux_status_line(text: str) -> dict | None:
    """`5h:NN%` / `7d:NN%` を含むテキストから使用率 (%) を抽出する (tmux capture-pane 出力用)."""
    found = dict(_TMUX_PCT_RE.findall(text))
    if '5h' not in found or '7d' not in found:
        return None
    return {'five_hour_pct': int(found['5h']), 'seven_day_pct': int(found['7d'])}


def capture_own_tmux_pane() -> str | None:
    """自分自身の tmux pane (`$TMUX_PANE`) の内容のみを読む. 他 pane は対象にしない."""
    pane = os.environ.get('TMUX_PANE')
    if not pane:
        return None
    try:
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', pane, '-p'],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return None


def get_quota_snapshot() -> dict:
    """現時点の 5h/7d 使用率 (%) を取得する. `--quota-snapshot` および worker の手動記録用."""
    cached = read_quota_cache(QUOTA_CACHE_PATH)
    if cached is not None:
        return {**cached, 'source': 'usage_cache_file'}
    pane_text = capture_own_tmux_pane()
    if pane_text is not None:
        parsed = parse_tmux_status_line(pane_text)
        if parsed is not None:
            return {**parsed, 'source': 'tmux_status_line'}
    return {'five_hour_pct': None, 'seven_day_pct': None, 'source': 'unavailable'}


def collect_usage(jsonl_paths: list[Path], start: datetime | None, end: datetime | None) -> dict:
    """時間窓内の assistant message usage を合算する. どのファイルにも触れなくても例外を出さない."""
    totals = {field: 0 for _, field in USAGE_FIELDS}
    session_ids: set[str] = set()
    matched = 0
    corrupt_lines = 0
    usage_missing = 0
    for jsonl_path in jsonl_paths:
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


def extract_quota_deltas(report_meta: dict[str, str]) -> dict:
    """Report の `quota_*_{before,after}_pct` から delta を計算する. 揃わなければ None (=未取得)."""

    def to_int(key: str) -> int | None:
        raw = report_meta.get(key, '')
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    before_5h, after_5h = to_int('quota_5h_before_pct'), to_int('quota_5h_after_pct')
    before_7d, after_7d = to_int('quota_7d_before_pct'), to_int('quota_7d_after_pct')
    return {
        'quota_5h_delta_pct': after_5h - before_5h if before_5h is not None and after_5h is not None else None,
        'quota_7d_delta_pct': after_7d - before_7d if before_7d is not None and after_7d is not None else None,
    }


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

    session_id = report_meta.get('session_id', '')
    attribution = 'approximate'
    jsonl_paths: list[Path] = []
    if session_id:
        exact_path = find_session_transcript(session_id)
        if exact_path is not None:
            jsonl_paths = [exact_path]
            attribution = 'exact'
        else:
            notes.append(
                f'report に session_id ({session_id}) はあるが transcript ファイルが '
                '見つからず approximate にフォールバック',
            )

    if attribution == 'approximate':
        session_dirs = session_dirs_for(task_text)
        if not session_dirs:
            notes.append('session transcript ディレクトリ (~/.claude/projects/...) が見つからない')
        for session_dir in session_dirs:
            jsonl_paths.extend(sorted(session_dir.glob('*.jsonl')))

    if start is None or end is None:
        # 区間の片側が不明なまま集計すると、際限なく過去まで遡って無関係な session まで
        # 合算してしまい (実測: Dispatcher の長期セッション全体を拾って桁違いの値になった)、
        # 0 と見分けのつかない不正確な数値よりも unknown の方が安全なため集計自体を行わない。
        notes.append('集計区間が不完全 (開始/終了のいずれかが不明) なため usage 集計をスキップ (unknown)')
        usage = {'corrupt_lines': 0, 'usage_missing': 0, 'session_ids': set(), 'matched_messages': 0}
        totals = dict.fromkeys(('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'), 0)
        has_usage = False
    else:
        usage = collect_usage(jsonl_paths, start, end)
        if usage['corrupt_lines']:
            notes.append(f'JSONL 破損行を {usage["corrupt_lines"]} 件スキップ')
        if usage['usage_missing']:
            notes.append(f'usage フィールド欠損の assistant message を {usage["usage_missing"]} 件スキップ')
        if attribution == 'approximate' and len(usage['session_ids']) > 1:
            notes.append(
                f'時間窓に {len(usage["session_ids"])} 個の異なる sessionId が含まれていた '
                '(同一 cwd での並行セッションと区別できず、他セッション分を含み過大の可能性)',
            )
        if usage['matched_messages'] == 0:
            notes.append('時間窓に一致する assistant message が 0 件 (usage は unknown)')
        has_usage = usage['matched_messages'] > 0
        totals = usage['totals']

    wall_clock_sec = (end - start).total_seconds() if (start is not None and end is not None) else None

    quota = extract_quota_deltas(report_meta)
    for label, delta in (('5h', quota['quota_5h_delta_pct']), ('7d', quota['quota_7d_delta_pct'])):
        if delta is not None and delta < 0:
            notes.append(f'quota_{label}_delta_pct が負 ({delta}) — 集計区間中に usage limit の reset を跨いだ可能性')

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
        'attribution': attribution,
        'quota_5h_delta_pct': quota['quota_5h_delta_pct'],
        'quota_7d_delta_pct': quota['quota_7d_delta_pct'],
        'quota_source': report_meta.get('quota_source', ''),
        'measured_at': datetime.now(tz=timezone.utc).isoformat(),
        'notes': '; '.join(notes) if notes else '',
    }


def summarize(record: dict) -> str:
    return (
        f'{record["task_id"]} [{record["project"]}/{record["worker"]}] model={record["model"]} '
        f'status={record["status"]}/{record["verify_status"]} attribution={record["attribution"]} '
        f'in={record["input_tokens"]} out={record["output_tokens"]} '
        f'cache_read={record["cache_read_tokens"]} cache_creation={record["cache_creation_tokens"]} '
        f'wall_clock_sec={record["wall_clock_sec"]} '
        f'quota_5h_delta_pct={record["quota_5h_delta_pct"]} quota_7d_delta_pct={record["quota_7d_delta_pct"]}'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project')
    parser.add_argument('--task-id')
    parser.add_argument(
        '--quota-snapshot',
        action='store_true',
        help=(
            '現時点の 5h/7d 使用率 (%) を JSON で標準出力に印字して終了する。'
            'worker が task 着手前後に呼び、report YAML の quota_5h_before_pct 等に'
            '手動で転記する運用を想定 (--project/--task-id とは併用しない)。'
        ),
    )
    args = parser.parse_args()

    if args.quota_snapshot:
        print(json.dumps(get_quota_snapshot(), ensure_ascii=False))
        return

    if not args.project or not args.task_id:
        parser.error('--project と --task-id が必要です (または --quota-snapshot 単独で指定)')

    record = measure(args.project, args.task_id)

    METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_PATH.open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')

    print(summarize(record))


if __name__ == '__main__':
    main()
