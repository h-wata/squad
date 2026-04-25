# DR シナリオ（1日分断）テスト設計書

| 項目 | 値 |
|------|-----|
| 作成日 | 2026-04-25 |
| Issue | https://github.com/h-wata/mesh-mem/issues/5 |
| 前提タスク | TASK-102（zenoh replication semantics 解析） |
| 参照 | docs/poc-reports/SUMMARY.md |

---

## 1. 目的

24時間（以下 24h）の完全分断を想定した Disaster Recovery シナリオにより、以下を検証する。

- **運用継続性**: 分断中（Office zenohd 停止）でも Home 側が書き込みを継続できること
- **収束性**: 再接続後 30 秒以内に digest alignment が完了すること
- **データ整合性**: 分断中に Home 側へ書き込んだ全件が再接続後に Office 側へ損失なく同期されること
- **ストレージ安定性**: 24h 分の書き込み（約 1,440 件）および tombstone 数件が RocksDB に収まり、zenohd のメモリ使用量が異常増大しないこと
- **TASK-102 仮説の実証**: `initial_alignment()` が「分断時間に関係なく接続直後 ~5 秒で収束する」という解明結果が 24h 規模でも成立するか確認する

### TASK-102 との整合確認

TASK-102（SUMMARY.md セクション 8.1）により、既存の全分断テスト（30 秒 / 65 秒 / 30 分）が ESTAB+5 秒以内で収束した理由は、zenohd 起動直後に `initial_alignment()` が interval 周期（10 秒）を待たず即実行されることによると解明されている。

24h 分断では約 1,440 件がすべて cold era（保存時刻が 360 秒以上前）に入る。cold era は XOR 単一 fingerprint による比較のため、差分が 1 件でも存在すると `AlignmentQuery::Discovery` で全件転送になる可能性がある（詳細は「5. 期待結果」参照）。この挙動を実測することが本テストの追加目的である。

---

## 2. 検証目標 (Goals)

| ID | ゴール | 判定方法 |
|----|--------|---------|
| G1 | Office 側 zenohd を 24h 停止しても Home 側で書き込みを継続できる | nohup writer の終了コードが 0 かつ生成された obs 件数が 1,440 件 |
| G2 | 24h 後の再接続で 30 秒以内に digest alignment が完了する | 両側の `mesh-mem search "dr-test" --limit 2000` の件数が一致した時刻を記録 |
| G3 | 双方向で過去 24h 分の追加データが完全同期する（loss 0） | 両側の全 obs ID の diff が空（セクション 4 Phase 4 手順参照） |
| G4 | RocksDB サイズが想定範囲に収まる（Home, Office） | `du -sh ~/.local/share/zenoh-mem` の値が合計 50 MB 未満 |
| G5 | NTP drift があっても収束する（オプション） | 別 Issue で扱う（セクション 8 参照） |

---

## 3. 環境とデータ

### ホスト構成

| 役割 | IP | zenohd config |
|------|----|---------------|
| Home | 192.168.134.28 | config/zenohd_home.json5 |
| Office | 192.168.128.12 | config/zenohd_office.json5 |

両ホストとも zenohd v1.9.0 (apt 管理)、zenoh-backend-rocksdb v1.9.0。

### Zenoh replication 設定（両側完全一致）

| パラメータ | 値 | 備考 |
|-----------|----|----|
| interval | 10.0 秒 | |
| sub_intervals | 5 | → sub_interval 粒度 = 2 秒 |
| hot | 6 | → hot era = 現在〜60 秒前 |
| warm | 30 | → warm era = 60〜360 秒前 |
| cold | - | → cold era = 360 秒より古いデータ |
| propagation_delay | 250 ms | |
| key_expr | mem/** | |

### データ格納先

`~/.local/share/zenoh-mem/agent_mem/`（ZENOH_BACKEND_ROCKSDB_ROOT に依存）

注: plan.md の推奨は `~/.local/share/mesh-mem` だが実運用では `zenoh-mem` で運用されている既知課題 (SUMMARY.md NOTICE N-1)。

### 初期データ方針

テスト開始前に既存データ（PoC で蓄積済みの ~30〜100+ 件: smoke, scale-bench 等）を **保持したまま** 実施する。dr-test プロジェクトを専用の project タグで分離するため、既存データとの混在は問題にならない。

ただし、件数確認は `--project dr-test` または tag フィルタで絞り込む。

### 24h 書き込みパターン

| 項目 | 値 |
|------|-----|
| 対象ホスト | Home 側のみ（Office は zenohd 停止中） |
| 頻度 | 1 obs / 分 × 1,440 分 = 合計 1,440 件 |
| project | dr-test |
| payload 例 | `"dr-test obs <N> <ISO8601時刻>"` (約 40〜60B) |
| tags | ["dr-test"] |
| 書き込み方法 | nohup run_dr_writer.sh（後述） |
| 頻度の根拠 | hot/warm 境界（60 秒 / 360 秒）を確実に通過し、cold era のデータを多数蓄積するため |

### 環境変数

```bash
export MESH_MEM_AGENT_FAMILY=claude
export MESH_MEM_CLIENT_ID=claude-code
# または
export MESH_MEM_AGENT_FAMILY=dr-test
export MESH_MEM_CLIENT_ID=dr-writer
```

MESH_MEM_AGENT_FAMILY / MESH_MEM_CLIENT_ID が未設定の場合、`[unknown/unknown]` で記録される（既知挙動）。新規ターミナルでは必ず設定すること (SUMMARY.md NOTICE N-4)。

---

## 4. シナリオ手順

### Phase 0: 事前準備（約 5 分）

```bash
# 両側の zenohd が起動していること確認
# Home
systemctl --user status mesh-mem-zenohd  # または ps aux | grep zenohd

# Office（SSH）
ssh office "ps aux | grep zenohd"

# baseline 件数を記録（dr-test プロジェクト）
mesh-mem search "dr-test" --project dr-test --limit 2000
# → 0 件のはず（初回実行時）

# DB サイズ baseline を記録
du -sh ~/.local/share/zenoh-mem
# Home と Office 双方で実施

# zenohd のメモリ使用量 baseline を記録
ps -p $(pgrep zenohd) -o pid,rss,vsz
# 参考値: Tier-1 ベンチ後で ~29.7 MB (29,704 KB) (SUMMARY.md セクション 4.7)

# NTP 確認（必須）
chronyc tracking | grep -E 'Stratum|Last offset|RMS offset|System time'
# Last offset > 100ms の場合はテスト中止 (R2)
```

### Phase 1: 分断開始（t=0）

```bash
# Office 側で zenohd を停止
ssh office "sudo systemctl stop mesh-mem-zenohd"
# または
ssh office "sudo kill -TERM \$(pgrep zenohd)"

# 開始時刻を記録
date -Iseconds > /tmp/dr_test_start_time.txt
echo "分断開始: $(cat /tmp/dr_test_start_time.txt)"

# Office 側から mesh-mem save が失敗することを確認
ssh office "mesh-mem save 'connectivity-check after split' --project dr-test-check 2>&1"
# → 接続エラーが出ること（Zenoh ルーター未起動）を確認
```

### Phase 2: 24h 中の書き込み（t=0 〜 t=24h）

```bash
# Home 側で writer を nohup 起動
nohup bash run_dr_writer.sh > /tmp/dr_writer.log 2>&1 &
echo "Writer PID: $!"

# 生存確認コマンド
ps aux | grep run_dr_writer

# 観測ポイント（6h, 12h, 18h, 24h ごとに実施）
# 件数確認（CLI default limit=20 に注意 — 必ず --limit 2000 を指定）
mesh-mem search "dr-test" --project dr-test --limit 2000 | wc -l
# または
mesh-mem search "dr-test" --project dr-test --limit 2000

# DB サイズ確認
du -sh ~/.local/share/zenoh-mem

# zenohd メモリ確認
ps -p $(pgrep zenohd) -o pid,rss,vsz

# writer ログ確認
tail -20 /tmp/dr_writer.log
```

**観測記録テンプレート（6h ごと）**:

| 時刻 | Home 側件数 | Home DB サイズ | zenohd RSS | writer 生存 |
|------|-----------|-------------|-----------|-----------|
| t=6h | | | | |
| t=12h | | | | |
| t=18h | | | | |
| t=24h | | | | |

### Phase 3: 再接続（t=24h）

```bash
# Office 側 zenohd を起動
ssh office "sudo systemctl start mesh-mem-zenohd"

# TCP 接続確立を確認
ssh office "ss -tn | grep 7447"
# ESTAB が表示されるまで待機

# 収束ポーリング（再接続後 5 秒ごとに 60 秒間）
for i in $(seq 1 12); do
  echo -n "$(date -Iseconds) Home: "
  mesh-mem search "dr-test" --project dr-test --limit 2000 | wc -l
  echo -n "$(date -Iseconds) Office: "
  ssh office "mesh-mem search 'dr-test' --project dr-test --limit 2000 | wc -l"
  sleep 5
done

# 収束時刻を記録（両側件数が 1,440 件に揃った時刻）
date -Iseconds > /tmp/dr_test_converge_time.txt
```

注: `--limit 2000` は CLI の default limit=20 問題 (SUMMARY.md セクション 5.4) を回避するために必須。

### Phase 4: 整合性検証

```bash
# Home 側で dr-test プロジェクトの全 obs ID 一覧を取得
mesh-mem search "dr-test" --project dr-test --limit 2000 \
  | grep -oE '[0-9a-f]{32}' | sort > /tmp/dr_home_ids.txt

# Office 側で同様に取得（SSH 経由）
ssh office "mesh-mem search 'dr-test' --project dr-test --limit 2000 \
  | grep -oE '[0-9a-f]{32}' | sort" > /tmp/dr_office_ids.txt

# diff で完全一致確認
diff /tmp/dr_home_ids.txt /tmp/dr_office_ids.txt
# 出力が空なら G3 PASS

# tombstone 同期確認（5 件を Home 側で削除）
for i in 1 2 3 4 5; do
  OBS_ID=$(head -n $i /tmp/dr_home_ids.txt | tail -n 1)
  mesh-mem delete "$OBS_ID"
done

# 5 秒待機後、両側で削除されていることを確認
sleep 5
diff <(mesh-mem search "dr-test" --project dr-test --limit 2000 | grep -oE '[0-9a-f]{32}' | sort) \
     <(ssh office "mesh-mem search 'dr-test' --project dr-test --limit 2000 | grep -oE '[0-9a-f]{32}' | sort")
```

---

## 5. 期待結果（仮説）

### 5.1 収束時間の仮説

TASK-102 の解析（SUMMARY.md セクション 8.1）により:

```
zenohd start
  → spawn_digest_publisher() / spawn_digest_subscriber() / spawn_aligner_queryable()
  → scouting delay (~500ms)
  → initial_alignment() 即実行
  → AlignmentQuery::Discovery で全件問い合わせ
  → 対向応答 → ストレージ適用 → 収束
```

**分断時間は initial_alignment() のタイミングに影響しない**。Office zenohd が起動した瞬間から ~0.5 秒後に Discovery が走る。

### 5.2 24h 分断特有の挙動仮説

24h のデータは保存時刻から 360 秒以上経過しているため、**全件が cold era**（XOR 単一 fingerprint）に入る。

cold era では：
- 定期 digest 比較: 全件を単一の XOR fingerprint で比較 → 1 件でも差分があれば全件 Discovery
- `initial_alignment()` 時: Discovery クエリで全件（1,440 件）を一括転送

このため、既存テスト（最大 30 件）と比べると転送データ量が 48 倍になる。収束時間は：
- 転送量が小さい場合（LAN 内）: ~5 秒（既存テストと同等）
- 転送量が大きい場合（1,440 件 × 約 50B/件 = 約 72 KB）: ネットワーク速度と zenohd の書き込みスループットに依存

**総合仮説: G2 の収束時間は 5〜30 秒**（ただし 60 秒を超える場合は詳細調査が必要）。

収束が階段状（~10 秒ずつ）に見える可能性：cold era の XOR fingerprint が全件一致するまで Discovery のラウンドトリップを繰り返す場合。

### 5.3 G4 DB サイズの仮説

| 項目 | 計算 | 推定値 |
|------|------|------|
| 新規 obs 件数 | 1,440 件 | |
| 1 件あたり payload | 約 60B（key_expr + content） | |
| RocksDB オーバーヘッド | 約 3〜5 倍（WAL + SSTable） | |
| 新規増分 | 1,440 × 60B × 5 | 約 432 KB |
| 既存データ（基準値） | Tier-1 後: 320 KB | |
| 合計推定 | 約 750 KB〜1 MB | |

zenohd の RSS は Tier-1（100 件、29.7 MB）を基準に、~31〜35 MB 程度に収まると予測（大幅な増大がなければ G4 PASS）。

---

## 6. 中断・復旧手順

### Writer 異常終了時

```bash
# Writer 生存確認
ps aux | grep run_dr_writer

# 途中終了した場合はログで何件まで書き込んだか確認
tail /tmp/dr_writer.log

# 残り件数から再開する場合（Phase 0 から再実行は不要、続きから実行）
# ただし obs ID の連番が途中から始まることに注意
nohup bash run_dr_writer_resume.sh <START_N> > /tmp/dr_writer_resume.log 2>&1 &
```

### 異常終了で全体やり直しの場合

Phase 0 に戻り、以下を実施:
1. `mesh-mem gc` で dr-test プロジェクトのデータを削除（または DB リセット）
2. 開始時刻を更新
3. writer を再起動

### DB バックアップ手順

```bash
# 実行前にバックアップ
cp -r ~/.local/share/zenoh-mem ~/.local/share/zenoh-mem.bak.$(date +%Y%m%d%H%M%S)
```

### zenohd 状態確認コマンド

```bash
# Home 側 zenohd プロセス確認
ps aux | grep zenohd
ps -p $(pgrep zenohd) -o pid,rss,vsz

# zenohd ログ確認（systemd の場合）
journalctl --user -u mesh-mem-zenohd --since "10 minutes ago"
```

---

## 7. リスクと対策

| ID | リスク | 評価 | 対策 |
|----|--------|------|------|
| R1 | Disk full | 低（推定増分は 1 MB 程度） | Phase 0 で既存 DB サイズを確認し、空き容量が 100 MB 以上あることを確認 |
| R2 | NTP drift > 100ms | 低〜中 | Phase 0 で `chronyc tracking` を確認。`Last offset > 100ms` の場合はテスト中止。HLC のタイムスタンプ競合が起きると digest 比較が誤る可能性 |
| R3 | 物理ネットワーク障害（想定外）| 中 | `run_dr_writer.sh` が Zenoh CONNECT エラーで停止した場合、ログで検出。手動で復旧後に writer を再開（Phase 2 中断・復旧手順参照） |
| R4 | zenohd メモリリーク | 低 | 6h ごとに `ps -p $(pgrep zenohd) -o rss` を記録。24h で RSS が 200 MB を超えた場合は BLOCKER 扱いで Issue 起票 |
| R5 | CLI default limit=20 による件数確認ミス | 確実に発生 | 全ポーリングコマンドに `--limit 2000` を付ける（SUMMARY.md セクション 5.4 の既知問題） |
| R6 | Office 側 writer が誤って起動される | 中 | Office zenohd を停止する際に、writer スクリプトが Office 側にないことを確認する |

---

## 8. 検収条件 (Pass criteria)

### 必須 (G1-G4 全達成で Pass)

| ゴール | 基準 | 判定 |
|--------|------|------|
| G1 | Home writer が 1,440 件すべてを write 完了（ログで確認） | Pass / Fail |
| G2 | 再接続後 **30 秒以内** に両側件数が一致 | Pass / Fail |
| G3 | 全 obs ID の diff が空（loss 0） | Pass / Fail |
| G4 | DB サイズが合計 50 MB 未満、zenohd RSS が 200 MB 未満 | Pass / Fail |

### 基準逸脱時の対応

| 状況 | 対応 |
|------|------|
| G2 が 60 秒超 | 詳細調査タスクを発行。収束ログ（polling 記録）を添付 |
| G3 で loss > 0 | BLOCKER 扱い。CHANGELOG/Issue 起票 |
| G4 DB サイズが 50 MB 超 | NOTICE 扱い。実測値と計算根拠を記録 |
| G4 zenohd RSS が 200 MB 超 | BLOCKER 扱い。メモリリーク調査タスクを発行 |

### オプション (G5)

NTP drift（>100ms）があっても収束する: 本テストのスコープ外。別 Issue（NTP skew test）へ。

---

## 9. 補助スクリプト案（擬似コード）

実装は別タスクで行う。本書ではインターフェースと仕様のみ定義する。

### run_dr_writer.sh

```bash
#!/usr/bin/env bash
# 1,440 回（1 obs/分）を Home 側で書き込む
# 実行前提: mesh-mem が PATH に存在し、MESH_MEM_AGENT_FAMILY などが設定済み

set -euo pipefail

LOG_FILE="${DR_LOG:-/tmp/dr_writer.log}"
TOTAL="${DR_TOTAL:-1440}"
INTERVAL="${DR_INTERVAL:-60}"  # 秒

for i in $(seq 1 "$TOTAL"); do
  TIMESTAMP=$(date -Iseconds)
  mesh-mem save "dr-test obs $i $TIMESTAMP" \
    --project dr-test \
    --tags dr-test
  echo "[$(date -Iseconds)] obs $i/$TOTAL saved" >> "$LOG_FILE"
  sleep "$INTERVAL"
done

echo "[$(date -Iseconds)] writer completed: $TOTAL obs" >> "$LOG_FILE"
```

仕様:
- 環境変数 `DR_TOTAL` で件数変更可（デフォルト 1440）
- 環境変数 `DR_INTERVAL` で間隔変更可（デフォルト 60 秒）
- 各 obs の書き込み完了を `$LOG_FILE` に追記（生存確認用）
- Zenoh 接続エラー時は `set -e` により即終了（R3 対応）

---

## 10. 実行タスクへの引き継ぎ事項

### スケジュール

24h 拘束されるため、開始タイミングは **業務時間外または休日** を推奨。
- 平日夜 23:00 開始 → 翌日 23:00 完了（翌日終業後に結果確認可能）
- 土曜日朝 09:00 開始 → 日曜日 09:00 完了

### Worker 割り当て方針

```
T=0:   分断開始 + writer 起動（Worker が手動操作）
T=6h:  観測チェック（Dispatcher BG または cron）
T=12h: 観測チェック
T=18h: 観測チェック
T=24h: 再接続 + 収束ポーリング + Phase 4 整合性検証
```

「裏で nohup 実行 + 6h ごとに観測」形式での実施が現実的。

### 引き継ぎ情報

| 項目 | 内容 |
|------|------|
| 設計根拠 | TASK-102 の initial_alignment() 解析（SUMMARY.md セクション 8.1） |
| 参照ファイル | docs/poc-reports/SUMMARY.md / memory/project_zenoh_replication_semantics.md |
| 既知ハマりポイント | CLI `--limit 2000` を忘れると件数が 20 件で打ち切られる |
| 既知ハマりポイント | 環境変数 MESH_MEM_AGENT_FAMILY 等は新ターミナルで再設定が必要 |
| DB パス | `~/.local/share/zenoh-mem/agent_mem/`（plan.md 記載の mesh-mem とは異なる） |
| 収束時間の仮説 | 5〜30 秒（TASK-102 理論値）。60 秒超で詳細調査タスク発行 |

### Phase 4 完了後の報告項目

1. G1〜G4 の Pass / Fail と測定値
2. 収束時間（再接続から両側件数一致まで）
3. 観測ログ（6h ごとの件数・DB サイズ・RSS）
4. diff 結果（空 = G3 PASS）
5. 仮説との差異（5〜30s 仮説に対する実測値）
6. 次フェーズへの推奨事項

---

*設計書のみ。スクリプトの実装・テスト実行は別タスクで実施する。*
