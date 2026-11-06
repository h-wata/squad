# report 配達 ledger

watch.sh が report を Dispatcher へ重複なく配達するための sqlite3 ledger の仕組みと、旧形式からの移行。README から移した。

report を Dispatcher に通知したかどうかは、セッションをまたいで共有する永続 ledger
`queue/.report_ledger.db` (sqlite3, 更新は `BEGIN IMMEDIATE` で直列化) が
**`(project, report_id)`** 単位で管理する。`report_id` は worker が report を新規作成する
ときに一度だけ発番する UUIDv4 で、修正・再出力・archive への移動では変えない。
project の担当セッションが移っても、既に別の watcher が通知した report は再通知されない。
通知は「配達権の取得 (期限付き lease) → 送信成功で配達済みを確定」の 2 段階で、送信に
失敗した report は 15秒 → 60秒 → 5分 → 30分 → 以降 30分ごとで再送される (試行回数の上限は
無い。lease 期限は `WATCH_LEDGER_LEASE`、既定 60 秒)。

mtime は配達判定に使わない。mtime は変更時刻であって到着時刻ではなく、mv/cp や archive
からの復帰で過去の値を保てるため、同一性・順序の代理にすると report を握り潰す。
`report_id` が無い / UUID でない / YAML を解釈できない report は握り潰さず、
`[REPORT-INVALID]` として Dispatcher に通知され、schema 準拠での再出力を促す。

ledger を消したり `queue/projects/<pj>` を後から配置したりすると、その project の既存
report が 1 回だけ再通知される (以後は `report_id` で抑止される)。Dispatcher は通知末尾の
`report_id` が既知のものと一致すれば重複として無視してよい。

## ledger の実体は sqlite3 (`queue/.report_ledger.db`)

`watch.sh` は薄いラッパで、実処理は `squad/watchd.py` (stdlib only) が行う。ledger は
`squad/ledger.py` の `deliveries` テーブル (`(project, report_id)` 主キー、path、
content_sha256、state、lease、attempt_count、next_attempt_at) で、排他は
`BEGIN IMMEDIATE` トランザクションで行う。ファイルパスは `WATCH_LEDGER_FILE` 環境変数で
上書きできる。

移行は自動で、手動操作は不要:

- 旧 schema (`reports` テーブル: path + mtime) や旧タブ区切りテキスト
  (`queue/.report_ledger`) が見つかった場合、**旧行は引き継がず**空の配達表へ移行する。
  旧行は path と mtime しか持たず `report_id` を安全に復元できないため、推測で「配達済み」
  にすると未配達 report を永久に沈黙させ得るからである。移行後は既存 report が最大 1 回だけ
  再通知され、その旨が起動時に log と Dispatcher 通知 (`[LEDGER] ...`) で明示される。
- 旧タブ区切りテキストは `queue/.report_ledger.legacy` へ退避される (参照用)。
- ledger がまだ 1 つも無い場合の一括登録 (baseline seed) は行わない。新規導入・DB 消失・
  再作成のいずれも区別できず、区別せずに登録すると未配達 report を永久に沈黙させ得るため。
  この場合も reports/ にある report はそのまま通常どおり通知される (導入直後は一斉通知に
  なるが、鳴らさないより鳴らす方を優先する)。

手動で確認したい場合は `sqlite3 queue/.report_ledger.db 'select * from deliveries;'`
のように直接クエリできる (スキーマは `squad/ledger.py` 参照)。
