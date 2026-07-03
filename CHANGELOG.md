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

### Fixed

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
