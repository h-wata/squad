# Changelog

## [Unreleased]

### Added

- `start.sh -p` / `SQUAD_OWNED_PROJECTS` に `queue/projects/` 配下に無い project を
  指定した場合、警告してスキップするのをやめて `<project>/{tasks,reports}` を自動作成する
  ようにした。手動 `mkdir` 忘れで担当 0 件のまま watcher が idle 起動するのを防ぐ。
  併せて project 名に `/` と先頭ドットを禁止した (`queue/projects` の外を掘る typo 対策)。
- `start.sh` に `SQUAD_OWNED_PROJECTS` (カンマ区切り) を追加。指定した project の
  `.squad_session` マーカーを起動時に自動生成する。未指定の既定動作は変更なし。
  既に別 session を指すマーカーは無警告で奪わない (スキップ + 警告表示) (SQUAD-210)。
- `squad/watchd.py`: 担当 project が 0 件の起動を `warn_missing_markers()` で明示的に
  `[WARN]` ログするようにした (無言の空回りを可視化)。
- report YAML に必須フィールド `report_id` (UUIDv4) と任意の `git_head` を追加。
  `report_id` は report を新規作成するときに一度だけ発番し、修正・再出力・archive からの
  復帰では変えない。`queue/templates/report.yaml` / `instructions/worker.md` /
  `instructions/worker-codex.md` / `instructions/dispatcher.md` に発番ルールと Dispatcher
  側の重複判定手順を追記した (SQUAD-215/216)。
- report 通知に機械可読な識別子を付加:
  `[REPORT project=… worker=… task_id=… report_id=… content_sha256=… git_head=… attempt=N]`。
  Dispatcher は `(project, report_id)` の一致で重複を判定でき、`attempt` で再送かどうかが
  分かる (SQUAD-216)。
- `report_id` が無い / UUID でない / YAML を解釈できない report を握り潰さず、
  `[REPORT-INVALID]` として path・SHA-256・エラー内容付きで通知するようにした。UUID を
  後付けで推測することはしない (SQUAD-216)。
- 通知の永続キュー化 (`squad/notify_queue.py`, `squad notify pull/ack`)。watcher の通知を
  `queue/notifications/<session>/` へ priority (critical/normal/low) 付きで永続化し、
  Pane 0 へは未 ack が閾値を超えたときだけ 1 行サマリを出す (SQUAD-220)。段階導入のため
  既定は**従来どおり Pane 0 直送**で、`WATCH_NOTIFY_QUEUE=1` で opt-in する (flag を外せば
  従来動作へ即 rollback)。cross-review で出た blocking 3 件も併せて対応した (SQUAD-226):
  ① `events.json` が破損・読出し不能なときは「空 queue」と同一視せず、既存ファイルを保持
  したまま enqueue を失敗させて ledger の再試行に戻す。health の `queue_readable` を false
  にし、`[QUEUE-ERROR]` を Pane 0 へ直送する。`notify pull/ack` も 0 件表示ではなく exit 2
  で落とす。② critical だけでなく normal (既定 900s) / low (既定 3600s) も未 ack age で
  昇格させ、Dispatcher が pull を忘れても永久に沈黙しない。③ `instructions/dispatcher.md`
  に pull/ack 規約・具体コマンド・queue 異常時の報告手順を追記した。

### Fixed

- `scripts/check_source_tree_clean.py`: `source_worktree` の allowlist 検証が
  `re.match` + `^...$` だったため、Python `re` の仕様上 `$` が文字列末尾の改行の
  直前にもマッチしてしまい、末尾に改行を1つ付けるだけでシェルメタ文字チェックを
  bypass できていた。`re.fullmatch` に変更して修正 (SQUAD-256, cross-review SQUAD-255)。
- `squad/watchd.py`: `WATCH_NOTIFY_QUEUE` を off に戻す (rollback) と、flag on 時に
  enqueue 済みの未 ack event が `_process_queue_fallbacks()` ごと呼ばれなくなり永久に
  Pane 0 へ届かなくなっていた不具合を修正。age-based fallback は flag の on/off に
  関わらず毎サイクル回すようにした。ただし `write_health()` の呼び出しは
  `notify_queue_enabled` が true、または既に notification ディレクトリが存在する場合
  だけに限定し、queue を一度も opt-in していない既定環境で `health.json` が新規作成
  され続ける副作用は避けた (SQUAD-228, cross-review SQUAD-229/230)。この判定・更新
  (`self.nq.dir.exists()` / `write_health()`) で `PermissionError` 等の I/O 例外が
  発生しても `cycle()` 全体を落とさないようにした。`run()` の主ループは `cycle()` の
  例外を捕捉しないため、ここで拾わないと watcher プロセスごと停止し、直前に実行済みの
  `_process_queue_fallbacks()` を含め以後の通知が恒久的に沈黙し得た
  (SQUAD-231/232, PR #31 re-review blocking)。ただしこの対応は `dir.exists()` /
  `write_health()` の経路だけを保護しており、それより手前で毎サイクル呼ばれる
  `_process_queue_fallbacks()` → `due_fallback()` は未保護のままだった。
- `squad/notify_queue.py`: `NotificationQueue._load_strict()` の `path.exists()` が
  try の外にあり、`queue/notifications/<session>/` 配下が権限で読めない
  (`os.chmod` mode 000 等) と `PermissionError` がそのまま `_process_queue_fallbacks()`
  経由で `cycle()` から伝播し、`run()` が捕捉しないため watcher プロセスごと恒久停止
  していた不具合を修正。`path.exists()` も read と同じ try に入れ、`OSError` 全般を
  `QueueUnreadableError` に正規化した。これにより既存の
  `except QueueUnreadableError` 経路 (`[QUEUE-ERROR]` を Pane 0 へ直送) が正しく効く
  ようになった。あわせて `_send_queue_alert()` の `mark_fallback_sent()` (backoff 状態の
  書込み) も同じ権限障害で失敗しうるため `OSError` を捕捉し、通知そのものは届いた上で
  次サイクルに再送する形にフォールバックするようにした
  (SQUAD-234, W4 re-review 実 filesystem 再現)。SQUAD-232 時点の回帰テストは mock 注入
  のみで実 filesystem の権限遮断を再現できておらず検出できていなかったため、
  `os.chmod` で実際に権限を落とす回帰テストを追加した。health.json 更新失敗の
  `[WARN]` ログは Dispatcher pane へは届かず、通常起動 (`start.sh`) では
  `/tmp/<session>-watch.log` に記録されるのみである点も実態に合わせて明記する。
- `squad/watchd.py`: 担当 project 0 件の警告が `print` のみで Dispatcher pane に
  届いていなかった不具合を修正。`warn_missing_markers()` に `notify_dispatcher()`
  呼び出しを追加した (SQUAD-212)。
- `squad/ledger.py`: `ReportLedger.baseline_seed()` を廃止した。ledger が存在しない
  ことを「新規導入」と「DB 消失・再作成」で区別できないまま delivered 登録していたため、
  DB 消失からの再作成時にまだ配達していない report まで永久に沈黙させる経路になっていた
  (SQUAD-218)。`Watcher.prepare_ledger()` からも呼び出しを削除し、`squad ledger seed`
  CLI サブコマンドも撤去した。導入直後は既存 report が一斉通知されるが、鳴らない経路を
  ゼロにする方針の下ではこれを受け入れる。
- `squad/ledger.py`: `delivery_key()` が返す invalid (`[REPORT-INVALID]`) 用キーに
  `path` を追加した。同一 project 内で report_id 欠落の別 worker が偶然同一内容の
  report を書くと、内容ハッシュだけをキーにしていたため一方だけが delivered となり、
  もう一方が永久に沈黙していた (SQUAD-218)。
- `squad/watchd.py`: `WATCH_NOTIFY_QUEUE` を on → off に戻す rollback で、既に enqueue
  済みの未 ack event の age-based fallback が `cycle()` の flag ガードで止まり、永久に
  Pane 0 へ届かなくなっていた不具合を修正 (PR #29 事後 cross-review, SQUAD-227/228)。
  `_process_queue_fallbacks()` を flag の on/off に関わらず毎サイクル実行するようにし、
  flag off 中も既存 queue の未 ack event が age 閾値超過で従来どおり Pane 0 へ届くように
  した。新規通知が flag off で queue を経由せず直送される既定挙動は変更していない。

### Changed

- report の配達判定を **mtime ベースから `(project, report_id)` ベースへ全面的に置換**
  (SQUAD-215 設計 / SQUAD-216 実装)。mtime は変更時刻であって到着時刻ではなく、mv/cp や
  archive からの復帰で過去の値を保てるため、同一性・順序の代理に使うと report を握り潰す。
  - `squad/ledger.py`: schema を `deliveries` (`(project, report_id)` 主キー、path、
    content_sha256、state、lease、attempt_count、next_attempt_at) へ移行。`claim/commit`
    を ID ベースに置換し、`release()` は再送予定を記録する `fail()` になった。
    `mtime_str` / `mtime_gt` / `seed_delivered()` は配達 API から削除
    (mtime 系は停止検知専用として `squad/watchd.py` へ移動)。
  - `squad/watchd.py`: 担当変更時の seed (`_seed_newly_owned()` / `_owned_initialized` /
    cutoff helper) と mtime による `stale` 分岐・「touch してください」WARN を**全廃**した。
    所有権の検出と report スナップショットを原子的に揃えることはできず、その隙間に書かれた
    report を永久に握り潰す TOCTOU になるため (PR #28 で 2 巡続けて blocking になった箇所)。
    処理済み report の再通知は共有 ledger が抑止し、ledger に無い report は 1 回だけ
    再通知される (永久沈黙より重複を選ぶ)。
  - 送信 / ledger 更新に失敗した report だけを 15秒 → 60秒 → 5分 → 30分 → 以降 30分ごとで
    再送する。**試行回数の上限は設けない** (有限上限は通信不能時に report を永久沈黙させる)。
    `next_attempt_at` / `attempt_count` は sqlite3 に永続化し、watcher 再起動後も維持する。
    DB を扱えない fail-open 時はプロセス内メモリで同じ backoff を適用する。
  - 旧 ledger (`reports` テーブル、または旧タブ区切りテキスト) は **delivered ID へ推測変換
    せず**空の配達表へ移行する。移行完了は log と Dispatcher 通知 (`[LEDGER] …`) で明示し、
    既存 report は最大 1 回だけ再通知される。旧テキストは `.legacy` へ退避する。
  - `squad/squad.py` の `ledger` サブコマンドを `claim/commit/fail` へ更新
    (claim は `<project> <report_id> <path> <sha>` を取る。`seed` は後日 baseline_seed
    廃止に伴い削除、詳細は Fixed 参照)。
- `watch.sh` (794行 bash) を `squad/watchd.py` + `squad/ledger.py` (stdlib only) へ
  全面移植し、`watch.sh` はそれを呼ぶ薄いラッパ (797→15行) に置き換えた (Issue #26)。
  report ledger は旧タブ区切りテキスト + `flock` から sqlite3 (`BEGIN IMMEDIATE`
  トランザクション) へ移行。旧 ledger からの移行は起動時に自動で行われる
  (`ReportLedger.migrate()`、詳細は README「ledger の実体は sqlite3」参照)。
  pidfile・起動コマンド・env (`SQUAD_SESSION` 等)・`discovery.yaml`・
  `.squad_session` マーカーによる運用互換は維持。旧 `tests/test_watch_report_ledger.sh`
  (66ケース) は `tests/test_ledger.py` (67 test) へ、`tests/test_watch_report_bridge.sh`
  相当の挙動確認は `tests/test_watchd.py` へ pytest として移植。`squad/squad.py` に
  `ledger` サブコマンドを追加 (Issue #26 の要求どおり、
  ledger の手動操作・デバッグ用。`watchd.py` 自体は `ReportLedger` をプロセス内で
  直接呼ぶため使わない)。

### Added

- ponytail 連携: `start.sh` が `PONYTAIL_DEFAULT_MODE` をロール別に設定 (Worker 1-3 は
  `full`、Dispatcher は `off`)。ponytail プラグイン導入済み環境では Worker が最小差分
  規範で実装するようになる。未導入環境では無視され従来通り。Worker 4 (Codex) は
  プラグイン機構がないため `instructions/worker-codex.md` に規範を直接記載し、
  cross-review の観点に過剰設計を追加。導入手順は README「Ponytail 連携 (任意)」参照。

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

- `watch.sh`: report の通知済み状態を全 watcher 共有の永続 ledger
  (`queue/.report_ledger`) に移し、PR #21 Codex cross-review の F1 (major) / F3 (minor)
  を根本対応した (Issue #22)。1 report path につき 1 行 `<mtime>\t<path>` を持ち、
  判定と更新を `flock` で直列化する。これにより
  (a) 担当が A→B→A と移っても B が通知済みの report を A が再通知しない、
  (b) `.squad_session` の mtime を「担当切替時刻」の代理に使う必要が無くなった
  (`cp -p` / symlink 置換 / 時計ずれの影響を受けない)、
  (c) watcher 停止中に書かれた report を再起動後に拾える (従来は起動時 baseline で
  握り潰していた)。担当切替時の既読化ヒューリスティック
  (`REPORT_SEEN` / `EVER_OWNED` / marker mtime cutoff) は不要になったため削除。
  記録は `<mtime>\t<lease>\t<path>` の 3 列で、claim (配達権の取得) と
  commit (配達済みの確定) の 2 段階にしている。claim した時点で「通知済み」にすると、
  送信に失敗した report や送信前に watcher が死んだ report が二度と橋渡しされないため、
  claim は期限付きの lease として記録し、送信に成功して初めて配達済みへ確定する。
  送信に失敗したら claim 前の記録に戻す (行ごと削除すると、直前に配達済みだった古い版
  の記録まで失われ、更新前の mtime を掴んでいた別 watcher がその古い版を再通知できて
  しまう)。lease 期限が切れた記録は別の watcher が再び claim できるため、commit や
  ロールバック自体に失敗しても、watcher が再起動しても、project の担当が変わっても、
  通知が永久に失われることはない (期限は `WATCH_LEDGER_LEASE`、既定 60 秒)。ledger にアクセスできない異常時
  (lock file を開けない・書き込めない) も通知する側に倒す。claim は記録済み mtime より
  真に新しい場合のみ成立させ、更新前の mtime を掴んだ watcher が ledger を巻き戻して
  同じ版を二重通知することを防ぐ。
  commit / release は「自分が書いた claim がまだ残っているか」を claim token
  (claim 時に書いた lease 期限値) で照合してから更新する。mtime だけで照合すると、
  A の lease が切れた後に B が同じ mtime を再 claim した状況で、遅れて戻った A が
  B の claim を commit したり release で消したりできてしまう (PR #24 Codex review
  4th round B2)。また lease 期限切れであっても記録より古い mtime は claim させない
  (許すと ledger の mtime と呼び出し側の mtime が食い違い、commit が空振りして
  lease 切れ後に二重通知される。同 B1)。
  claim token は "<lease 期限>:<nonce>" 形式にする。期限値だけでは、新しい mtime の
  claim が既存 lease を待たずに成立する都合で、担当切替の瞬間に 2 つの watcher が
  同じ秒に同じ path を claim すると衝突しうる (PR #24 Claude review #1)。
  mtime は find %T@ の文字列を小数秒込みでそのまま記録・比較する。整数秒に切り捨てると
  同一秒内に書き直された report (in_progress -> blocked 等) が同じ版とみなされて恒久的に
  握り潰される (同 #2)。同一版かは文字列一致、新旧判定のみ数値比較で行う。
  mtime が記録より古い report は引き続き通知しないが (ledger 巻き戻し防止)、
  気づけない抑止にならないよう path ごとに 1 回 WARN をログに出す (同 #8)。
  なお次の 2 つは「握り潰して気づけない」より「重複通知」を選ぶ方針上の割り切りで、
  仕様として残している:
  (a) ledger 生成後に queue/projects へ現れた project (archive からの復元など) の
      既存 report は、ledger に記録が無いため通知される。
  (b) ledger ファイルを削除して作り直すと、その時点で queue にある report は
      すべて通知済みとして再登録される (停止中セッションの未配達 report を含む)。
  6 巡目レビュー (Claude multi-agent) 対応: (i) ledger の書き換えを「既存 ledger を
  読めなければ何も変更しない」helper (`_ledger_rewrite`) に集約した。従来はグループ
  コマンドの終了ステータスが最後の printf に化けるため、ledger を読めない状態で
  claim すると ledger 全体が新規 1 行に置き換わり、全 report の配達済み記録が消えて
  一斉再通知になった (6th #1)。(ii) 新旧判定の `gt` を整数部・小数部分離の比較にした
  (awk の double は約 16 桁で、find %T@ の 20 桁では下位桁が落ちて新しい版を STALE と
  誤判定する。6th #8)。(iii) 中間リビジョンだけが書いていた旧 2 列形式の互換読みを
  削除した (整数秒 mtime のため実際には互換にならず、全件再通知や誤 STALE を招く。
  6th #3。pre-merge リビジョンの watcher を動かしていた場合は ledger を削除して
  作り直すこと)。(iv) discovery / sweep / stall 通報も送信失敗を握り潰さないようにした
  (nudge は PENDING_NUDGE として毎サイクル再送、stall は通報済みマークを送信成功時のみ
  更新。6th #2)。(v) baseline seed の失敗を全経路で WARN ログするようにした (6th #4)。
  (vi) claim が fail-open して ledger に記録が無い場合のログを実態に合わせた (6th #5)。
  (vii) mtime 巻き戻しの WARN は同じ path・同じ mtime が 2 サイクル連続したときだけ
  出す (担当切替時の良性競合で 1 回限りの WARN が消費されるのを防ぐ。6th #6)。
  7 巡目レビュー (Codex) 対応: claim は ledger への記録に成功したときだけ token を返す
  (記録できていない token を返すとログと再送時期が実態と食い違う)、seed の find|awk
  失敗を PIPESTATUS で検査して部分 ledger を正本にしない、保留 nudge がある間は
  discovery を延期して 1 スロットの PENDING_NUDGE を上書きしない、STALE の連続判定を
  サイクル単位の集合入れ替えにして「過去に一度見た」だけで WARN を消費しないようにした。
  ledger ファイルが存在しない場合のみ、監視開始前に `queue/projects` 配下の既存 report を
  すべて通知済みとして一括登録する (担当 project だけを登録すると、後から起動した別
  セッションの watcher が自分の担当 project の過去 report を一斉通知してしまうため)。
  `queue/` は .gitignore 済みで、ledger は commit されない。
  8 巡目レビュー (Codex) 対応: `flock` (util-linux) を必須化した (無ければ起動時に
  エラー終了。ロック無しフォールバックは複数 watcher の read-modify-rename が交錯し、
  配達済み行の消失や二重通知を招くため削除)。ledger の path 照合を awk の `-v` から
  環境変数 (`ENVIRON`) 渡しに変更した (`-v` は値の backslash エスケープをデコードする
  ため、リテラル `\n` 等を含む path で照合が恒久的に外れて毎回再通知される)。タブ・
  改行を含む path はタブ区切り行指向の ledger 形式と両立しないため記録せず、claim
  未記録の WARN 付きで通知する (fail-open)。

### Added

- `dashboard-updater` サブエージェントを追加し、dashboard 更新の定型作業を
  Dispatcher から委譲可能にした。
- GitHub Actions による CI を追加 (Issue #23)。`bash -n` による構文チェック、
  `tests/*.sh` の実行、`git diff --check`、`shellcheck --severity=warning` を push / PR で
  自動実行する。
- `tests/test_watch_report_ledger.sh`: ledger の claim semantics (再通知しない / mtime
  更新で再通知 / 別プロセスの通知済み状態を尊重 / 並行 claim の直列化 / 1 path 1 行) を
  watch.sh 本体から関数を source して検証する。旧 `tests/test_watch_mtime_boundaries.sh`
  は対象の `should_suppress()` が削除されたため置き換え。
- `tests/test_watch_report_bridge.sh`: report-bridge ループ (claim → 送信 → commit /
  ロールバック) の結合テスト。tmux をスタブに差し替えて watch.sh を実プロセスとして
  起動し、送信失敗時に配達済みにしないこと・復旧後に再送すること・二重送信しないことを
  検証する。watcher 2 プロセス + `.squad_session` 切替のシナリオも含み、担当が移っても
  配達済み report を再通知しないこと・新規 report を新担当だけが 1 回通知することを
  ループレベルで検証する (Issue #22 F1)。

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
