#!/usr/bin/env python3
# ruff: noqa: CPY001
"""report YAML のスキーマ検証スクリプト.

task 側には check_task_yaml.py があるが、report 側には無かった。worker が
`status: completed` と書けばそのまま通る状態で、実測で 2 つの穴が見つかった:

1. **検証したことにできる**: `verify:` のある task でも、verifier を呼ばずに
   自己申告で `verify_status: pass` と書ける。Opencode / Codex worker には
   verifier サブエージェントが無いため、実際に自己採点になっていた。
2. **判断を隠せる**: 仕様が曖昧な箇所を自分で解釈して押し通しても、report の
   言い回しでそれを消せる。Issue #34 の実測では、受け入れ条件と実装が
   厳密には一致しないことを worker が report に書かずに済ませていた
   (同じ曖昧さを Sonnet の verifier は指摘できた)。

このスクリプトは「良い判断をさせる」ことはできないが、
**判断したことを隠せなくする / 検証を省いたことを隠せなくする** ことはできる。
内容の当否は判定せず、開示されているかどうかだけを機械的に見る。

使い方:
  check_report_yaml.py <report.yaml> [...]   指定した report YAML を検査する
  check_report_yaml.py --all                 queue/projects/*/reports/*.yaml 全件

exit code は NG が 1 件でもあれば 1。WARN では落とさない (fail-soft)。
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import yaml

# 走査対象の squad root は _repo_root() で解決する。スクリプト位置に固定すると、
# 別の squad root (検証用のコピー等) に対して実行しても本物の queue を走査してしまい、
# task_id 重複を誤検出する。テストが開発者の queue の中身に依存する原因でもあった。
REPORT_GLOB_PATTERNS = ('queue/projects/*/reports/*.yaml',)
# 「未記入」とみなす値。assumptions に "none" と明記するのは有効な回答なので含めない。
PLACEHOLDER_VALUES = ('-', 'n/a', 'tbd', '')
# 検証を要求しない report 種別 (verdict 自体 / cross-review は別 schema)
SKIP_VERDICT_SUFFIXES = ('_verdict.yaml', '_review.yaml')


class Result:
    """1 検査項目の結果 (項目番号 + OK/NG/WARN + 理由)."""

    def __init__(self, item: int, status: str, reason: str) -> None:
        self.item = item
        self.status = status
        self.reason = reason


def is_blank(value: object) -> bool:
    """未記入 (プレースホルダ含む) かどうか."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in PLACEHOLDER_VALUES
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def find_task_yaml(report_path: Path, data: dict) -> Path | None:
    """Report に対応する task YAML を探す (reports/ の隣の tasks/)."""
    worker = data.get('worker')
    if not isinstance(worker, str) or not worker:
        return None
    candidate = report_path.parent.parent / 'tasks' / f'{worker}.yaml'
    return candidate if candidate.is_file() else None


def load_yaml(path: Path) -> tuple[dict | None, str]:
    """YAML を dict として読む。失敗理由は文字列で返す (例外は投げない)."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        return None, str(e)
    if not isinstance(data, dict):
        return None, 'top-level が mapping ではありません'
    return data, ''


def check_assumptions(data: dict) -> Result:
    """項目1: 仕様が沈黙していて自分で決めた点が開示されているか.

    内容の当否は見ない。書いてあるか (無ければ "none" と明記してあるか) だけを見る。
    """
    if 'assumptions' not in data:
        return Result(
            1,
            'NG',
            'assumptions がありません。仕様が沈黙していて自分で決めた点を列挙するか、'
            '無ければ "none" と明記してください',
        )
    if is_blank(data.get('assumptions')):
        return Result(1, 'NG', 'assumptions が空です。無いなら "none" と明記してください')
    return Result(1, 'OK', 'assumptions: 記載あり')


def check_verdict(report_path: Path, data: dict) -> list[Result]:
    """項目2-3: verify: のある task を completed と報告するなら、独立検証の証拠を要求する."""
    results: list[Result] = []
    if report_path.name.endswith(SKIP_VERDICT_SUFFIXES):
        results.append(Result(2, 'OK', 'verdict / review report のため対象外'))
        return results

    task_path = find_task_yaml(report_path, data)
    if task_path is None:
        results.append(Result(2, 'WARN', '対応する task YAML が見つからず、検証要否を判定できません'))
        return results
    task, err = load_yaml(task_path)
    if task is None:
        results.append(Result(2, 'WARN', f'task YAML を読めません ({task_path}): {err}'))
        return results
    if is_blank(task.get('verify')):
        results.append(Result(2, 'OK', 'task に verify: が無いため独立検証は不要'))
        return results
    if data.get('status') != 'completed':
        results.append(Result(2, 'OK', f'status={data.get("status")!r} のため独立検証は必須ではありません'))
        return results

    verdict_path = data.get('verdict_path')
    if is_blank(verdict_path):
        results.append(
            Result(
                2,
                'NG',
                'task に verify: があり completed と報告していますが verdict_path が空です。'
                'scripts/verify-task.sh で独立検証を実行してください (自己申告の pass は不可)',
            )
        )
        return results

    vp = Path(str(verdict_path))
    if not vp.is_absolute():
        vp = report_path.parent / vp
    if not vp.is_file():
        results.append(Result(2, 'NG', f'verdict_path のファイルがありません: {verdict_path}'))
        return results
    results.append(Result(2, 'OK', f'verdict_path: {verdict_path}'))

    verdict, err = load_yaml(vp)
    if verdict is None:
        results.append(Result(3, 'NG', f'verdict を読めません: {err}'))
        return results
    result_value = verdict.get('result')
    if result_value != 'pass':
        results.append(
            Result(3, 'NG', f'verdict の result が {result_value!r} です (completed と報告できるのは pass のときだけ)')
        )
        return results
    if verdict.get('task_id') != data.get('task_id'):
        results.append(
            Result(
                3,
                'NG',
                f'verdict の task_id ({verdict.get("task_id")!r}) が report ({data.get("task_id")!r}) と一致しません',
            )
        )
        return results
    results.append(Result(3, 'OK', 'verdict: result=pass、task_id も一致'))
    return results


def check_report_yaml(path: Path) -> list[Result]:
    """1 ファイルの全検査項目を実行し、結果を蓄積して返す (例外は握って NG 報告)."""
    data, err = load_yaml(path)
    if data is None:
        return [Result(0, 'NG', f'parse できません: {err}')]
    results = [check_assumptions(data)]
    results.extend(check_verdict(path, data))
    return results


def _repo_root() -> Path:
    """走査対象の squad root を返す (呼び出しのたびに環境変数を見る).

    import 時に固定すると、テストや別 root からの実行で差し替えられない。
    """
    return Path(os.environ.get('SQUAD_ROOT') or Path(__file__).resolve().parent.parent)


def find_repo_report_yaml_files() -> list[Path]:
    files: list[Path] = []
    for pattern in REPORT_GLOB_PATTERNS:
        files.extend(sorted(_repo_root().glob(pattern)))
    return files


def print_file_results(path: Path, results: list[Result]) -> bool:
    has_ng = any(r.status == 'NG' for r in results)
    print(f'{path}:')
    for r in results:
        print(f'  [{r.item}] {r.status}: {r.reason}')
    return has_ng


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: check_report_yaml.py <report.yaml> [...] | --all', file=sys.stderr)
        return 1

    targets = find_repo_report_yaml_files() if argv == ['--all'] else [Path(a).resolve() for a in argv]

    ng_count = 0
    for path in targets:
        if print_file_results(path, check_report_yaml(path)):
            ng_count += 1

    print(f'\n合計: {len(targets)} ファイル, NG {ng_count} 件')
    return 1 if ng_count else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
