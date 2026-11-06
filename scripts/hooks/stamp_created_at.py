#!/usr/bin/env python3
# ruff: noqa: CPY001
"""PreToolUse Hook: task YAML の Write 時に created_at を実時刻へ差し替える (#87).

対象は Write で `queue/projects/<project>/tasks/<name>.yaml` を書くときだけ。
同じパスに既存ファイルがあり created_at を持つなら、それを残す(書き直しで時刻が動かない)。
何かあっても Write を壊さないよう、失敗時は何も出力せず exit 0 する。
SQUAD_STAMP_NOW を設定すると、その値を現在時刻として使う(テスト用)。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys

CREATED_AT = re.compile(r'^created_at:.*$', re.MULTILINE)


def is_task_yaml(path: Path) -> bool:
    tasks = path.parent
    return (
        path.suffix == '.yaml'
        and tasks.name == 'tasks'
        and tasks.parent.parent.name == 'projects'
        and tasks.parent.parent.parent.name == 'queue'
    )


def stamp(content: str, path: Path) -> str | None:
    """差し替え後の content を返す。変更が要らなければ None を返す."""
    if not CREATED_AT.search(content):
        return None
    line = None
    if path.is_file():
        old = CREATED_AT.search(path.read_text(encoding='utf-8', errors='replace'))
        line = old.group(0) if old else None
    if line is None:
        now = os.environ.get('SQUAD_STAMP_NOW') or datetime.now().astimezone().isoformat(timespec='seconds')
        line = f'created_at: "{now}"'
    new = CREATED_AT.sub(lambda _: line, content)
    return None if new == content else new


def main() -> None:
    data = json.load(sys.stdin)
    if data.get('tool_name') != 'Write':
        return
    tool_input = data['tool_input']
    path = Path(data.get('cwd') or '.') / tool_input['file_path']
    content = tool_input['content']
    if not is_task_yaml(path):
        return
    new = stamp(content, path)
    if new is None:
        return
    json.dump(
        {
            'hookSpecificOutput': {
                'hookEventName': 'PreToolUse',
                'updatedInput': {**tool_input, 'content': new},
            }
        },
        sys.stdout,
    )


if __name__ == '__main__':
    try:
        main()
    except Exception:  # noqa: BLE001  Hook の失敗で Write を止めない
        pass
