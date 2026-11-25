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
   (例: `BENCH-issue34-sonnet-1`)。`task_id` は trial ごとの実行 ID を兼ねるので、同じ値を使い回さない。`bench` は次の形で、`check_task_yaml.py` が形式を検査する。

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
- `metrics/task_evals.jsonl` (`scripts/eval_trajectory.py`) の `verify_attempt` / `verify_result` は、watchd が
  attempt ごとに写した `queue/projects/<pj>/verdicts/` から読む。写し始める前 (SQUAD-397 より前) の task は null。
  再試行で pass した trial は `verify_attempt >= 2` かつ `verify_result: pass`

## task 完了時の自動評価 (metrics/task_evals.jsonl)

watchd は worker の report の配達を確定すると、その task を `scripts/eval_trajectory.py` で評価し、
`metrics/task_evals.jsonl` に 1 行追記する (`squad/task_eval.py`、Refs #130 #136)。手で走らせる
`eval_trajectory.py --project <pj> --task-id <id>` もこれまでどおり使え、同じファイルに追記する。

- 評価するのは `workerN_report.yaml` のうち `status` が `completed` か `blocked` のものだけ。
  それ以外 (progress などの途中の report、`failed`) と cross-review の `*_review.yaml` は評価しない
- 重複排除のキーは `report_id`。追記の直前に、ファイルを flock した状態で同じ `report_id` の行があるかを見る。
  watchd の再起動・再配達・複数の watchd が重なっても同じ `report_id` の行は 1 行だけになる。手で走らせた行は
  `report_id` を持たないので、この判定には入らない
- 評価は子プロセスで走り、watchd は終わるのを待たない (同時に 2 件まで)。子は `WATCH_TASK_EVAL_TIMEOUT`
  (既定 300 秒) で自分を打ち切り、`error` に `timeout` を入れた行を書く。子が行を書けずに終わったとき
  (異常終了、起動失敗) と、上限 + 10 秒を過ぎても終わらないとき (kill する) は watchd が `error` 付きの行を書き、
  `watch.log` に `[WARN] task 評価に失敗` を出す。どの場合も report の配達・ledger の更新・通知は先に済んでいる
- 子の実行中に watchd が止まっても、子は自分の時間上限まで走って行を書く。配達の後、子を起動する前に watchd が
  止まった report は評価されない (再起動後に拾い直さない)
- `WATCH_TASK_EVAL=0` で止まる。出力先は `WATCH_TASK_EVALS_FILE` で変えられる (既定は watchd のある checkout の
  `metrics/task_evals.jsonl`)
- このファイルは gitignore している。`metrics/task_costs.jsonl` は計測した人が選んで commit するので追跡しているが、
  こちらは本番の main checkout で watchd が task ごとに追記するので、追跡すると main が常に dirty になる
- watchd に反映するには、merge 後に watchd の再起動が要る

行の schema (評価の出力は平らに並べる):

| キー | 型 | 意味 |
|---|---|---|
| project / task_id / report_id | str | report の値 |
| worker | str | report の `worker` (無ければ report のファイル名の `workerN`) |
| agent | str | report の `agent` (無ければ空文字) |
| status | str | report の `status` (`completed` / `blocked`) |
| evaluated_at | str | 行を作った時刻 (TZ 付き ISO8601) |
| source | str | `watchd` (手で走らせた行には無い) |
| error | str \| null | 評価に失敗した理由。成功なら null。`timeout: ...` / `not_found: ...` (report か transcript が無い) / `exit=<n>: <stderr>` / `spawn: ...` / 例外の traceback |
| tool_calls ... measured_at | | 成功したときだけ。`eval_trajectory.py` の出力そのもの (`tool_calls`, `tool_errors`, `retries`, `verify_ran`, `verify_attempt`, `verify_result`, `memory_calls`, `trace_search_calls`, `writes_outside_workspace`, `deny_pattern_matches`, `denied_samples`, `external_write_attempts`, `external_write_attempt_dups`, `attribution`, `measured_at`) |

`bench_report.py` は `error` のある行を読み飛ばす (失敗した行で前の正常な行を隠さない)。
