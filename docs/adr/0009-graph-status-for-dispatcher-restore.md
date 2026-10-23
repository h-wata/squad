# ADR 0009: Dispatcher の状態復元は探索グラフの読み取りビュー `squad graph status` から始め、自動再開はしない

- **Status**: Accepted
- **Date**: 2026-10-23
- **Supersedes**: なし
- **Related**: Issue #57、#34、[ADR 0007](0007-exploration-graph-as-derived-index.md)

## Context

Dispatcher は `/clear` やセッションの再起動のあと、`dashboard.md` と `dashboards/<pj>.md` を読んで
仕掛かりを把握していた。dashboard は Dispatcher が手で更新する Markdown で、誤記が繰り返し起きた
（そのため `scripts/check_dashboard_update.py` がある）。実 queue でも、dashboard の Active 表には
2 か月前の task が残っている一方、`tasks/worker{N}.yaml` には別の task が載っていた。

#55 の探索グラフで、task と report の関係は YAML から機械的に引けるようになった。

参考にした ARTEX (h-wata/ARTEX a9d11cb) の実装:

- Planner は毎回新しいセッションで起動し、会話履歴ではなく、グラフから作った `graph_overview` を
  読んで状況を把握する (`agent/planner.go`)。
- カバレッジ（分母に対する tested の割合）は Planner にだけ見せる (`agent/tools.go`)。

## Decision

1. **`squad graph status [--project P] [--format markdown|json]` を足す。** PJ ごとに、goal（Issue）
   別の intent の件数（running / blocked / done / verified / unreported）と、未完了（running /
   blocked）の intent の一覧を出す。各行に node_id と日付（running は task の発行、blocked は
   report）を付け、blocked には最新 report の summary を理由として付ける。
2. **status は毎回 sync してから読む。** watchd は report を配達したときにしか sync しないので、
   発行したばかりの task が running に出ない。sync は実 queue で約 1.2 秒なので、毎回作り直す。
3. **verified は YAML から機械的に決める。** done のうち、最新 report の `verify_status` が
   `pass` で、最新の cross-review の `verdict` が `approve` のもの。cross-review は実装と別の
   task_id で発行されることが多い（実 queue の review report 156 件中 142 件）ので、task_id に加えて
   `pr_url` でも突き合わせる。
4. **出力するだけで、再開・再起票はしない。** `dispatcher.md` の「勝手に再開・再起票はしない」は
   そのまま。状態復元の最初の手順に status を足したが、dashboard は廃止しない。
5. **起票時の重複チェックは既存の検査で足りる。** running の intent は必ず `tasks/` 直下にあり、
   `check_task_yaml.py` の項目 6（`tasks/*.yaml` 間の task_id 重複）がそれを NG にする。
   新しい検査は足さない。
6. **task YAML に任意の `replan_reason` を足し、intent の payload に取り込む。**

## 採用しなかった案

- **dashboard を graph から自動生成する。** dashboard には判断メモや PR の状況など、YAML に無い
  情報が書かれている。status を運用してみて、足りるかどうかを判断する。
- **Issue 本文の「同じ task_id で running の result が既にあるものは割当済み」表示。** Squad では
  task YAML を置いた時点で worker に割り当てられており、未割当の intent が存在しない。
- **番号だけの `parent_issue`（`goal:<pj>#N`）と URL（`goal:<owner>/<repo>#N`）を同じ goal に
  まとめる。** project 名から owner を決められない。表の行が分かれるだけなので、そのままにする。

## Consequences

- 実 queue では、running の中に 2 か月以上前の task が含まれる。report を出さずに外れた task で、
  graph からは見分けられない。日付を出しているので、Dispatcher が判断する。
- blocked は、次の report が来るまで blocked のまま出続ける（人間が別経路で解決した場合も）。
- verified は、report が上書きされて残っていない task では判定できない（unreported になる）。
