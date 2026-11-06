# 主なコンポーネント、squad CLI、squad web

README から移した、ファイルごとの役割の詳細表、`squad` CLI、`squad web` / `squad graph` の説明。

| ファイル | 役割 |
|---|---|
| `start.sh` / `stop.sh` | tmux session の起動・終了 |
| `watch.sh` | 常駐監視デーモン。report 検知→Dispatcher 自動通知、承認プロンプト自動応答、停止 worker 検知、Issue/PR/CI の低頻度 discovery、merge 済み worktree の GC |
| `scripts/notify-worker.sh` | Dispatcher → Worker への通知を timing 込みでラップ（`/clear` `/model` `/new` 後の待ち時間を吸収） |
| `scripts/ci-watch.sh` | `gh` の JSON だけで対象 PR の CI 失敗・ハングを検知し、失敗時のみ `scripts/pi-log-triage.sh` にログ一次切り分けを依頼して `queue/_inbox.md` に追記する（検知自体に LLM は使わない） |
| `scripts/pi-log-triage.sh` | ログ 1 本を pi (local vLLM) に読ませ、根拠付きの一次切り分け YAML を返す薄いラッパー |
| `scripts/hooks/on-event.sh` | Claude Code hook。Stop/Notification 等のイベントを `squad/state/<worker>.json` に即時反映 |
| `bin/squad` | `squad` コマンドの入口。start / stop / status / attach / log / root は自分で処理し、ls / assign / dashboard / ledger / notify / graph / trace / web は `squad/squad.py` に渡す。`squad status` はセッションと watcher の状態、worker の状態は `squad ls` |
| `squad/squad.py` | worker 状態確認・タスク割当・dashboard 生成用の軽量 CLI (stdlib only) |
| `squad/graph.py` | task / report YAML から探索グラフの派生索引 `queue/.graph.db` を作る (`squad graph sync / why / show / status`)。つなぎ方は下の「squad graph」 |
| `squad/trace_search.py` / `squad/mask.py` | worker の Claude Code transcript を横断検索する `squad trace search / get`。出力は `pi-log-triage.sh` と同じ規則で secret をマスクする |
| `squad/web.py` | 既定は読み取り専用の Web ダッシュボード (`queue/.graph.db` と tmux だけを読む)。詳細は下の「squad web」 |
| `instructions/dispatcher.md` / `worker.md` / `worker-codex.md` | 各エージェントの役割定義。Claude には `--append-system-prompt`、Codex (W4) には同等フラグが無いため初期プロンプトとして渡す |
| `queue/projects/<project>/` | PJ 単位のタスク/報告 YAML 置き場 |
| `dashboard.md` / `dashboards/<project>.md` | 全体 index / PJ 別の進捗ダッシュボード |
| `.claude/agents/dashboard-updater.md` | report 受領後に dashboard.md / dashboards/<pj>.md を更新するサブエージェント (haiku) |
| `.claude/agents/task-yaml-author.md` | Dispatcher が worker への task YAML を生成する際に使うサブエージェント定義 |
| `.claude/agents/verifier.md` | worker の実装完了後、`verify:` ブロックを独立検証するサブエージェント定義 |

`queue/templates/task.yaml` 等に登場する `{WORK_DIR}` プレースホルダは、各リポジトリの
checkout / git worktree を置く親ディレクトリ（`{SQUAD_ROOT}` の親ディレクトリに相当）を指す。
詳細は `.claude/agents/task-yaml-author.md` を参照。

## squad CLI

```bash
cd squad && make install     # ~/.local/bin/squad に bin/squad のシンボリックリンク
squad ls                     # 全 worker の状態一覧 (busy/idle/permission_wait/...)
squad assign w1 <task.yaml>  # task YAML を読み notify-worker.sh 経由で通知
squad dashboard              # worker 状態表を Markdown で出力
```

daemon 系（report 検知・停止検知・自動承認）は `watch.sh` が担当し、`squad` は
インタラクティブな単発操作（状態確認・割当・dashboard 生成）に専念する。
宛先は `<session>:w1` 形式の window 名 ([setup.md](setup.md) の tmux 構成)。

## squad graph

`squad graph sync` は task / report YAML から `queue/.graph.db` (sqlite3, 派生索引) を毎回作り直す。
正本は YAML のままで、DB を消しても sync で戻る。

- ゴールは task の `context.parent_issue` で決まる (#88)。番号、`#N`、Issue の URL、`owner/repo#N` を
  同じ Issue として正規化する (project と同名の repo は project の Issue)。カンマ区切りは最初の Issue。
- `parent_issue` が無い task は、`pr_url`、`task_id` の参照、レビュー元などの手がかりで他の task と
  つなぐ。つないだ task には `derived_from` の辺を張る。Issue が違う task 同士には張らない。
- 状態 (running / blocked / done / verified など) の判定は sync が DB に入れる。`squad graph status` と
  `squad web` は同じ DB の値を読むので判定が一致する。

## squad web

セッション別の稼働状況、Projects の表、ゴール単位の Graph を見るダッシュボード。

```bash
SQUAD_WEB_TOKEN=<token> squad web            # 読み取り専用
SQUAD_WEB_TOKEN=<token> squad web --allow-write   # 指示の投入を許す
```

- LAN の別端末から `http://<host>:8765/?token=<token>` で開く。トークン必須 (未設定なら起動しない)。
  既定は読み取り専用で GET のみ。
- squad セッションは `aux` window があり pane タイトルが `Aux-Shell` のものを選ぶ (旧 7-pane 構成は
  タイトルだけで見分ける)。worker の状態は `squad ls` と同じ判定。
- Graph はゴール単位 (#93)。列は `derived_from` の深さ (親より必ず右)、同じ列の中は発注時刻の順で、
  時間の順に左から右へ並ぶ。task が 1 件だけのゴールはカードで出す。
- `--allow-write` (#85) で活動タブから Dispatcher に指示を出せる。`POST /api/instructions` は
  `queue/instructions/<session>/` に 1 件 1 ファイルで書くだけで tmux には送らない。トークンは
  `Authorization` ヘッダだけ、`Origin` が自分自身でなければ 403、本文は 4000 文字まで。
  watchd が Dispatcher の待機中にだけ 1 件ずつ打ち込み、打ち込む前に `delivered_at` を書く
  (再起動しても二重に送らない)。フラグ無しでは POST は 405 で入力欄も出ない。
