# ADR 0002: squad CLI (bin/squad) で起動・停止・状態確認を一元化する

- **Status**: Accepted
- **Date**: 2026-08-08
- **Supersedes**: なし
- **Related**: [ADR 0001](0001-multi-session-isolation-by-project-ownership.md)

## Context

start.sh / stop.sh は squad ディレクトリに cd して実行する前提だった。
複数 Squad 並行運用（ADR 0001）でセッション指定が必須になると、
`SQUAD_SESSION=xxx ./start.sh ~/work/yyy` のような env 前置きの長いコマンドを
毎回 squad ディレクトリで打つことになり、以下の不満・リスクがあった:

- どこからでも操作できず、わざわざ squad ディレクトリに移動する必要がある
- env 前置きを忘れると既定 `ros-agents` に誤爆する（stop.sh で実際に事故）
- 「今どのセッションと watcher が動いているか」を確認する手段が
  tmux list-sessions + ps の手作業しかない

選択肢:

- **A. shell alias / function** — 個人 rc 依存で配布できず、リポジトリで管理できない
- **B. bin/squad ラッパー + symlink 配布** — リポジトリ管理のまま
  `~/.local/bin` に symlink するだけで PATH に乗る
- **C. Python CLI 化 (squad/squad.py に統合)** — 既存 hook 用モジュールとの
  責務混在になり、単純な tmux ラッパーには過剰

## Decision

**B を採用**。`bin/squad` (bash) を追加し、`~/.local/bin/squad` に symlink する。

- サブコマンド: `start <workspace> [-s <session>]` / `stop [<session>]` /
  `status` / `attach [<session>]` / `log [<session>] [-f]` / `root`
- `readlink -f` で symlink を辿って SQUAD_ROOT を解決するため、
  どこに symlink しても実体の start.sh / stop.sh を呼べる
- `-s` オプションは内部で `SQUAD_SESSION` env に変換して start.sh に渡す
  （既存スクリプトのインターフェースは変えない = 直接実行も従来通り動く）
- `status` はセッション別 pidfile (`/tmp/<session>-watch.pid`) と
  /proc environ 照合で tmux セッションと watcher の生存を突き合わせる

## Consequences

- **良い点**:
  - 任意の cwd から `squad start ~/work/xxx -s yyy` で起動でき、
    セッション名がコマンドライン引数として明示される（env 忘れ誤爆の抑止）
  - start.sh / stop.sh は無変更のラッパーなので、既存の運用・ドキュメントと共存
  - `squad status` で並行運用時の全体像（session × watcher）が1コマンドで見える
- **悪い点**:
  - symlink 設置は手動（インストーラは無い）。fresh clone では
    `ln -sf <repo>/bin/squad ~/.local/bin/squad` を1回打つ必要がある
  - bash 依存（Windows 非対応）。ただし squad 自体が tmux 前提なので実害なし
