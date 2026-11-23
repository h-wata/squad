#!/usr/bin/env python3
# ruff: noqa: CPY001
"""Review の指摘を category ごとに数え、Markdown の表を標準出力に出す (Issue #136, Refs #130).

入力は `queue/projects/*/reports/` 以下 (archive/ を含む) の YAML。数えるのは 2 種類:
  - cross-review の指摘。ファイル名に `review` を含むか `verdict` を持つ YAML の `findings` / `blocking` /
    `non_blocking` の要素 (書式は揃えていないので両方の形を読む。`findings` の下に `blocking` 等が入れ子になった
    形も読む)。`category` の無い指摘は `uncategorized` として件数だけ数える (過去分に遡って付けない)
  - worker の report の `issues` の要素のうち `category` を持つもの。`category` の無い issues は数えない
同じ report (配達前の `worker4_review.yaml` と reports/archive/ の写し) は report_id (無ければ中身のハッシュ) で
1 件にする。category は語彙 (VOCABULARY) の外の値もそのまま数える。

category ごとに 件数 / 初出 / 直近 / 該当 PR を出す。日時は report の `completed_at`、無ければファイルの mtime
(mtime は archive への写しで進むことがあるので近似。表の下に注記する)。

使い方:
  review_category_tally.py [--queue-dir <queue>] [--json] [--inbox <_inbox.md>]

`--inbox` を付けると、同じ category (uncategorized 以外) が 2 件以上になったものを `queue/_inbox.md` に
チェック化の候補として 1 行ずつ足す。行には `category:<name>` の印を入れ、同じ印の行が既にあれば足さない。
`squad graph sync` も同じ集計を `queue/.graph.db` の `review_categories` 表に書く (web dashboard の読み元)。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

# 初期語彙 (Issue #136)。instructions/worker-codex.md と instructions/worker.md に同じ表がある
VOCABULARY = (
    'stale-state',
    'identity-mismatch',
    'shared-file-conflict',
    'naming-semantics',
    'secret-leak',
    'missing-test',
    'scope-creep',
    'doc-mismatch',
    'other',
)
UNCATEGORIZED = 'uncategorized'
FINDING_KEYS = ('findings', 'blocking', 'non_blocking')
PR_LIMIT = 3  # 表に出す PR の数。残りは「他 N 件」
_LOADER = getattr(yaml, 'CSafeLoader', yaml.SafeLoader)
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)
_PLACEHOLDER_ID = '00000000-0000-4000-8000-000000000000'  # queue/templates/report.yaml の雛形の値
_PR_RE = re.compile(r'/pull/(\d+)$')


def _s(value: object) -> str:
    return '' if value is None else str(value).strip()


def _parse_at(value: object) -> datetime | None:
    """ISO8601 を UTC にする. squad/graph.py の parse_iso8601 と同じ扱い (naive はローカル時刻)."""
    text = _s(value)
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError:
        return None


def _report_key(data: dict[str, Any], raw: bytes) -> str:
    report_id = _s(data.get('report_id'))
    if _UUID_RE.match(report_id) and report_id != _PLACEHOLDER_ID:
        return report_id.lower()
    return 'sha:' + hashlib.sha256(raw).hexdigest()[:16]


def is_review(path: Path, data: dict[str, Any]) -> bool:
    """cross-review の YAML か. archive の写しはファイル名から分からないことがあるので verdict でも見る."""
    return 'review' in path.name or bool(_s(data.get('verdict')))


def _items(value: object) -> list[object]:
    """指摘の配列の要素. findings が dict (blocking / non_blocking の入れ子) ならその中の配列を読む."""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [item for key in FINDING_KEYS for item in _items(value.get(key))]
    return []


def _category(item: object) -> str:
    return _s(item.get('category')) if isinstance(item, dict) else ''


def collect(queue_dir: Path) -> list[dict[str, Any]]:
    """Category 付きの出現を 1 件 1 要素で返す. YAML は読むだけで書き換えない."""
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted((queue_dir / 'projects').glob('*/reports/**/*.yaml')):
        project = path.relative_to(queue_dir / 'projects').parts[0]
        try:
            raw = path.read_bytes()
            mtime = path.stat().st_mtime
            data = yaml.load(raw, Loader=_LOADER)  # noqa: S506 (SafeLoader 系のみ)
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or not _s(data.get('task_id')):
            continue
        key = (project, _report_key(data, raw))
        if key in seen:
            continue
        seen.add(key)
        at = _parse_at(data.get('completed_at'))
        base = {
            'project': project,
            'task_id': _s(data.get('task_id')),
            'pr_url': _s(data.get('pr_url')).rstrip('/'),
            'at': (at or datetime.fromtimestamp(mtime, timezone.utc)).isoformat(timespec='seconds'),
            'approx': at is None,
        }
        if is_review(path, data):
            for item in (i for k in FINDING_KEYS for i in _items(data.get(k))):
                entries.append({**base, 'category': _category(item) or UNCATEGORIZED, 'source': 'review'})
        elif _s(data.get('status')):
            issues = data.get('issues')
            for item in issues if isinstance(issues, list) else []:
                if category := _category(item):
                    entries.append({**base, 'category': category, 'source': 'report'})
    return entries


def tally(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Category ごとの 件数 / 初出 / 直近 / 該当 PR / task. 件数の多い順で、uncategorized は最後."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        groups.setdefault(e['category'], []).append(e)
    rows = []
    for category, es in groups.items():
        es.sort(key=lambda e: e['at'])
        rows.append(
            {
                'category': category,
                'count': len(es),
                'first_seen': es[0]['at'],
                'last_seen': es[-1]['at'],
                'prs': list(dict.fromkeys(e['pr_url'] for e in es if e['pr_url'])),
                'tasks': list(dict.fromkeys(e['task_id'] for e in es)),
                'approx': any(e['approx'] for e in es),
            }
        )
    rows.sort(key=lambda r: (r['category'] == UNCATEGORIZED, -r['count'], r['category']))
    return rows


def _pr_label(url: str) -> str:
    m = _PR_RE.search(url)
    return f'#{m.group(1)}' if m else url


def render_markdown(rows: list[dict[str, Any]]) -> list[str]:
    lines = ['| category | 件数 | 初出 | 直近 | PR |', '|---|---|---|---|---|']
    for r in rows:
        prs = ', '.join(_pr_label(p) for p in r['prs'][:PR_LIMIT]) or '-'
        if len(r['prs']) > PR_LIMIT:
            prs += f' 他 {len(r["prs"]) - PR_LIMIT} 件'
        mark = '*' if r['approx'] else ''
        lines.append(
            f'| {r["category"]} | {r["count"]} | {r["first_seen"][:10]}{mark} | {r["last_seen"][:10]}{mark} | {prs} |'
        )
    if any(r['approx'] for r in rows):
        lines += ['', '* completed_at の無い report を含み、日付はファイルの mtime による近似']
    return lines


def marker(category: str) -> str:
    return f'`category:{category}`'


def append_inbox(inbox: Path, rows: list[dict[str, Any]]) -> list[str]:
    """2 件以上になった category を inbox に 1 行ずつ足し、足した行を返す (同じ印の行があれば足さない).

    印は backtick で囲んで完全一致で探す (`category:stale-state-x` の行は `stale-state` の印とみなさない)。
    複数の watchd が同時に sync しても二重に足さないよう、確認から追記までファイルをロックする。
    """
    due = [r for r in rows if r['category'] != UNCATEGORIZED and r['count'] >= 2]
    if not due:
        return []
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open('a+', encoding='utf-8') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        text = fh.read()
        added = []
        for r in due:
            if marker(r['category']) in text:
                continue
            tasks = ' / '.join(r['tasks'][:PR_LIMIT])
            added.append(
                f'- [ ] [review-category] category `{r["category"]}` の指摘が {r["count"]} 件 ({tasks})。'
                f'チェック化の候補  {marker(r["category"])}'
            )
        if added:
            fh.write(('' if not text or text.endswith('\n') else '\n') + '\n'.join(added) + '\n')
    return added


def main(argv: list[str] | None = None) -> int:
    # measure_task_cost は import 時に git を呼ぶ。graph.py (watchd) から import されるので main の中でだけ読む
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from measure_task_cost import QUEUE_ROOT  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--queue-dir', type=Path, default=QUEUE_ROOT / 'queue')
    parser.add_argument('--json', action='store_true', help='Markdown の代わりに JSON を出す')
    parser.add_argument(
        '--inbox', type=Path, help='2 件目に達した category を候補として足す inbox (queue/_inbox.md 等)'
    )
    args = parser.parse_args(argv)
    rows = tally(collect(args.queue_dir))
    if args.inbox:
        for line in append_inbox(args.inbox, rows):
            print(f'inbox に追加: {line}', file=sys.stderr)
    if args.json:
        print(json.dumps({'categories': rows}, ensure_ascii=False, indent=2))
    else:
        print('\n'.join(render_markdown(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
