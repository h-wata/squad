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

指摘は 1 件ずつ blocking / non_blocking / 確認項目 (check) に分ける (classify)。確認項目は「指摘ではなく
確かめた項目」(`result: pass` / `status: resolved` / `severity: info` 等) で、失敗には数えない。
判定の順は classify の docstring に書く。

category ごとに task 数 / blocking / non_blocking / 確認項目 / 初出 / 直近 / 該当 PR を出す。task 数は失敗
(blocking + non_blocking) を出した distinct task_id の数で、同じ task の中の複数の指摘は 1 と数える (SQUAD-406)。
初出 / 直近 / PR は失敗から出す (失敗が無い category だけ確認項目から)。日時は report の `completed_at`、
無ければファイルの mtime (mtime は archive への写しで進むことがあるので近似。表の下に注記する)。

使い方:
  review_category_tally.py [--queue-dir <queue>] [--json] [--inbox <_inbox.md>]

`--inbox` を付けると、同じ category (uncategorized 以外) の失敗が別々の task_id で 2 つ以上になったものを
`queue/_inbox.md` にチェック化の候補として 1 行ずつ足す。行には `category:<name>` の印を入れ、同じ印の行が既にあれば
(`- [x]` で閉じた行も) 足さない。
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
BLOCKING, NON_BLOCKING, CHECK = 'blocking', 'non_blocking', 'check'
KINDS = (BLOCKING, NON_BLOCKING, CHECK)
CANDIDATE_TASKS = 2  # この数の distinct task_id で失敗した category を inbox の候補にする
# 実データ (reports/ と archive/ の review) に出てくる値。小文字・空白と `_` を `-` にしてから比べる
_CHECK_RESULTS = ('pass',)  # result が pass / pass_with_comment(s) なら確認項目
_FAIL_RESULTS = ('fail',)
_CHECK_STATUSES = ('resolved', 'verified')  # resolved / resolved (no regression) / resolved_for_single_watcher 等
_OPEN_STATUSES = ('open', 'partial', 'partially-resolved', 'unresolved')
_CHECK_SEVERITIES = ('info', 'none', 'resolved')
_BLOCKING_SEVERITIES = ('blocking', 'critical', 'major', 'high')
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


def _norm(value: object) -> str:
    return re.sub(r'[\s_]+', '-', _s(value).lower())


def _flag(value: object) -> bool | None:
    """Bool と 'true' / 'false' の文字列を bool にする. それ以外は None."""
    if isinstance(value, bool):
        return value
    return {'true': True, 'false': False}.get(_norm(value))


def classify(item: object, key: str) -> str:
    """指摘 1 件を blocking / non_blocking / check に分ける. key は指摘が入っていた配列の名前.

    確認項目 (check) かどうかを先に決め、失敗なら blocking / non_blocking を決める:
      1. `result` が pass で始まる (pass / pass_with_comment(s)) → check。fail で始まる → 失敗
      2. `resolved: true` か、`status` が resolved / verified で始まる → check (前回の指摘の解消確認)。
         `status` が open / partial 等なら失敗
      3. `severity` が info / none / resolved で始まる → check (指摘ではなく所見)
      4. 残りは失敗。トップレベルの `blocking` / `non_blocking` 配列はその名前で分ける。`findings` の要素は
         `blocking: true|false` があればそれ、無ければ severity が blocking / critical / major / high なら blocking、
         それ以外 (minor / nit / suggestion / non-blocking / 無し) は non_blocking
    文字列だけの要素は 4 のみ (配列の名前で分け、findings なら non_blocking)。
    """
    if isinstance(item, dict):
        result, status, severity = _norm(item.get('result')), _norm(item.get('status')), _norm(item.get('severity'))
        if result.startswith(_CHECK_RESULTS):
            return CHECK
        failed = result.startswith(_FAIL_RESULTS) or status.startswith(_OPEN_STATUSES)
        if not failed and (_flag(item.get('resolved')) or status.startswith(_CHECK_STATUSES)):
            return CHECK
        if not failed and severity.startswith(_CHECK_SEVERITIES):
            return CHECK
    if key in (BLOCKING, NON_BLOCKING):
        return key
    if not isinstance(item, dict):
        return NON_BLOCKING
    if (flag := _flag(item.get('blocking'))) is not None:
        return BLOCKING if flag else NON_BLOCKING
    return BLOCKING if _norm(item.get('severity')).startswith(_BLOCKING_SEVERITIES) else NON_BLOCKING


def _findings(data: dict[str, Any]) -> list[tuple[object, str]]:
    """(指摘, 入っていた配列の名前). findings の下の入れ子 blocking / non_blocking はその名前を使う."""
    out: list[tuple[object, str]] = []
    for key in FINDING_KEYS:
        value = data.get(key)
        if isinstance(value, dict):
            out += [(item, sub) for sub in (BLOCKING, NON_BLOCKING) for item in _items(value.get(sub))]
            out += [(item, key) for item in _items(value.get('findings'))]
        else:
            out += [(item, key) for item in _items(value)]
    return out


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
            for item, k in _findings(data):
                entries.append(
                    {
                        **base,
                        'category': _category(item) or UNCATEGORIZED,
                        'kind': classify(item, k),
                        'source': 'review',
                    }
                )
        elif _s(data.get('status')):
            # worker の report の issues は review の指摘ではない。severity / blocking を書いていなければ non_blocking
            issues = data.get('issues')
            for item in issues if isinstance(issues, list) else []:
                if category := _category(item):
                    entries.append(
                        {**base, 'category': category, 'kind': classify(item, 'issues'), 'source': 'report'}
                    )
    return entries


def sort_key(row: dict[str, Any]) -> tuple[bool, int, int, str]:
    """Task 数の多い順、同じなら失敗件数の多い順。uncategorized は最後 (squad/web.py も同じ規則で並べる)."""
    return (
        row['category'] == UNCATEGORIZED,
        -row['task_count'],
        -row['blocking'] - row['non_blocking'],
        row['category'],
    )


def tally(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Category ごとの task 数 / blocking / non_blocking / 確認項目 / 初出 / 直近 / 該当 PR / task.

    task 数と tasks は失敗 (blocking + non_blocking) を出した distinct task_id。確認項目は件数だけ数える。
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        groups.setdefault(e['category'], []).append(e)
    rows = []
    for category, es in groups.items():
        es.sort(key=lambda e: e['at'])
        failures = [e for e in es if e['kind'] != CHECK]
        basis = failures or es  # 初出 / 直近 / PR は失敗から。失敗の無い category だけ確認項目から
        tasks = list(dict.fromkeys(e['task_id'] for e in failures))
        rows.append(
            {
                'category': category,
                'task_count': len(tasks),
                **{kind: sum(e['kind'] == kind for e in es) for kind in (BLOCKING, NON_BLOCKING)},
                'checks': sum(e['kind'] == CHECK for e in es),
                'first_seen': basis[0]['at'],
                'last_seen': basis[-1]['at'],
                'prs': list(dict.fromkeys(e['pr_url'] for e in failures if e['pr_url'])),
                'tasks': tasks,
                'approx': any(e['approx'] for e in basis),
            }
        )
    rows.sort(key=sort_key)
    return rows


def _pr_label(url: str) -> str:
    m = _PR_RE.search(url)
    return f'#{m.group(1)}' if m else url


def render_markdown(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        '| category | task 数 | blocking | non_blocking | 確認項目 | 初出 | 直近 | PR |',
        '|---|---|---|---|---|---|---|---|',
    ]
    for r in rows:
        prs = ', '.join(_pr_label(p) for p in r['prs'][:PR_LIMIT]) or '-'
        if len(r['prs']) > PR_LIMIT:
            prs += f' 他 {len(r["prs"]) - PR_LIMIT} 件'
        mark = '*' if r['approx'] else ''
        lines.append(
            f'| {r["category"]} | {r["task_count"]} | {r["blocking"]} | {r["non_blocking"]} | {r["checks"]} | '
            f'{r["first_seen"][:10]}{mark} | {r["last_seen"][:10]}{mark} | {prs} |'
        )
    if any(r['approx'] for r in rows):
        lines += ['', '* completed_at の無い report を含み、日付はファイルの mtime による近似']
    return lines


def marker(category: str) -> str:
    return f'`category:{category}`'


def append_inbox(inbox: Path, rows: list[dict[str, Any]]) -> list[str]:
    """別々の task で 2 回以上失敗した category を inbox に 1 行ずつ足し、足した行を返す.

    同じ task の中の複数の指摘は 1 回と数える (task_count)。同じ印の行があれば、`- [x]` で閉じた行でも足さない
    (誤検知として閉じた候補が sync のたびに戻らないように)。
    印は backtick で囲んで完全一致で探す (`category:stale-state-x` の行は `stale-state` の印とみなさない)。
    複数の watchd が同時に sync しても二重に足さないよう、確認から追記までファイルをロックする。
    """
    due = [r for r in rows if r['category'] != UNCATEGORIZED and r['task_count'] >= CANDIDATE_TASKS]
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
                f'- [ ] [review-category] category `{r["category"]}` の失敗が {r["task_count"]} task で '
                f'{r["blocking"] + r["non_blocking"]} 件 ({tasks})。'
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
        '--inbox', type=Path, help='2 task 目で失敗した category を候補として足す inbox (queue/_inbox.md 等)'
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
