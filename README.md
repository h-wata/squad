# squad

tmux 上で Claude Code (複数) + Codex CLI を並行稼働させ、Dispatcher 1体がタスクを
YAML で振り分けて進捗を回すマルチエージェント開発環境。

## Prerequisites

- **tmux** — 各エージェントの pane を管理する。ディストリのパッケージマネージャで
  インストール可（例: `apt install tmux`）。公式: https://github.com/tmux/tmux
- **Claude Code CLI (`claude`)** — Dispatcher / Worker 1-3 に必須。
  https://docs.claude.com/en/docs/claude-code
- **Codex CLI (`codex`)** — Worker 4 (Codex 担当) を使う場合のみ必須。使わない場合は
  `start.sh` の Pane 6 起動部分を省略してよい。
- **Python 3** — `squad/squad.py` は標準ライブラリのみで動作し、追加パッケージの
  インストールは不要。

### 初回セットアップ

1. `git clone` する
2. `.claude/settings.local.json.example` は手動コピー不要。`./start.sh` の初回起動時に
   自動生成される（`{SQUAD_ROOT}` プレースホルダは実パスに置換される）。カスタマイズ
   したい場合（例: 追加で参照したい他リポジトリのパスを `additionalDirectories` に
   足したい場合）は生成後の `.claude/settings.local.json` を直接編集すればよい。
3. `./start.sh <workspace_path>` で起動

## 構成

```
tmux session: ros-agents
  Pane 0: Dispatcher (Claude)         — タスク分配・進捗管理
  Pane 1-3: Worker 1-3 (Claude)       — 実装・調査全般
  Pane 4: Terminal                    — 汎用シェル
  Pane 5: Aux-Shell                   — SSH 等の汎用利用
  Pane 6: Worker 4 (Codex)            — 設計・cross-review 担当
```

Dispatcher はコードを書かない。ユーザー指示を受けて `queue/projects/<project>/tasks/worker{N}.yaml`
にタスクを書き、Worker に通知し、`queue/projects/<project>/reports/worker{N}_report.yaml` の
報告を待って dashboard を更新する。

## 起動 / 終了

```bash
./start.sh <workspace_path>   # tmux session 起動 + watch.sh をバックグラウンド起動
./stop.sh                     # 全 pane 終了 + watch.sh 停止
tmux attach -t ros-agents     # 再アタッチ
```

## 主なコンポーネント

| ファイル | 役割 |
|---|---|
| `start.sh` / `stop.sh` | tmux session の起動・終了 |
| `watch.sh` | 常駐監視デーモン。report 検知→Dispatcher 自動通知、承認プロンプト自動応答、停止 worker 検知、Issue/PR/CI の低頻度 discovery、merge 済み worktree の GC |
| `scripts/notify-worker.sh` | Dispatcher → Worker への通知を timing 込みでラップ（`/clear` `/model` `/new` 後の待ち時間を吸収） |
| `scripts/hooks/on-event.sh` | Claude Code hook。Stop/Notification 等のイベントを `squad/state/<worker>.json` に即時反映 |
| `squad/squad.py` | worker 状態確認・タスク割当・dashboard 生成用の軽量 CLI (stdlib only) |
| `instructions/dispatcher.md` / `worker.md` / `worker-codex.md` | 各エージェントの役割定義。Claude には `--append-system-prompt`、Codex (W4) には同等フラグが無いため初期プロンプトとして渡す |
| `queue/projects/<project>/` | PJ 単位のタスク/報告 YAML 置き場 |
| `dashboard.md` / `dashboards/<project>.md` | 全体 index / PJ 別の進捗ダッシュボード |

## squad CLI

```bash
cd squad && make install     # ~/.local/bin/squad にシンボリックリンク
squad ls                     # 全 worker の状態一覧 (busy/idle/permission_wait/...)
squad assign w1 <task.yaml>  # task YAML を読み notify-worker.sh 経由で通知
squad dashboard              # worker 状態表を Markdown で出力
```

daemon 系（report 検知・停止検知・自動承認）は `watch.sh` が担当し、`squad` は
インタラクティブな単発操作（状態確認・割当・dashboard 生成）に専念する。

## タスク YAML の最小形

```yaml
task_id: TASK-001
project: <project>
assigned_to: worker1
agent: claude            # claude | codex
routing_reason: "実装メインのため Claude"
model: sonnet            # Claude のみ
title: "タスクのタイトル"
description: |
  詳細
acceptance_criteria:
  - 完了条件
verify:                  # コード変更タスクは必須
  commands:
    - "pytest tests/ -q"
  expect: "all pass"
```

詳細なフォーマット・運用ルールは `instructions/dispatcher.md` を参照。
