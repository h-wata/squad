#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad — tmux multi-agent dispatcher (token-0, stdlib only).

Subcommands:
  ls / status               全 worker の状態一覧 + state/<w>.json 保存
  send <worker> "<msg>"     send-keys 2段送信（連結バグ回避）
  watch [--interval N]      ls を定期実行

config.json で worker→pane マッピング。state/ に状態 JSON を保存。
状態判定は poll-sonnet-workers skill のパターン移植。
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / 'config.json'
STATE_DIR = ROOT / 'state'
CAPTURE_TAIL_LINES = 25
SEND_KEYS_GAP_SEC = 0.5

# ---------- config ----------


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f'config not found: {CONFIG_PATH}')
    return json.loads(CONFIG_PATH.read_text())


def resolve_worker(cfg: dict, name: str) -> dict:
    workers = cfg.get('workers', {})
    if name not in workers:
        sys.exit(f"unknown worker '{name}'. known: {sorted(workers)}")
    return workers[name]


# ---------- tmux primitives ----------


def tmux_capture(pane: str, lines: int = CAPTURE_TAIL_LINES) -> tuple[bool, str]:
    """Return (reachable, tail_text). reachable=False when pane/session missing."""
    r = subprocess.run(['tmux', 'capture-pane', '-t', pane, '-p'], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()
    text = r.stdout
    if lines and lines > 0:
        text = '\n'.join(text.splitlines()[-lines:])
    return True, text


def tmux_send(pane: str, msg: str) -> None:
    """2段送信: 本文(-l literal) → sleep → Enter (連結バグ & キー名誤解釈の両方を回避)."""
    r1 = subprocess.run(['tmux', 'send-keys', '-t', pane, '-l', msg], capture_output=True, text=True)
    if r1.returncode != 0:
        sys.exit(f'send-keys (msg) failed: {r1.stderr.strip()}')
    time.sleep(SEND_KEYS_GAP_SEC)
    r2 = subprocess.run(['tmux', 'send-keys', '-t', pane, 'Enter'], capture_output=True, text=True)
    if r2.returncode != 0:
        sys.exit(f'send-keys (Enter) failed: {r2.stderr.strip()}')


# ---------- 状態判定 (poll-sonnet-workers パターン移植) ----------

PERMISSION_PATTERNS = [
    r'Do you want to proceed\?',
    r'requires confirmation',
    r'Press enter to confirm',
    r'\b1\.\s+Yes\b',
]
THINKING_PATTERNS = [
    # Claude Code: "✶ Sprouting…", "· Whirring…", etc. + 経過秒
    r'(Sprouting|Whirring|Topsy-turvying|Pondering|Thinking|Working|Cogitating|Crafting)…',
    # Codex 風
    r'Working \(\d+m \d+s\)',
]
# bottom bar 例:
#   実機 (Claude Code v2): "ctx 96%"      <- 最頻
#   旧表記:                "◔ 123,456 (74%)"
#   別表記:                "Context 89% left"
CTX_PATTERNS = [
    re.compile(r'\bctx\s+(\d+)%', re.IGNORECASE),
    re.compile(r'[◔◑◕○]\s*[\d,]+\s*\((\d+)%\)'),
    re.compile(r'Context\s+(\d+)%\s+left'),
]
# bottom bar 例:
#   実機: "Opus 4.8 (1M context)", "Opus 4.7", "Sonnet 4.6"
#   旧:   "✱ Sonnet 4.6"
# group(1) で 'Opus 4.8' のような version 込みのフルネームを取る
MODEL_PATTERNS = [
    re.compile(r'\b((?:Sonnet|Opus|Haiku|Fable)\s+\d[\d.]*)'),
    re.compile(r'[✱✦★◆]\s*((?:Sonnet|Opus|Haiku|Fable)\s+\S+)'),
]


def detect_status(tail: str) -> dict:
    """capture-pane の tail から状態を抽出."""
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

    return {
        'status': status,
        'context_pct': context_pct,
        'model': model,
        'last_line': last_line[:200],
    }


# ---------- state 保存 ----------


def save_state(worker: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f'{worker}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


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
            entry = {
                'worker': w,
                'pane': pane,
                'agent': meta.get('agent'),
                **det,
                'updated_at': now,
            }
        save_state(w, entry)
        rows.append(entry)

    icon = {'idle': '🟢', 'busy': '🔵', 'permission_wait': '🟡', 'unreachable': '⚫'}
    print(f'== squad ls @ {now} ==')
    for r in rows:
        ic = icon.get(r['status'], '⚪')
        ctx = f'{r["context_pct"]}%' if r['context_pct'] is not None else '-'
        model = r['model'] or '-'
        agent = r['agent'] or '-'
        print(f'{ic} {r["worker"]:<3} {r["pane"]:<22} {r["status"]:<16} ctx={ctx:<5} model={model:<14} agent={agent}')
        if r['status'] in ('permission_wait', 'unreachable'):
            print(f'     ↳ {r["last_line"]}')
    return 0


def cmd_send(args: argparse.Namespace, cfg: dict) -> int:
    meta = resolve_worker(cfg, args.worker)
    pane = meta['pane']
    msg = args.message
    if not msg:
        sys.exit('empty message')
    # 安全弁: pane 到達不能なら送らない
    reachable, err = tmux_capture(pane, lines=1)
    if not reachable:
        sys.exit(f'cannot reach pane {pane}: {err}')
    tmux_send(pane, msg)
    print(f'sent to {args.worker} ({pane}): {msg[:60]}{"…" if len(msg) > 60 else ""}')
    return 0


def cmd_watch(args: argparse.Namespace, cfg: dict) -> int:
    interval = max(1, args.interval)
    print(f'watching every {interval}s (Ctrl-C to stop)')
    try:
        while True:
            print('\033[2J\033[H', end='')  # clear screen
            cmd_ls(args, cfg)
            time.sleep(interval)
    except KeyboardInterrupt:
        print('\nwatch stopped')
    return 0


# ---------- entry ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='squad')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_ls = sub.add_parser('ls', help='list worker status')
    p_ls.set_defaults(func=cmd_ls)
    sub.add_parser('status', help='alias of ls').set_defaults(func=cmd_ls)

    p_send = sub.add_parser('send', help='send message to worker (2-step)')
    p_send.add_argument('worker')
    p_send.add_argument('message')
    p_send.set_defaults(func=cmd_send)

    p_watch = sub.add_parser('watch', help='periodic ls')
    p_watch.add_argument('--interval', type=int, default=10)
    p_watch.set_defaults(func=cmd_watch)

    args = ap.parse_args(argv)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == '__main__':
    raise SystemExit(main())
