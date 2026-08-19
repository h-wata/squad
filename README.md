# squad

tmux 上で Claude Code (複数) + Codex CLI を並行稼働させ、Dispatcher 1体がタスクを
YAML で振り分けて進捗を回すマルチエージェント開発環境。

## Prerequisites

- **git** — このリポジトリの clone・worker のブランチ操作に必須。
- **tmux** — 各エージェントの pane を管理する。ディストリのパッケージマネージャで
  インストール可（例: `apt install tmux`）。公式: https://github.com/tmux/tmux
- **GitHub CLI (`gh`)** — worker が PR 作成・レビュー・`watch.sh` の Issue/PR/CI
  discovery を行うために必須。https://cli.github.com/ 。`gh auth login` で認証済みで
  あること。
- **Claude Code CLI (`claude`)** — Dispatcher / Worker 1-3 に必須。
  https://docs.claude.com/en/docs/claude-code
- **Codex CLI (`codex`)** — Worker 4 (Codex 担当) を使う場合のみ必須。使わない場合は
  `SQUAD_ENABLE_CODEX=0` を指定して起動すれば Pane 6 (Codex) を起動せずに済む
  （詳細は下記「起動 / 終了」参照）。
- **Python 3** — `squad/squad.py` は標準ライブラリのみで動作し、追加パッケージの
  インストールは不要。
  report ledger は Python 標準の `sqlite3` で排他制御するため、`flock` 等の追加ツールは
  不要（Issue #26 の Python 移植で `flock` 依存は廃止）。

### 初回セットアップ

1. `git clone` する
2. `.claude/settings.local.json.example` は手動コピー不要。`./start.sh` の初回起動時に
   自動生成される（`{SQUAD_ROOT}` プレースホルダは実パスに置換される）。カスタマイズ
   したい場合（例: 追加で参照したい他リポジトリのパスを `additionalDirectories` に
   足したい場合）は生成後の `.claude/settings.local.json` を直接編集すればよい。
3. (任意) `./scripts/link-pi-config.sh` を 1 回実行する。ローカル LLM への委譲
   (`local-coder` agent) を使う場合のみ必要。`config/pi/models.json` を
   `~/.pi/agent/models.json` に symlink する。既存ファイルが違う内容なら退避を促して
   止まるので、黙って壊すことはない。使わないなら実行しなくてよい。
4. `./start.sh <workspace_path>` で起動。`workspace_path` は Worker 1-3/4 が実際に
   作業する対象リポジトリ（群）を置く親ディレクトリで、squad 自体のディレクトリとは
   別の場所を指す（例: `~/work`）。Worker はこのディレクトリを起点に各タスクの
   `context.workspace` (worktree 等) へ `cd` する。

初期 allowlist (`.claude/settings.local.json.example`) は起動に必要な最小セットです。`git push` や `gh api` 等の追加権限が必要になった場合は、利用者が `.claude/settings.local.json` に明示的に追記してください。

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

セッション名と担当 project はオプションでも指定できる (環境変数より覚えやすい形):

```bash
./start.sh -s rmf -p rmf_ws ~/rmf_ws/src   # SQUAD_SESSION=rmf SQUAD_OWNED_PROJECTS=rmf_ws と同義
```

`-s` / `SQUAD_SESSION` とも未指定で端末が対話可能なら、起動時にセッション名を聞く
(空 Enter で既定の `ros-agents`)。同様に、担当 project がどのマーカーにも無い session で
起動した場合は担当 project を聞く (空 Enter でスキップ)。`-p` で指定した project の
ディレクトリが無ければ scaffold ごと作成される。非対話 (CI 等) では従来通り
既定値に落ちるだけで、対話プロンプトは出ない。

Codex CLI を使わない場合は Pane 6 (Worker 4/Codex) の起動自体をスキップできる:

```bash
SQUAD_ENABLE_CODEX=0 ./start.sh <workspace_path>   # 既定は 1 (Codex を起動する)
```

この場合 W4 (Worker 4/Codex) は起動されず、Dispatcher にもその旨が起動時メッセージで
通知される（設計レビュー / cross-review も Claude W1-W3 に振る運用になる）。

`watch.sh`（常駐監視デーモン）は `start.sh` が自動でバックグラウンド起動し、`stop.sh`
が自動で停止する。個別に起動・停止したい場合（`start.sh` を介さない場合等）は:

```bash
./watch.sh &             # 手動起動 (SQUAD_SESSION 環境変数を見る。詳細は次節)
# 手動停止 (watch.sh は squad/watchd.py を exec するラッパなので、稼働中の
# プロセス名は watchd.py になる。移行期間の旧 watch.sh プロセスも一緒に拾う)
pkill -f "$(pwd)/(watch\.sh|squad/watchd\.py)"
```

## tmux session 名のカスタマイズ

tmux session 名は既定で `ros-agents` だが、`SQUAD_SESSION` 環境変数で変更できる。
同一マシンで複数の squad インスタンスを並行稼働させたい場合などに使う。

```bash
SQUAD_SESSION=myproj ./start.sh <workspace_path>
SQUAD_SESSION=myproj ./watch.sh &        # start.sh 経由でなく手動起動する場合も同様
SQUAD_SESSION=myproj scripts/notify-worker.sh W2 "..."
SQUAD_SESSION=myproj ./stop.sh
tmux attach -t myproj
```

`squad` CLI (`squad/squad.py`) も同じ環境変数を見る。未設定時は従来通り `ros-agents` になり、
`squad/config.json` の pane 番号 (0.1/0.2/0.3/0.6) も変わらない。

report を Dispatcher に通知したかどうかは、セッションをまたいで共有する永続 ledger
`queue/.report_ledger.db` (sqlite3, 更新は `BEGIN IMMEDIATE` で直列化) が
**`(project, report_id)`** 単位で管理する。`report_id` は worker が report を新規作成する
ときに一度だけ発番する UUIDv4 で、修正・再出力・archive への移動では変えない。
project の担当セッションが移っても、既に別の watcher が通知した report は再通知されない。
通知は「配達権の取得 (期限付き lease) → 送信成功で配達済みを確定」の 2 段階で、送信に
失敗した report は 15秒 → 60秒 → 5分 → 30分 → 以降 30分ごとで再送される (試行回数の上限は
無い。lease 期限は `WATCH_LEDGER_LEASE`、既定 60 秒)。

mtime は配達判定に使わない。mtime は変更時刻であって到着時刻ではなく、mv/cp や archive
からの復帰で過去の値を保てるため、同一性・順序の代理にすると report を握り潰す。
`report_id` が無い / UUID でない / YAML を解釈できない report は握り潰さず、
`[REPORT-INVALID]` として Dispatcher に通知され、schema 準拠での再出力を促す。

ledger を消したり `queue/projects/<pj>` を後から配置したりすると、その project の既存
report が 1 回だけ再通知される (以後は `report_id` で抑止される)。Dispatcher は通知末尾の
`report_id` が既知のものと一致すれば重複として無視してよい。

### ledger の実体は sqlite3 (`queue/.report_ledger.db`)

`watch.sh` は薄いラッパで、実処理は `squad/watchd.py` (stdlib only) が行う。ledger は
`squad/ledger.py` の `deliveries` テーブル (`(project, report_id)` 主キー、path、
content_sha256、state、lease、attempt_count、next_attempt_at) で、排他は
`BEGIN IMMEDIATE` トランザクションで行う。ファイルパスは `WATCH_LEDGER_FILE` 環境変数で
上書きできる。

移行は自動で、手動操作は不要:

- 旧 schema (`reports` テーブル: path + mtime) や旧タブ区切りテキスト
  (`queue/.report_ledger`) が見つかった場合、**旧行は引き継がず**空の配達表へ移行する。
  旧行は path と mtime しか持たず `report_id` を安全に復元できないため、推測で「配達済み」
  にすると未配達 report を永久に沈黙させ得るからである。移行後は既存 report が最大 1 回だけ
  再通知され、その旨が起動時に log と Dispatcher 通知 (`[LEDGER] ...`) で明示される。
- 旧タブ区切りテキストは `queue/.report_ledger.legacy` へ退避される (参照用)。
- ledger がまだ 1 つも無い場合の一括登録 (baseline seed) は行わない。新規導入・DB 消失・
  再作成のいずれも区別できず、区別せずに登録すると未配達 report を永久に沈黙させ得るため。
  この場合も reports/ にある report はそのまま通常どおり通知される (導入直後は一斉通知に
  なるが、鳴らさないより鳴らす方を優先する)。

手動で確認したい場合は `sqlite3 queue/.report_ledger.db 'select * from deliveries;'`
のように直接クエリできる (スキーマは `squad/ledger.py` 参照)。

## Dispatcher 起動モデルのカスタマイズ

Dispatcher (Pane 0) の起動モデルは既定で `opus`（曖昧な指示の明確化・複雑な判断を担うため）。
token 節約は report のスリム化等、他の手段で行う方針。`SQUAD_DISPATCHER_MODEL` 環境変数で
変更できる。

```bash
SQUAD_DISPATCHER_MODEL=sonnet ./start.sh <workspace_path>
```

## Ponytail 連携 (任意)

[ponytail](https://github.com/DietrichGebert/ponytail) は AI エージェントに「怠惰な
シニア開発者」規範 (YAGNI、stdlib 優先、最小差分) を注入するプラグイン。squad は
ロール別に組み込み済みで、プラグインを導入するだけで有効になる。未導入なら何も
起きない (環境変数が無視されるだけ)。

導入 (一度だけ、claude 内で):

```
/plugin marketplace add DietrichGebert/ponytail
/plugin install ponytail@ponytail
```

ロール別の挙動 (`start.sh` が `PONYTAIL_DEFAULT_MODE` で制御):

- Worker 1-3 (Claude): `full` — ladder (YAGNI → 既存コード → stdlib → native → 1行) を
  適用して実装する。レベルを変えたい場合は `start.sh` の該当行を編集 (lite/full/ultra)
- Dispatcher: `off` — コードを書かないためルールセット注入はコンテキストの無駄
- Worker 4 (Codex): プラグイン機構がないため `instructions/worker-codex.md` に規範を直接
  記載済み。cross-review でも過剰設計を指摘する

補足: ponytail のモードフラグ (`~/.claude/.ponytail-active`) は Claude セッション間で
共有されるが、影響するのはサブエージェント注入とステータスライン表示のみ。各 pane の
メインルールセットは起動時に環境変数から個別に決まる。なお `off` の Dispatcher pane で
`/clear` すると SessionStart が再発火してこのフラグが削除され、Worker 側のサブエージェント
注入も Worker 側で次に SessionStart (起動 / `/clear`) が走るまで止まる (メインルール
セットは影響を受けない)。Worker が意図的簡略化に残す
`ponytail:` コメントは `/ponytail-debt` で台帳化できる。

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
| `.claude/agents/dashboard-updater.md` | report 受領後に dashboard.md / dashboards/<pj>.md を更新するサブエージェント (haiku) |
| `.claude/agents/task-yaml-author.md` | Dispatcher が worker への task YAML を生成する際に使うサブエージェント定義 |
| `.claude/agents/verifier.md` | worker の実装完了後、`verify:` ブロックを独立検証するサブエージェント定義 |

`queue/templates/task.yaml` 等に登場する `{WORK_DIR}` プレースホルダは、各リポジトリの
checkout / git worktree を置く親ディレクトリ（`{SQUAD_ROOT}` の親ディレクトリに相当）を指す。
詳細は `.claude/agents/task-yaml-author.md` を参照。

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

## 新規プロジェクトの立ち上げ手順

新しい PJ を squad に追加するときの最小手順。`queue/templates/` の各ファイルをコピーして使う。

```bash
# 1. PJ 用ディレクトリを作成
mkdir -p queue/projects/<project>/{tasks,reports}

# 2. task テンプレートを最初の worker 用にコピー (report.yaml は worker が完了時に自分で作るのでコピー不要)
cp queue/templates/task.yaml queue/projects/<project>/tasks/worker1.yaml
# 中身を編集して project / agent / routing_reason / verify 等を実タスクに合わせる

# 3. PJ 別ダッシュボードを作成
cp dashboards/_template.md dashboards/<project>.md
# PJ 概要・Active タスクを埋める

# 4. 全体ダッシュボード (index) にエントリを追記
# dashboard.md の「アクティブ Project」テーブルに <project> の行を1行追加する

# 5. (任意) Issue/PR/CI/TODO の自動発見を有効にする場合
cp queue/templates/discovery.yaml queue/projects/<project>/discovery.yaml
# repo / gh_repo 等を実値に書き換える
```

cross-review が必要になったら `queue/templates/review.yaml` を、通常の完了報告には
`queue/templates/report.yaml` を、その都度 `queue/projects/<project>/reports/` にコピーして使う。

`context/project.md` は PJ ごとにコピーするテンプレートではなく、リポジトリ直下に
単一ファイルとして存在する運用ルールメモ。squad 全体（または現在動かしている PJ 群
共通）の技術スタック・コーディング規約・設計決定など、Dispatcher/Worker が前提として
知っておくべき内容を直接編集して書き込む。
