# 新規 project の立ち上げ手順

README から移した。`start.sh -p <project>` を使えば手順 1 は自動で行われる。

## タスク YAML の最小形

[README](../README.md) の「タスク YAML の最小形」を参照。詳細なフォーマット・運用ルールは `instructions/dispatcher.md`。

## 新規 project の手動手順

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
