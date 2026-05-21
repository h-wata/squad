# search server-side filtering 設計書

| 項目 | 値 |
|------|-----|
| TASK | TASK-131 |
| 作成日 | 2026-04-27 |
| Issue | https://github.com/h-wata/mesh-mem/issues/7 |
| 起票元 | TASK-115 Tier-3 ベンチ（`docs/poc-reports/raw/TASK-115-tier3-bench.yaml`） |
| 関連 | `docs/poc-reports/SUMMARY.md`、Issue #5（PoC ベンチ集約）、Issue #9（schema 拡張） |
| ステータス | 設計のみ。実装は別タスク |

---

## 1. 問題の整理

### 1.1 現状実装（事実確認）

`src/mesh_mem/store.py:173-236` の `search_observations()` は次のフローで動作している。

1. `mem/tomb/{agent_family or *}/{client_id or *}/{pc_id or *}/{session_id or *}/**` を session.get で全件走査し、tombstone の observation_id を `set` に集める
2. `mem/obs/...` 同形を全件走査し、各 reply の payload を `Observation.from_json` でデコード
3. tombstone マッチ／`project` 一致／`since_iso`／`query`（content / project / tags の部分一致）を **Python 側で** filter
4. `created_at` 降順にソートして `[:limit]` で打ち切り

key 階層は `mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}` で、**`project` は key に含まれず payload 内にのみ存在**。識別系（agent_family / client_id / pc_id / session_id）以外は zenoh selector で絞れない。`limit` も Python の slice なので scan 量を減らさない。

### 1.2 計測（TASK-115 Tier-3 / Tier-3x）

`docs/poc-reports/raw/TASK-115-tier3-bench.yaml` より、`limit=1000` 固定での search latency:

| DB件数 | latency (ms) | 備考 |
|--------|--------------|------|
| 100    | 21           | Tier-1 baseline |
| 1,000  | 55           | Tier-2 |
| 6,100  | 574          | Tier-3 (n=5000) |
| 16,100 | 2,243        | Tier-3x (n=10000) |

`limit` を 10 に下げても 6,100件で 610ms / 16,100件で 1,359ms と、**limit に対してほぼ flat**。これは「全件取得 → Python で slice」という構造の自然な帰結。

### 1.3 ボトルネックの分解（仮説）

| 段階 | 推定占有 | 根拠 |
|------|----------|------|
| zenohd → CLI のネットワーク往復＋ペイロード転送 | 大（>50%） | DB件数に比例して増えるのは reply 数 |
| Python での JSON decode | 中 | Observation.from_json を全件で実行 |
| in-memory filter / sort | 小 | リスト操作のみ |

→ **scan 量 (= reply 数) を削減することが第一の打ち手**。

### 1.4 受け入れ条件（Issue #7 より）

- `search_observations(limit=N)` の latency が 50,000 obs で sub-200 ms
- 公開 API（`search_observations` のシグネチャ）不変
- 既存 functional test がそのまま PASS
- Tier-1/2/3 ベンチが性能面で improvement
- tombstone セマンティクス（存在ベースの論理削除）は維持

---

## 2. 候補案

### 案 A：Zenoh queryable に server-side filter を持たせる

**概要**：各 zenohd にカスタム queryable プラグイン（Rust）を実装し、selector の query string で `project=foo&since=...` を渡す。プラグインが RocksDB を直接走査し、フィルタ済み reply のみを返す。

**利点**
- zenoh-native で完結。replication / tombstone モデルを変えない
- 副次 store の整合性問題が出ない
- query の semantics は zenoh 側で集中管理できる

**欠点**
- Rust の queryable プラグインを書く必要がある（実装コスト大）
- 配布・運用が複雑化（zenohd プロセスにプラグインを load させる、バージョン管理が増える）
- key 階層に project が無いので、プラグイン側でも RocksDB の payload を全件 deserialize して filter するなら scan 量自体は減らない（key prefix で前段絞りしないと意味がない）
- zenoh-rocksdb plugin は基本「key_expression による prefix match」までで、value 解析は別レイヤ
- PoC スコープを大きく超える

**判定**：本格運用フェーズで再検討する候補。**PoC 範囲では採用しない**。

### 案 B：SQLite (FTS5) を local index として並走させる

**概要**
- zenoh-rocksdb は replication source としてそのまま残す
- 各 PC に SQLite ファイル（例 `~/.local/share/mesh-mem/index.sqlite`）を持ち、`(observation_id PRIMARY KEY, project, created_at, agent_family, client_id, pc_id, session_id, deleted_at NULL, content_fts)` を保持
- `put_observation` 成功時に SQLite にも upsert、`put_tombstone` で `deleted_at` を立てる
- `search_observations` は **SQLite で WHERE 句を回し**、ヒットした observation_id のみ zenoh から get（または SQLite に保持した payload キャッシュから返す）

**利点**
- SQL でレンジ検索（`created_at`）と prefix / FTS 検索（`content`、`tags`）が容易
- index の追加（`memory_type`、`importance`、`subject`、`source_files`）は ALTER で済むので Issue #9 拡張と相性がよい
- SQLite 単体の読み latency は通常 1〜10 ms。50k obs でも sub-200ms に収まる見込み
- 既存 zenoh ストレージは触らないので migration / replication への影響が無い

**欠点**
- index と zenoh の **eventual consistency** 管理が必要
  - `put_observation` 後の SQLite insert が失敗したらどうするか
  - 他 PC から replication で到着した obs を捕捉するため subscriber を別途走らせる必要
  - restart 時の rebuild（SQLite が壊れた／削除された場合）
- tombstone を SQLite 側でどう表現するか（`deleted_at NOT NULL` を tombstone と扱うのが素直）
- "single binary, no extra service" の PoC 思想に対し、永続ファイルが 1 つ増える

**判定**：**PoC スコープでの第一推奨**。

### 案 C：key 階層に `project` を組み込む

**概要**
- 現行 key：`mem/obs/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}`
- 新 key：`mem/obs/{project_or_default}/{agent_family}/{client_id}/{pc_id}/{session_id}/{observation_id}`
- `project=''`（未指定）は `_default` のような sentinel に正規化
- search 側は `mem/obs/{project}/**` の selector で zenoh ネイティブ prefix match で絞る

**利点**
- 追加 backend 不要。zenoh-rocksdb のままで scan 量を **project 単位で削減できる**
- 実装コストが小さい（key 構成と replication 設定の調整のみ）
- 案 B の前段に置けば「project で粗く絞ってから SQLite で詳細 filter」と組み合わせも素直

**欠点**
- 既存データの migration が必要（key を rewrite する batch スクリプト or 互換 read path）
- `project=''` のハンドリング（sentinel 衝突、検索時に空 project とリテラル `_default` を混同しない）
- Issue #9 で `subject` / `memory_type` も key に入れたくなる誘惑が出るが、key を肥大化させると selector の表現が弱くなる
- `since` や `query` 全文検索は依然として Python 側

**判定**：**B と組み合わせる前提なら有効**。単体だと sub-200ms@50k は達成困難（since / query が Python filter のままなので）。

### 案 D：hybrid（C → B の段階導入）

**概要**
- **短期（Phase 1〜2）**：案 C で key に project を組み込む（migration 含む）
  → project 指定 search が zenoh-native で絞れるようになり、project 別の sub-200ms は 50k obs でも到達可能
- **中期（Phase 3〜5）**：案 B で SQLite を導入
  → since レンジ・FTS・複合 filter で sub-200ms を完成

**利点**
- 段階的に効果が出る（短期で project 絞り込みが速くなる）
- 各 phase の DoD を独立に検証できる

**欠点**
- 全体の実装コストは B 単体より大きい
- C の migration を 1 度実施した後、SQLite 導入で再度コードパスが増える

**判定**：**長期的にはこの方向だが、PoC 完了の閾値は B 単体で十分**。

---

## 3. 推奨案と根拠

### 3.1 推奨：**案 B（SQLite local index）を PoC スコープで実装**

理由
1. **受け入れ条件達成の最短経路**：sub-200ms@50k は SQLite 単体 query の典型値（10ms 未満）＋ zenoh から limit 件 get（typical 50ms）で十分達成見込み
2. **Rust プラグイン回避**：案 A の主リスク（プラグイン開発・配布）を踏まずに済む
3. **Issue #9 拡張との相性**：`memory_type` / `importance` / `subject` を index 列に追加するだけで複合 filter が効く
4. **API 不変**：`search_observations` のシグネチャは変えずに内部実装だけ差し替え可能

### 3.2 sub-200ms@50k 達成見込み（要約）

- SQLite における WHERE + ORDER BY DESC LIMIT 50 は、適切な複合 index があれば 50k 行で **1〜10 ms オーダ**（実測ベンチは Phase 4 で確認）
- ヒットした 50 件分のみ zenoh から get：50 reply × 1 ms 前後 ≈ **50 ms**
- payload キャッシュを SQLite 側に保持するなら zenoh round-trip 不要で **<20 ms**
- 合計：**60〜100 ms**（pessimistic でも 200 ms 以内）に収まる
- 比較対象：現状 16k で 2.2 秒 / 線形外挿で 50k なら 7 秒前後 → **約 35〜100 倍の改善**

### 3.3 Schema 設計（叩き台）

```sql
CREATE TABLE observations (
  observation_id TEXT PRIMARY KEY,        -- 32 hex
  agent_family   TEXT NOT NULL,
  client_id      TEXT NOT NULL,
  pc_id          TEXT NOT NULL,
  session_id     TEXT NOT NULL,
  project        TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL,           -- ISO 8601, indexable as TEXT
  memory_type    TEXT NOT NULL DEFAULT 'note',
  importance     INTEGER NOT NULL DEFAULT 2,
  subject        TEXT NOT NULL DEFAULT '',
  summary        TEXT NOT NULL DEFAULT '',
  content        TEXT NOT NULL,
  tags_json      TEXT NOT NULL DEFAULT '[]',
  payload_json   TEXT NOT NULL,           -- 完全な Observation JSON（zenoh round-trip 回避用キャッシュ）
  deleted_at     TEXT                     -- NULL=live, NOT NULL=tombstone
);

CREATE INDEX idx_obs_project_created ON observations(project, created_at DESC);
CREATE INDEX idx_obs_created          ON observations(created_at DESC);
CREATE INDEX idx_obs_agent_session    ON observations(agent_family, client_id, pc_id, session_id);
-- FTS5 仮想表で content / project / tags を検索
CREATE VIRTUAL TABLE observations_fts USING fts5(
  content, project, tags_json,
  content='observations', content_rowid='rowid'
);
```

`tombstone` を別表にせず `deleted_at` 列で管理するのは、search の WHERE で `deleted_at IS NULL` を一発で書ける利点が大きい。

### 3.4 書き込み・整合性フロー（叩き台）

| イベント | 動作 |
|---------|------|
| `put_observation(obs)` | ① zenoh put 成功 ② SQLite UPSERT（payload_json 含む） ③ FTS5 sync |
| `put_tombstone(obs)` | ① zenoh put（mem/tomb/...） ② SQLite UPDATE `SET deleted_at=now` |
| 他 PC からの replication 受信 | zenoh subscriber thread が `mem/obs/**` / `mem/tomb/**` を購読し、SQLite に同等の操作を流す |
| restart 時 | SQLite が無い／壊れている場合、zenoh から `mem/obs/**` + `mem/tomb/**` を全件取得して rebuild |
| `gc_expired_tombstones` | zenoh 側 delete 後、SQLite からも DELETE |
| `physical_delete_observation` | 同上 |

整合性ポリシー
- **zenoh が真実**。SQLite はあくまでローカル index（破損したら zenoh から rebuild できる）
- restart 時の rebuild が現実的かを Phase 1 で確認（50k obs の rebuild が数秒で済むか）

---

## 4. 受け入れ条件への対応

| 受け入れ条件 | 案 A | 案 B（推奨） | 案 C | 案 D |
|-------------|------|--------------|------|------|
| sub-200ms@50k | ◎（key prefix が効けば） | ○（SQLite で達成見込み） | △（project 限定なら可、since/query は不可） | ◎ |
| 公開 API 不変 | ○ | ○ | ○（内部 key 変換でラップ） | ○ |
| 既存テスト互換 | ○ | ○（実装次第） | △（key 形式テストは更新） | △ |
| Tier-1/2/3 改善 | ○ | ○ | ○（project 系のみ） | ○ |
| 実装コスト | × Rust プラグイン | △ Python のみ | ○ key 構成変更のみ | × 段階多 |

---

## 5. リスクと未解決事項

| ID | 項目 | 評価 | 対策 |
|----|------|------|------|
| R1 | SQLite と zenoh の eventual consistency 不一致（subscriber 漏れ） | 中 | restart 時 rebuild、定期 reconciliation（cron で zenoh 全件と diff）を別 Issue 化 |
| R2 | tombstone 反映の race（put_tombstone → SQLite UPDATE 失敗） | 中 | tombstone は zenoh 側に必ず存在 → SQLite が古ければ subscriber／rebuild で追従 |
| R3 | restart 時 rebuild に時間がかかる | 低〜中 | 50k obs × ~1 ms decode = 50 秒以下を見込み、Phase 1 で実測 |
| R4 | Issue #9 schema 拡張との衝突 | 低 | schema は migration script で前方互換、Phase 0 で `memory_type` 等を含めて設計 |
| R5 | SQLite ファイル肥大化（payload_json をフルに保持） | 中 | 50k × 1KB ≈ 50MB。許容範囲だが、別 PC との sync は不要なのでローカルのみ |
| R6 | concurrent write（複数プロセスが同 SQLite を開く） | 中 | WAL モードで書き込み競合を許容、読みは無制約 |
| R7 | 案 C を将来採用する場合の migration | 中 | Phase 5（任意）に分離、PoC スコープ外 |

---

## 6. 実装ステップ（次タスクで使う叩き台）

PoC スコープでは **案 B（SQLite index 導入）** を 5 phase に分けて段階導入する。

### Phase 1：spike — SQLite index の rebuild ベンチ

- 目的：50k obs を zenoh から取り出して SQLite に rebuild する所要時間を実測
- DoD
  - throwaway スクリプトで `mem/obs/**` を全件取得し、上記 schema に insert
  - 50k obs で rebuild < 30s を確認（fail なら schema or 取得方法を見直す）
  - SQLite query `SELECT ... WHERE project=? ORDER BY created_at DESC LIMIT 50` の latency を ms 単位で測定
- test 観点：機能テストなし（spike）。timing メトリクスを poc-reports/raw に保存
- 出力：`docs/poc-reports/raw/TASK-XXX-sqlite-spike.yaml`

### Phase 2：write path — `put_observation` / `put_tombstone` の二重書き

- 目的：書き込み API が SQLite にも反映されるようにする
- DoD
  - `store.py` に `_index` レイヤを追加（SQLite open / upsert / update）
  - `put_observation` 成功直後に SQLite UPSERT
  - `put_tombstone` 直後に SQLite UPDATE `deleted_at`
  - SQLite が無い・壊れている場合は warning 出して書き込みをスキップ（fall back）
- test 観点
  - 既存 `test_store_single.py` / `test_gc.py` が PASS
  - 新規：write 後に SQLite からも観測できる単体 test（in-memory SQLite）

### Phase 3：read path — `search_observations` を SQLite-first に切替

- 目的：search が SQLite を WHERE で絞ってから返す
- DoD
  - SQLite WHERE 構文：`project = ? AND created_at >= ? AND deleted_at IS NULL`
  - `query` は FTS5（content / project / tags）を使う
  - SQLite から `payload_json` を直接デコードして Observation を返す（zenoh round-trip 省略）
  - SQLite が無いときは現行の zenoh full scan に fall back（`MESH_MEM_DISABLE_INDEX=1` env で同様の動作切替）
  - 公開 API（引数・返り値）は変更しない
- test 観点
  - 既存 `test_store_single.py` の search テストすべて PASS
  - 新規：SQLite と zenoh full scan で同一クエリ → 同一 result set（property test）

### Phase 4：subscriber & rebuild — replication 取り込みと自動復旧

- 目的：他 PC から replication で到着した obs／tomb を SQLite に取り込む
- DoD
  - 起動時に `mem/obs/**` / `mem/tomb/**` を subscribe するスレッドを立ち上げる
  - 受信ごとに SQLite を更新
  - 起動時、SQLite が空なら zenoh から bulk get → SQLite rebuild
  - rebuild は 50k obs / 30s 以下（Phase 1 ベンチ基準）
- test 観点
  - 2-router 連携テスト：Office 側 put → Home 側 SQLite に到達する
  - SQLite を削除して再起動 → 全件 rebuild される統合テスト
  - mock subscriber を使った単体テスト（zenoh を立てない）

### Phase 5：Tier-1/2/3 ベンチ再実行 & Tier-4 計測

- 目的：性能改善の定量確認と回帰防止
- DoD
  - Tier-1（100）/ Tier-2（1k）/ Tier-3（6k）/ Tier-3x（16k）を再実行 → 既存 yaml と比較
  - 新規 Tier-4（50k）を実行 → `limit=50, project='foo'` で sub-200ms を確認
  - Issue #7 のベンチ表を更新、`SUMMARY.md` に追記
- test 観点
  - bench スクリプトのみ。CI には載せず、release 前手動実行
  - 結果は `docs/poc-reports/raw/TASK-XXX-tier4-bench.yaml`

---

## 7. 既存テストへの影響

### 影響あり（修正必要）

- `tests/test_store_single.py::test_search_*` シリーズ
  - SQLite が write path に入るため、fixture で SQLite を都度クリーンアップする必要
  - もしくは in-memory SQLite (`:memory:`) を default 化
- `tests/test_gc.py`
  - `physical_delete_observation` / `gc_expired_tombstones` 後、SQLite からも消えていることを assert する追加 case

### 影響なし

- `tests/test_models.py`（schema 単体）
- `tests/test_identity.py`
- `tests/test_mcp_*`（MCP 経由 API は内部実装に依らない）

### 新規テスト

| ファイル | 概要 |
|---------|------|
| `tests/test_index_writer.py` | put / tombstone → SQLite に反映 |
| `tests/test_index_reader.py` | SQLite から search できる、zenoh full scan と同一 result |
| `tests/test_index_rebuild.py` | SQLite 削除 → rebuild される |
| `tests/test_index_subscriber.py` | replication で受信した obs が SQLite に流れる |

---

## 8. PoC スコープと次フェーズ境界

### PoC（本設計の範囲）

- 案 B 単体導入（Phase 1〜5）
- SQLite はローカル副次 index 扱い
- `MESH_MEM_DISABLE_INDEX=1` で従来挙動に切り戻せる safety を残す

### PoC スコープ外

- 案 A（Rust queryable プラグイン）
- 案 C（key 階層への project 組み込み）：別 Issue で起票候補
- SQLite の cross-PC 同期：基本的に不要（各 PC が zenoh から rebuild できれば足りる）
- 自動 reconciliation cron（zenoh と SQLite の整合性を定期 diff）：別 Issue で起票候補

---

## 9. 引き継ぎ事項

### 設計確認後の最初のアクション

1. dispatcher へ「案 B で進める」承認を取る
2. Phase 1 spike を独立タスクで起票（命名例：`TASK-XXX: SQLite index spike (Phase 1)`）
3. Issue #7 に本設計書のリンクをコメント

### 別 Issue 起票候補

| 候補 | 概要 | 優先度 |
|------|------|--------|
| feat: key 階層に project を組み込む | 案 C 単体実装。SQLite を入れずに済む project 限定の高速化 | low |
| feat: zenoh × SQLite reconciliation cron | eventual consistency の確実な収束 | medium |
| feat: Rust queryable plugin | 案 A 本格運用フェーズ向け | low |

---

*設計書のみ。コード変更なし、commit/push なし。実装は Phase 1（spike）以降の別タスクで実施する。*
