# ADR 0007: 探索グラフは YAML から作り直せる派生索引にし、sync のたびに全件を作り直す

- **Status**: Accepted
- **Date**: 2026-10-21
- **Supersedes**: なし
- **Related**: Issue #55、[ADR 0006](0006-approval-deny-and-wall-clock-budget.md)

## Context

task YAML と report YAML の間のつながり（どの Issue から来た task か、どの report がどの task の
結果か、report の `decision_bearing_claims` がどの結果から出た事実か）は、ファイルを開いて
目で追うしかなかった。task は `tasks/worker{N}.yaml` に上書きされ、旧 task は archive に移り、
report も同じ名前で上書きされる。そのため、セッションをまたいで「この結論はどこから来たか」を
辿れなかった。

参考にした ARTEX (h-wata/ARTEX a9d11cb) の実装:

- ノードを 1 テーブルにまとめ、`kind` を CHECK 制約で固定している
  (`db/schema.sql` の `exploration_nodes`)。
- エッジ `exploration_edges(src_id, rel, dst_id)` の `rel` を `spawns / derived_from / yields /
  proves / covers` に限定している。
- ARTEX ではグラフ（PostgreSQL）が状態の正本で、planner / worker が直接書き込む。

## Decision

1. **正本は YAML のまま。** `queue/.graph.db` (sqlite3) は YAML から作る派生索引で、YAML は
   一切書き換えない。ARTEX と違ってグラフを正本にしないのは、Squad の worker / Dispatcher が
   YAML を読み書きする運用と検査スクリプト (`check_*.py`) が既に回っているため。kioku-mesh
   ADR-0028（Raw Observation が正本、索引は派生ビュー）と同じ考え方。
2. **sync は毎回全件を作り直す。** 差分更新はしない。消えた・移動した・上書きされた YAML を
   追跡する必要がなくなり、DB を消しても `squad graph sync` で同じ内容に戻る。実 queue
   (9 project、YAML 約 750 件) で 1 回 約 1.2 秒（libyaml の `CSafeLoader` 使用時）。
3. **ノードは goal / intent / result / fact の 4 種、エッジは spawns / yields / derived_from の
   3 種に固定する。** node_id は YAML から決定的に決まる（`intent:<project>/<task_id>`、
   `result:<report_id>` など）ので、何度 sync しても重複しない。
4. **report 配達 ledger (`queue/.report_ledger.db`) とは DB ファイルを分ける。** ledger は
   「通知したか」の正本で、壊れると通知が止まる。作り直してよい派生索引と同じファイルにしない。
5. **watchd は report を配達したサイクルの最後に sync する。** 失敗しても（PyYAML が無い、
   YAML が壊れている等）ログだけ残して配達は止めない。
6. **graph だけ PyYAML を使う。** ネストした `context.parent_issue` /
   `decision_bearing_claims` / `derived_from` を読む必要があるため。`squad.py` / `watchd.py` の
   stdlib only は崩さず、`graph` を import するのは `squad graph` サブコマンドと
   `Watcher.sync_graph()` の中だけにする。

## 採用しなかった案

- **差分更新（watchd が配達した report 1 件だけを upsert する）。** 速いが、archive への移動や
  上書きで古いノードが残る。1.2 秒の全件作り直しで足りるうちは採らない。
- **ARTEX の `hint` / `digest` / `covers`（グラフの圧縮）や `proves`（goal の達成判定）。**
  goal の達成は GitHub 側の Issue の状態で、YAML には無い。ノード数が問題になってから検討する。
- **ledger の DB に相乗りする。** 上記 4 の理由で採らない。

## Consequences

- `squad graph why <node_id>` で、fact → result → intent → goal の導出経路を辿れる。
- report が上書きされて残っていない古い task は `unreported` になる。実 queue では intent
  約 380 件のうち 260 件余りがこれで、過去分の追跡には限界がある。
- goal の状態は常に `unknown`（GitHub に問い合わせない）。
- schema を変えるときは `SCHEMA_VERSION` を上げるだけでよい。古い DB は次の sync で作り直される。
