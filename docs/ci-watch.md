# CI 監視 (scripts/ci-watch.sh)

PR の CI 失敗・ハングを検知する単体スクリプトの判定と既知の制約。README から移した。

```bash
scripts/ci-watch.sh <PR番号>     # 対象 PR 1件の最新 CI run を確認
scripts/ci-watch.sh --all-open   # 対象 repo の open PR 全件を確認
```

失敗 (`failure` / `cancelled` / `timed_out`) か、ある job が `CI_WATCH_STALL_SECONDS`
秒（既定 900）を超えて `in_progress` のままハングしているかを `gh` の JSON だけで判定する
（検知に LLM は使わない）。異常を検知した場合のみログを取得し
`scripts/pi-log-triage.sh` に一次切り分けを依頼して、結果を `queue/_inbox.md` に
`- [ ] [CI] PR #<N> <run URL> - <失敗ステップ/ハング中のステップ>` の形式で追記する
（同一 run の重複追記はしない。同一 run に対する複数プロセスの並行実行にも `flock` で
対応済み）。ログ取得や pi 側の失敗（vLLM 停止・タイムアウト等）でも全体は落とさず、
triage 無しの生検知結果を出力して続行する。pi が返す `next_check` / `candidate_causes`
は人間向けの手がかりであり、自動実行はしない。`gh`/`jq` 自体の操作が失敗した場合
（API 障害・認証・rate limit 等）は「CI run がまだ無い」正常系とは区別し、exit 3 で
異常終了する。

`watch.sh` 本体への組み込みは未実施（単体で動作確認するのが現段階のスコープ）。
`CI_WATCH_REPO=owner/repo` で対象 repo を明示できる（squad リポジトリ自身の CI ではなく、
他 repo の PR を監視する運用を想定）。実行環境は Linux 前提（GNU coreutils の `date -d`
と util-linux の `flock` に依存。macOS/BSD 非対応）。

## `CI_WATCH_STALL_SECONDS` の決め方

既定値 900 秒は汎用の目安に過ぎない。プロジェクトごとに実測した「最長の正常な run 時間」
に余裕を加えて設定すること。既定値のままだと、cold cache や依存パッケージの初回
インストールで正常に伸びる run を誤ってハングと判定する（実例: 通常は数分で終わる CI が
apt 経由のパッケージインストールで 734 秒かかった正常なケースがあった）。

```bash
# 例: 通常 5 分、cold build で 12 分程度かかるプロジェクトなら 20 分の余裕を見る
CI_WATCH_STALL_SECONDS=1200 scripts/ci-watch.sh --all-open
```

## 巨大ログの扱いについて（既知の制約）

`gh run view --log` / `--log-failed` で取得したログはそのまま `scripts/pi-log-triage.sh`
に渡す。現時点でログサイズの上限や、大きすぎる場合に失敗行の周辺だけを抽出する戦略は
実装していない。非常に大きい run ログでは pi の呼び出しがタイムアウトする、または
`local-vllm` のコンテキスト長を超える可能性がある。対応が必要になった場合は、
head/tail だけでなく失敗行 (`FAILED` / `Error` 等) の前後を優先的に含める抽出戦略を
別途実装すること。
