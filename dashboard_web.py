# Copyright (c) 2026 SoftBank Corp.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Multi-Agent Task Dashboard - Web UI."""

from datetime import datetime
from pathlib import Path
import re

from flask import Flask
from flask import jsonify
import yaml

app = Flask(__name__)
BASE_DIR = Path(__file__).parent
QUEUE_DIR = BASE_DIR / 'queue'
DASHBOARD_MD = BASE_DIR / 'dashboard.md'

WORKERS = ['worker1', 'worker2', 'worker3', 'coder', 'reviewer', 'documenter']


def _read_yaml(path: Path) -> dict | None:
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def _parse_dashboard_md() -> dict:
    """Parse dashboard.md for status info."""
    result = {
        'last_updated': '',
        'workers': [],
        'active_tasks': [],
        'pending_tasks': [],
        'completed_tasks': [],
        'issues': [],
    }
    if not DASHBOARD_MD.exists():
        return result

    text = DASHBOARD_MD.read_text()

    m = re.search(r'\*\*最終更新\*\*:\s*(.+)', text)
    if m:
        result['last_updated'] = m.group(1).strip()

    # Parse worker table
    worker_section = re.search(r'## 現在のステータス\s*\n\n\|.*\n\|[-| ]+\n((?:\|.*\n)*)', text)
    if worker_section:
        for line in worker_section.group(1).strip().split('\n'):
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) >= 4:
                result['workers'].append({
                    'name': cols[0],
                    'pane': cols[1],
                    'status': cols[2],
                    'task': cols[3],
                })

    # Parse active tasks table
    active_section = re.search(r'## アクティブタスク\s*\n\n\|.*\n\|[-| ]+\n((?:\|.*\n)*)', text)
    if active_section:
        for line in active_section.group(1).strip().split('\n'):
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) >= 4:
                result['active_tasks'].append({
                    'task_id': cols[0],
                    'assignee': cols[1],
                    'title': cols[2],
                    'purpose': cols[3],
                })

    # Parse pending tasks
    pending_section = re.search(r'## 待機中タスク[^\n]*\n\n((?:- .+\n)*)', text)
    if pending_section:
        for line in pending_section.group(1).strip().split('\n'):
            m2 = re.match(r'- (.+)', line)
            if m2:
                result['pending_tasks'].append(m2.group(1).strip())

    # Parse completed tasks table
    completed_section = re.search(r'## 完了タスク\s*\n\n\|.*\n\|[-| ]+\n((?:\|.*\n)*)', text)
    if completed_section:
        for line in completed_section.group(1).strip().split('\n'):
            cols = [c.strip() for c in line.split('|')[1:-1]]
            if len(cols) >= 4:
                result['completed_tasks'].append({
                    'task_id': cols[0],
                    'assignee': cols[1],
                    'title': cols[2],
                    'completed_at': cols[3],
                })

    # Parse issues
    issues_section = re.search(r'## 保留中の問題\s*\n\n((?:- .+\n)*)', text)
    if issues_section:
        for line in issues_section.group(1).strip().split('\n'):
            m2 = re.match(r'- (.+)', line)
            if m2 and 'ありません' not in m2.group(1):
                result['issues'].append(m2.group(1).strip())

    return result


def _load_queue_data() -> dict:
    """Load task and report YAML files."""
    tasks = {}
    reports = {}

    tasks_dir = QUEUE_DIR / 'tasks'
    reports_dir = QUEUE_DIR / 'reports'

    if tasks_dir.exists():
        for f in tasks_dir.glob('*.yaml'):
            if f.name.startswith('.'):
                continue
            data = _read_yaml(f)
            if data and data.get('task_id'):
                tasks[f.stem] = data

    if reports_dir.exists():
        for f in reports_dir.glob('*.yaml'):
            if f.name.startswith('.'):
                continue
            data = _read_yaml(f)
            if data and data.get('task_id'):
                reports[f.stem] = data

    return {'tasks': tasks, 'reports': reports}


@app.route('/api/status')
def api_status() -> dict:
    """Return dashboard and queue status as JSON."""
    dashboard = _parse_dashboard_md()
    queue = _load_queue_data()
    return jsonify({
        'dashboard': dashboard,
        'queue': queue,
        'timestamp': datetime.now().isoformat(),
    })


@app.route('/')
def index() -> str:
    """Return HTML dashboard template."""
    return HTML_TEMPLATE


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Agent Dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --surface: #161b22;
    --surface2: #21262d;
    --border: #30363d;
    --text: #e6edf3;
    --text-dim: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --yellow: #d29922;
    --red: #f85149;
    --purple: #bc8cff;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }

  /* ── Header ── */
  .header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  .header h1 { font-size: 18px; font-weight: 600; }
  .header-meta {
    display: flex; align-items: center; gap: 16px;
    font-size: 12px; color: var(--text-dim);
  }
  .pulse {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .container { max-width: 1100px; margin: 0 auto; padding: 20px 24px; }

  /* ── Progress Bar ── */
  .progress-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px 24px;
    margin-bottom: 20px;
  }
  .progress-header {
    display: flex; justify-content: space-between; align-items: baseline;
    margin-bottom: 12px;
  }
  .progress-title { font-size: 15px; font-weight: 600; }
  .progress-numbers { font-size: 28px; font-weight: 700; color: var(--accent); }
  .progress-numbers span { font-size: 14px; color: var(--text-dim); font-weight: 400; }
  .progress-bar-track {
    width: 100%; height: 12px;
    background: var(--surface2); border-radius: 6px;
    overflow: hidden; display: flex;
  }
  .progress-bar-done {
    height: 100%; background: var(--green);
    transition: width .5s ease;
  }
  .progress-bar-active {
    height: 100%; background: var(--accent);
    transition: width .5s ease;
    animation: barPulse 2s infinite;
  }
  .progress-bar-pending {
    height: 100%; background: var(--yellow); opacity: .5;
    transition: width .5s ease;
  }
  @keyframes barPulse { 0%,100%{opacity:1} 50%{opacity:.6} }
  .progress-legend {
    display: flex; gap: 20px; margin-top: 8px; font-size: 11px; color: var(--text-dim);
  }
  .legend-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 4px; vertical-align: middle;
  }

  /* ── Worker Panels ── */
  .workers-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 14px;
    margin-bottom: 20px;
  }
  .worker-panel {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
    transition: border-color .2s;
  }
  .worker-panel:hover { border-color: var(--accent); }
  .worker-panel.active { border-top: 3px solid var(--green); }
  .worker-panel.idle { border-top: 3px solid var(--border); }

  .wp-header {
    padding: 14px 16px 10px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .wp-name { font-size: 16px; font-weight: 700; }
  .wp-badge {
    padding: 3px 10px; border-radius: 12px;
    font-size: 11px; font-weight: 600;
  }
  .wp-badge.working {
    background: rgba(63,185,80,.15); color: var(--green);
  }
  .wp-badge.idle-b {
    background: rgba(139,148,158,.15); color: var(--text-dim);
  }

  .wp-task {
    padding: 0 16px 14px;
  }
  .wp-task-id {
    font-size: 12px; color: var(--accent); font-weight: 700;
    margin-bottom: 2px;
  }
  .wp-task-title {
    font-size: 14px; font-weight: 600; margin-bottom: 6px;
  }
  .wp-task-desc {
    font-size: 12px; color: var(--text-dim); line-height: 1.7;
    white-space: pre-line;
    max-height: 150px; overflow-y: auto;
    padding: 10px 12px;
    background: var(--surface2);
    border-radius: 6px;
  }
  .wp-task-meta {
    display: flex; gap: 12px; margin-top: 8px;
    font-size: 11px; color: var(--text-dim);
  }
  .wp-task-meta .priority-tag {
    padding: 1px 6px; border-radius: 4px; font-weight: 600;
  }
  .priority-tag.high { background: rgba(248,81,73,.15); color: var(--red); }
  .priority-tag.medium { background: rgba(210,153,34,.15); color: var(--yellow); }
  .priority-tag.critical { background: rgba(248,81,73,.3); color: var(--red); }

  .wp-idle-msg {
    padding: 0 16px 14px;
    font-size: 13px; color: var(--text-dim); font-style: italic;
  }

  /* ── Pending Section ── */
  .pending-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
    margin-bottom: 20px;
  }
  .pending-title {
    font-size: 13px; font-weight: 600; color: var(--yellow);
    margin-bottom: 8px;
  }
  .pending-item {
    font-size: 13px; color: var(--text-dim); padding: 4px 0;
  }
  .pending-item::before {
    content: ''; display: inline-block;
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--yellow); margin-right: 8px; vertical-align: middle;
  }

  /* ── Completed Timeline ── */
  .completed-section {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
  }
  .comp-header {
    padding: 14px 20px;
    font-size: 13px; font-weight: 600; color: var(--text-dim);
    cursor: pointer; user-select: none;
    display: flex; align-items: center; gap: 8px;
    border-bottom: 1px solid var(--border);
  }
  .comp-header:hover { color: var(--text); }
  .comp-arrow {
    transition: transform .2s; font-size: 10px; display: inline-block;
  }
  .comp-arrow.open { transform: rotate(90deg); }
  .comp-body {
    max-height: 400px; overflow-y: auto;
  }
  .comp-body.collapsed { display: none; }
  .comp-item {
    display: flex; align-items: baseline; gap: 12px;
    padding: 8px 20px;
    font-size: 12px;
    border-bottom: 1px solid var(--border);
  }
  .comp-item:last-child { border-bottom: none; }
  .comp-item:hover { background: var(--surface2); }
  .comp-id { color: var(--accent); font-weight: 600; min-width: 72px; }
  .comp-who { color: var(--purple); min-width: 70px; }
  .comp-what { flex: 1; color: var(--text); }
  .comp-when { color: var(--text-dim); white-space: nowrap; }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div class="header">
  <h1>Multi-Agent Dashboard</h1>
  <div class="header-meta">
    <span><span class="pulse"></span> Live</span>
    <span id="last-update"></span>
  </div>
</div>

<div class="container">
  <div class="progress-section" id="progress-section"></div>
  <div class="workers-grid" id="workers-grid"></div>
  <div id="pending-section"></div>
  <div class="completed-section">
    <div class="comp-header" onclick="toggleComp()">
      <span class="comp-arrow" id="comp-arrow">&#9654;</span>
      <span>Completed (<span id="comp-count">0</span>)</span>
    </div>
    <div class="comp-body collapsed" id="comp-body"></div>
  </div>
</div>

<script>
const REFRESH_MS = 5000;

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function toggleComp() {
  document.getElementById('comp-body').classList.toggle('collapsed');
  document.getElementById('comp-arrow').classList.toggle('open');
}

/* Find YAML task detail for a worker */
function findTaskForWorker(workerName, queueTasks) {
  const key = workerName.toLowerCase().replace(/\s+/g, '');
  if (queueTasks[key]) return queueTasks[key];
  for (const k of Object.keys(queueTasks)) {
    if (queueTasks[k].assigned_to === key) return queueTasks[k];
  }
  return null;
}

function renderProgress(data) {
  const d = data.dashboard;
  const done = d.completed_tasks.length;
  const active = d.active_tasks.length;
  const pending = d.pending_tasks.length;
  const total = done + active + pending;
  const pctDone = total ? (done / total * 100) : 0;
  const pctActive = total ? (active / total * 100) : 0;
  const pctPending = total ? (pending / total * 100) : 0;

  document.getElementById('progress-section').innerHTML = `
    <div class="progress-header">
      <div class="progress-title">Overall Progress</div>
      <div class="progress-numbers">
        ${done}<span> / ${total} tasks done</span>
      </div>
    </div>
    <div class="progress-bar-track">
      <div class="progress-bar-done" style="width:${pctDone}%"></div>
      <div class="progress-bar-active" style="width:${pctActive}%"></div>
      <div class="progress-bar-pending" style="width:${pctPending}%"></div>
    </div>
    <div class="progress-legend">
      <span><span class="legend-dot" style="background:var(--green)"></span>Done ${done}</span>
      <span><span class="legend-dot" style="background:var(--accent)"></span>Active ${active}</span>
      <span><span class="legend-dot" style="background:var(--yellow);opacity:.5"></span>Pending ${pending}</span>
    </div>
  `;
}

function renderWorkers(data) {
  const el = document.getElementById('workers-grid');
  const workers = data.dashboard.workers;
  const qTasks = data.queue.tasks || {};
  if (!workers.length) {
    el.innerHTML = '<div style="color:var(--text-dim);padding:20px">No workers</div>';
    return;
  }

  el.innerHTML = workers.map(w => {
    const isActive = w.status.includes('作業');
    const cls = isActive ? 'active' : 'idle';
    const badgeCls = isActive ? 'working' : 'idle-b';
    const badgeText = isActive ? 'Working' : 'Idle';

    let taskHtml = '';
    if (isActive && w.task && w.task !== '-') {
      /* Extract task ID from "TASK-028: title" */
      const taskIdMatch = w.task.match(/TASK-\d+/);
      const taskId = taskIdMatch ? taskIdMatch[0] : '';
      const taskTitle = w.task.replace(/^TASK-\d+:\s*/, '');

      /* Look up YAML details */
      const yaml = findTaskForWorker(w.name, qTasks);
      const desc = yaml ? (yaml.description || '').trim() : '';
      const priority = yaml ? (yaml.priority || '') : '';
      const purpose = yaml && yaml.context ? (yaml.context.purpose || '') : '';

      taskHtml = `
        <div class="wp-task">
          <div class="wp-task-id">${esc(taskId)}</div>
          <div class="wp-task-title">${esc(taskTitle)}</div>
          ${desc ? `<div class="wp-task-desc">${esc(desc)}</div>` : ''}
          <div class="wp-task-meta">
            ${priority ? `<span class="priority-tag ${esc(priority)}">${esc(priority)}</span>` : ''}
            ${purpose ? `<span>${esc(purpose)}</span>` : ''}
          </div>
        </div>`;
    } else {
      taskHtml = '<div class="wp-idle-msg">Waiting for next task...</div>';
    }

    return `
      <div class="worker-panel ${cls}">
        <div class="wp-header">
          <span class="wp-name">${esc(w.name)}</span>
          <span class="wp-badge ${badgeCls}">${badgeText}</span>
        </div>
        ${taskHtml}
      </div>`;
  }).join('');
}

function renderPending(data) {
  const items = data.dashboard.pending_tasks;
  const el = document.getElementById('pending-section');
  if (!items.length) { el.innerHTML = ''; return; }
  el.innerHTML = `
    <div class="pending-section">
      <div class="pending-title">Up Next</div>
      ${items.map(i => `<div class="pending-item">${esc(i)}</div>`).join('')}
    </div>`;
}

function renderCompleted(data) {
  const tasks = data.dashboard.completed_tasks;
  document.getElementById('comp-count').textContent = tasks.length;
  document.getElementById('comp-body').innerHTML = tasks.map(t => `
    <div class="comp-item">
      <span class="comp-id">${esc(t.task_id)}</span>
      <span class="comp-who">${esc(t.assignee)}</span>
      <span class="comp-what">${esc(t.title)}</span>
      <span class="comp-when">${esc(t.completed_at)}</span>
    </div>
  `).join('');
}

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    renderProgress(data);
    renderWorkers(data);
    renderPending(data);
    renderCompleted(data);
    document.getElementById('last-update').textContent =
      new Date(data.timestamp).toLocaleTimeString('ja-JP');
  } catch (e) {
    console.error('Refresh failed:', e);
  }
}

refresh();
setInterval(refresh, REFRESH_MS);
</script>
</body>
</html>
"""

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Multi-Agent Dashboard Web UI')
    parser.add_argument('-p', '--port', type=int, default=5555)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    print(f'Dashboard: http://{args.host}:{args.port}')
    app.run(host=args.host, port=args.port, debug=False)
