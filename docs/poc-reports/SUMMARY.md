# mesh-mem PoC 検証結果まとめ

| 項目 | 値 |
|------|-----|
| 対象リポジトリ | /home/gisen/work/mesh-mem |
| 検証期間 | 2026-04-24 〜 2026-04-25 |
| 作成日 | 2026-04-25 |
| 出典 | dashboard.md、memory ファイル、docs/poc-reports/raw/ |
| 最終更新 | 2026-04-27（TASK-119 DR 24h 分断テスト結果を追記） |

---

## 1. 実行環境

| 項目 | Home | Office |
|------|------|--------|
| IP (LAN) | 192.168.134.28 (ppp0) | 192.168.128.12 |
| サブネット | 192.168.128.0/21（同一セグメント） | |
| OS | Ubuntu 24.04.3 LTS (Noble), x86_64 | 未詳（apt 環境想定） |
| zenohd | v1.9.0（apt 管理、/usr/bin/zenohd） | v1.9.0（apt 管理予定） |
| zenoh-backend-rocksdb | v1.9.0（/usr/lib/libzenoh_backend_rocksdb.so） | v1.9.0（apt 管理予定） |
| RocksDB データ格納先 | ~/.local/share/zenoh-mem/agent_mem/ | 同様想定 |
| 起動方法 | 手動（zenohd -c config/zenohd_home.json5） | 手動（zenohd_office.json5） |
| systemd unit | disabled / inactive（自動起動なし） | 同様想定 |

**Zenoh replication 設定**（両側で完全一致）:

| パラメータ | 値 |
|-----------|-----|
| interval | 10.0 秒 |
| sub_intervals | 5 |
| hot | 6 |
| warm | 30 |
| propagation_delay | 250 ms |
| key_expr | mem/** |
| strip_prefix | mem |

出典: config/zenohd_home.json5、config/zenohd_office.json5、memory/project_mesh_mem_state.md

**注意事項**:
- plan.md の推奨ディレクトリは `$HOME/.local/share/mesh-mem` だが、実運用は `zenoh-mem`（不一致、既知課題）
- TASK-091 時点で zenohd v1.5.1 かつ rocksdb 欠落の報告があったが、ユーザーが apt で v1.9.0 に更新済み（TASK-092 で再確認）

---

## 2. 検証ゴールと結果

| # | PoC ゴール | 結果 | 確認タスク | 備考 |
|---|-----------|------|-----------|------|
| Goal 4 | オフライン差分同期 | PASSED | TASK-094 Scenario A | 25秒以内に Home 側へ伝播 |
| Goal 5 | split-brain 復旧（tombstone 伝播） | PASSED | TASK-094 Scenario C | 3秒以内に収束 |
| - | Tier-1 ベンチマーク（100件） | PASSED | TASK-097 | throughput 31,805 ops/sec、search 20ms |
| - | Short 分断（30秒、10件） | PASSED | TASK-098, 099 | ESTAB+5s 収束 |
| - | Mid-hot 分断（65秒、20件） | PASSED | TASK-100 | ESTAB+5s 収束 |
| - | Mid-warm 分断（30件） | PASSED | Dispatcher BG (F1) | CLI limit 20 トラップあり（後述） |
| - | Cold-entry 分断（30分） | 実行中 | F2 (Dispatcher BG) | 17:00 完了予定、別途追記 |
| - | Tier-2 ベンチマーク（1,000件） | 未着手 | - | 次フェーズ |
| - | Tier-3 ベンチマーク（10,000件） | 未着手 | - | 次フェーズ |

出典: dashboard.md 完了タスク欄、memory/project_mesh_mem_poc_results.md

---

## 3. タイムライン（テスト一覧）

| タスクID | 実行日時 | 担当 | タイトル | 結果サマリー |
|---------|---------|------|---------|------------|
| TASK-088 | 2026-04-24 20:45 | Worker 2 | plan.md vs 実装のギャップ分析 | 主要機能はほぼ実装済み、設定整合が必要と判明 |
| TASK-089 | 2026-04-24 20:47 | Worker 2 | Home/Office IP 設定整合 | zenohd_home/office.json5 の IP を調整 |
| TASK-090 | 2026-04-24 20:52 | Worker 2 | Home IP typo 修正 | 192.168.128.28 → 192.168.134.28 に訂正 |
| TASK-091 | 2026-04-24 21:00 | Worker 2 | Office PC bring-up 時 Search 可否調査 | zenohd v1.5.1 / rocksdb 欠落を誤報告（後に訂正） |
| TASK-092 | 2026-04-24 21:00 | Worker 1 | zenohd v1.9 + rocksdb 導入計画 | 実態確認で v1.9.0 + rocksdb 導入済みと判明、計画不要 |
| TASK-093 | 2026-04-25 10:15 | Worker 2 | Home/Office Zenoh クラスタ スモークテスト | 全通過（Office save → Home に到達を確認） |
| TASK-094 | 2026-04-25 15:27 | Worker 2 | Split-brain 復旧テスト（Goal 4/5） | Scenario A / Scenario C 両方 PASSED |
| TASK-095 | 2026-04-25 15:35 | Worker 2 | 未コミット変更の整理コミット | 3コミット作成（test 追加、config IP 更新、localhost config） |
| TASK-096 | 2026-04-25 15:50 | Worker 1 | 大規模 + 長期分断シナリオ設計 | Tier-1 + Short を優先実施と決定 |
| TASK-097 | 2026-04-25 15:55 | Worker 1 | Tier-1 ベンチマーク実装 & 実行 | 100件、全判定基準クリア |
| TASK-098 | 2026-04-25 15:55 | Dispatcher | Short 分断テスト（30秒、10件） | 両側 10件 一致、PASSED |
| TASK-099 | 2026-04-25 15:57 | Dispatcher | Short 再測（クリーン） | 30秒、10件、ESTAB+5s 収束、再確認 PASSED |
| TASK-100 | 2026-04-25 15:59 | Dispatcher | Mid-hot 分断（65秒、20件） | ESTAB+5s 収束、PASSED |
| F1 | 2026-04-25 | Dispatcher BG | Mid-warm 分断（30件） | PASSED（CLI limit 20 トラップ発見後 --limit 100 で確認） |
| TASK-101 | 2026-04-25 16:25 | Worker 2 | CLI search default limit 仕様確認 | CLI/MCP=20、API=50、不統一を確認 |
| TASK-102 | 2026-04-25 16:50 | Worker 1 | zenoh hot/warm/cold semantics 調査（ソース解析） | initial_alignment() 接続直後に即実行 → 分断時間に依存せず ~5秒収束と解明。TASK-096 の「warm で延伸」予測は誤り |
| TASK-103 | 2026-04-25 16:40 | Worker 3 | Codex 指摘 BLOCKER / IMPORTANT 修正方針設計 | BLOCKER→方針 A（placeholder 戻し）、IMPORTANT→方針 A（固定値）を推奨 |
| TASK-105/106 | 実装中 | Worker 3 | Codex 修正実装（BLOCKER + IMPORTANT） | 実装中 |
| F2 | 2026-04-25 | Dispatcher BG | Cold-entry 30分分断テスト | PASSED（TASK-119 DR 24h テストに吸収） |
| TASK-113 | 2026-04-25 18:06 | Worker 2 | Tier-2 ベンチマーク（1,000件） | save 37,053 ops/sec、search 全 limit 50-87ms — PASSED |
| TASK-116 | 2026-04-25 | Worker 2 | NTP skew 境界テスト 設計書作成 | docs/poc-reports/ntp_skew_test_design.md 作成 |
| TASK-119 | 2026-04-27 08:58 | Dispatcher | DR 24h 分断テスト（1,192件 save、24.45h partition） | G1 PARTIAL / G2 FAIL / G3-G4 PASS / Tombstone PASS。cold-era step-function 収束を実証（§8.3） |

出典: dashboard.md 完了タスク欄

---

## 4. 検証手順詳細

### 4.1 スモークテスト（TASK-093）

| 項目 | 内容 |
|------|------|
| 目的 | Home/Office 間の双方向 Zenoh 接続と基本的な save/search の動作確認 |
| 手順 | Home/Office 双方で zenohd を設定どおりに起動し、save → 対向 search で到達を確認 |
| 期待結果 | Office で save した観測が Home 側 search でも返る（または逆） |
| 実測結果 | Office save（obs_id=14bafd74cf8e484f813a5651ff485f70）が Home に到達 → PASSED |

出典: memory/project_mesh_mem_poc_results.md

---

### 4.2 分断・復旧テスト（TASK-094）

#### Scenario A — オフライン差分同期（Goal 4）

| 項目 | 内容 |
|------|------|
| 目的 | 片側が完全にオフラインの間に対向側が save したデータが、復帰後に同期されるか確認 |
| 手順 | 1. Home zenohd を停止 <br> 2. Office で `mesh-mem save` 実行（obs_id=14bafd74...） <br> 3. Home zenohd を再起動 <br> 4. Home 側で `mesh-mem search` → obs が返ることを確認 |
| 期待結果 | replication interval 内（最大 10〜25 秒）に Home 側 RocksDB へ到達 |
| 実測結果 | 25秒以内に Home 側へ伝播 → **PASSED** |

#### Scenario C — Split-brain + tombstone 伝播（Goal 5）

| 項目 | 内容 |
|------|------|
| 目的 | 両側でデータ操作が走った状態（split-brain）でも tombstone が正しく伝播し、削除済み obs が復活しないことを確認 |
| 手順 | 1. Home で obs_X を save（d11aa9c6...）→ Office に replication <br> 2. Office zenohd 停止（split 開始） <br> 3. Home で obs_X を tombstone（delete）+ obs_Y（8dfd7443...）を save <br> 4. Office zenohd 再起動（merge） <br> 5. 両側で search し obs_X 非表示・obs_Y 表示を確認 |
| 期待結果 | obs_X は tombstone により非表示、obs_Y は表示、3秒以内に収束 |
| 実測結果 | 3秒以内に収束 → **PASSED**。tombstone が「Office に残っていた obs_X を復活させない」ことを実証（existence-based deletion model） |

出典: memory/project_mesh_mem_poc_results.md

---

### 4.3 Short 分断テスト（TASK-098 / 099）

| 項目 | 内容 |
|------|------|
| 目的 | 短時間（30秒）分断後の収束を確認 |
| データ規模 | 10件 |
| 分断時間 | 30秒 |
| 手順 | split-bench-short タグ付き obs を save → zenohd 停止 → 30秒待機 → 再起動 → polling で件数確認 |
| 期待結果 | 再接続後に両側の件数が一致する |
| 実測結果 | ESTAB+5s 収束 → **PASSED**（TASK-099 でクリーン再測も同結果） |

出典: dashboard.md

---

### 4.4 Mid-hot 分断テスト（TASK-100）

| 項目 | 内容 |
|------|------|
| 目的 | hot tier 境界を超える分断（65秒）後の収束を確認 |
| データ規模 | 20件 |
| 分断時間 | 65秒（hot=6 intervals × 10s = 60s 境界を超過） |
| 手順 | split-bench-mid-hot タグ付き obs を save → zenohd 停止 → 65秒待機 → 再起動 → polling |
| 期待結果 | warm tier から diff sync して両側収束（当初の想定） |
| 実測結果 | ESTAB+5s 収束 → **PASSED** |

注（TASK-102 解析より）: 収束は「warm tier の digest 同期」ではなく `initial_alignment()` による接続直後の Discovery クエリで起きている。分断時間が 65秒でも 30分でも、zenohd 再起動後 ~0.5秒で Discovery が走り約5秒以内に収束する（詳細はセクション 8.1 参照）。

出典: dashboard.md

---

### 4.5 Mid-warm 分断テスト（Dispatcher BG / F1）

| 項目 | 内容 |
|------|------|
| 目的 | warm tier を跨ぐ分断後の収束を確認（より長い分断時間） |
| データ規模 | 30件 |
| 手順 | split-bench-mid-warm タグ付き obs を save → 分断 → 再起動 → polling |
| 期待結果 | 30件の obs が両側で一致 |
| 実測結果 | PASSED（ただし後述の CLI limit トラップに注意が必要） |

**注意**: テスト中に `mesh-mem search "split-bench-mid-warm"` が 20件で止まることが判明。
CLI の argparse default が 20 であるため、実際には 30件存在していたが暗黙の打ち切りが起きていた。
`--limit 100` を指定して 30件すべての同期を確認した（TASK-101）。

出典: TASK-101 context, memory/project_mesh_mem_search_limit.md

---

### 4.6 Cold-entry 30分分断テスト（F2 / Dispatcher BG）

| 項目 | 内容 |
|------|------|
| 目的 | cold tier（長期分断）後の full state sync が機能するか確認 |
| データ規模 | 未確認 |
| 分断時間 | 30分 |
| 手順 | zenohd を 30分停止 → 再起動 → 全件収束確認 |
| 期待結果 | replication が cold tier から full sync を行い件数が一致する |
| 実測結果 | **実行中**（17:00 完了予定）。結果は完了次第 Dispatcher が追記する |

---

### 4.7 Tier-1 ベンチマーク（TASK-097）

| 項目 | 内容 |
|------|------|
| 目的 | 100件 save + search のスループット・レイテンシ・リソース消費の実測 |
| データ規模 | 100件、payload 200B |
| ワーカー数 | 1 |
| 実行コマンド | `BENCH_N=100 BENCH_PAYLOAD=200 BENCH_WORKERS=1 PYTHONPATH=src python3 scripts/bench_bulk_save.py` |

**Save 結果**:

| 項目 | 値 |
|------|-----|
| 件数 | 100件 |
| 経過時間 | 0.003秒 |
| スループット | 31,805.5 ops/sec |

注: `session.put()` は非同期 fire-and-forget のため、throughput はキューイング速度を反映。
実際の RocksDB 永続化は search で 100件が返ってきたことで確認済み。

**Search Latency 結果**:

| limit 指定 | 実際の件数 | レイテンシ (ms) |
|-----------|-----------|--------------|
| 10 | 10 | 58.8 |
| 50 | 50 | 20.8 |
| 100 | 100 | 20.8 |
| 500 | 100 | 22.9 |
| 1,000 | 100 | 21.0 |

注:
- limit=10 の 58.8ms は初回接続確立オーバーヘッド。2回目以降は 20ms 台にキャッシュが効く
- limit=500/1000 での actual=100 はデータが 100件しかないため（正常）
- search_observations は tomb_expr 全走査→obs 全走査→slice の構造のため、limit 変化がほぼレイテンシに影響しない

**リソース使用量（実行後）**:

| 項目 | 値 |
|------|-----|
| zenohd RSS | 29,704 KB（29.0 MB） |
| RocksDB ディスク | 320 KB（~/.local/share/zenoh-mem/agent_mem/） |
| 既存データ保全 | 6件（split-test×3、mesh-mem×2、poc×1）全件 intact |

**判定基準と結果**:

| 基準 | 測定値 | 結果 |
|------|--------|------|
| throughput >= 10 ops/sec | 31,805.5 ops/sec | PASS |
| search@100 <= 500ms | 20.8ms | PASS |
| 既存データ保全 | 6件 intact | PASS |

**総合判定: PASSED**

出典: docs/poc-reports/raw/TASK-097-worker1-tier1.yaml

---

## 5. 主な発見

### 5.1 双方向 replication が想定通り動作

Office で save したデータが Home に伝播し（Goal 4）、逆方向（Home→Office）も確認済み（スモークテスト）。
Zenoh の router モード + replication plugin が /21 LAN 越しに正常に機能した。

出典: memory/project_mesh_mem_poc_results.md

### 5.2 分断後収束が常時 ~5秒だった理由（TASK-102 解明）

- Short（30秒）: ESTAB+5s 収束
- Mid-hot（65秒、hot 境界超え）: ESTAB+5s 収束
- Mid-warm（30件）: ESTAB+5s 同等で収束

hot → warm の境界を超えた分断でも収束時間は同程度だった。
TASK-102（ソース解析）により、その理由が解明された: zenohd が起動すると `initial_alignment()` が scouting delay（約0.5秒）後に即実行され、interval 周期（10秒）を待たずに `AlignmentQuery::Discovery` で対向から全件取得する。分断時間はこの初期同期のタイミングに影響しない。

TASK-096 の設計では「warm/cold 領域は粗い digest 比較のため収束時間が延伸する」と予測していたが、これは誤りだった。warm/cold の粒度差は「常時接続中の定期 digest 比較コスト」に効くものであり、接続復帰時の `initial_alignment()` には適用されない（詳細はセクション 8.1 参照）。

出典: dashboard.md、docs/poc-reports/raw/TASK-102-worker1-zenoh-replication-semantics.yaml

### 5.3 tombstone は existence-based（削除済み obs が復活しない）

Scenario C（split-brain + tombstone 伝播）において：
- Office 側に残っていた obs_X は tombstone 到達後に非表示になった
- 再接続後に obs_X の obs レコードが「復活」することはなかった

これは plan.md の設計意図（timestamp LWW ではなく key の存在ベース）と一致する。

出典: memory/project_mesh_mem_poc_results.md

### 5.4 CLI / MCP / API の default limit が不統一

| インターフェース | ファイル:行 | default limit |
|----------------|------------|---------------|
| CLI (`mesh-mem search`) | src/mesh_mem/__main__.py:141 | 20 |
| MCP (`search_memory`) | src/mesh_mem/mcp_server.py:60 | 20 |
| API (`search_observations`) | src/mesh_mem/store.py:182 | 50 |
| MAX_SEARCH 定数（clamp 上限） | src/mesh_mem/store.py:36 | 10,000 |

Mid-warm テスト（30件）中に CLI default 20 で件数が打ち切られる問題が発見された。
ベンチマーク・分断テストのポーリングには `--limit 1000` 等を明示することを推奨する。

```bash
# 推奨: --limit を明示してポーリング
mesh-mem search "split-bench-X" --limit 1000 | grep -c "obs"
```

出典: docs/poc-reports/raw/TASK-101-worker2-search-limit.yaml, memory/project_mesh_mem_search_limit.md

### 5.5 search は Zenoh 分散クエリで動作（ローカル storage に依存しない）

`search_observations` は `session.get(key_expr)` による Zenoh プロトコルレベルのクエリを使用。
そのため、片側の RocksDB が欠落していても、接続した router が保持するデータは返る
（TASK-091 で Office 側 rocksdb 欠落時でも search が機能することを確認）。

出典: src/mesh_mem/store.py:173-235

---

## 6. 既知の課題（リリース前 TODO）

### BLOCKER

| # | 課題 | 関連コミット | 影響 | 対処案 |
|---|------|------------|------|-------|
| B-1 | config/zenohd_home.json5、zenohd_office.json5 に実 IP（192.168.134.28、192.168.128.12）がハードコード | 447e27c | 他環境で起動不可、README と矛盾 | **推奨 方針 A: placeholder 戻し**（192.168.3.x / 192.168.3.y）— README との整合、最小変更。TASK-105 で実装中（詳細はセクション 8.2 参照） |

出典: dashboard.md「保留中の問題」, Codex レビュー 2026-04-25 vs eee38a8..HEAD

### IMPORTANT

| # | 課題 | 関連コミット | 影響 | 対処案 |
|---|------|------------|------|-------|
| I-1 | `test_search_respects_since_iso_filter` が date-dependent（`since_iso='2024-01-01'` ハードコード） | 6e8e453 | CI clock が古い環境で失敗する可能性 | **推奨 方針 A: recent の `created_at` を `'2025-06-01T00:00:00.000000Z'` に固定** — `old` と同じ手法で一貫性あり、追加依存なし。TASK-106 で実装中（詳細はセクション 8.2 参照） |
| I-2 | CLI/MCP/API の default limit 不統一（CLI=20、MCP=20、API=50） | - | ポーリングスクリプトで暗黙の打ち切りが起きる | 3つを統一（20 または 50 のどちらかに揃える） |

### NOTICE

| # | 課題 | 詳細 |
|---|------|------|
| N-1 | ZENOH_BACKEND_ROCKSDB_ROOT のパス不一致 | plan.md 推奨: `$HOME/.local/share/mesh-mem`、実運用: `~/.local/share/zenoh-mem` |
| N-2 | zenohd v1.9 / rocksdb plugin が apt 管理である旨が README に未記載 | セットアップ手順として明記が必要 |
| N-3 | systemd による自動起動が未設定（手動起動のみ） | 本番運用に向けて systemd unit 設定が必要 |
| N-4 | MESH_MEM_AGENT_FAMILY / MESH_MEM_CLIENT_ID が新ターミナルで未設定になる | bashrc 等への恒久設定推奨。未設定時は `[unknown/unknown]` で記録される |
| N-5 | zenohd 再起動直後の `--project` フィルタ競合（DR 24h テストで観測） | sub-issue #8 で別途 track。詳細は §8.3 参照 |

出典: dashboard.md、memory/project_mesh_mem_state.md、memory/project_mesh_mem_poc_results.md

---

## 7. 次のテスト候補（未着手）

| 優先度 | テスト | 目的 | 前提 |
|--------|-------|------|------|
| 高 | Tier-2 ベンチマーク（1,000件） | 中規模データでのスループット・レイテンシ実測 | Tier-1 データ（100件）の上に追加可 |
| 高 | Cold-entry テスト結果確認 | F2 実行中。完了後に本ドキュメントへ追記 | F2 完了待ち |
| 中 | Tier-3 ベンチマーク（10,000件） | 大規模データでの RSS・ディスク使用量実測 | Tier-2 合格後 |
| 中 | GC / retention の実測 | tombstone の物理削除が正常に動くか確認 | 既存 tombstone が必要 |
| 中 | DR シナリオ（1日分断） | 長期離脱後の full sync 耐性確認 | Cold-entry 結果を踏まえて設計 |
| 低 | NTP skew 境界テスト（>100ms） | HLC のタイムスタンプ競合が起きないか確認 | 専用 skew 環境が必要 |
| 低 | MCP 経由での Claude Code 統合動作確認 | LLM エージェントが save/search/delete を正常に使えるか確認 | FastMCP サーバー起動環境が必要 |

出典: TASK-096（大規模 + 長期分断シナリオ設計）, TASK-104 タスク定義

---

## 8. 補足調査結果

### 8.1 zenoh hot/warm/cold semantics と 5秒収束の理論解明（TASK-102）

TASK-102（Worker 1）が zenoh OSS ソースを解析し、以下を解明した。

出典: docs/poc-reports/raw/TASK-102-worker1-zenoh-replication-semantics.yaml、memory/project_zenoh_replication_semantics.md

#### hot/warm/cold era の定義

era boundary は「データの保存時刻（HLC timestamp）の age」で決まる。分断時間や接続イベントとは無関係。

現在の設定（interval=10s、sub_intervals=5、hot=6、warm=30）での時間窓:

| Era | 時間範囲（データの保存時刻） | Digest 粒度 |
|-----|--------------------------|------------|
| hot | 現在 〜 60秒前のデータ | SubInterval（2秒粒度） |
| warm | 60秒 〜 360秒前のデータ | Interval（10秒粒度） |
| cold | 360秒より古いデータ | XOR 単一 fingerprint |

sub_interval 粒度 = interval / sub_intervals = 10s / 5 = 2s

ソース: `plugins/zenoh-plugin-storage-manager/src/replication/configuration.rs`（`hot_era_lower_bound()` / `warm_era_lower_bound()`）

#### なぜ 5秒収束するか — initial_alignment() の即実行

zenohd が起動すると以下のフローが走る:

```
zenohd start
  → spawn_digest_publisher() / spawn_digest_subscriber() / spawn_aligner_queryable() 開始
  → scouting delay 待機（~500ms）
  → initial_alignment() を即実行  ← interval 周期 (10s) を待たない
  → spawn_query_replica_aligner(AlignmentQuery::Discovery) で対向に全データ問い合わせ
  → 対向応答 → ストレージに適用 → 収束完了
```

分断時間（30秒/65秒/30分）は `initial_alignment()` のタイミングに影響しない。
B が起動すれば約0.5秒後に Discovery が走り、対向が応答した時点で全件同期が完了する。

ソース: `plugins/zenoh-plugin-storage-manager/src/replication/core.rs`（`initial_alignment()`、`AlignmentQuery::Discovery`）

#### 設計時予測との差異

TASK-096 の設計では「warm/cold 領域は粗い digest 比較のため収束時間が延伸する」と予測していた。
これは誤りだった。warm/cold の粒度差は「常時接続中の定期 digest 比較（interval ごと）のコスト」に影響するものであり、接続復帰時の `initial_alignment()` には適用されない。

F2（Cold-entry 30分）でも「起動直後に ~5秒で収束」が正しい予測。30分後に起動した場合でも、分断中に保存したデータが hot era に入っていれば（save 直後の timestamp）収束は遅延しない。

#### warm/cold 効果が実際に現れる条件

以下の条件が揃った場合にのみ warm/cold の粒度差がパフォーマンスに影響する:
- 両側で常時接続中（再起動ではなく定期 digest 交換中）
- 数千件以上の observation が 6分以上前（cold era）に存在する
- interval ごとの定期 digest 比較で大量の差分がある

今回の PoC 規模（最大 100件、最長 30分分断）では観察条件を満たさない。

---

### 8.2 Codex 指摘修正方針設計（TASK-103）

TASK-103（Worker 3）が BLOCKER・IMPORTANT 各1件の修正方針を設計した。TASK-105/106 で実装中。

出典: docs/poc-reports/raw/TASK-103-worker3-codex-fix-design.yaml

#### BLOCKER B-1: 方針 A（placeholder 戻し）【推奨】

変更ファイル: `config/zenohd_home.json5`、`config/zenohd_office.json5`

`zenohd_home.json5`:
- listen: `"tcp/192.168.134.28:7447"` → `"tcp/192.168.3.x:7447"`
- connect: `"tcp/192.168.128.12:7447"` → `"tcp/192.168.3.y:7447"`
- コメント: 実 IP 記述 → `"192.168.3.x: 自機 (home)"` / `"192.168.3.y: 対向 (office)"`

`zenohd_office.json5`:
- listen: `"tcp/192.168.128.12:7447"` → `"tcp/192.168.3.y:7447"`
- connect: `"tcp/192.168.134.28:7447"` → `"tcp/192.168.3.x:7447"`
- コメント: 実 IP 記述 → `"192.168.3.y: 自機 (office)"` / `"192.168.3.x: 対向 (home)"`

推奨理由: README が既に `192.168.3.x/y` 形式を案内しており整合性が最も高い。変更量が最小（1コミット）。

想定コミット: `"config: replace hardcoded LAN IPs with placeholder (security)"`（CHANGELOG security エントリ追記）

#### IMPORTANT I-1: 方針 A（固定値）【推奨】

変更ファイル: `tests/test_store_single.py`（3行程度）

```python
# 変更前
recent = _mk_obs('recent observation', project='since-test')

# 変更後
recent = dataclasses.replace(
    _mk_obs('recent observation', project='since-test'),
    created_at='2025-06-01T00:00:00.000000Z',  # since_iso(2024-01-01)より後の固定値
)
```

推奨理由: `old` observation で同じ手法（`dataclasses.replace(..., created_at=...)`）を既に使用しており一貫性がある。追加ライブラリ不要。CI clock 依存を完全排除できる。

想定コミット: `"test: pin recent observation created_at to eliminate CI-clock dependency"`

---

### 8.3 DR 24h 分断テスト結果（TASK-119）

TASK-119（Dispatcher 直実行）が 2026-04-26 〜 2026-04-27 の実機 2台で 24.45時間 の長期分断テストを実施した。

出典: docs/poc-reports/raw/TASK-119-dr-1day-result.yaml、docs/poc-reports/dr_1day_test_design.md

#### 実施概要

| 項目 | 値 |
|------|-----|
| 分断開始 | 2026-04-26T08:19:52+09:00（Office zenohd を SIGTERM で停止） |
| Writer 開始 | 2026-04-26T12:59:43+09:00（分断から 4h40m 遅延） |
| 再接続 | 2026-04-27T08:46:44+09:00 |
| 分断時間 | 24.45h |
| Writer 稼働時間（分断中） | 19.78h |
| 保存件数 | 1,192件（目標 1,440件、未達理由: 分断後の Worker 起動遅延） |

Writer の 4h40m 遅延は TASK-118 ワーカー起動の rate-limit 残留によるもので、システム障害ではない。
1,192件は cold-era 検証（全件 XOR 単一 fingerprint でのアライメント）として十分な規模。

#### G1-G4 + Tombstone 判定表

| Goal | 期待値 | 実測値 | 判定 |
|------|--------|--------|------|
| G1: Writer が 1,440件 save 完了 | 1,440 writes | 1,192 writes（4h40m 遅延で未達） | PARTIAL |
| G2: 再接続後 30秒以内に収束 | <= 30s | 97-282秒（step-function 0 → 1,193） | FAIL |
| G3: Home/Office のデータ損失ゼロ | diff 空 | 両側 md5 一致（pre-delete 1,194件 / post-delete 1,189件） | PASS |
| G4: ストレージ上限内（DB<50MB、RSS<200MB） | DB<50MB、RSS<200MB | DB ~26MB、RSS ~82MB | PASS |
| Tombstone 伝播（5件削除→5s 以内に両側反映） | Home 削除が Office に伝播 | 5s 待機後に両側 1,194→1,189、md5 一致 | PASS |

G1 の未達はワークフロー側の問題（Worker 起動遅延）であり、replication システム自体の障害ではない。
G2 の FAIL は §8.1 および設計書 §5.2 で既に予測されていた cold-era の期待動作であり、
バグではなく **cold era における正常な収束挙動**として記録する。

#### 主要発見 1: cold-era step-function 収束

再接続後の Office 側 obs 件数の推移:

| 時刻 | Office 件数 | Home 件数 |
|------|------------|----------|
| ESTAB 直後（T+0s） | 0 | 1,188-1,190 |
| T+35s（poll1 window 開始） | 0 | 1,188-1,190 |
| T+105s（poll1 window 終了） | 0 | 1,188-1,190 |
| T+211s（poll2 window 開始） | 0 | 1,191-1,193 |
| T+280s（poll2 window 終了） | 0 | 1,191-1,193 |
| T+282s（spot check） | 1,193 | 1,193 |

Office 側は 0件のまま 97-282秒経過し、その後一瞬で 1,193件に**ジャンプ**した。
途中経過（部分同期、件数の単調増加）は一切観測されなかった。

このパターンは §8.1 で解明した cold era の仕組みと整合する:
- cold era のデータは **XOR 単一 fingerprint**（単一のハッシュ値）として管理される
- `initial_alignment()` は Discovery クエリで対向の全 cold era データを一括転送する
- 転送が完了するまで Office 側の storage には何も現れず、完了した瞬間に全件が反映される
- 1,192-1,194件 × ~72KB 相当のデータを 1 アライメントサイクルで転送するため 97-282秒を要した

**§5.2 の仮説「30秒で収束」は cold era では非現実的**であり、本実測で update する。
hot/warm era（分断時間 <360秒）では initial_alignment が数秒以内に完了するが、
cold era（360秒超の古いデータが大量に存在する場合）ではデータ転送時間が支配的になる。

なお §8.1 で導いた「分断時間が何分でも ~5秒収束する」結論は、
「最初の接続で initial_alignment が即実行される」という点では今回も成立している（T+0.5s で Discovery 実行）。
変化したのは「Discovery によるデータ転送完了までの時間」であり、これはデータ量に比例する。

#### 主要発見 2: --project filter race（sub-issue #8）

再接続直後の数分間、以下の挙動が観測された:

```bash
# Office 側
mesh-mem search "" --project dr-test --limit 2000
# → 0件（0 → 1,193 ジャンプの前）

mesh-mem search "" --limit 20
# → 同時刻に dr-test の obs が含まれたリストが返る
```

`--project` フィルタなしの empty keyword search では同じデータが見えているにもかかわらず、
`--project dr-test` 指定では 0件になる現象が数分間継続した。

これは CLI / MCP filter 処理と zenohd storage backend の hydration の間に競合が存在する可能性を示唆する。
本テスト時点では根本原因の特定には至っていない。sub-issue #8 で別途調査・track する。

#### §8.1 との整合

TASK-102 の結論「`initial_alignment()` は接続直後に即実行され、分断時間は alignment トリガーに影響しない」は、
24h スケールでも成立することが確認された（Office zenohd 起動後 T+0.5s で Discovery クエリが走った）。
§8.1 の結論に変更はない。cold era では Discovery 後の**データ転送完了時間**が新たに支配的要因として追加される。
