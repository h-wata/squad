# ADR 0001: 複数 Squad 並行運用は project ownership マーカーで分離する

- **Status**: Accepted
- **Date**: 2026-08-08
- **Supersedes**: なし
- **Related**: なし

## Context

squad は 1 チェックアウト = 1 tmux セッション（既定 `ros-agents`）前提で作られていた。
`SQUAD_SESSION` を変えて 2 つ目の Squad を並行起動すると、次の問題が起きた:

1. **watch.sh の通知が両セッションに届く** — 監視対象 `queue/projects/**` が
   `SCRIPT_DIR` 基準で決まり、セッションで絞られていなかった
2. **Dispatcher/Worker が常に `ros-agents` へ send-keys** — instructions に
   セッション名がハードコードされ、pane 内プロセスに `SQUAD_SESSION` env も
   渡っていなかった（tmux 既存 server は client env を pane に継承しない）
3. **stop.sh が env 未指定時に既定 `ros-agents` を落とし、`pkill -f watch.sh` で
   全セッションの watcher を道連れにする** — 実際に誤爆事故が発生した

選択肢は 2 つあった:

- **A. チェックアウト分離**: セッションごとに git worktree / clone を分ける。
  コード変更ゼロだが、セッション数だけディレクトリが増える
- **B. project ownership マーカー**: 単一チェックアウト・単一 queue のまま、
  project ごとに担当セッションを割り当てて watcher / instructions を絞る

## Decision

**B を採用**（ユーザー判断: worktree 増殖を避けたい）。

- `queue/projects/<pj>/.squad_session` に担当セッション名を 1 行書く。
  マーカーが無い project は `SQUAD_DEFAULT_OWNER`（既定 `ros-agents`）の担当
- watch.sh は毎サイクル担当 project を再計算し、report-bridge / 停止検知 /
  discovery をその project 群に限定する。discovery の seen/inbox も
  セッション別ファイルに分離（既定セッションは従来名を維持し後方互換）
- worktree GC は glob ベースで project に絞れないため、既定セッションの
  watcher のみが実行（重複実行回避。merged+clean しか触らないので安全）
- instructions のセッション名は `{SQUAD_SESSION}` プレースホルダにし、
  start.sh が render_prompt.py で展開 + 各 pane のコマンド行に inline env
  `SQUAD_SESSION=<name>` を埋め込んで hook / notify-worker.sh にも伝搬する
- stop.sh は引数 > env > 既定 の順で対象を決め、watcher はセッション別
  pidfile（`/tmp/<session>-watch.pid`、フォールバックは /proc environ 照合）で
  自セッション分だけ止める。引数も env も無しで複数 watcher 稼働中なら
  誤爆防止でエラー終了する

## Consequences

- **良い点**:
  - worktree / clone を増やさず単一チェックアウトで複数 Squad を並行運用できる
  - queue・dashboard・テンプレートが一元管理のままで、既存の絶対パス運用
    （task YAML 通知など）を変えなくてよい
  - 単一セッション運用は無変更で動く（マーカー無し = 全 project が既定セッション）
- **悪い点**:
  - project → セッションの割当は `.squad_session` マーカーの手動管理が必要で、
    書き忘れると既定セッションに通知が流れる
  - Dispatcher がどの project を担当するかは instructions / 運用で伝える必要が
    あり、watcher の分離だけでは強制されない
  - 同一 project を 2 セッションで同時に扱う構成は想定外（担当は排他）
