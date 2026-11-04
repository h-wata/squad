#!/usr/bin/env python3
# ruff: noqa: CPY001
"""ハーネス構成ごとのベンチマーク結果を suite × config で集計する (Issue #59).

`metrics/task_costs.jsonl` のうち `bench` を持つレコードを集め、Markdown の表を標準出力に出す。
同じ task を測り直したレコードは最後の 1 件だけを使う。数値は trial の中央値 (unknown は除く)。
手順は docs/benchmark.md。

重複作業率は、同じ task (intent) に report (result) が 2 件以上ある trial の割合。探索グラフの
索引 (`queue/.graph.db`) から数えるので、先に `squad graph sync` を実行しておく。索引が無ければ `-`。

使い方:
  bench_report.py [--metrics <jsonl>] [--graph-db <db>]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_task_cost import METRICS_PATH  # noqa: E402
from measure_task_cost import QUEUE_ROOT  # noqa: E402

COLUMNS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens', 'wall_clock_sec', 'attempts')


def load_bench_records(path: Path) -> list[dict]:
    """Bench 付きのレコードを、(suite, config, trial) ごとに最後の 1 件だけ返す.

    同じ trial を 2 回数えると trials・成功率・中央値がずれる。別の task に同じ trial 番号を
    付けていたら、後のものを採って stderr に出す (測り直しは同じ task なので黙って置き換える).
    """
    latest: dict[tuple[str, str, str], dict] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not (isinstance(rec, dict) and isinstance(rec.get('bench'), dict)):
            continue
        key = tuple(str(rec['bench'].get(k, '')) for k in ('suite', 'config', 'trial'))
        old = latest.get(key)
        if old and (old.get('project'), old.get('task_id')) != (rec.get('project'), rec.get('task_id')):
            print(
                f'bench {key} が {old.get("task_id")} と {rec.get("task_id")} で重複しています。'
                f'{rec.get("task_id")} を使います',
                file=sys.stderr,
            )
        latest[key] = rec
    return list(latest.values())


def result_counts(db_path: Path, records: list[dict]) -> dict[tuple[str, str], int] | None:
    """Task ごとの report 件数 (cross-review の report は除く). 索引が無ければ None."""
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
    try:
        return {
            (r['project'], r['task_id']): conn.execute(
                'SELECT COUNT(*) FROM edges e JOIN nodes n ON n.node_id = e.dst_id '
                "WHERE e.src_id = ? AND e.rel = 'yields' AND n.kind = 'result' "
                "AND NOT json_extract(n.payload_json, '$.review')",
                (f'intent:{r["project"]}/{r["task_id"]}',),
            ).fetchone()[0]
            for r in records
        }
    finally:
        conn.close()


def _median(values: list) -> str:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return f'{median(nums):.0f}' if nums else '-'


def render(records: list[dict], counts: dict[tuple[str, str], int] | None) -> list[str]:
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        groups.setdefault((str(r['bench'].get('suite', '')), str(r['bench'].get('config', ''))), []).append(r)
    lines = [
        '| suite | config | trials | 成功 | ' + ' | '.join(COLUMNS) + ' | 重複作業 |',
        '|---' * (len(COLUMNS) + 5) + '|',
    ]
    for (suite, config), rs in sorted(groups.items()):
        ok = sum(r.get('status') == 'completed' and r.get('verify_status') == 'pass' for r in rs)
        dup = '-' if counts is None else f'{sum(counts[r["project"], r["task_id"]] >= 2 for r in rs)}/{len(rs)}'
        cells = [_median([r.get(c) for r in rs]) for c in COLUMNS]
        lines.append(f'| {suite} | {config} | {len(rs)} | {ok}/{len(rs)} | ' + ' | '.join(cells) + f' | {dup} |')
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--metrics', type=Path, default=METRICS_PATH)
    parser.add_argument('--graph-db', type=Path, default=QUEUE_ROOT / 'queue' / '.graph.db')
    args = parser.parse_args(argv)
    records = load_bench_records(args.metrics) if args.metrics.is_file() else []
    if not records:
        print(f'{args.metrics} に bench 付きのレコードがありません', file=sys.stderr)
        return 1
    counts = result_counts(args.graph_db, records)
    if counts is None:
        print(f'{args.graph_db} が無いため重複作業率は - (squad graph sync で作れる)', file=sys.stderr)
    print('\n'.join(render(records, counts)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
