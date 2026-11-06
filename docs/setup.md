# セットアップ、起動、tmux 構成

README から移した、前提ツール、初回セットアップ、起動・終了のオプション、tmux 構成、Dispatcher モデルの変更。

## 前提ツール

- **git**: このリポジトリの clone と worker のブランチ操作に使う。
- **tmux**: 各エージェントの window を管理する。例: `apt install tmux`。公式: https://github.com/tmux/tmux
- **GitHub CLI (`gh`)**: worker の PR 作成・レビューと、`watch.sh` の Issue/PR/CI discovery に必須。
  https://cli.github.com/ 。`gh auth login` で認証しておく。
- **Claude Code CLI (`claude`)**: Dispatcher と Worker 1-3 に必須。https://docs.claude.com/en/docs/claude-code
- **Codex CLI (`codex`)**: Worker 4 を使う場合のみ必須。使わないなら `SQUAD_ENABLE_CODEX=0` で起動して
  w4 window を作らない (下記)。
- **Python 3 + PyYAML**: task / report の検査スクリプト (`scripts/check_*.py`) と `squad graph` が PyYAML を使う
  (`apt install python3-yaml` か `pip install pyyaml`)。それ以外の `squad/squad.py` のサブコマンドは
  標準ライブラリだけで動く。report ledger は Python 標準の `sqlite3` で排他制御するので `flock` は不要
  (Issue #26 で廃止)。

## 初回セットアップ

1. `git clone` する
2. `.claude/settings.local.json.example` は手動コピー不要。`./start.sh` の初回起動時に
   自動生成される（`{SQUAD_ROOT}` プレースホルダは実パスに置換される）。カスタマイズ
   したい場合（例: 追加で参照したい他リポジトリのパスを `additionalDirectories` に
   足したい場合）は生成後の `.claude/settings.local.json` を直接編集すればよい。
3. (任意) `./scripts/link-pi-config.sh` を実行する。pi 経由でローカル LLM を叩く
   スクリプト (`scripts/pi-log-triage.sh` 等) を使う場合のみ必要。`config/pi/models.json.template` の
   `__LLM_HOST__` を埋めて `~/.pi/agent/models.json` に配置する。接続先は
   `LLM_HOST` で渡す (既定 `dell-server01.cs.local`)。

   ```bash
   ./scripts/link-pi-config.sh                        # 既定のホスト
   LLM_HOST=192.168.0.x ./scripts/link-pi-config.sh   # 別環境
   ```

   既存ファイルが違う内容なら退避を促して止まるので、黙って壊すことはない。
   **symlink ではなく生成ファイルなので、テンプレートを更新したら再実行が要る。**
   使わないなら実行しなくてよい。
4. `./start.sh <workspace_path>` で起動。`workspace_path` は Worker 1-3/4 が実際に
   作業する対象リポジトリ（群）を置く親ディレクトリで、squad 自体のディレクトリとは
   別の場所を指す（例: `~/work`）。Worker はこのディレクトリを起点に各タスクの
   `context.workspace` (worktree 等) へ `cd` する。

初期 allowlist (`.claude/settings.local.json.example`) は起動に必要な最小セットです。`git push` や `gh api` 等の追加権限が必要になった場合は、利用者が `.claude/settings.local.json` に明示的に追記してください。

## tmux 構成 (エージェントごとに window)

```
tmux session: ros-agents   (エージェントごとに 1 window。prefix + w で一覧)
  dispatcher : Dispatcher (Claude)    タスク分配・進捗管理
  w1 / w2 / w3: Worker 1-3 (Claude)   実装・調査全般
  w4         : Worker 4 (Codex)       設計・cross-review 担当
  term       : Terminal               汎用シェル (SQUAD_ENABLE_OPENCODE=1 で Opencode)
  aux        : Aux-Shell              SSH 等の汎用利用
```

- 宛先は window 名で `<session>:<window>` の形に指す (例: `ros-agents:w1`)。window の番号は
  base-index で変わるので使わない。`squad/config.json` の `pane` も `w1` 等の window 名。
- `start.sh` は window 名が自動で変わらないよう `automatic-rename` / `allow-rename` を切る。
- `term` は既定で汎用ターミナル。`SQUAD_ENABLE_OPENCODE=1` で Opencode (Flash-Next、既定モデル
  `local/qwen38-flash-next`) として起動する。
- 全員の様子を一度に見るときは `squad web` の稼働状況タブを使う ([components.md](components.md))。
- 以前は 1 つの window を 7 pane に分けていた (#95 で変更)。その構成で起動したままのセッションも、
  `notify-worker.sh` / watcher / hook / `squad ls` / `squad web` / `stop.sh` は従来の pane (`0.1` 等) で
  扱う後方互換がある。新しい構成にするには `./stop.sh <session>` → `./start.sh -s <session> ...` で
  作り直す。

Dispatcher はコードを書かない。ユーザー指示を受けて `queue/projects/<project>/tasks/worker{N}.yaml`
にタスクを書き、Worker に通知し、`queue/projects/<project>/reports/worker{N}_report.yaml` の
報告を待って dashboard を更新する。

## 起動 / 終了

```bash
./start.sh <workspace_path>   # tmux session 起動 + watch.sh をバックグラウンド起動
./stop.sh                     # 全 window 終了 + watch.sh 停止
tmux attach -t ros-agents     # 再アタッチ
```

セッション名と担当 project はオプションでも指定できる (環境変数より覚えやすい形):

```bash
./start.sh -s rmf -p rmf_ws ~/rmf_ws/src   # SQUAD_SESSION=rmf SQUAD_OWNED_PROJECTS=rmf_ws と同義
```

`-s` / `SQUAD_SESSION` とも未指定で端末が対話可能なら、起動時にセッション名を聞く
(空 Enter で既定の `ros-agents`)。同様に、担当 project がどのマーカーにも無い session で
起動した場合は担当 project を聞く (空 Enter でスキップ)。非対話 (CI 等) では従来通り
既定値に落ちるだけで、対話プロンプトは出ない。`-p` に `queue/projects/` 配下に無い
project を指定した場合は、`queue/projects/<project>/{tasks,reports}` を自動で作成する
(手動 `mkdir` を忘れて担当 0 件のまま watcher が idle 起動するのを防ぐため)。

Codex CLI を使わない場合は w4 window (Worker 4/Codex) の起動自体をスキップできる:

```bash
SQUAD_ENABLE_CODEX=0 ./start.sh <workspace_path>   # 既定は 1 (Codex を起動する)
```

この場合 W4 は起動されず、Dispatcher にもその旨が起動時メッセージで通知される
（設計レビュー / cross-review も Claude W1-W3 に振る運用になる）。

## watch.sh の手動起動・停止

`watch.sh`（常駐監視デーモン）は `start.sh` が自動でバックグラウンド起動し、`stop.sh`
が自動で停止する。個別に起動・停止したい場合（`start.sh` を介さない場合等）は:

```bash
./watch.sh &             # 手動起動 (SQUAD_SESSION 環境変数を見る。下の節を参照)
# 手動停止 (watch.sh は squad/watchd.py を exec するラッパなので、稼働中の
# プロセス名は watchd.py になる。移行期間の旧 watch.sh プロセスも一緒に拾う)
pkill -f "$(pwd)/(watch\.sh|squad/watchd\.py)"
```

## tmux session 名を環境変数で指定する

tmux session 名は既定で `ros-agents` だが、`SQUAD_SESSION` 環境変数で変更できる。
同一マシンで複数の squad インスタンスを並行稼働させたい場合などに使う。

```bash
SQUAD_SESSION=myproj ./start.sh <workspace_path>
SQUAD_SESSION=myproj ./watch.sh &        # start.sh 経由でなく手動起動する場合も同様
SQUAD_SESSION=myproj scripts/notify-worker.sh W2 "..."
SQUAD_SESSION=myproj ./stop.sh
tmux attach -t myproj
```

`squad` CLI (`bin/squad` と、そこから呼ぶ `squad/squad.py`) も同じ環境変数を見る。未設定時は従来通り `ros-agents` になり、
`squad/config.json` の宛先 (window 名 w1/w2/w3/w4) も変わらない。

## Dispatcher 起動モデルのカスタマイズ

Dispatcher (dispatcher window) の起動モデルは既定で `opus`（曖昧な指示の明確化・複雑な判断を担うため）。
token 節約は report のスリム化等、他の手段で行う方針。`SQUAD_DISPATCHER_MODEL` 環境変数で
変更できる。

```bash
SQUAD_DISPATCHER_MODEL=sonnet ./start.sh <workspace_path>
```
