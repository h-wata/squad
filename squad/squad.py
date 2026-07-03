#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad — tmux multi-agent インタラクティブ CLI (token-0, stdlib only).

棲み分け:
  - 常駐 daemon (report/permission/stall/discovery/GC) → watch.sh が担当
  - send-keys timing 制御 (/clear /model /new + 着手確認) → notify-worker.sh が担当
  - インタラクティブ単発: 状態確認 / 指示 / dashboard 生成 → このスクリプト

Subcommands:
  ls / status               全 worker の状態一覧 + state/<w>.json 保存
  assign <w> <task.yaml>    task YAML を読み notify-worker.sh で通知
  dashboard                 Worker ステータス表を生成して stdout
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

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
CONFIG_PATH = ROOT / 'config.json'
STATE_DIR = ROOT / 'state'
NOTIFY_WORKER = REPO_ROOT / 'scripts' / 'notify-worker.sh'
QUEUE_DIR = REPO_ROOT / 'queue' / 'projects'
CAPTURE_TAIL_LINES = 25

# ---------- config ----------


def resolve_session(cfg: dict) -> str:
    """Tmux session 名解決: SQUAD_SESSION env → 既定 'ros-agents'.

    start.sh / stop.sh / watch.sh / notify-worker.sh と同じ優先順位に揃える
    (config.json の 'session' キーは参照しない)。
    """
    return os.environ.get('SQUAD_SESSION') or 'ros-agents'


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f'config not found: {CONFIG_PATH}')
    cfg = json.loads(CONFIG_PATH.read_text())
    session = resolve_session(cfg)
    # pane は config.json 上 session 非依存のサフィックス (例 "0.1") で持つ。ここで
    # 解決済み session 名と結合し、以降の呼び出し元は meta['pane'] をそのまま使える。
    for meta in cfg.get('workers', {}).values():
        pane = meta.get('pane', '')
        if pane and ':' not in pane:
            meta['pane'] = f'{session}:{pane}'
    return cfg


def resolve_worker(cfg: dict, name: str) -> dict:
    workers = cfg.get('workers', {})
    if name not in workers:
        sys.exit(f"unknown worker '{name}'. known: {sorted(workers)}")
    return workers[name]


# ---------- tmux primitives ----------


def tmux_capture(pane: str, lines: int = CAPTURE_TAIL_LINES) -> tuple[bool, str]:
    """Return (reachable, tail_text)."""
    r = subprocess.run(['tmux', 'capture-pane', '-t', pane, '-p'], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()
    text = r.stdout
    if lines and lines > 0:
        text = '\n'.join(text.splitlines()[-lines:])
    return True, text


# ---------- 状態判定 (poll-sonnet-workers パターン移植) ----------

PERMISSION_PATTERNS = [
    r'Do you want to proceed\?',
    r'requires confirmation',
    r'Press enter to confirm',
    r'\b1\.\s+Yes\b',
]
THINKING_PATTERNS = [
    r'(Sprouting|Whirring|Topsy-turvying|Pondering|Thinking|Working|Cogitating|Crafting)…',
    r'Working \(\d+m \d+s\)',
]
CTX_PATTERNS = [
    re.compile(r'\bctx\s+(\d+)%', re.IGNORECASE),
    re.compile(r'[◔◑◕○]\s*[\d,]+\s*\((\d+)%\)'),
    re.compile(r'Context\s+(\d+)%\s+left'),
]
MODEL_PATTERNS = [
    re.compile(r'\b((?:Sonnet|Opus|Haiku|Fable)\s+\d[\d.]*)'),
    re.compile(r'[✱✦★◆]\s*((?:Sonnet|Opus|Haiku|Fable)\s+\S+)'),
]


def detect_status(tail: str) -> dict:
    status = 'idle'
    if any(re.search(p, tail) for p in PERMISSION_PATTERNS):
        status = 'permission_wait'
    elif any(re.search(p, tail) for p in THINKING_PATTERNS):
        status = 'busy'

    context_pct: int | None = None
    for p in CTX_PATTERNS:
        m = p.search(tail)
        if m:
            context_pct = int(m.group(1))
            break

    model: str | None = None
    for p in MODEL_PATTERNS:
        m = p.search(tail)
        if m:
            model = m.group(1)
            break

    last_line = ''
    for ln in reversed(tail.splitlines()):
        ln = ln.strip()
        if ln:
            last_line = ln
            break

    return {'status': status, 'context_pct': context_pct, 'model': model, 'last_line': last_line[:200]}


# ---------- state 保存 / hook 読み出し ----------


def save_state(worker: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f'{worker}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def load_state(worker: str) -> dict | None:
    p = STATE_DIR / f'{worker}.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------- shallow YAML reader (stdlib only) ----------


def yaml_shallow(path: Path) -> dict[str, str]:
    """top-level の `key: value` だけ拾う簡易リーダー.

    block scalar (`|`, `>`) や list, nested は無視。CLI で必要な
    task_id/project/assigned_to/agent/model/title/status/summary/completed_at
    だけ取れれば十分。
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    pat = re.compile(r'^([A-Za-z_][\w\-]*)\s*:\s*(.+?)\s*$')
    for line in path.read_text(errors='replace').splitlines():
        # top-level 行 (インデント無し) のみ対象
        if not line or line[0].isspace() or line.startswith('#'):
            continue
        m = pat.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # block scalar 開始マーカは捨てる (本文は読まない)
        if val in ('|', '>', '|-', '>-', '|+', '>+'):
            continue
        # クオート剥がし
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    return out


# ---------- subcommands ----------


def cmd_ls(_: argparse.Namespace, cfg: dict) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    rows = []
    for w, meta in cfg.get('workers', {}).items():
        pane = meta['pane']
        reachable, tail = tmux_capture(pane)
        if not reachable:
            entry = {
                'worker': w,
                'pane': pane,
                'agent': meta.get('agent'),
                'status': 'unreachable',
                'context_pct': None,
                'model': None,
                'last_line': tail[:200],
                'updated_at': now,
            }
        else:
            det = detect_status(tail)
            entry = {'worker': w, 'pane': pane, 'agent': meta.get('agent'), **det, 'updated_at': now}
        # 既存 state があり、hook 由来の last_event があれば保持
        prev = load_state(w)
        if prev and prev.get('last_event'):
            entry['last_event'] = prev['last_event']
            entry['last_event_at'] = prev.get('last_event_at')
        save_state(w, entry)
        rows.append(entry)

    icon = {
        'idle': '🟢',
        'busy': '🔵',
        'permission_wait': '🟡',
        'unreachable': '⚫',
        'completed': '✅',
        'stop_failure': '🔴',
    }
    print(f'== squad ls @ {now} ==')
    for r in rows:
        ic = icon.get(r['status'], '⚪')
        ctx = f'{r["context_pct"]}%' if r['context_pct'] is not None else '-'
        model = r['model'] or '-'
        agent = r['agent'] or '-'
        evt = f' evt={r["last_event"]}' if r.get('last_event') else ''
        print(f'{ic} {r["worker"]:<3} {r["pane"]:<22} {r["status"]:<16} '
              f'ctx={ctx:<5} model={model:<14} agent={agent}{evt}')
        if r['status'] in ('permission_wait', 'unreachable'):
            print(f'     ↳ {r["last_line"]}')
    return 0


# worker name (w1..w4) → notify-worker.sh の worker label (W1..W4)
_W_PATTERN = re.compile(r'^w(\d+)$', re.IGNORECASE)


def to_notify_label(worker: str) -> str:
    m = _W_PATTERN.match(worker)
    if not m:
        sys.exit(f"worker name 'w{{N}}' (e.g. w1) を期待: {worker}")
    return f'W{m.group(1)}'


def cmd_assign(args: argparse.Namespace, cfg: dict) -> int:
    """Task YAML を読み notify-worker.sh で通知."""
    resolve_worker(cfg, args.worker)  # 存在チェック
    task_path = Path(args.task_yaml).expanduser().resolve()
    if not task_path.exists():
        sys.exit(f'task YAML not found: {task_path}')
    if not NOTIFY_WORKER.exists():
        sys.exit(f'notify-worker.sh not found: {NOTIFY_WORKER}')

    meta = yaml_shallow(task_path)
    agent = (meta.get('agent') or '').lower()
    model = meta.get('model') or ''
    task_id = meta.get('task_id') or '?'
    project = meta.get('project') or '?'
    title = meta.get('title') or ''

    is_codex = args.worker.lower() == 'w4' or agent == 'codex'

    msg = f'新しいタスクがあります。{task_path} を確認してください。'
    cmd = [str(NOTIFY_WORKER), to_notify_label(args.worker), msg]
    if model and not is_codex:
        cmd += ['--model', model]
    if args.clear and not is_codex:
        cmd += ['--clear']
    if args.no_new and is_codex:
        cmd += ['--no-new']

    print(f'[assign] {args.worker} ← {task_id} ({project}) "{title[:50]}"')
    print(
        f'[assign] agent={agent or "?"} model={model or "-"} codex={is_codex} clear={args.clear} no_new={args.no_new}')
    print(f'[assign] cmd: {" ".join(cmd)}')

    if args.dry_run:
        print('[assign] dry-run のため実行しません')
        return 0

    r = subprocess.run(cmd)
    return r.returncode


def _newest_report_for(worker_num: str) -> Path | None:
    """worker{N}_report.yaml のうち最新 mtime を返す."""
    candidates: list[tuple[float, Path]] = []
    if not QUEUE_DIR.exists():
        return None
    for p in QUEUE_DIR.glob(f'*/reports/worker{worker_num}_report.yaml'):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _current_task_for(worker_num: str) -> Path | None:
    """worker{N}.yaml のうち最新 mtime を返す (現在の担当タスク推定)."""
    candidates: list[tuple[float, Path]] = []
    if not QUEUE_DIR.exists():
        return None
    for p in QUEUE_DIR.glob(f'*/tasks/worker{worker_num}.yaml'):
        try:
            candidates.append((p.stat().st_mtime, p))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def cmd_dashboard(_: argparse.Namespace, cfg: dict) -> int:
    """Worker ステータス表を Markdown で stdout に出力."""
    now_local = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')
    print(f'<!-- generated by squad dashboard @ {now_local} -->')
    print('| Worker | Pane | Agent | 現在のPJ/タスク | 状態 | 直近の完了タスク |')
    print('|--------|------|-------|----------------|------|------------------|')
    for w, meta in cfg.get('workers', {}).items():
        m = _W_PATTERN.match(w)
        wnum = m.group(1) if m else ''
        st = load_state(w) or {}
        status = st.get('status', '?')
        ctx = f' ctx={st["context_pct"]}%' if st.get('context_pct') is not None else ''
        model = st.get('model') or '-'
        evt = st.get('last_event')

        cur_task = _current_task_for(wnum) if wnum else None
        cur_meta = yaml_shallow(cur_task) if cur_task else {}
        cur_label = f'{cur_meta.get("task_id", "?")} ({cur_meta.get("project", "?")})' if cur_meta else '-'

        last_report = _newest_report_for(wnum) if wnum else None
        rep_meta = yaml_shallow(last_report) if last_report else {}
        rep_label = (
            f'{rep_meta.get("task_id", "?")} {rep_meta.get("status", "")} — {rep_meta.get("summary", "")[:60]}'
            if rep_meta else '-')

        agent = meta.get('agent', '-')
        state_cell = f'{status}{ctx}'
        if evt:
            state_cell += f' (evt={evt})'
        print(f'| {w} | {meta["pane"]} | {agent} ({model}) | {cur_label} | {state_cell} | {rep_label} |')
    return 0


# ---------- entry ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='squad')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_ls = sub.add_parser('ls', help='list worker status')
    p_ls.set_defaults(func=cmd_ls)
    sub.add_parser('status', help='alias of ls').set_defaults(func=cmd_ls)

    p_as = sub.add_parser('assign', help='dispatch a task YAML to a worker via notify-worker.sh')
    p_as.add_argument('worker', help='w1 / w2 / w3 / w4')
    p_as.add_argument('task_yaml', help='path to task YAML')
    p_as.add_argument('--clear', action='store_true', help='/clear before sending (Claude only)')
    p_as.add_argument('--no-new', action='store_true', help='skip /new (Codex/W4 only)')
    p_as.add_argument('--dry-run', action='store_true', help='print the cmd without executing')
    p_as.set_defaults(func=cmd_assign)

    p_db = sub.add_parser('dashboard', help='print worker status table (Markdown)')
    p_db.set_defaults(func=cmd_dashboard)

    args = ap.parse_args(argv)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == '__main__':
    raise SystemExit(main())
