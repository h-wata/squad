# ADR 0006: 中隊 (複数 Squad の統率) は HQ session + link-window で作り、新しい階層は作らない

- **Status**: Accepted
- **Date**: 2026-09-10
- **Supersedes**: なし
- **Related**: [ADR 0001](0001-multi-session-isolation-by-project-ownership.md),
  [ADR 0002](0002-squad-cli-wrapper.md)

## Context

ADR 0001 で複数 Squad の並行運用（小隊の連立）は成立した。一方で「今どの Squad が何を
抱えているか」を横断で把握し、複数の Dispatcher に同じ指示を出す手段が無かった。
利用者からの要求は「Squad を tmux のタブで連立させ、先頭を全体管理の pane にしたい」。

現状の実測（2026-09-10）:

- `queue/projects/` に 16 project、`.squad_session` マーカーがあるのは 4 件
  (forward / pochi / rmf / squad)
- 残り 12 project は既定 owner `ros-agents` の担当になるが、その session は起動して
  いない。この 12 project には未配達 report が 5 件滞留していた
- 稼働中の tmux session は `pochi`（Squad）と `1`（利用者の手動セッション）のみ。
  `pochi` は tmux は生きているが watcher が停止しており、report 1 件が滞留
- 横断ビューとして `dashboard.md` が既にあるが、最終更新は 10 日前で、行は
  `…(詳細は該当 dashboards/*.md)` で切られた長文になっており腐っていた

つまり不足しているのは「ダッシュボードという成果物」ではなく、**担当の付いていない
仕掛かりが見えないこと**だった。

選択肢:

- **A. 横断コンソール（人間が中隊長）** — 全 session を 1 画面に出す読み取り専用の
  `muster` と、選んだ Dispatcher に同じ指示を送る `order` を既存 CLI に足す。
  割り当て（`.squad_session`）は引き続き人間が決める
- **B. 中隊長エージェント** — Dispatcher の上位に指揮官エージェントを新設する。
  専用 instructions と通知経路に加え、エージェントが `.squad_session` を書き換え
  られる必要がある。ADR 0001 が「マーカーは人間の手動管理」と割り切って設計した
  排他・競合の前提を作り直すことになり、コストの大半はここ
- **C. session 構成の作り替え** — 1 session 内の複数 window に Squad を並べる。
  利用者の「タブ」要求には最も素直に見えるが、watcher pidfile (`/tmp/<session>-watch.pid`)、
  `notify-worker.sh` の送信先、hook に渡す `SQUAD_SESSION`、`stop.sh` の停止対象が
  すべて session 名を鍵にしているため、ADR 0001 の分離モデル全体の書き直しになる

## Decision

**A を採用し、タブ表示は tmux の `link-window` で実現する**（利用者判断: 割り当ては
人間が持つ）。新しいエージェント階層は作らない。

- `squad hq` — HQ session を作り、各 Squad session の window 0 を `link-window` で
  tab として貼る。window 0 が中隊長のコンソール（shell）、window 1.. が各 Squad。
  link された window は実体が同じなので、Squad 側の session / watcher / hook は無傷。
  HQ を kill しても Squad は生き残り、再実行は貼り直し（冪等）。C の書き直しを
  せずに「タブで連立」を満たすのがこの選択の要点
- `squad muster` — session ごとに tmux / watcher の生死、W1-W4 の状態、担当 project 数、
  未配達 report 数を並べる read-only コマンド。担当 project があるのに session が
  起動していない場合は明示する
- `squad order` — 各 session の Dispatcher (pane 0.0) に既存の `notify-worker.sh`
  経由で送る。送信先は `-s` で明示するのが既定で、全 Squad への送信は `--all` を
  明示したときだけ行う（暗黙のブロードキャストはしない）
- worker 状態は tmux から直接読み、`squad/state/*.json` には書かない。この state は
  session 非依存（`w1.json` が全 session で共有）で、横断コマンドから書くと hook が
  維持している per-worker 状態を壊すため
- 横断ビューの入力は tmux・ledger DB・`.squad_session` マーカーとし、`dashboard.md`
  および `dashboards/*.md` は読まない。腐った Markdown の上に建てると腐りを継承する
- squad session かどうかの判定（利用者の手動 tmux session を除外する条件）は
  `squad.py` 側に実装し、`bin/squad status` は `muster` への転送にした（`bin/squad` の
  `watcher_pid_for` は削除）。なお `stop.sh` は誤爆防止のため独立した watcher 検出を
  持っており、これは今回の統合対象にしていない

## Consequences

- **良い点**:
  - ADR 0001 の分離モデル（project ownership マーカー、session 別の watcher / 停止 /
    通知）に一切手を入れずに横断操作が入る
  - 「担当 session が無い project」「tmux は生きているが watcher が落ちている session」
    「未配達 report の滞留数」が 1 コマンドで出る。導入直後に、担当 session の無い
    12 project と未配達 report 5 件を検出した
  - B は A を否定しない。中隊長エージェントを作る場合でも、その指揮官が読むべき状態は
    `muster` が出しているものと同じなので、A は B の前段として無駄にならない
- **悪い点**:
  - 割り当て（`.squad_session`）は依然として人間の手動管理で、ADR 0001 が挙げた
    「書き忘れると既定 session に流れる」弱点はそのまま残る。`muster` はそれを
    可視化するだけで、直しはしない
  - tab 名を session 名に rename するため、link 元の Squad 側 window 名も変わる。
    Squad session は window 1 枚なので実害は無いが、利用者の別 session に同名の
    window があると bare な `tmux -t <name>` 指定が曖昧になる
  - 未配達 report 数の照合は ledger の `delivery_key` に委譲する。`worker*_review.yaml`
    は schema 上 report_id を持たず `review:<path>:<sha>` がキーになるため、ここを
    自前で導出すると配達済み review を永久に未配達として数える（実際に初版で作り込み、
    Codex のレビューで検出した）。なお archive されずに残った古い report は未配達として
    計上される
  - `order` は Dispatcher の pane を `0.0` 固定で決め打ちしている。`start.sh` の
    pane 構成が変わると追従が要る（worker 側の `notify-worker.sh` と同じ性質の依存）
  - 未知のサブコマンドを `bin/squad` から `squad.py` へ転送するようにしたことで、
    `bin/squad` が唯一の正しい入口になった。`squad/Makefile` の `make install` は
    `squad.py` を直接 `~/.local/bin/squad` に貼っていたため `bin/squad` を迂回し、
    `start` / `stop` / `attach` / `hq` が消えていた。`make install` の貼り先を
    `bin/squad` に変えて解消したが、二重の入口を作れてしまう構造自体は残っている
    (`squad.py` を直接叩けば依然として start/stop は無い)
