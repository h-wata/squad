#!/usr/bin/env python3
# ruff: noqa: CPY001
"""task YAML のスキーマ検証スクリプト (SQUAD-260).

task-yaml-author (現状 Sonnet) の生成物を下位モデル (Haiku / local-coder) に委譲する
前提として、成果物の妥当性を機械的に判定できるようにする。判定できないと下位モデルの
誤った task YAML が静かに通過してしまうため。

使い方:
  check_task_yaml.py <task.yaml> [...]   指定した task YAML だけを検査する
  check_task_yaml.py --all               queue/projects/*/tasks/*.yaml と
                                          queue/projects/*/archive/*.yaml 全件を検査する

task_id 重複検査 (項目6) は、位置引数のみの実行でも常にリポジトリ全体
(queue/projects/**/tasks/, queue/projects/**/archive/) と突き合わせる。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_GLOB_PATTERNS = ('queue/projects/*/tasks/*.yaml', 'queue/projects/*/archive/*.yaml')

REQUIRED_FIELDS = (
    'task_id',
    'project',
    'assigned_to',
    'agent',
    'routing_reason',
    'priority',
    'title',
    'description',
    'acceptance_criteria',
    'created_at',
)
VALID_AGENTS = ('claude', 'codex')
VALID_MODELS = ('opus', 'sonnet', 'haiku', 'fable')
EVIDENCE_CARD_FIELDS = (
    'claim',
    'evidence_as_of',
    'data_window',
    'semantic_definition',
    'current_state_check',
    'disconfirming_check',
    'decision_if_false',
)
PLACEHOLDER_VALUES = ('-', 'n/a', 'tbd', '')


class Result:
    """1 検査項目の結果 (項目番号 + OK/NG/WARN + 理由)."""

    def __init__(self, item: int, status: str, reason: str) -> None:
        self.item = item
        self.status = status
        self.reason = reason


def is_placeholder(value: object) -> bool:
    """evidence_card の値がプレースホルダ (未記入相当) かどうか."""
    if not isinstance(value, str):
        return True
    return value.strip().lower() in PLACEHOLDER_VALUES


def find_repo_task_yaml_files() -> list[Path]:
    """リポジトリ全体の task YAML (tasks/ + archive/) を集める."""
    files: list[Path] = []
    for pattern in TASK_GLOB_PATTERNS:
        files.extend(sorted(REPO_ROOT.glob(pattern)))
    return files


def collect_task_id_locations(files: list[Path]) -> dict[str, list[Path]]:
    """検査対象外も含め、リポジトリ全体の task_id -> 出現ファイル一覧を作る."""
    locations: dict[str, list[Path]] = {}
    for path in files:
        try:
            data = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        task_id = data.get('task_id')
        if isinstance(task_id, str) and task_id:
            locations.setdefault(task_id, []).append(path)
    return locations


def check_task_yaml(path: Path, task_id_locations: dict[str, list[Path]]) -> list[Result]:
    """1 ファイルの全検査項目を実行し、結果を蓄積して返す (例外は握って NG 報告)."""
    results: list[Result] = []

    try:
        text = path.read_text()
    except OSError as e:
        results.append(Result(1, 'NG', f'ファイルを読めません: {e}'))
        return results

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        results.append(Result(1, 'NG', f'YAML として parse できません: {e}'))
        return results

    if not isinstance(data, dict):
        results.append(Result(1, 'NG', f'top-level が dict ではありません: {type(data).__name__}'))
        return results

    results.append(Result(1, 'OK', 'parse できました'))

    # 2. 必須フィールド
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        results.append(Result(2, 'NG', f'必須フィールド欠落: {", ".join(missing)}'))
    else:
        results.append(Result(2, 'OK', '必須フィールドは揃っています'))

    # 3. agent
    agent = data.get('agent')
    if agent not in VALID_AGENTS:
        results.append(Result(3, 'NG', f'agent が不正です: {agent!r} (期待: {VALID_AGENTS})'))
    else:
        results.append(Result(3, 'OK', f'agent: {agent}'))

    # 4. model
    model = data.get('model')
    if model is not None and model not in VALID_MODELS:
        results.append(Result(4, 'NG', f'model が不正です: {model!r} (期待: {VALID_MODELS})'))
    elif agent == 'codex' and model is not None:
        results.append(Result(4, 'WARN', f'agent: codex では model ({model!r}) は無視されます'))
    else:
        results.append(Result(4, 'OK', f'model: {model!r}'))

    # 5. assigned_to と ファイル名 worker{N}.yaml の一致
    assigned_to = data.get('assigned_to')
    filename_match = path.stem
    if filename_match.startswith('worker') and filename_match[len('worker') :].isdigit():
        file_n = filename_match[len('worker') :]
        expected = f'worker{file_n}'
        if assigned_to != expected:
            results.append(Result(5, 'NG', f'assigned_to ({assigned_to!r}) がファイル名 ({path.name}) と一致しません'))
        else:
            results.append(Result(5, 'OK', f'assigned_to はファイル名と一致 ({assigned_to})'))
    else:
        results.append(Result(5, 'OK', f'ファイル名 {path.name} は worker{{N}}.yaml 形式ではないため対象外'))

    # 6. task_id 重複
    task_id = data.get('task_id')
    if isinstance(task_id, str) and task_id:
        locations = task_id_locations.get(task_id, [])
        others = [p for p in locations if p != path]
        if others:
            others_str = ', '.join(str(p) for p in others)
            results.append(Result(6, 'NG', f'task_id ({task_id}) が重複しています: {others_str}'))
        else:
            results.append(Result(6, 'OK', f'task_id ({task_id}) は一意です'))
    else:
        results.append(Result(6, 'NG', 'task_id が存在しないか空です'))

    # 7. acceptance_criteria
    acceptance_criteria = data.get('acceptance_criteria')
    if not isinstance(acceptance_criteria, list) or not acceptance_criteria:
        results.append(Result(7, 'NG', f'acceptance_criteria が空でないリストではありません: {acceptance_criteria!r}'))
    else:
        results.append(Result(7, 'OK', f'acceptance_criteria: {len(acceptance_criteria)} 件'))

    # 8, 9. verify / verify_skip_reason
    verify = data.get('verify')
    if verify is not None:
        if not isinstance(verify, dict):
            results.append(Result(8, 'NG', f'verify が dict ではありません: {type(verify).__name__}'))
        else:
            commands = verify.get('commands')
            expect = verify.get('expect')
            max_attempts = verify.get('max_attempts')
            errs = []
            if not isinstance(commands, list) or not commands:
                errs.append(f'commands が空でないリストではありません: {commands!r}')
            if expect is None or (isinstance(expect, str) and not expect.strip()):
                errs.append(f'expect が存在しないか空です: {expect!r}')
            if max_attempts is not None and (not isinstance(max_attempts, int) or max_attempts <= 0):
                errs.append(f'max_attempts が正整数ではありません: {max_attempts!r}')
            if errs:
                results.append(Result(8, 'NG', '; '.join(errs)))
            else:
                results.append(Result(8, 'OK', 'verify は妥当です'))
        results.append(Result(9, 'OK', 'verify があるため verify_skip_reason は不要'))
    else:
        skip_reason = data.get('verify_skip_reason')
        results.append(Result(8, 'OK', 'verify がないため対象外'))
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            results.append(Result(9, 'NG', 'verify も verify_skip_reason (空でない文字列) もありません'))
        else:
            results.append(Result(9, 'OK', f'verify_skip_reason: {skip_reason!r}'))

    # 10. evidence_card
    evidence_card = data.get('evidence_card')
    if evidence_card is None:
        results.append(Result(10, 'OK', 'evidence_card はありません (対象外)'))
    elif not isinstance(evidence_card, dict):
        results.append(Result(10, 'NG', f'evidence_card が dict ではありません: {type(evidence_card).__name__}'))
    else:
        bad = [f for f in EVIDENCE_CARD_FIELDS if f not in evidence_card or is_placeholder(evidence_card.get(f))]
        if bad:
            results.append(Result(10, 'NG', f'evidence_card の未記入/プレースホルダ: {", ".join(bad)}'))
        else:
            results.append(Result(10, 'OK', 'evidence_card は全フィールド記入済みです'))

    # 11. created_at
    created_at = data.get('created_at')
    if not isinstance(created_at, str) or not created_at:
        results.append(Result(11, 'NG', f'created_at が文字列ではないか空です: {created_at!r}'))
    else:
        try:
            datetime.fromisoformat(created_at)
        except ValueError:
            results.append(Result(11, 'NG', f'created_at が ISO8601 として解釈できません: {created_at!r}'))
        else:
            results.append(Result(11, 'OK', f'created_at: {created_at}'))

    # 12. context.workspace
    context = data.get('context')
    workspace = context.get('workspace') if isinstance(context, dict) else None
    if workspace is None:
        results.append(Result(12, 'OK', 'context.workspace はありません (対象外)'))
    elif not isinstance(workspace, str) or not Path(workspace).exists():
        results.append(Result(12, 'WARN', f'context.workspace が存在しません: {workspace!r}'))
    else:
        results.append(Result(12, 'OK', f'context.workspace: {workspace}'))

    return results


def print_file_results(path: Path, results: list[Result]) -> bool:
    """1 ファイルの検査結果を表示し、NG が 1 件でもあれば True を返す."""
    has_ng = any(r.status == 'NG' for r in results)
    print(f'{path}:')
    for r in results:
        print(f'  [{r.item}] {r.status}: {r.reason}')
    return has_ng


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: check_task_yaml.py <task.yaml> [...] | --all', file=sys.stderr)
        return 1

    if argv == ['--all']:
        targets = find_repo_task_yaml_files()
    else:
        targets = [Path(a).resolve() for a in argv]

    # task_id 重複はリポジトリ内の全 task YAML + 明示指定された対象ファイルを突き合わせる
    # (パスは resolve() して repo 内発見分と明示指定分を同一視できるようにする)。
    all_known = {p.resolve(): p.resolve() for p in find_repo_task_yaml_files()}
    all_known.update({p: p for p in targets})
    task_id_locations = collect_task_id_locations(list(all_known.values()))

    ng_count = 0
    for path in targets:
        results = check_task_yaml(path, task_id_locations)
        if print_file_results(path, results):
            ng_count += 1

    print(f'\n合計: {len(targets)} ファイル, NG {ng_count} 件')
    return 1 if ng_count else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
