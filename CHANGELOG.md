# Changelog

## [Unreleased]

### Added

- `dashboard-updater` サブエージェントを追加し、dashboard 更新の定型作業を
  Dispatcher から委譲可能にした。

### Changed

- Dispatcher pane (Pane 0) の起動モデルを `SQUAD_DISPATCHER_MODEL` 環境変数（デフォルト
  `sonnet`）で指定できるようにし、Dispatcher の token 消費を削減。
- worker report 様式をスリム化: `summary` は 10 行以内、`details` ブロックを廃止し
  必要な場合のみ `details_path` で詳細ファイルを参照する方式に変更
  (`instructions/worker.md`, `instructions/worker-codex.md`, `queue/templates/report.yaml`)。
- Dispatcher のセッション開始時復元を軽量化: `dashboards/<pj>.md` はアクティブタスクが
  ある PJ のみ読み、`search_memory` の `limit` を 30→10 に削減 (`instructions/dispatcher.md`)。
- Dispatcher 起動モデルのデフォルトを sonnet から opus に変更（曖昧指示の明確化を優先する
  ユーザー判断）。
- squad 運用のトークン消費監査 (ORCH-005) の上位提案5件を実装 (ORCH-006):
  `dashboard.md`/`dashboards/<pj>.md` の「更新:」行を直近1件のみ保持し過去履歴を
  `*_history.md` にローテーションする運用を明文化・`dashboard-updater` サブエージェントに
  実装、worker 側の report 保存後の手動 send-keys 通知を廃止して `watch.sh` 自動通知に一本化、
  `report.yaml` の `summary` 10行厳守と超過時の `details_path` 必須化を強化、
  Plan/設計文書の cross-review 提出前 author セルフチェックリスト（時間上限・優先順位・
  計時源・境界演算子統一 + advisor 確認）を追加。
- `task-yaml-author.md` の worktree セットアップ (Step 0) テンプレートに
  codegraph index 構築手順を追記し、以後発行されるタスクの worktree で
  `.codegraph/` が自動的に init されるようにした（CLI が無い/失敗する
  環境では fail-soft でスキップ）。

### Fixed

- fresh clone 実走テスト (SQUAD-011) で見つかった onboarding friction を修正
  (SQUAD-012): Prerequisites に `gh`/`git` を明記、`context/project.md` の
  単一テンプレート運用を README/`instructions/worker.md` に整合、`start.sh` に
  `SQUAD_ENABLE_CODEX`（既定 1）を追加し Pane 6/Codex 起動をスキップ可能に、
  README のコンポーネント表に `task-yaml-author.md`/`verifier.md` と
  `{WORK_DIR}` の定義を追加、`workspace_path` の説明追記と usage 例の非ROS化、
  `watch.sh` の自動/手動起動・停止の説明を一箇所に整理。
- `instructions/worker-codex.md` の kioku-mesh 節が「必読」と書かれ、他の指示書
  (`instructions/worker.md` 等) と異なり未設定環境でのスキップ条件が書かれていなかった
  不整合を修正。設定が無ければスキップしてよい旨を明記。
- `context/project.md` テンプレートに残っていた ROS2/Jazzy 固有の既定値を、squad が
  技術スタック非依存であることに合わせて汎用プレースホルダに置き換え。
- start.sh 内の instructions/*.md プレースホルダ展開 (`{SQUAD_ROOT}` / `{N}`) を
  sed から `scripts/render_prompt.py` (python3 str.replace) に置き換え。クローン先
  パスに `&` `|` `"` 等の特殊文字が含まれても正しく展開されるようになった
  (PR #6 re-review 対応)。
- start.sh が tmux pane に送る send-keys コマンド行自体に埋め込まれる `$SCRIPT_DIR` /
  `$WORKSPACE` / settings ファイルパスも `printf '%q'` でエスケープするよう修正。
  従来は render_prompt.py への引数のみ保護されており、`cd $SCRIPT_DIR` や
  `--add-dir "$WORKSPACE"` 等のコマンド行自体は raw のままだったため、クローン先
  パスに `&` `|` `"` `;` を含むと pane 側シェルが起動コマンドを誤ってパース・分解して
  しまう不具合があった (PR #6 cross-review F2 対応)。
- `watch.sh`: worker が一度停止通報された後に活動再開したことを検知したら
  `STALL_NOTIFIED` をリセットし、同一タスク内で再び停止した際にも再通報できる
  ようにした。従来は task YAML の mtime をキーに「同一タスクにつき生涯 1 回だけ」
  通報する設計だったため、一度復旧した worker が再度停止しても Dispatcher が
  気づけなかった (SQUAD-013)。再通報の解禁には活動再開が
  `WATCH_STALL_RESUME_CYCLES`（既定 2 = 30s）連続したことを条件とし、1 サイクル
  だけの揺れでは解禁されないスパム防止ガードを入れた。あわせて、再開カウント
  (`RESUME_COUNT`) がタスク完了時にリセットされず次タスクへ持ち越されてしまい、
  タスク跨ぎで 1 サイクルの揺れだけで誤って再通報が解禁される不具合を修正
  (PR #12 cross-review 対応)。
