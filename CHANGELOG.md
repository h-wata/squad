# Changelog

## [Unreleased]

### Fixed

- `watch.sh`: `.squad_session` マーカー未設定の project を起動時に1回だけ警告するように
  し、無言フォールバックによる通知漏れを可視化。また、マーカー追加等で project の担当
  セッションが切り替わった際、既存の完了済み report を既読扱いで初期化し、過去 report が
  一斉に再通知される問題を修正 (SQUAD-016)。
- `watch.sh`: 上記の既読化ロジックが、マーカー変更とほぼ同時に書かれた正当な新規 report
  や、担当が他セッションへ移り戻ってきた project の未通知 report まで握り潰してしまう
  cross-review 指摘 (PR #21) に対応。既読化カットオフをマーカーファイルの mtime ベース
  に変更し、このセッションが過去に一度でも担当した project は既読化そのものをスキップ
  するようにした。なお、マーカー編集直後に正当な report が書かれてから watcher がまだ
  それを処理していない状態でマーカーが再度編集されるという限定的な条件下では、既読化の
  取りこぼしが理論上残る (詳細は watch.sh 内のコメント参照)。
- `watch.sh`: marker (整数秒) と report (小数秒) の mtime 取得精度が揃っておらず、同一秒
  境界で新規 report を握り潰す不具合を修正 (PR #21 Codex cross-review F2)。report 側の
  mtime を整数秒に切り捨てて marker と同一精度で比較し、同一秒になった場合は既読化せず
  通知する側に倒した。

### Known limitations (既知の制約)

- `watch.sh` の既読化カットオフは `REPORT_SEEN` (プロセスメモリのみ、セッションごとに
  分離) と `.squad_session` の mtime (owner 変更時刻の代理) に依存しており、以下 2 点が
  未解決のまま残っている (PR #21 Codex cross-review F1/F3。根本対応は別 Issue で追跡):
  - **F1 (major)**: 通知済み状態がセッション間で共有されないため、project の担当が
    A→B→A と切り替わると、B が担当中に発生し B が既に通知済みの report を A が復帰時に
    再通知することがある (二重通知)。
  - **F3 (minor)**: `.squad_session` の mtime は owner 変更時刻を正確には表さない
    (`cp -p` や過去 mtime のファイルでの置換、symlink 化して参照先だけ差し替えるケース
    など)。時計ずれや保存された未来 mtime がある場合、正当な report を握り潰す方向にも
    倒れ得る。

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
  `.codegraph/` が未初期化なら `codegraph init -i` で自動的に init、既存 worktree
  再利用時は `codegraph sync` で index を更新するようにした（CLI が無い/失敗する
  環境では fail-soft でスキップ）。
- Dispatcher 指示書を Opus 5 前提にチューニング (`instructions/dispatcher.md`):
  「報告スタイル」節を新設して冗長化・過剰な実況・不要な訂正narrationを抑制、
  サブエージェントを `task-yaml-author` / `dashboard-updater` の 2 つに限定、
  冒頭に「スコープの原則」を追加してタスクの勝手な膨張を抑止、`/compact` を
  「5 タスクごと」から「残量 20% 未満」に変更（1M context で長期一貫性が保たれるため
  予防 compact はむしろ状態を失う）、worker のモデル選択基準を「難しさ」から
  「規模（触るファイル数・横断範囲）」に変更し複数ファイル横断の実装は opus を
  指定するようにした。`task-yaml-author` にも同じ `model:` 選択基準を追記。
  あわせて cross-review (PR #18) 指摘対応として、`task-yaml-author` の返却サマリに
  `model:` 行を必須化し（Dispatcher は生成 YAML を読まないため、書かないと
  `notify-worker.sh --model` に渡す値が伝わらず YAML の宣言と実モデルがズレる）、
  「スコープの原則」に watcher 由来の自動発見候補は対象外である旨を明記した
  （Discovery / Triage の起票ループを止めないため）。

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
