#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad — tmux multi-agent インタラクティブ CLI (token-0, stdlib only).

棲み分け:
  - 常駐 daemon (report/permission/stall/discovery/GC) → watch.sh が担当
  - send-keys timing 制御 (/clear /model /new + 着手確認) → notify-worker.sh が担当
  - インタラクティブ単発: 状態確認 / 指示 / dashboard 生成 → このスクリプト

Subcommands:
  ls                        全 worker の状態一覧 + state/<w>.json 保存
  assign <w> <task.yaml>    task YAML を読み notify-worker.sh で通知
  dashboard                 Worker ステータス表を生成して stdout
  graph sync / why <id> / show <id>
                             task / report YAML の探索グラフ索引 (squad/graph.py, Issue #55)
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

import ctx  # noqa: E402
from ledger import ReportLedger  # noqa: E402
from notify_queue import notify_dir_for  # noqa: E402
from notify_queue import NotificationQueue  # noqa: E402
from notify_queue import QueueUnreadableError  # noqa: E402

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
CONFIG_PATH = ROOT / 'config.json'
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
    # pane は config.json 上 session 非依存の window 名 (例 "w1") で持つ。ここで解決済み session 名と
    # 結合し、以降の呼び出し元は meta['pane'] をそのまま使える。旧構成のセッションなら "0.1" 等になる
    windows = tmux_windows(session)
    for meta in cfg.get('workers', {}).values():
        pane = meta.get('pane', '')
        if pane and ':' not in pane:
            meta['pane'] = tmux_target(session, pane, windows)
    return cfg


def resolve_worker(cfg: dict, name: str) -> dict:
    workers = cfg.get('workers', {})
    if name not in workers:
        sys.exit(f"unknown worker '{name}'. known: {sorted(workers)}")
    return workers[name]


# ---------- tmux primitives ----------


# start.sh はエージェントごとに window を作り、役割の名前を付ける (#95)。旧構成は window 0 を 7 pane に
# 分けていた。マージ前から動いているセッションは旧構成のままなので、window が無ければ旧 pane に落とす
LEGACY_PANES = {'dispatcher': '0.0', 'w1': '0.1', 'w2': '0.2', 'w3': '0.3', 'term': '0.4', 'aux': '0.5', 'w4': '0.6'}
_LEGACY_WINDOWS = {v: k for k, v in LEGACY_PANES.items()}


def tmux_windows(session: str) -> list[str] | None:
    """Session の window 名一覧。セッションが無い・tmux が無ければ None."""
    try:
        r = subprocess.run(
            ['tmux', 'list-windows', '-t', f'={session}', '-F', '#{window_name}'], capture_output=True, text=True
        )
    except OSError:
        return None
    return r.stdout.splitlines() if r.returncode == 0 else None


def tmux_target(session: str, name: str, windows: list[str] | None) -> str:
    """役割名 (w1 / dispatcher 等) か旧 pane ("0.1") を、そのセッションの構成に合った宛先にする.

    旧構成 (セッションはあるがその名前の window が無い) なら "<session>:0.1"、それ以外は "<session>:w1"。
    windows は tmux_windows(session) の結果 (1 回の list-windows を呼び出し元で使い回す)。
    """
    window = _LEGACY_WINDOWS.get(name, name)
    if windows is None or window in windows or window not in LEGACY_PANES:
        return f'{session}:{window}'
    return f'{session}:{LEGACY_PANES[window]}'


def tmux_capture(pane: str, lines: int = CAPTURE_TAIL_LINES) -> tuple[bool, str]:
    """Return (reachable, tail_text)."""
    r = subprocess.run(['tmux', 'capture-pane', '-t', pane, '-p'], capture_output=True, text=True)
    if r.returncode != 0:
        return False, r.stderr.strip()
    # 画面の下の空行を落としてから末尾を取る (watchd の Tmux.capture と同じ)。背の高い window で出力が
    # 画面の上にあると、空行だけの末尾になってスピナー行が範囲外に出て、作業中を待機中と読む (#95)
    text = r.stdout.rstrip()
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
    # スピナー行 `<動詞>… (<経過時間>`。動詞は毎回入れ替わるので一覧にしない。
    # 終了表示 `✻ Sautéed for 20s` には `…` が付かないので idle のまま
    # 本文中の引用で誤検知しないよう、スピナーの記号 (· ✢ ✳ ✶ ✻ ✽ の許可リスト) で始まる行に限る
    r'(?m)^[ \t]*[·✢✳✶✻✽][ \t]+[\w-]+…[ \t]*\(\d+[ms]',
    r'(?m)^[ \t]*[·✢✳✶✻✽][ \t]+Working \(\d+m \d+s\)',
    # Codex: `• Working (1m 06s • esc to interrupt)`。終了は `─ Worked for 7m 07s` で別の形
    r'(?m)^[ \t]*•[ \t]+Working \((?:\d+h )?(?:\d+m )?\d+s • esc to interrupt\)',
]
# (パターン, 使用率への変換)。pane 読みは補助 (ctx の第一の取得元は status line, #105)
CTX_PATTERNS = [
    # `ctx 12%` (squad の status line) と `ctx:54%` (Dispatcher の個人 status line)
    (re.compile(r'\bctx\s*:?\s*(\d+)%', re.IGNORECASE), lambda n: n),
    (re.compile(r'[◔◑◕○]\s*[\d,]+\s*\((\d+)%\)'), lambda n: n),
    # Codex の `Context 100% left` は残りの割合なので、使用率は 100 から引く
    (re.compile(r'Context\s+(\d+)%\s+left'), lambda n: max(0, 100 - n)),
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
    for p, to_used in CTX_PATTERNS:
        m = p.search(tail)
        if m:
            context_pct = to_used(int(m.group(1)))
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


def load_state(worker: str) -> dict | None:
    p = ctx.state_dir() / f'{worker}.json'
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ---------- shallow YAML reader (stdlib only) ----------


def yaml_shallow(path: Path) -> dict[str, str]:
    """top-level の `key: value` だけ拾う簡易リーダー.

    block scalar (`|`, `>`) は本文を空白区切りの 1 行に畳んで返す (表示用。report の summary は
    `summary: |` で書かれることが多い)。list, nested は無視。CLI で必要な
    task_id/project/assigned_to/agent/model/title/status/summary/completed_at
    だけ取れれば十分。
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    pat = re.compile(r'^([A-Za-z_][\w\-]*)\s*:\s*(.+?)\s*$')
    block_key = ''  # 本文を集めている block scalar の key
    block: list[str] = []
    for line in path.read_text(errors='replace').splitlines():
        if block_key and (not line.strip() or line[0].isspace()):
            if line.strip():
                block.append(line.strip())
            continue
        if block_key:
            out[block_key] = ' '.join(block)
            block_key, block = '', []
        # top-level 行 (インデント無し) のみ対象
        if not line or line[0].isspace() or line.startswith('#'):
            continue
        m = pat.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if val in ('|', '>', '|-', '>-', '|+', '>+'):
            block_key = key
            continue
        # クオート剥がし
        if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
            val = val[1:-1]
        out[key] = val
    if block_key:
        out[block_key] = ' '.join(block)
    return out


# ---------- subcommands ----------


def probe_worker(pane: str) -> dict:
    """Pane 1 つの状態 (idle / busy / permission_wait / unreachable)。squad ls と squad web が共有する."""
    reachable, tail = tmux_capture(pane)
    if not reachable:
        return {'status': 'unreachable', 'context_pct': None, 'model': None, 'last_line': tail[:200]}
    return detect_status(tail)


def ctx_label(r: dict) -> str:
    """`12%(statusline 3分前)`。取れなければ `不明` (0% や古い値で埋めない, #105)."""
    if r.get('context_pct') is None:
        if r.get('context_tokens') is not None:
            return f'不明({r["context_tokens"]}tok {r["context_source"]} {ctx.age_label(r.get("context_at"))})'
        return '不明'
    return f'{r["context_pct"]}%({r["context_source"]} {ctx.age_label(r.get("context_at"))})'


def cmd_ls(_: argparse.Namespace, cfg: dict) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    session = resolve_session(cfg)
    rows = []
    # Dispatcher は config.json の workers に無い。status line が state/dispatcher.json に書く値を出すため先頭に足す
    targets = {'dispatcher': {'agent': 'claude', 'pane': tmux_target(session, 'dispatcher', tmux_windows(session))}}
    targets.update(cfg.get('workers', {}))
    for w, meta in targets.items():
        pane = meta['pane']
        probe = probe_worker(pane)

        # hook (last_event 等) と status line (ctx_sources) が書いた値は残し、画面から読んだ分だけ上書きする
        def merge(st: dict, w: str = w, pane: str = pane, meta: dict = meta, probe: dict = probe) -> None:
            st.update({'worker': w, 'pane': pane, 'agent': meta.get('agent')})
            st.update({k: probe[k] for k in ('status', 'last_line')})
            st['updated_at'] = now
            ctx.apply(st, session, probe)

        rows.append(ctx.update_state(ctx.state_dir() / f'{w}.json', merge))

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
        model = r['model'] or '-'
        agent = r['agent'] or '-'
        evt = f' evt={r["last_event"]}' if r.get('last_event') else ''
        print(
            f'{ic} {r["worker"]:<10} {r["pane"]:<22} {r["status"]:<16} '
            f'ctx={ctx_label(r):<22} model={model:<14} agent={agent}{evt}'
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


def _graph_paths(args: argparse.Namespace) -> tuple[object, Path, Path]:
    """Graph モジュールと (queue_dir, db_path)。PyYAML が無ければ ImportError を上げる."""
    import graph  # noqa: PLC0415 (PyYAML 依存を graph サブコマンドに閉じ込める)

    queue_dir = Path(args.queue_dir) if args.queue_dir else REPO_ROOT / 'queue'
    return graph, queue_dir, Path(args.db) if args.db else graph.default_db_path(queue_dir)


def cmd_graph(args: argparse.Namespace, _cfg: dict) -> int:
    try:
        graph, queue_dir, db = _graph_paths(args)
    except ImportError as e:
        print(f'squad graph には PyYAML が必要です: {e}', file=sys.stderr)
        return 2
    if args.graph_cmd == 'sync':
        n_nodes, n_edges = graph.sync(queue_dir, db)
        print(f'synced {n_nodes} nodes, {n_edges} edges -> {db}')
        return 0
    if args.graph_cmd == 'status':
        # watchd は report 配達時にしか sync しないので、発行直後の task も拾えるよう毎回作り直す
        graph.sync(queue_dir, db)
        data = graph.status(db, args.project)
        print(
            json.dumps(data, ensure_ascii=False, indent=2)
            if args.format == 'json'
            else '\n'.join(graph.render_status(data))
        )
        return 0
    if not db.exists():
        print(f'{db} がありません。先に squad graph sync を実行してください', file=sys.stderr)
        return 1
    lines = graph.why(db, args.node_id) if args.graph_cmd == 'why' else graph.show(db, args.node_id)
    print('\n'.join(lines))
    return 0


def cmd_trace(args: argparse.Namespace, _cfg: dict) -> int:
    import trace_search  # noqa: PLC0415

    if args.trace_cmd == 'get':
        print('\n'.join(trace_search.get(args.trace_id, args.context)))
        return 0
    queue_dir = Path(args.queue_dir) if args.queue_dir else REPO_ROOT / 'queue'
    srcs, missing = trace_search.sources(queue_dir, args.project, args.task)
    hits = trace_search.search(srcs, args.query, args.tool, args.errors_only, args.limit)
    for e in hits:
        print(trace_search.fmt(e, args.query))
    if missing:
        shown = ', '.join(missing[:5]) + (' ...' if len(missing) > 5 else '')
        print(f'transcript が見つからない task: {len(missing)} 件 ({shown})')
    if any(not e.source.exact for e in hits):
        print('~ 付きは report に session_id が無く、時間窓で近似した transcript (他 session を含みうる)')
    return 0


def cmd_web(args: argparse.Namespace, _cfg: dict) -> int:
    import web  # noqa: PLC0415

    token = args.token or os.environ.get('SQUAD_WEB_TOKEN', '')
    if not token:
        sys.exit(
            'squad web: トークンが必要です (SQUAD_WEB_TOKEN か --token)。LAN に公開するため未設定では起動しません'
        )
    queue_dir = Path(args.queue_dir) if args.queue_dir else REPO_ROOT / 'queue'
    srv = web.make_server(args.host, args.port, token, queue_dir / '.graph.db', allow_write=args.allow_write)
    mode = f'指示の投入あり: {queue_dir / "instructions"}' if args.allow_write else '読み取り専用'
    print(f'squad web: http://{args.host}:{srv.server_address[1]}/?token=<token> ({mode}, Ctrl-C で停止)')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
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

    p_graph = sub.add_parser('graph', help='task / report YAML の探索グラフ索引 (Issue #55)')
    graph_sub = p_graph.add_subparsers(dest='graph_cmd', required=True)
    graph_sub.add_parser('sync', help='YAML から索引を作り直す (YAML は書き換えない)')
    p_gs = graph_sub.add_parser('status', help='goal 別の intent 件数と未完了 intent 一覧 (sync してから表示)')
    p_gs.add_argument('--project')
    p_gs.add_argument('--format', choices=['markdown', 'json'], default='markdown')
    for name, help_text in (
        ('why', 'node_id から導出元 (goal 方向) へ遡る'),
        ('show', '1 ノードと前後のエッジを表示'),
    ):
        graph_sub.add_parser(name, help=help_text).add_argument(
            'node_id', help='例: intent:<project>/<task_id>, result:<report_id>'
        )
    for p in graph_sub.choices.values():
        p.add_argument('--queue-dir', help=f'queue ルート path (既定: {REPO_ROOT / "queue"})')
        p.add_argument('--db', help='graph DB path (既定: <queue-dir>/.graph.db)')
    p_graph.set_defaults(func=cmd_graph)

    p_trace = sub.add_parser('trace', help='worker の Claude Code transcript を横断検索 (Issue #56)')
    trace_sub = p_trace.add_subparsers(dest='trace_cmd', required=True)
    p_ts = trace_sub.add_parser('search', help='tool の入力・結果と assistant の text を検索 (出力はマスク済み)')
    p_ts.add_argument('query')
    p_ts.add_argument('--project')
    p_ts.add_argument('--task', help='task_id')
    p_ts.add_argument('--tool', help='tool 名 (例: Bash)。assistant の text は text')
    p_ts.add_argument('--errors-only', action='store_true', help='is_error の tool_result だけ')
    p_ts.add_argument('--limit', type=int, default=20)
    p_ts.add_argument('--queue-dir', help=f'queue ルート path (既定: {REPO_ROOT / "queue"})')
    p_tg = trace_sub.add_parser('get', help='trace_id の詳細 (出力はマスク済み)')
    p_tg.add_argument('trace_id', help='<session_id>:<行>.<block> (<行> だけならその行の全 block)')
    p_tg.add_argument('--context', type=int, default=0, help='前後に含める行数')
    p_trace.set_defaults(func=cmd_trace)

    p_web = sub.add_parser('web', help='全 project の進捗を見る読み取り専用 Web ダッシュボード (Issue #76)')
    p_web.add_argument('--host', default='0.0.0.0', help='既定 0.0.0.0 (LAN から開ける)')  # noqa: S104
    p_web.add_argument('--port', type=int, default=8765)
    p_web.add_argument('--token', help='アクセストークン (既定: env SQUAD_WEB_TOKEN。未設定なら起動しない)')
    p_web.add_argument('--queue-dir', help=f'queue ルート path (既定: {REPO_ROOT / "queue"})。読むのは .graph.db だけ')
    p_web.add_argument(
        '--allow-write',
        action='store_true',
        help='Dispatcher への指示の投入 (POST /api/instructions -> queue/instructions/<session>/) を許す (#85)',
    )
    p_web.set_defaults(func=cmd_web)

    args = ap.parse_args(argv)
    cfg = load_config()
    return args.func(args, cfg)


if __name__ == '__main__':
    raise SystemExit(main())
