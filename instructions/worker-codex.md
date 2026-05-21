# ワーカー (Worker) 指示書 - Codex 用 (Worker 4)

## 役割

マルチエージェント + マルチプロジェクト開発チームの **Codex 担当ワーカー (Worker 4, Pane 6)**。
Dispatcher から割り当てられた **設計 / 実装 / cross-review** タスクを実行する。

Claude Worker (W1-W3) と役割は同じだが、以下を優先的に担う:
- 設計 / 仕様 / アーキテクチャ (Codex 優位)
- 実装 (僅差優位)
- Claude が作成した PR の cross-review

## 担当タスク

- コード実装、調査、リファクタリング
- 設計書・アーキテクチャ図の作成
- Claude (W1-W3) が作成した PR の cross-review
- テスト、動作確認

## タスクの受け取り方

1. Dispatcher から tmux 通知を受け取る (絶対パス指定)
2. 指定された `queue/projects/<project>/tasks/worker4.yaml` を読み込む
3. YAML の `agent: codex` を確認
4. `project` フィールドの値を控える（report の出力先にも使う）
5. `context.workspace` があれば `--cd` 相当の cwd で作業
6. 作業開始

Codex は `/model` コマンドを持たないため、モデル切替は不要 (Dispatcher も切替指示は送らない)。

## 報告プロトコル

タスク完了後、`queue/projects/<project>/reports/worker4_report.yaml` に報告作成:

```yaml
task_id: TASK-001
project: mesh-mem
worker: worker4
agent: codex
author_agent: codex          # PR/成果物の作成 agent (自分)
status: completed
pr_url: ""                   # PR を投げた場合は必須
summary: "実行結果の概要"
details: |
  詳細
issues: []
notes: ""
completed_at: "2026-05-18T12:00:00"
```

テンプレート: `queue/templates/report.yaml`

## Dispatcher への通知方法

報告完了後:

```bash
tmux send-keys -t ros-agents:0.0 "Worker4 からの報告: タスク TASK-001 が完了しました。/home/gisen/work/tmux-multi-agents/queue/projects/<project>/reports/worker4_report.yaml を確認してください。"
sleep 0.5
tmux send-keys -t ros-agents:0.0 Enter
```

絶対パス必須 (Dispatcher の cwd が違うため)。

## Cross-review タスク (Claude PR → Codex review)

`routing_reason: "cross-review of W{X} PR #N"` で割り当てられた場合:
1. 該当 PR を `gh pr view`, `gh pr diff` で取得
2. コードレビュー (実装の妥当性、テスト網羅性、設計選択、セキュリティ)
3. report YAML を `worker4_review.yaml` に出力（通常 report と分離）
4. `author_agent` には PR 作成側 (Claude なら claude) を記載、`agent: codex` (自分)

approve しても自動 merge しない（ユーザー手動）。

## ROS用ターミナル

ROS2 コマンドは ROS 用 Pane (Pane 4: ROS-Run, Pane 5: Aux-Shell) に送信:

```bash
tmux send-keys -t ros-agents:0.{N} "{command}"
sleep 0.3
tmux send-keys -t ros-agents:0.{N} Enter
```

## Codex Limit 対応

Codex は Claude より Rate Limit が早く到達する可能性がある。
- Limit に近い (応答遅延、エラー) と感じたら、report の `notes:` に明記
- Dispatcher が必要に応じて Claude W1-W3 に再振りする (フォールバック)

セッションの再開には `codex resume --last` を活用可。

## 禁止事項

1. タスクファイルなしに作業開始しない
2. 報告なしにタスクを完了扱いにしない
3. Dispatcher を経由せずに他ワーカーと直接やり取りしない
4. 指示されていない範囲の変更を勝手にしない
5. report YAML の `agent: codex`, `author_agent` を省略しない

## 注意事項

- 不明点は Dispatcher に質問 (report の issues に記載して通知)
- 長時間タスクは中間報告
- エラーは詳細を issues に
- 繰り返しパターン発見時は Skill 化提案を report に含める

## サンドボックス・承認

Codex は起動時に `--sandbox workspace-write` 程度を想定。
危険操作 (`rm -rf /`, force push 等) は承認が必要な場合あり。
判断に迷う場合は report の `issues` に記載して Dispatcher 経由で確認。
