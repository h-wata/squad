#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Task 完了時の自動評価 (Refs #130, #136). watchd が配達した最終 report ごとに 1 行を task_evals.jsonl に足す.

watchd (squad/watchd.py) は report の配達を確定した後、このファイルを子プロセスとして起動する
(`python3 squad/task_eval.py --project ... --task-id ... --report-id ...`)。子は
scripts/eval_trajectory.py の evaluate() で 1 task 分を数え、report の情報を足して 1 行追記する。

- 評価するのは status が completed / blocked の report だけ (FINAL_STATUSES)。progress などの途中の report と
  review は評価しない。failed は task YAML の退避では終わった扱いだが、ここでは評価しない。
- 重複排除のキーは report_id。追記はファイルの flock の下で「同じ report_id の行が既にあるか」を見てから行う。
  watchd の再起動・再配達・複数の watchd が重なっても 2 行目は出ない。手で走らせた eval_trajectory.py の
  行 (report_id 無し) はキーにならない。
- 評価の例外・import 失敗・時間切れ (--timeout 秒、SIGALRM) は `error` に理由を入れた行として残す。
  子が行を書けずに終わったときは watchd が error 行を書く (watchd.Watcher.poll_task_evals)。

stdlib only (watchd から import する。eval_trajectory は watchd を import するので main() の中でだけ読む)。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable
import contextlib
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import traceback

ROOT = Path(__file__).resolve().parent.parent
FINAL_STATUSES = frozenset({'completed', 'blocked'})
ERROR_CHARS = 2000
EXIT_WRITTEN = 0
EXIT_DUPLICATE = 3  # 同じ report_id の行が既にあったので書かなかった


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _ids(lines: Iterable[str]) -> set[str]:
    ids: set[str] = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get('report_id'), str) and rec['report_id']:
            ids.add(rec['report_id'])
    return ids


def evaluated_ids(out: Path) -> set[str]:
    """既に out に行がある report_id の集合. ファイルが無ければ空."""
    try:
        with out.open(encoding='utf-8', errors='replace') as f:
            return _ids(f)
    except FileNotFoundError:
        return set()


def append_row(out: Path, row: dict) -> bool:
    """Row を 1 行追記する. 同じ report_id の行が既にあれば書かずに False.

    確認と追記を同じ flock の中で行うので、並行して書く watchd / 子プロセスがいても重複しない。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('a+', encoding='utf-8') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            if row['report_id'] in _ids(f):
                return False
            f.seek(0, os.SEEK_END)
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
            return True
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def base_row(project: str, task_id: str, report_id: str, worker: str, agent: str, status: str) -> dict:
    """行の共通部分. 評価の出力 (eval_trajectory.evaluate の dict) はこの後ろに平らに足す."""
    return {
        'project': project,
        'task_id': task_id,
        'report_id': report_id,
        'worker': worker,
        'agent': agent,
        'status': status,
        'evaluated_at': now_iso(),
        'source': 'watchd',
        'error': None,
    }


def error_row(base: dict, error: str) -> dict:
    return {**base, 'evaluated_at': now_iso(), 'error': error[-ERROR_CHARS:]}


class EvalTimeoutError(Exception):
    pass


def _on_alarm(_signum: int, _frame: object) -> None:
    raise EvalTimeoutError


def run(args: argparse.Namespace) -> dict:
    """評価して行を作る. 失敗は例外にせず error 付きの行で返す."""
    base = base_row(args.project, args.task_id, args.report_id, args.worker, args.agent, args.status)
    if args.timeout > 0:
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(args.timeout)
    try:
        sys.path.insert(0, str(ROOT / 'scripts'))
        import eval_trajectory  # noqa: PLC0415 (watchd を import するので、watchd からの import 時には読まない)

        rec = eval_trajectory.evaluate(args.queue_dir, args.project, args.task_id)
    except EvalTimeoutError:
        return error_row(base, f'timeout: 評価が {args.timeout}s で終わらなかった')
    except Exception:  # noqa: BLE001 (評価の失敗は行に残し、配達には影響させない)
        return error_row(base, traceback.format_exc())
    finally:
        with contextlib.suppress(ValueError):
            signal.alarm(0)
    if rec is None:
        return error_row(base, 'not_found: report か transcript が見つからない')
    # evaluate の worker / project / task_id は report と同じ値。report 由来の値 (report_id 等) を優先する
    return {**base, **rec, **{k: base[k] for k in ('project', 'task_id', 'report_id', 'worker')}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--report-id', required=True)
    parser.add_argument('--worker', default='')
    parser.add_argument('--agent', default='')
    parser.add_argument('--status', default='')
    parser.add_argument('--queue-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=0, help='評価の時間上限 (秒, 0 で無制限)')
    args = parser.parse_args(argv)
    if args.report_id in evaluated_ids(args.out):
        return EXIT_DUPLICATE
    row = run(args)
    return EXIT_WRITTEN if append_row(args.out, row) else EXIT_DUPLICATE


if __name__ == '__main__':
    sys.exit(main())
