# ADR 0011: worker と Dispatcher は brief を書いて /clear し、brief から再開する。区切りは watchd が決める

- **Status**: Accepted
- **Date**: 2026-11-18
- **Supersedes**: なし
- **Related**: #105、#109、#110、[ADR 0009](0009-graph-status-for-dispatcher-restore.md)、
  [ADR 0010](0010-worker-own-claude-config-dir.md)

## Context

#105 の 1 (ctx の取り方) は #109 / #110 で入った。Claude worker と Dispatcher の ctx は
`scripts/statusline.py` が `squad/state/<worker>.json` の `ctx_sources.statusline` に書き、次に
hook (`scripts/hooks/on-event.sh`) が transcript の usage を `ctx_sources.transcript` に書く。
`squad/ctx.py` の `resolve()` が statusline > transcript > pane の順で値を選び、取れなければ `null`
のままにする。

残りは #105 の 2-4 と、完了条件の「導入前後で 1 タスクあたりのトークン数を比べられる」。実装前に
読んだ現状は次のとおり。

- `scripts/notify-worker.sh` は `--clear` を付けたときだけ `/clear` を送る (Opencode は `/new` と
  bootstrap)。Codex (W4) には毎回 `/new` を送り、`--no-new` で外す。`squad assign` も `--clear` を
  そのまま渡すだけ (`squad/squad.py` の `cmd_assign`)。
- watchd (`squad/watchd.py`) が worker に送るのは承認の自動応答 (`auto_answer`) だけ。Dispatcher には
  `notify_dispatcher()` で report 通知や web の指示を送る。web の指示は Dispatcher が待機中
  (`detect_status() == 'idle'`) のときだけ、1 サイクル 1 件にしている (`deliver_instructions`)。
- hook は Stop / Notification / StopFailure で `last_event` を書く。Dispatcher は `SQUAD_WORKER_ID` を
  持たず、`squad/config.json` の `workers` にも無いので、hook は Dispatcher の state を書かない。
- worker の種類 (`claude` / `claude-local` / `opencode`) は start.sh の `WORKER_AGENTS` にしか無い。
  `squad/config.json` は w1-w3 を常に `agent: claude` としている。start.sh が付ける pane タイトル
  (`Worker1 (Claude)` など) は Claude Code が作業内容で上書きする (2026-11-18 時点の
  `✳ Worker1.yaml の新規タスク` など)。
- `instructions/dispatcher.md` の「コンテキスト管理」は「残量 20% を切ったら `/compact`、定期的な
  予防 compact はしない」。`instructions/worker.md` の「コンテキスト管理ルール」は「報告後に
  `/compact`、10% 以下で `/clear`」だが、worker は自分に slash command を打てないので実行できない。
- `inherit-wip` は `~/.claude/skills` にしか無い。ADR 0010 で worker が見る skill は squad repo の
  `.claude/skills/` に限ったので、トークンありの Claude worker からは見えない。
- `scripts/measure_task_cost.py` は report の `session_id` から transcript を 1 つ特定する (`exact`)。
  タスク途中で `/clear` すると session が分かれ、最初の 1 つしか数えない。

## Decision

各値の境界はすべて「以上 (`>=`)」で判定する。経過時間は、state に残して watchd の再起動をまたぐもの
(タスク開始・最後の /clear からの時間) を wall clock (`time.time()` と ISO8601 の時刻)、watchd の中で
完結する待ち (返事待ち、送信後の待ち) を `time.monotonic()` で測る。

### 1. タスク単位の /clear を既定にする

- `notify-worker.sh` は Codex 以外の worker に毎回 `/clear` (Opencode は `/new` と bootstrap) を送る。
  ローカルモデルの worker も対象にする。タスクの切れ目では持ち越す文脈が無いため。
- 外す手段は 1 つ。task YAML の `context.keep_context: true` と、それに対応する
  `notify-worker.sh --keep-context`。`squad assign` は YAML を読んで `--keep-context` を足す。
  Codex には `--keep-context` を `--no-new` と同じに扱う。`--clear` と `--no-new` は互換のため
  受け付けるが、`--clear` は何もしない。
- `keep_context: true` を書くのは、同じ worker に前の文脈が要る続きを振るときだけ (自分の PR の
  review 修正、同じタスクの差し戻しなど)。`check_task_yaml.py` は bool 以外を NG にする。

### 2. タスク途中の区切り (worker)

判定と送信は watchd が行う。`check_workers()` の後に `check_context_breaks()` を足す。対象は
`pending` (task があり report が未着) の w1-w3 のうち、5. の条件を満たすもの。

**区切る条件** (どれか 1 つで成立。同時に成立しても動作は同じなので優先順位は付けない。ログの
`reason` には上から最初に成立したものを書く):

| 条件 | 初期値 | 環境変数 |
|---|---|---|
| ctx が取れていて、使用率がしきい値以上 | 50% | `WATCH_BREAK_CTX_PCT` |
| ctx が不明で、このタスク (または最後の区切り) から Stop が N 回以上 | 8 回 | `WATCH_BREAK_STOPS` |
| ctx が不明で、タスクの `created_at` (または最後の区切り) から M 分以上 | 60 分 | `WATCH_BREAK_MINUTES` |

- 「ctx が取れている」は `ctx.resolve()` の `context_pct` が数値で、`context_at` が 10 分以内のとき。
  それより古い値は不明として扱う。
- Stop の回数は hook が `state/w<n>.json` の `stop_count` (単調増加) に足す。watchd はタスクの開始時と
  区切りの後にその値を `break.stop_base` に控え、差を数える。

**待機中の判定** (すべて満たすとき):

- `detect_status()` が `idle` で、pane の hash が 2 サイクル (既定 30 秒) 続けて変わっていない
- state の `last_event` が `Stop` か `idle_prompt` で、watchd がこの worker に最後に送った時刻より新しい
- 承認プロンプトを保留していない (`approval_held` に無い)

**流れ** (状態は `state/w<n>.json` の `break` に残す):

1. watchd が worker に送る:
   `[BRIEF-REQUEST task_id=<id> reason=<ctx:52% | stops:8 | minutes:60>] コンテキストを区切ります。<brief の絶対パス> を instructions/worker.md の 4 項目で書き直し、書けたら BRIEF-READY とだけ返してください。バックグラウンド処理の完了待ちなど今は区切れないときは BRIEF-DEFER <理由> と返してください。`
2. 10 分以内に brief の mtime が依頼時刻より新しくなり、pane に `BRIEF-READY` が出て待機中に戻ったら、
   `/clear` を送り 1.5 秒待って次を送る:
   `[RESUME task_id=<id>] /clear 後の再開です。<task YAML の絶対パス> と <brief の絶対パス> を読み、brief の「今の意図と次の一手」から続けてください。`
3. `BRIEF-DEFER` が返ったら 30 分は依頼しない。10 分以内にどちらも返らないときも `/clear` は送らず、
   30 分後にやり直す。
4. 区切った後 30 分は次の区切りをしない。1 タスクで 5 回区切ったら以後は区切らず、Dispatcher に
   `[BREAK-LIMIT]` を low で通知する。
5. 途中で task YAML が変わるか report が来たら、その流れは捨てる。

### 3. brief

- パスは `queue/projects/<pj>/briefs/<task_id>.md`。`task_id` に `[\w.-]` 以外の文字があるときは区切り
  の対象にしない (watchd の `ARCHIVE_NAME_UNSAFE_RE` と同じ基準)。queue は gitignore 済みなので
  追跡しない。タスクが終わっても消さない。
- 形式は次に固定する。見出しの文言は変えない。全体で 200 行以内を目安にする。

  ```markdown
  # brief: <task_id>
  - task_yaml: <絶対パス>
  - updated_at: <TZ 付き ISO8601>
  - break_count: <これまでに区切った回数>

  ## 1. タスク文の写し
  task YAML の title / goal / acceptance_criteria と禁止事項を、ほぼそのまま写す。

  ## 2. 今の意図と次の一手
  いま何をしようとしていたか。再開して最初に打つコマンドや開くファイル。

  ## 3. 計画と完了条件
  ### 済んだもの
  ### 残っているもの
  acceptance_criteria の各項目と verify の attempt 回数を、済み / 残りに分けて書く。

  ## 4. 再開の手がかり
  workspace、branch、最新 commit の SHA、未 commit の変更ファイル、PR URL、
  直前に走らせたコマンドと結果、verdict のパス。
  ```

- 再開時、worker は task YAML、brief の順に読み、`git -C <workspace> status -s` と
  `git -C <workspace> log --oneline -3` で 4. と食い違いが無いか確かめる。食い違えば git を正として
  brief を直してから続ける。
- `inherit-wip` は squad repo の `.claude/skills/inherit-wip/` に写し、squad 側を正にする (ADR 0010 の
  5. と同じ扱い)。手順 1 の最初に「`queue/projects/<pj>/briefs/<task_id>.md` があれば先に読む」を足す。

### 4. Dispatcher brief

- パスは `dashboards/<session>-brief.md` (gitignore 済み)。dashboard (`dashboard.md` と
  `dashboards/<pj>.md`) に載る worker 状態・タスク表は写さない。書くのは次の 4 つ。

  ```markdown
  # dispatcher brief: <session>
  - updated_at: <TZ 付き ISO8601>

  ## 1. 人間への確認待ち
  質問、聞いた時刻、選択肢、答えが来たら何をするか。無ければ「- なし」だけを書く。
  ## 2. 約束した次の行動
  誰に何をするか、何が起きたら (どの report が来たら) するか。
  ## 3. 進行中のやりとり
  cross-review の往復、人間と相談中の論点、モデルを切り替えたまま戻していない worker など。
  ## 4. 再開の手がかり
  読むべき dashboards/<pj>.md、最後に処理した report_id、未 ack の通知。
  ```

- Dispatcher は人間に質問して turn を終える前に 1. を書く。答えを受けたら 1. から消す。

**区切る条件** (どれか 1 つで成立。優先順位の扱いは 2. と同じ):

| 条件 | 初期値 | 環境変数 |
|---|---|---|
| ctx が取れていて、使用率がしきい値以上 | 50% | `WATCH_DISPATCHER_BREAK_CTX_PCT` |
| ctx が不明で、最後の /clear から完了報告 (`[REPORT` の配達) を N 件以上 | 10 件 | `WATCH_DISPATCHER_BREAK_REPORTS` |
| ctx が不明で、最後の /clear から M 時間以上 | 4 時間 | `WATCH_DISPATCHER_BREAK_HOURS` |

- 「最後の /clear」は、statusline が書く `session_id` が変わった時刻 (`state/dispatcher.json` の
  `last_clear_at`)。watchd が送ったものも人間が打ったものも同じに数える。statusline に
  `session_id` を残すのはこの ADR で足す。

**割り込まない条件** (どれか 1 つでも当てはまれば、そのサイクルは依頼しない):

- `detect_status()` が `idle` でない、または pane の hash が 2 サイクル続けて同じでない
- このサイクルで既に Dispatcher に送った (`_sent_to_dispatcher`)、または未配達の web 指示がある
- 人間が最後に入力してから 20 分未満 (`WATCH_DISPATCHER_HUMAN_QUIET_MIN`)
- brief の「1. 人間への確認待ち」に「- なし」以外の行がある
- transcript の最後の assistant メッセージが `AskUserQuestion` の tool_use で、tool_result がまだ無い

人間の入力は次で見分ける。hook に `UserPromptSubmit` を足し、on-event.sh は window 名 `dispatcher`
を worker `dispatcher` に解決する (`SQUAD_WORKER_ID` は足さない)。watchd は Dispatcher に送るたびに
`state/dispatcher.json` の `watchd_sent` (直近 20 件) に `{"crc32": <本文の crc32>, "at": <時刻>,
"human_origin": <bool>}` を残す。`human_origin` は `deliver_instructions()` が送る web の指示
(`[squad-web 指示]` で始まる本文) だけ `true`、report 通知など他はすべて `false` にする。

hook は入力ごとに次の順で判定し、最初に当てはまったもので決める。

1. 入力が `[squad-web 指示]` で始まる: crc32 が `watchd_sent` にあっても人間の入力とし、
   `last_human_prompt_at` を書く。
2. crc32 が `watchd_sent` にあり、その記録の `human_origin` が `true`: 人間の入力とし、
   `last_human_prompt_at` を書く。
3. crc32 が `watchd_sent` にあり、その記録の `human_origin` が `false`: watchd の送信とし、
   `last_human_prompt_at` は書かない。
4. それ以外 (crc32 が無い、`watchd_sent` や `human_origin` が無い・読めない・壊れている、入力が読めない):
   照合できないので人間の入力とし、`last_human_prompt_at` を書く (区切らない側)。

**流れ**は 2. と同じで、文面だけ変える:

- 依頼: `[BRIEF-REQUEST dispatcher reason=<ctx:52% | reports:10 | hours:4>] コンテキストを区切ります。dashboards/<session>-brief.md を 4 項目で書き直し、dashboards/<pj>.md も最新にしてから BRIEF-READY とだけ返してください。人間とのやりとりの途中など今は区切れないときは BRIEF-DEFER <理由> と返してください。`
- 再開: `[RESUME dispatcher] /clear 後の再開です。dashboards/<session>-brief.md を読み、instructions/dispatcher.md の「セッション開始時（状態復元）」の 0-5 を行ってから、brief の 2. と 3. を続けてください。`
- `BRIEF-READY` を見てから `/clear` を送るまでの間に watchd が Dispatcher に何か送っていたら、その回は
  `/clear` を送らずにやり直す (送った通知が文脈から消えるため)。`check_context_breaks()` の Dispatcher
  分は `report_bridge()` より前に回し、`/clear` と再開文を送ったサイクルの通知はその後に届ける。

### 5. ローカルモデルの worker には途中の /clear を当てない

- start.sh が起動時に `squad/state/agents-<session>.json` に `{"w1": "claude", "w2": "claude-local", ...}`
  を書く。pane タイトルは Claude Code が上書きするので使わない。
- 途中の区切りを当てるのは、このファイルで `claude` の worker と Dispatcher だけ。ファイルが無い・読めない・
  値が不明・`claude-local`・`opencode` のときは区切らない。Codex (w4) も対象外にする (`/clear` が無く、
  `/new` で区切る仕組みはこの ADR では作らない)。
- 判定は watchd の `check_context_breaks()` の中の 1 か所 (`_break_eligible(n)`) に置く。途中の
  `/clear` を送るのはこの関数を通った経路だけにする。1. のタスク単位の `/clear` は notify-worker.sh 側
  なので、ローカルモデルにも今までどおり当たる。

### 6. 導入前後のトークン比較

- 単位は 1 タスク (project + task_id)。値は transcript の usage の合計で、`input_tokens`、
  `output_tokens`、`cache_read_input_tokens`、`cache_creation_input_tokens` を別々に残し、合計も出す。
  `/clear` の効果は主に cache read に出るので、合計だけにしない。
- `measure_task_cost.py` に `worker_dir` の集計を足す。ADR 0010 で transcript は worker ごとの
  `config/claude-worker/<session>/w<n>/projects/` に分かれたので、そこにある main の `*.jsonl` のうち
  タスクの区間 (task YAML の mtime から report の `completed_at`) に入る assistant メッセージを全部足す。
  `/clear` で session が分かれても漏れない。worker のディレクトリがあればこれを優先し、無ければ従来の
  `exact` / `approximate` を使う。session は project の `.squad_session` から引く。
- `metrics/task_costs.jsonl` のレコードに `total_tokens`、`sessions` (数えた session の数)、`breaks`
  (brief の `break_count`、brief が無ければ 0) を足す。
- 同じレコードに `task_created_at` も足す。値の正は task YAML の `created_at` で、TZ 付き ISO8601 として
  読めたものを UTC に揃えて保存する。`measured_at` や task YAML の mtime では代用しない (実装前に始まり
  実装後に終わったタスクを「後」に数えてしまうため)。`created_at` が無いときは `task_created_at: null` と
  `task_created_at_error: "missing"`、読めない・TZ が無いときは `null` と `"invalid"` を保存し、`notes` に
  元の値を書く。読めたときの `task_created_at_error` は `null`。
- 比較は `measure_task_cost.py --compare --cutover <ISO8601>` で出す。`task_costs.jsonl` を
  project + task_id ごとに最新 1 件へまとめ、`attribution` が `exact` か `worker_dir` のものだけを使う。
  保存した `task_created_at` が cutover より前のものを「前」、cutover 以上のものを「後」とし、model × agent
  ごとに件数、`total_tokens` と `cache_read_tokens` の中央値と平均を Markdown の表で標準出力に出す。
  `task_created_at` が `null` のもの、この項目が無い既存レコードは比較から外し、表の下に理由ごとの件数
  (`missing` / `invalid` / 項目なし (この ADR より前のレコード) / `attribution` が対象外) を出す。cutover には
  実装 PR を merge した時刻を使い、CHANGELOG に書く。

### 7. instructions の書き換え

- `dispatcher.md` の「コンテキスト管理 / Dispatcher 自身」から「残量 20% で `/compact`、予防 compact は
  しない」を消し、次に置き換える: `/compact` は使わない。区切りは watchd の `BRIEF-REQUEST` で行う。
  brief の 4 項目と書く時機 (人間に質問する前、約束をしたとき)、`BRIEF-READY` / `BRIEF-DEFER` の
  返し方、`RESUME` の後は brief の 2. と 3. を人間に聞かずに続けてよいこと (「勝手に再開しない」は
  `RESUME` が無い起動時の規則として残す)。
- `dispatcher.md` の「Worker 管理」と「tmux 通知」から「残量 20% 以下に `/clear` を指示」「80% を
  超えたら `--clear`」を消し、`/clear` が既定になったことと `keep_context` の使いどころを書く。
- `worker.md` の「コンテキスト管理ルール」を置き換える: brief の形式 (3. と同じ見出し)、
  `BRIEF-REQUEST` を受けたら手を止めて brief を書き `BRIEF-READY` だけを返すこと、区切れないとき
  (バックグラウンドの完了待ち、verifier の実行中) は `BRIEF-DEFER` を返すこと、`RESUME` の後の
  読む順と git での突き合わせ。claude-local / opencode には依頼が来ないこと、来たら
  `BRIEF-DEFER local-model` と返すこと。
- `worker-qwen.md` と `worker-codex.md` は変えない。

## 採用しなかった案

- **`/compact` で続ける。** 要約の中身を決められず、タスク文や完了条件が抜けても気付けない。#105 が
  元にした論文でも、効いたのはタスク文の写し・意図と次の一手・計画と完了条件を持つ brief だった。
- **brief を kioku-mesh に保存する。** 別ホストや別 worker からの再開は #105 で「やらない」と決めた。
  入れるかはこの運用を見てから別に決める。brief は queue の下のファイルで足りる。
- **worker や Dispatcher が自分で区切りを判断する。** 自分に `/clear` を打てず、ctx も見えない。今の
  worker.md の「10% 以下で `/clear`」が実行されていないのと同じことになる。
- **watchd が brief を書かせずに `/clear` する。** 再開の材料が無くなる。`BRIEF-READY` と brief の
  mtime の両方を見てから送る。
- **ctx だけで区切る。** statusline は応答前と `/clear` 直後に `null` を返し、hook が入らない環境もある。
  #105 のとおり ctx に頼らない目安を常に持つ。
- **ローカルモデルの判定に `squad/config.json` の `agent` や pane タイトルを使う。** config.json は
  w1-w3 を常に `claude` と書き、pane タイトルは Claude Code が上書きする。どちらも誤って
  ローカルモデルを区切る側に倒れる。
- **Dispatcher に `SQUAD_WORKER_ID=dispatcher` を足して hook に解決させる。** worker 向けの処理が
  Dispatcher を worker として扱いかねない。on-event.sh の window 名の解決に足すほうが影響が狭い。
- **トークン比較を report の `session_ids` (複数) で行う。** `/clear` の後に前の session ID を worker が
  知る手段が無い。worker ごとの transcript ディレクトリと時間の区間で足りる。

## Consequences

- しきい値 (50%、8 回、60 分、10 件、4 時間、人間の入力から 20 分) はどれも勘で決めた初期値。運用で
  直す。最適化は #105 の対象外。
- 1 つのタスクは 1 回の turn で終わることが多いので、worker の途中の区切りが起きるのは、worker が
  タスクの途中で止まったとき (Dispatcher からの追加指示待ち、review 修正の続き、バックグラウンドの
  完了待ちなど) に限られる。turn の最中には割り込まない。
- worker が `BRIEF-DEFER` を返し続けると区切れない。`BREAK-LIMIT` と従来の停止通報・予算通報で
  Dispatcher が気付く前提。
- Dispatcher は人間への質問の答えを待っている間は区切らないので、答えが長く来ないと ctx が満杯まで
  進みうる。そのときは人間が brief を確かめて手で `/clear` する。
- 区切りの文面 (`BRIEF-REQUEST` / `BRIEF-READY` / `BRIEF-DEFER` / `RESUME`) は watchd、worker.md、
  dispatcher.md の 3 か所で揃える必要がある。変えるときは 3 つとも直す。
- `measure_task_cost.py` の `worker_dir` 集計は、同じ worker が区間内に別のタスクを並行していないことを
  前提にする。worker は 1 度に 1 タスクなので成り立つが、区間がずれると前後のタスクの末尾が混ざりうる。
  トークン無しで `~/.claude` を使う worker は従来の集計のままで、`/clear` をまたいだ分が抜ける。

## 追記 (2026-11-19): PR #120 の実装で確定した細則

PR #120 (Dispatcher の区切り) の cross-review と実装報告で、この ADR が定めていなかった扱いが 6 件
見つかった。Decision は変えず、現行実装の挙動を書き留める。

- **`[REPORT` の「配達」の数え方。** 4. の完了報告の件数は、queue への enqueue が成功した時点で
  `state/dispatcher.json` の `report_notified_at` に時刻を足して数える (`_record_report_notified()`)。
  直送に落ちた場合も同じ enqueue の経路を通るので数え方は変わらない。形式不正で通知しない report は数えない。
- **brief が読めないとき。** Dispatcher brief が読めない (`OSError`)、または「## 1. 人間への確認待ち」の
  見出しが無いときは、確認待ちあり (区切らない側) として扱う。brief ファイルが存在しないときだけ
  確認待ち無しとする (`brief_question_pending()`)。
- **`transcript_path` の保存。** 4. の AskUserQuestion の判定のため、hook が state に `transcript_path` を
  書き (`scripts/hooks/on-event.sh`)、watchd がそのパスの transcript を読む。
- **テストの state 隔離。** watchd は Dispatcher に送るたびに `state/dispatcher.json` へ書くので、テストは
  `SQUAD_STATE_DIR` を tmp にする。`tests/conftest.py` の autouse fixture が全テストに設定し、本番の
  `squad/state` を汚さない。
- **Dispatcher の agents ファイルが無いとき。** 5. のとおり `agents-<session>.json` が無い・読めない・
  dict でないときは Dispatcher も区切らない (`_break_eligible()`)。start.sh を通らずに動いている
  session には当てない。
- **hook の Dispatcher 解決 (既知の制約)。** on-event.sh は window 名 `dispatcher` を、session に関係なく
  worker `dispatcher`、つまり `state/dispatcher.json` に解決する。複数の session で動かすと Dispatcher の
  state を共有する。この ADR では変えない。
