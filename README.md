# squad

tmux 上で Claude Code (複数) + Codex CLI を並行稼働させ、Dispatcher 1体がタスクを
YAML で振り分けて進捗を回すマルチエージェント開発環境。

## クイックスタート

必要なもの: git, tmux, GitHub CLI (`gh`, 認証済み), Claude Code CLI (`claude`), Python 3 + PyYAML。
Worker 4 を使うなら Codex CLI (`codex`) も要る。詳細は [docs/setup.md](docs/setup.md)。

```bash
git clone <this repo> && cd squad
./start.sh <workspace_path>   # tmux session 起動 + watch.sh をバックグラウンド起動
tmux attach -t ros-agents     # 再アタッチ
./stop.sh                     # 全 window 終了 + watch.sh 停止
```

- `workspace_path` は Worker が作業する対象リポジトリ群を置く親ディレクトリ (squad 自体とは別の場所、例: `~/work`)。
- `.claude/settings.local.json` は初回の `./start.sh` が自動生成する。
- セッション名と担当 project は `./start.sh -s <session> -p <project> <workspace_path>` でも指定できる。
- Codex を使わないなら `SQUAD_ENABLE_CODEX=0 ./start.sh ...` で w4 window を作らない。

オプション、環境変数、`watch.sh` の手動起動は [docs/setup.md](docs/setup.md) を参照。

## 構成

tmux はエージェントごとに 1 window (`prefix + w` で一覧)。

```
dispatcher : Dispatcher (Claude)    タスク分配・進捗管理
w1 / w2 / w3: Worker 1-3 (Claude)   実装・調査全般
w4         : Worker 4 (Codex)       設計・cross-review 担当
term       : Terminal               汎用シェル (SQUAD_ENABLE_OPENCODE=1 で Opencode)
aux        : Aux-Shell              SSH 等の汎用利用
```

宛先は `<session>:w1` のように window 名で指す (既定 session は `ros-agents`)。以前の 7 pane 構成で
起動したままのセッションも従来の pane 番号で扱える (後方互換)。詳細は [docs/setup.md](docs/setup.md)。

Dispatcher はコードを書かない。ユーザー指示を受けて `queue/projects/<project>/tasks/worker{N}.yaml`
にタスクを書き、Worker に通知し、`queue/projects/<project>/reports/worker{N}_report.yaml` の
報告を待って dashboard を更新する。

## 主要コマンド

```bash
cd squad && make install     # ~/.local/bin/squad にシンボリックリンク
squad ls                     # 全 worker の状態一覧 (busy/idle/permission_wait/...)
squad assign w1 <task.yaml>  # task YAML を読み notify-worker.sh 経由で通知
squad dashboard              # worker 状態表を Markdown で出力
scripts/ci-watch.sh <PR番号>  # PR の CI 失敗・ハングを検知
SQUAD_WEB_TOKEN=<token> python3 squad/squad.py web   # Web ダッシュボード
```

- 常駐の監視 (report 検知、停止検知、自動承認) は `watch.sh` が担当し、`squad` は単発操作に専念する。
- `squad web` はセッション別の稼働状況、Projects の表、ゴール単位の Graph を見る。既定は読み取り専用で、
  `--allow-write` を付けると Dispatcher に指示を出せる。詳細は [docs/components.md](docs/components.md)。
- ファイルごとの役割一覧も [docs/components.md](docs/components.md)。

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

## ドキュメント

| ファイル | 内容 |
|---|---|
| [docs/setup.md](docs/setup.md) | 前提ツール、初回セットアップ、起動・終了、tmux 構成、`SQUAD_SESSION`、Dispatcher モデル |
| [docs/components.md](docs/components.md) | ファイルごとの役割、`squad` CLI、`squad graph`、`squad web` |
| [docs/ledger.md](docs/ledger.md) | report 配達 ledger (sqlite3) の仕組みと旧形式からの移行 |
| [docs/ci-watch.md](docs/ci-watch.md) | CI 監視 `scripts/ci-watch.sh` の判定、`CI_WATCH_STALL_SECONDS`、既知の制約 |
| [docs/new-project.md](docs/new-project.md) | 新規 project の立ち上げ手順 |
| [docs/ponytail.md](docs/ponytail.md) | Ponytail 連携 (任意) |
| [docs/benchmark.md](docs/benchmark.md) | ベンチマーク |
| [docs/adr/](docs/adr) | 設計判断の記録 (ADR) |
