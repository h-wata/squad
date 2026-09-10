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
  muster                    全 squad session を横断表示 (中隊ビュー・read-only)
  order -s <s1,s2> "..."    選んだ session の Dispatcher に同じ指示を送る
  hq                        squad session を tab として束ねる HQ session を作る
  dashboard                 Worker ステータス表を生成して stdout
  ledger claim/commit/fail
                             report 配達 ledger (squad/ledger.py, sqlite3) を手動操作する
                             デバッグ用 CLI。watchd.py 自体は ReportLedger をプロセス内で
                             直接呼ぶため通常運用では使わない (Issue #26)。
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ledger import delivery_key  # noqa: E402
from ledger import find_reports  # noqa: E402
from ledger import report_identity  # noqa: E402
from ledger import ReportLedger  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import QueueUnreadableError  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
CONFIG_PATH = ROOT / 'config.json'
STATE_DIR = ROOT / 'state'
NOTIFY_WORKER = REPO_ROOT / 'scripts' / 'notify-worker.sh'
QUEUE_DIR = REPO_ROOT / 'queue' / 'projects'
CAPTURE_TAIL_LINES = 25
DEFAULT_LEDGER_PATH = REPO_ROOT / 'queue' / '.report_ledger.db'

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
        print(
            f'{ic} {r["worker"]:<3} {r["pane"]:<22} {r["status"]:<16} '
            f'ctx={ctx:<5} model={model:<14} agent={agent}{evt}'
        )
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
        f'[assign] agent={agent or "?"} model={model or "-"} codex={is_codex} clear={args.clear} no_new={args.no_new}'
    )
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
            if rep_meta
            else '-'
        )

        agent = meta.get('agent', '-')
        state_cell = f'{status}{ctx}'
        if evt:
            state_cell += f' (evt={evt})'
        print(f'| {w} | {meta["pane"]} | {agent} ({model}) | {cur_label} | {state_cell} | {rep_label} |')
    return 0


def _ledger(args: argparse.Namespace) -> ReportLedger:
    path = Path(args.ledger_file) if args.ledger_file else DEFAULT_LEDGER_PATH
    return ReportLedger(path)


def cmd_ledger_claim(args: argparse.Namespace, _cfg: dict) -> int:
    c = _ledger(args).claim(args.project, args.report_id, args.path, args.sha)
    print(json.dumps(c._asdict(), ensure_ascii=False))
    return 0 if c.ok else 1


def cmd_ledger_commit(args: argparse.Namespace, _cfg: dict) -> int:
    ok = _ledger(args).commit(args.project, args.report_id, args.token)
    print(json.dumps({'ok': ok}))
    return 0 if ok else 1


def cmd_ledger_fail(args: argparse.Namespace, _cfg: dict) -> int:
    ok = _ledger(args).fail(args.project, args.report_id, args.token)
    print(json.dumps({'ok': ok}))
    return 0 if ok else 1


# ---------- notify queue (SQUAD-220 の pull/ack helper) ----------


def _notify_queue(args: argparse.Namespace, cfg: dict) -> NotificationQueue:
    session = resolve_session(cfg)
    queue_dir = Path(args.queue_dir) if getattr(args, 'queue_dir', None) else REPO_ROOT / 'queue'
    return NotificationQueue(session, notify_dir_for(queue_dir, session))


def _unreadable(nq: NotificationQueue, e: QueueUnreadableError) -> int:
    """Queue 破損を「0 件」と誤読させないため、明示的なエラーで落とす (SQUAD-226)."""
    payload = {'error': 'queue_unreadable', 'detail': str(e), 'health': nq.read_health()}
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 2


def cmd_notify_pull(args: argparse.Namespace, cfg: dict) -> int:
    """未 ack event (+ health) を JSON Lines で表示する。Dispatcher は Read だけで良い."""
    nq = _notify_queue(args, cfg)
    try:
        events = nq.unacked()
    except QueueUnreadableError as e:
        return _unreadable(nq, e)
    if args.priority:
        events = [e for e in events if e['priority'] == args.priority]
    for e in events:
        print(json.dumps(e, ensure_ascii=False))
    if args.health:
        print(json.dumps(nq.read_health(), ensure_ascii=False))
    return 0


def cmd_notify_ack(args: argparse.Namespace, cfg: dict) -> int:
    """Event を ack する ('all' で現在の未 ack を一括 ack)."""
    nq = _notify_queue(args, cfg)
    try:
        ids = [e['event_id'] for e in nq.unacked()] if args.event_id == 'all' else [args.event_id]
    except QueueUnreadableError as e:
        return _unreadable(nq, e)
    ok = True
    for eid in ids:
        r = nq.ack(eid, by=args.by or '')
        ok = ok and r
        print(json.dumps({'event_id': eid, 'ok': r}))
    return 0 if ok else 1


# ---------- 中隊 (cross-session) ----------

WORKER_SHORT = {'busy': 'busy', 'idle': 'idle', 'permission_wait': 'PERM'}


def default_owner() -> str:
    """マーカーの無い project を担当する既定 session を返す (watchd.py と同じ既定値)."""
    return os.environ.get('SQUAD_DEFAULT_OWNER') or 'ros-agents'


def project_owner(pj_dir: Path) -> str:
    """`.squad_session` マーカーを読む (無い / 空なら既定 owner)."""
    try:
        first = pj_dir.joinpath('.squad_session').read_text().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return default_owner()
    return first or default_owner()


def owner_map() -> dict[str, list[str]]:
    """Session 名 -> 担当 project 名リストを返す."""
    out: dict[str, list[str]] = {}
    if QUEUE_DIR.is_dir():
        for d in sorted(QUEUE_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith('.'):
                out.setdefault(project_owner(d), []).append(d.name)
    return out


def tmux_sessions() -> set[str]:
    r = subprocess.run(['tmux', 'list-sessions', '-F', '#{session_name}'], capture_output=True, text=True)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def watcher_pid(session: str) -> int | None:
    """Watcher の生存確認: pidfile → /proc environ 照合 (bin/squad status と同じ順序)."""
    try:
        pid = int(Path(f'/tmp/{session}-watch.pid').read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        pass
    # watch.sh は watchd.py を exec するため、移行期の両方の名前を拾う
    r = subprocess.run(['pgrep', '-f', rf'{REPO_ROOT}/(watch\.sh|squad/watchd\.py)'], capture_output=True, text=True)
    for pid_s in r.stdout.split():
        try:
            environ = Path(f'/proc/{pid_s}/environ').read_bytes().decode('utf-8', 'replace')
        except OSError:
            continue
        got = next((v.split('=', 1)[1] for v in environ.split('\0') if v.startswith('SQUAD_SESSION=')), 'ros-agents')
        if got == session:
            return int(pid_s)
    return None


def is_squad_session(session: str, owners: dict[str, list[str]]) -> bool:
    """Squad と無関係な tmux session を弾く (bin/squad status と同じ判定)."""
    return session in owners or watcher_pid(session) is not None or Path(f'/tmp/{session}-watch.log').exists()


def pending_reports(pj_dir: Path, ledger: ReportLedger) -> int:
    """Dispatcher にまだ配達されていない report 数を数える.

    配達キーの導出は watchd.py と同じく ledger 側の delivery_key に任せる。
    worker*_review.yaml は schema 上 report_id を持たず `review:<path>:<sha>` を
    キーにするため、ここで report_id を直接読むと配達済み review を永久に
    未配達として数えてしまう。
    """
    n = 0
    for project, path in find_reports([pj_dir]):
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue  # 走査中に消えた / 読めない
        sha, meta, parse_error = report_identity(data)
        report_id, _invalid = delivery_key(meta, sha, parse_error, path)
        if not ledger.is_delivered(project, report_id):
            n += 1
    return n


def session_workers(session: str, cfg: dict, alive: bool) -> dict[str, str]:
    """Session 内の worker 状態を tmux から直接読む (state/*.json は session 非依存なので使わない)."""
    out: dict[str, str] = {}
    for name, meta in sorted(cfg.get('workers', {}).items()):
        if not alive:
            out[name] = '-'
            continue
        pane = meta.get('pane', '').rsplit(':', 1)[-1]
        reachable, tail = tmux_capture(f'{session}:{pane}')
        out[name] = WORKER_SHORT.get(detect_status(tail)['status'], '?') if reachable else '-'
    return out


def cmd_muster(_: argparse.Namespace, cfg: dict) -> int:
    """全 squad session を 1 画面に並べる (read-only)."""
    owners = owner_map()
    live = tmux_sessions()
    sessions = sorted(set(owners) | {s for s in live if is_squad_session(s, owners)})
    if not sessions:
        print('squad session が見つかりません (queue/projects/*/.squad_session と tmux を確認してください)')
        return 1

    ledger = ReportLedger(DEFAULT_LEDGER_PATH)
    has_ledger = ledger.exists() and ledger.is_sqlite()
    worker_names = sorted(cfg.get('workers', {}))

    head = ['SESSION', 'TMUX', 'WATCH', *(w.upper() for w in worker_names), 'PJ', 'PEND']
    rows = []
    for s in sessions:
        alive = s in live
        pid = watcher_pid(s)
        pjs = owners.get(s, [])
        pend = sum(pending_reports(QUEUE_DIR / p, ledger) for p in pjs) if has_ledger else -1
        ws = session_workers(s, cfg, alive)
        rows.append(
            (
                [
                    s,
                    'alive' if alive else 'DOWN',
                    f'pid:{pid}' if pid else '-',
                    *(ws[w] for w in worker_names),
                    str(len(pjs)),
                    '?' if pend < 0 else str(pend),
                ],
                s,
                alive,
                pjs,
                pend,
            )
        )

    widths = [max(len(h), *(len(r[0][i]) for r in rows)) for i, h in enumerate(head)]
    print('  '.join(h.ljust(w) for h, w in zip(head, widths)))
    for cells, *_ in rows:
        print('  '.join(c.ljust(w) for c, w in zip(cells, widths)))

    print()
    for _cells, s, alive, pjs, pend in rows:
        if not pjs:
            continue
        flag = '' if alive else '  ← session 未起動 (この PJ の report は誰も見ていない)'
        print(f'{s}: {", ".join(pjs)}{flag}')
        if alive and pend > 0:
            print(f'{" " * len(s)}  未配達 report {pend} 件')
    return 0


def cmd_order(args: argparse.Namespace, _cfg: dict) -> int:
    """選んだ session の Dispatcher (pane 0.0) に同じ指示を送る."""
    owners = owner_map()
    live = tmux_sessions()
    if args.all:
        targets = sorted(s for s in live if is_squad_session(s, owners))
    else:
        targets = [s.strip() for s in args.sessions.split(',') if s.strip()]
    if not targets:
        print('送信先がありません', file=sys.stderr)
        return 1

    print(f'送信先: {", ".join(targets)}')
    if args.dry_run:
        return 0

    failed = 0
    for s in targets:
        if s not in live:
            print(f'  {s}: セッション未起動 (skip)')
            failed += 1
            continue
        env = {**os.environ, 'SQUAD_SESSION': s}
        # Dispatcher は pane 0.0。notify-worker.sh が pane 直指定と timing 制御を持っている
        r = subprocess.run([str(NOTIFY_WORKER), '0.0', args.message], env=env, capture_output=True, text=True)
        if r.returncode == 0:
            print(f'  {s}: Dispatcher 送信 OK')
        else:
            print(f'  {s}: 送信失敗 — {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown"}')
            failed += 1
    return 1 if failed else 0


def _tmux(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(['tmux', *argv], capture_output=True, text=True)


def cmd_hq(args: argparse.Namespace, _cfg: dict) -> int:
    """Squad session を tab として束ねる HQ session を作る / 貼り直す.

    window 0 が中隊長のコンソール (shell)、window 1.. は各 squad session の
    window 0 への link。link なので squad 側の session / watcher / hook は無傷で、
    HQ を kill しても squad は生き残る。
    """
    hq = args.session
    owners = owner_map()
    live = tmux_sessions()
    targets = sorted(s for s in live if s != hq and is_squad_session(s, owners))
    if not targets:
        print('起動中の squad session がありません。先に squad start してください', file=sys.stderr)
        return 1

    if hq not in live:
        _tmux('new-session', '-d', '-s', hq, '-n', 'HQ')
        _tmux('send-keys', '-t', f'{hq}:0', f'{sys.argv[0]} muster', 'Enter')
        print(f'HQ session を作成: {hq}')

    # 既存の link を外す。unlink なので squad 側の window は消えない。
    # link されていない window (利用者が HQ session に自分で作ったもの) は触らない
    rows = _tmux('list-windows', '-t', hq, '-F', '#{window_index} #{window_linked}').stdout.splitlines()
    linked = [r.split()[0] for r in rows if r.split()[1:2] == ['1']]
    for w in sorted(linked, key=int, reverse=True):
        _tmux('unlink-window', '-t', f'{hq}:{w}')

    for s in targets:
        used = [int(w) for w in _tmux('list-windows', '-t', hq, '-F', '#{window_index}').stdout.split()]
        nxt = max(used, default=0) + 1
        r = _tmux('link-window', '-s', f'{s}:0', '-t', f'{hq}:{nxt}')
        if r.returncode != 0:
            print(f'  {s}: link 失敗 — {r.stderr.strip()}', file=sys.stderr)
            continue
        # tab 名を session 名に。link された window は実体が同じなので squad 側の
        # window 名も変わるが、squad session は window 1 枚なので実害はない
        _tmux('set-window-option', '-t', f'{hq}:{nxt}', 'automatic-rename', 'off')
        _tmux('rename-window', '-t', f'{hq}:{nxt}', s)
        print(f'  {hq}:{nxt} <- {s}')

    print(f'\ntmux attach -t {hq}   (window 0 = HQ, 1.. = 各 squad)')
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

    p_mu = sub.add_parser('muster', help='全 squad session を横断表示 (中隊ビュー・read-only)')
    p_mu.set_defaults(func=cmd_muster)

    p_or = sub.add_parser('order', help='選んだ session の Dispatcher に同じ指示を送る')
    p_or.add_argument('message', help='Dispatcher に送る指示')
    p_or.add_argument('-s', '--sessions', default='', help='送信先 session (カンマ区切り)')
    p_or.add_argument('--all', action='store_true', help='起動中の全 squad session に送る (-s の代わりに明示指定)')
    p_or.add_argument('--dry-run', action='store_true', help='送信先を表示するだけ')
    p_or.set_defaults(func=cmd_order)

    p_hq = sub.add_parser('hq', help='squad session を tab として束ねる HQ session を作る / 貼り直す')
    p_hq.add_argument('-s', '--session', default='hq', help='HQ session 名 (既定: hq)')
    p_hq.set_defaults(func=cmd_hq)

    p_db = sub.add_parser('dashboard', help='print worker status table (Markdown)')
    p_db.set_defaults(func=cmd_dashboard)

    p_ledger = sub.add_parser('ledger', help='report 配達 ledger (sqlite3) の手動操作 (デバッグ用)')
    ledger_sub = p_ledger.add_subparsers(dest='ledger_cmd', required=True)
    for name, func, extra in (
        ('claim', cmd_ledger_claim, (('project', {}), ('report_id', {}), ('path', {}), ('sha', {}))),
        ('commit', cmd_ledger_commit, (('project', {}), ('report_id', {}), ('token', {}))),
        ('fail', cmd_ledger_fail, (('project', {}), ('report_id', {}), ('token', {}))),
    ):
        p = ledger_sub.add_parser(name)
        for arg_name, kwargs in extra:
            p.add_argument(arg_name, **kwargs)
        p.add_argument('--ledger-file', help=f'ledger DB path (既定: {DEFAULT_LEDGER_PATH})')
        p.set_defaults(func=func)

    p_notify = sub.add_parser('notify', help='session-local durable notification queue の pull/ack (SQUAD-220)')
    notify_sub = p_notify.add_subparsers(dest='notify_cmd', required=True)
    p_pull = notify_sub.add_parser('pull', help='未 ack event (+ health) を表示')
    p_pull.add_argument('--priority', choices=['critical', 'normal', 'low'])
    p_pull.add_argument('--health', action='store_true', help='health.json も表示')
    p_pull.add_argument('--queue-dir', help=f'queue ルート path (既定: {REPO_ROOT / "queue"})')
    p_pull.set_defaults(func=cmd_notify_pull)
    p_ack = notify_sub.add_parser('ack', help='event を ack する (event_id か all)')
    p_ack.add_argument('event_id')
    p_ack.add_argument('--by', help='ack した主体 (省略可)')
    p_ack.add_argument('--queue-dir', help=f'queue ルート path (既定: {REPO_ROOT / "queue"})')
    p_ack.set_defaults(func=cmd_notify_ack)

    args = ap.parse_args(argv)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == '__main__':
    raise SystemExit(main())
