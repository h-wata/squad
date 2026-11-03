# ハーネス構成の比較ベンチマーク

ハーネスの変更 (探索グラフ、Trace 検索、kioku-mesh 連携など) が性能に効いたと主張する前に、
この手順で構成どうしを比べる。ADR 0005 で Issue #34 を 3 構成で手作業で比べた手順を一般化したもの。
task を自動で投げる runner は持たない (構成を切り替えるたびに環境を作り直すので、手順書で足りる)。

## 用語

- **suite**: 比べる題材。1 つ以上の Issue と、作業を始める main の commit の組 (例: `issue34`)
- **config**: ハーネス構成のラベル (例: `claude-sonnet` / `claude-qwen` / `opencode-qwen`、
  `baseline` / `graph` / `trace` / `context-compiler`)
- **trial**: 同じ suite × config を流した回数の番号 (1 始まり)

## 手順

1. **suite を決める。** 題材の Issue と、作業を始める commit (`git rev-parse origin/main`) を控える。
   全 config・全 trial でこの commit から worktree を作る。
2. **config を決める。** 比べたい構成に加えて、Qwen3.8-Flash-Next で動く構成
   (`SQUAD_W<N>_AGENT=claude-local`) を必ず 1 つ入れる。1 つの config は 3 trial 以上流す。
3. **task YAML を書く。** config・trial ごとに 1 つ書く。差分は `assigned_to` / `agent` / `model` /
   `context.workspace` / `task_id` / `bench` だけにする。`task_id` は trial ごとに変える
   (例: `BENCH-issue34-sonnet-1`)。`bench` は次の形で、`check_task_yaml.py` が形式を検査する。

   ```yaml
   bench: {suite: "issue34", config: "claude-sonnet", trial: 1}
   ```

4. **流す。** 普段どおり Dispatcher から振る。worker は report に `session_id` を書く
   (同じ cwd の並行セッションを混ぜずに token を測れる)。やり直しが起きたら、上書きされる前の
   report を `reports/archive/` に移しておく (重複作業率は report の件数から数える)。
5. **再起動後の復元を見る trial を決める (任意)。** 途中で Dispatcher に `/clear` を入れ、
   `squad graph status` が未完了の task を全件出せたかを手で判定し、その trial の report の `notes` に
   `restore: ok` か `restore: ng (<出なかった task_id>)` と書く。集計は自動化しない。
6. **計測する。** 各 task の report を受け取ったら、その都度実行する。

   ```bash
   python3 scripts/measure_task_cost.py --project <pj> --task-id <task_id>
   ```

   `bench` は `metrics/task_costs.jsonl` のレコードにそのまま書き写される。
7. **集計する。**

   ```bash
   squad graph sync
   python3 scripts/bench_report.py
   ```

## 出力の読み方

`bench_report.py` は suite × config ごとに 1 行を出す。数値は trial の中央値で、測れなかった値 (unknown) は除く。

| 列 | 意味 |
|---|---|
| trials | 集計した trial 数。同じ task を測り直したときは最後の計測だけを使う |
| 成功 | `status: completed` かつ `verify_status: pass` の trial 数 / trials |
| input_tokens ... cache_creation_tokens | token の中央値 |
| wall_clock_sec | task YAML の発行から report の `completed_at` までの秒数の中央値 |
| attempts | verify の試行回数の中央値 (verdict が無ければ `-`) |
| 重複作業 | 同じ task に report (cross-review を除く) が 2 件以上ある trial 数 / trials。索引が無ければ `-` |

数字を読むときの注意:

- trial が 3 回程度だと差は誤差に埋もれやすい。成功の数と、受け入れ条件の矛盾を報告したかどうか
  (ADR 0005 の表) のような質的な差も一緒に見る
- `attribution: approximate` のレコードは、同じ cwd の並行セッションの token を含みうる
  (`notes` に出る)。比べる trial はなるべく並行させない
- 5h/7d の quota はアカウント全体で共有されるので、構成の比較には使わない
