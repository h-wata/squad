# GC / Retention テスト設計書

| 項目 | 値 |
|------|-----|
| 作成日 | 2026-04-27 |
| Issue | https://github.com/h-wata/mesh-mem/issues/5 |
| 調査元 | src/mesh_mem/store.py, src/mesh_mem/__main__.py |

---

## 事前調査結果（実装の事実）

設計書作成前に以下をソース確認した。

### GC の実装概要

| 関数 | ファイル:行 | 動作 |
|------|-----------|------|
| `gc_expired_tombstones(retention_days=30)` | store.py:366 | tombstone の `deleted_at` が cutoff より古いものを tombstone + 対応 obs ともに物理削除 |
| `physical_delete_observation(observation_id)` | store.py:314 | ID 指定で obs + tombstone を即時物理削除。broadcast wildcard delete で対向に波及（ベストエフォート） |

### CLIコマンド

```
mesh-mem gc [--force-id FORCE_ID] [--retention-days RETENTION_DAYS]
```

- `--retention-days`（デフォルト 30）: N 日超の tombstone とその obs を物理削除
- `--force-id`（32 文字 ID）: tombstone 有無に関わらず obs を即時物理削除（緊急手順）

### MCP 経由のGC

`mcp_server.py` に gc ツールは存在しない。MCP から GC を実行する手段はない（CLI のみ）。

### tombstone の性質

- tombstone は `mem/tomb/{agent}/{client}/{pc}/{session}/{obs_id}` に Zenoh pub されて永続化される
- `gc_expired_tombstones` 実行まで tombstone レコード自体は RocksDB に残り続ける（物理削除されない）
- `deleted_at` が `retention_days` より古い tombstone のみが GC 対象
- tombstone のない live obs は GC の対象外（`physical_delete_observation` で強制削除する場合を除く）

### retention の設定方法

- CLI 引数のみ（`--retention-days N`）
- 設定ファイルや環境変数による永続設定は実装されていない
- 自動 timer / cron は実装なし（定期実行は外部 cron で `mesh-mem gc` を呼ぶ）

### テスト実行上の注意

- `--retention-days 0` を使えば、当日削除した tombstone も GC 対象にできる（`deleted_at >= cutoff` が `>= now` で判定されるため、now より過去の tombstone がすべて対象）
- 2-router 環境での GC: `physical_delete_observation` は broadcast wildcard delete を送るが、`gc_expired_tombstones` は対向に broadcast しない（**ローカル router の replica のみ物理削除**）

---

## 1. 目的

- `mesh-mem gc` が単なる呼び出し成功ではなく、実際にデータが物理削除されることを確認する
- tombstone の物理削除が `retention_days` 設定どおりに動くことを確認する
- 2-router 環境での GC 動作（ローカルのみ削除か、対向にも波及するか）を実測する
- 実装されていない機能（live obs の自動 age-based 削除、MCP 経由 GC 等）を明示し、別 issue 候補として記録する

---

## 2. 検証目標 (Goals)

| ID | ゴール | 備考 |
|----|--------|------|
| G1 | tombstone が 0 件の状態で `mesh-mem gc` を実行すると "0 件を物理削除" を返す（no-op） | 正常系の最小確認 |
| G2 | tombstone を作成して `--retention-days 0` で実行すると tombstone + 対応 obs が物理削除される | 即時削除パスの確認 |
| G3 | `--force-id` で tombstone なしの live obs を直接物理削除できる | 緊急削除パスの確認 |
| G4 | 2-router 環境で Home 側 gc → Office 側の replica に影響するか実測する | broadcast ベストエフォートの確認 |
| G5 | GC 中の concurrent search との race が起きても crash しない | 安定性確認 |

**NOTICE**: 以下は実装されていないため G として立てない。別 Issue 候補として記録する。

- live obs（tombstone なし）の age-based 自動削除: 実装なし
- MCP 経由の GC: `mcp_server.py` に gc ツールなし
- gc の自動 timer: 実装なし（外部 cron 必須）

---

## 3. 環境とツール

### ホスト

| 役割 | ホスト | コマンド |
|------|--------|---------|
| Home | 192.168.134.28 | `/home/gisen/.local/bin/mesh-mem` |
| Office | 192.168.128.12 (SSH) | `mm`（alias） |

### 現在のデータ状態（設計書作成時点の推定）

- DR 24h テスト後: dr-test プロジェクト ~1,189 件の live obs が存在
- scale-bench 観測も含む（別 project タグ）
- gc テストは `gc-test` プロジェクト専用の obs/tombstone を使って既存データへの影響を避ける

### DB パス

```
~/.local/share/zenoh-mem/agent_mem/  (ZENOH_BACKEND_ROCKSDB_ROOT に依存)
```

---

## 4. シナリオ手順

### Phase 0: baseline 記録（約 5 分）

```bash
# 全件数確認（gc-test プロジェクトは 0 件のはず）
mesh-mem search "" --project gc-test --limit 1000
# → 0 件

# tombstone 数の確認（直接ではなく delete でテスト用 obs を作成後にカウント）
# 現在の総件数
mesh-mem status

# DB サイズ baseline
du -sh ~/.local/share/zenoh-mem

# Office 側も確認
ssh office "du -sh ~/.local/share/zenoh-mem"
```

### Phase 1: G1 — no-op gc（tombstone 0 件）

```bash
# gc-test プロジェクトに obs がない状態で gc 実行
mesh-mem gc --retention-days 0
# 期待: "retention 0 日超の tombstone: 0 件を物理削除しました"
```

### Phase 2: G2 — tombstone → retention 0 で即時物理削除

```bash
# 1. gc-test プロジェクトに obs を 3 件保存
OBS1=$(mesh-mem save "gc-test 1" --project gc-test | grep -oE '[0-9a-f]{32}')
OBS2=$(mesh-mem save "gc-test 2" --project gc-test | grep -oE '[0-9a-f]{32}')
OBS3=$(mesh-mem save "gc-test 3" --project gc-test | grep -oE '[0-9a-f]{32}')
echo "saved: $OBS1 $OBS2 $OBS3"

# 2. 3 件を論理削除（tombstone 発行）
mesh-mem delete "$OBS1"
mesh-mem delete "$OBS2"
mesh-mem delete "$OBS3"

# 3. search で 0 件（論理削除済み）になっていること確認
mesh-mem search "" --project gc-test --limit 100
# 期待: 0 件

# 4. DB サイズ確認（obs + tombstone が RocksDB に残っているはず）
du -sh ~/.local/share/zenoh-mem

# 5. gc を retention-days 0 で実行
mesh-mem gc --retention-days 0
# 期待: "retention 0 日超の tombstone: 3 件を物理削除しました"

# 6. DB サイズ確認（物理削除後に縮小しているか）
du -sh ~/.local/share/zenoh-mem

# 7. search で引き続き 0 件であること確認
mesh-mem search "" --project gc-test --limit 100
```

### Phase 3: G3 — --force-id による tombstone なし live obs の強制削除

```bash
# 1. live obs を 1 件保存
LIVE_OBS=$(mesh-mem save "force-delete-test" --project gc-test | grep -oE '[0-9a-f]{32}')
echo "live obs: $LIVE_OBS"

# 2. search で 1 件あることを確認
mesh-mem search "force-delete-test" --project gc-test --limit 100

# 3. --force-id で物理削除（論理削除 = delete なしに直接）
mesh-mem gc --force-id "$LIVE_OBS"
# 期待: "物理削除完了 (obs) + broadcast purge: <ID>"

# 4. search で 0 件になったことを確認
mesh-mem search "force-delete-test" --project gc-test --limit 100
```

### Phase 4: G4 — 2-router 環境での gc 波及確認

```bash
# 事前: Office 側にも同じ obs が replication されていること確認
OBS4=$(mesh-mem save "2router-gc-test" --project gc-test | grep -oE '[0-9a-f]{32}')
sleep 10  # replication 待機
ssh office "mesh-mem search '2router-gc-test' --project gc-test --limit 100"
# 期待: Office 側でも 1 件

# 論理削除
mesh-mem delete "$OBS4"
sleep 5

# Home 側 gc 実行
mesh-mem gc --retention-days 0

# Office 側で gc 後の状態を確認（replication による tombstone 削除は起きているか？）
sleep 10
ssh office "mesh-mem search '2router-gc-test' --project gc-test --limit 100"
# NOTICE: gc_expired_tombstones は broadcast しない設計のため、
# Office 側の tombstone は残ったまま、obs のみ削除される可能性がある

# Office 側でも gc を実行して確認
ssh office "mesh-mem gc --retention-days 0"
```

注: `gc_expired_tombstones`（`--retention-days`）は broadcast なし。`physical_delete_observation`（`--force-id`）のみ broadcast wildcard delete を実行する。2-router 環境での完全な物理削除は各 PC で独立して `mesh-mem gc` を実行する必要がある。

### Phase 5: G5 — concurrent search との race 確認

```bash
# バックグラウンドで gc を繰り返す（5回）
for i in $(seq 1 5); do
  mesh-mem save "race-test $i" --project gc-test-race
done
for i in $(seq 1 5); do
  OBS=$(mesh-mem search "race-test $i" --project gc-test-race --limit 1 | grep -oE '[0-9a-f]{32}' | head -1)
  [ -n "$OBS" ] && mesh-mem delete "$OBS"
done

# gc と concurrent search を同時実行
mesh-mem gc --retention-days 0 &
for i in $(seq 1 10); do
  mesh-mem search "" --project gc-test-race --limit 100
  sleep 0.2
done
wait

# crash や例外が出ていないことを確認
```

### Phase 6: クリーンアップ

```bash
# gc-test / gc-test-race プロジェクトの残存 obs を確認
mesh-mem search "" --project gc-test --limit 100
mesh-mem search "" --project gc-test-race --limit 100

# 残っていれば削除 → gc
# （必要に応じて繰り返す）
```

---

## 5. 期待結果（仮説）

### G1 - no-op

`gc_expired_tombstones` は `_list_tombstones()` をスキャンして cutoff より古い tombstone を探す。gc-test の tombstone が存在しない状態では 0 件が返る。

### G2 - retention-days 0 での即時削除

`cutoff = now - timedelta(days=0) = now`。tombstone の `deleted_at` が now 以前なら削除対象。
直前に発行した tombstone の `deleted_at` は now より過去のため、**3 件すべてが削除される**。
DB サイズは数 KB 程度の減少が見られるはず（RocksDB の compaction タイミングにより見えにくい場合あり）。

### G3 - --force-id

`physical_delete_observation` は obs レコードを直接削除 + broadcast wildcard delete を送信。
tombstone なしでも obs が物理削除される。broadcast は Office 側にも波及するはずだが、ベストエフォート。

### G4 - 2-router gc 波及

- `gc_expired_tombstones` はローカル replica のみ削除（broadcast なし）
- Home 側 gc 後、Office 側の tombstone は残り obs は削除される（不整合状態）
- Office 側でも gc を実行することで tombstone も物理削除される
- この挙動は設計通り（各 PC で独立して gc を実行する運用を想定）

### G5 - concurrent race

`_list_tombstones()` と `search_observations()` はどちらも `session.get()` による read-only query。Zenoh の read は並行して問題ない。crash は起きないはず。

---

## 6. 中断・復旧手順

### 事前バックアップ（必須）

```bash
# gc 実行前に DB をバックアップ
cp -r ~/.local/share/zenoh-mem ~/.local/share/zenoh-mem.bak.$(date +%Y%m%d%H%M%S)

# Office 側
ssh office "cp -r ~/.local/share/zenoh-mem ~/.local/share/zenoh-mem.bak.\$(date +%Y%m%d%H%M%S)"
```

### 誤 gc でデータが消えた場合

```bash
# バックアップから復元（zenohd 停止後に実施）
systemctl --user stop mesh-mem-zenohd
cp -r ~/.local/share/zenoh-mem.bak.<timestamp> ~/.local/share/zenoh-mem
systemctl --user start mesh-mem-zenohd
```

### gc が想定外に大量削除した場合

バックアップからの復元後、`--retention-days` の値を確認する。
`--retention-days 0` は当日分も含めて削除する点に注意。

---

## 7. リスクと対策

| ID | リスク | 評価 | 対策 |
|----|--------|------|------|
| R1 | gc がテスト用以外のデータを削除する | 中 | gc-test / gc-test-race プロジェクトに限定して obs を作成。それでも `--retention-days 0` は全 tombstone が対象になる点に注意 |
| R2 | gc で全データ消失 | 低 | Phase 0 で必ず DB バックアップを取る |
| R3 | 2-router 環境で gc 後に整合性が破綻（tombstone あり obs なし）| 中 | 設計上の既知動作。両 PC で gc を実行すれば解消。Phase 4 で実測して記録 |
| R4 | retention 未実装 → テスト不可 | 対応済み | `retention_days` パラメータは実装あり（store.py:366-408）。NOTICE は live obs の自動削除が未実装な点 |
| R5 | RocksDB compaction により DB サイズが即時縮小しない | 中 | DB サイズの変化は参考値扱い。件数の変化を主判定基準とする |

---

## 8. 検収条件 (Pass criteria)

| ゴール | 基準 | 判定 |
|--------|------|------|
| G1 | gc が "0 件" を返す | Pass / Fail |
| G2 | `--retention-days 0` で 3 件の tombstone + obs が物理削除される | Pass / Fail |
| G3 | `--force-id` で live obs が物理削除される | Pass / Fail |
| G4 | 2-router で Home gc → Office 側 tombstone は残るが obs は消える（設計通り） | Pass / Observation |
| G5 | concurrent gc + search で crash / exception が出ない | Pass / Fail |

### 実測値記録テンプレート

| ゴール | 期待値 | 実測値 | 判定 |
|--------|--------|--------|------|
| G1 | purged=0 | | |
| G2 | purged=3 | | |
| G3 | obs_removed=True, broadcast 送信 | | |
| G4 | Home: purged=1, Office: tombstone 残存 | | |
| G5 | 例外なし | | |

---

## 9. 補助スクリプト案（擬似コード）

### gc 前後の sizing 比較

```bash
#!/usr/bin/env bash
# gc_sizing_diff.sh — gc 前後の RocksDB サイズと件数を記録する

set -euo pipefail

LOG="${GC_LOG:-/tmp/gc_test.log}"
PROJECT="${GC_PROJECT:-gc-test}"

snapshot() {
  local label="$1"
  echo "=== $label at $(date -Iseconds) ===" >> "$LOG"
  echo "DB size: $(du -sh ~/.local/share/zenoh-mem)" >> "$LOG"
  echo "obs count: $(mesh-mem search '' --project $PROJECT --limit 1000 | wc -l)" >> "$LOG"
  echo "total status:" >> "$LOG"
  mesh-mem status >> "$LOG"
}

snapshot "BEFORE_GC"
mesh-mem gc --retention-days 0
snapshot "AFTER_GC"
echo "diff saved to $LOG"
```

### tombstone 件数の確認（間接的）

tombstone を直接カウントする CLI はないが、以下で推定できる:

```bash
# 1. gc 前の総件数（search には tombstone は出ない）
BEFORE=$(mesh-mem search '' --limit 10000 | grep -c 'obs' || true)

# 2. gc 実行
PURGED=$(mesh-mem gc --retention-days 0 | grep -oE '[0-9]+' | head -1)

# 3. gc 後の件数
AFTER=$(mesh-mem search '' --limit 10000 | grep -c 'obs' || true)

echo "tombstone purged: $PURGED"
echo "obs before: $BEFORE, after: $AFTER (diff: $((BEFORE - AFTER)))"
```

---

## 10. 引き継ぎ事項

### 実行タスクへの前提条件

- DR 24h テストの前か後かを選択する（DR テスト後は ~1,189 件の live obs が存在する状態）
- 本テストは GC 対象を `gc-test` プロジェクトに限定するので DR データへの影響はない
- ただし `--retention-days 0` は全 project の tombstone が対象になる点に注意。DR テスト後に tombstone が大量にある状態では予期しない削除が起きる可能性がある

### 未実装機能（別 Issue 候補）

| 機能 | 状態 | 推奨対応 |
|------|------|---------|
| live obs の age-based 自動削除 | 未実装 | Issue 起票（"feat: add retention for live obs"） |
| MCP 経由の GC | 未実装 | Issue 起票（"feat: expose gc tool via MCP"） |
| gc の自動 timer | 未実装 | 外部 cron で代替（README に記載あり） |
| 2-router での gc broadcast | 設計上 `--retention-days` では行わない | 運用ガイドに「両 PC で gc を実行」と明記 |

### 実行後の報告項目

1. G1-G5 の Pass / Fail と実測値
2. DB サイズの変化（Phase 0 → Phase 2 後 → Phase 3 後）
3. 2-router gc の整合性状態（G4 の実測）
4. 別 Issue 起票推奨項目の確認

---

*設計書のみ。実行は別タスクで（gc 実行自体は短時間だが、2-router 確認に zenohd 両台の操作が必要）。mesh-mem 側コード変更なし。*
