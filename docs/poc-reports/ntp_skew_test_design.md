# NTP Skew 境界テスト 設計書

| 項目 | 値 |
|------|-----|
| Issue | https://github.com/h-wata/mesh-mem/issues/5 |
| 対象 AC | NTP skew boundary (>100 ms offset) behaviour |
| 作成日 | 2026-04-25 |
| 作成者 | TASK-116 (worker2) |
| ステータス | 設計書のみ（実行は別タスク） |

---

## 1. 目的

mesh-mem の Zenoh レプリケーションは、クライアント側ホストの wall clock に依存した
`created_at` を observation に付与する（`src/mesh_mem/models.py:49`、`_utc_now_iso()`）。
また zenoh router は自機の wall clock を使って replication era (hot/warm/cold) の
境界を評価する（`zenoh-plugin-storage-manager/src/replication/configuration.rs`）。

2台のホスト間に NTP skew が存在すると、以下の3経路で障害が発生しうる：

- **経路 A**: `created_at` の客観的順序が崩れ、`search --since-iso` フィルタが
  対向ホスト保存 obs を取りこぼす（または未来扱いで早期ヒット）
- **経路 B**: 2台の zenoh router が同一 observation を **異なる era** に分類し、
  digest 比較の粒度不整合によって alignment コスト・収束遅延が増加する
- **経路 C**: tombstone TTL / `mesh-mem gc` の retention 判定が skew 分ずれ、
  tombstone の有効期限評価が host ごとに異なる

本テストの目的は：
1. skew 値 100ms / 1s / 10s / 60s / 600s の各境界で経路 A/B/C のどれが顕在化するかを特定する
2. 安全運用の skew 上限（Pass 境界）を実測で決める
3. 致命的破綻を再現した場合は BLOCKER として Issue 起票する

---

## 2. 検証目標 (Goals)

| ID | 目標 | 合格基準 |
|----|------|---------|
| G1 | skew=100ms で通常動作に影響なし | save/search/sync 全て正常 |
| G2 | skew=1s でも replication が破綻しない | alignment が 30秒以内に収束 |
| G3 | skew=10s で経路 A（since_iso フィルタ外れ）が観測できるか | 期待: 外れる |
| G4 | skew=60s で era 不整合（hot vs warm）が観測できるか | 期待: era 分類が食い違う |
| G5 | skew=600s で何が壊れるかを記録する | 破綻記録（Pass/Fail 問わず） |

G3〜G5 の「期待: 外れる/食い違う」は仮説検証であり、
**外れた = FAIL ではなく、仮説確認 = expected behaviour** として記録する。
予期しない破綻（alignment 不収束、データ消失、zenohd crash など）が
BLOCKER 候補となる。

---

## 3. 環境とツール

### 3.1 前提

- **Home ホスト**: 時計操作禁止（本番 zenohd 稼働中）
- **Office ホスト**: 時計操作対象。物理 Office PC または Office 役 VM（推奨）
- **推奨隔離方法**: LXC コンテナまたは Podman rootless コンテナで Office 側 zenohd を起動し、
  コンテナ内の時計だけをシフトする（ホスト OS の NTP を汚染しない）

### 3.2 ツール

| ツール | 用途 |
|--------|------|
| `timedatectl set-ntp false` | systemd-timesyncd を停止して自動修正を無効化 |
| `sudo date -s "$(date -d "+N seconds")"` | 時計を N 秒進める |
| `chronyc -a 'makestep 0.1 -1'` | 強制 NTP 再同期（復元用） |
| `systemctl start systemd-timesyncd` | timesyncd 再開（復元用） |
| `mesh-mem search "" --project ntp-skew-test` | 各 Case での obs 取得確認 |
| `zenohd -c config/zenohd_office.json5` | Office 側 zenohd 起動 |

### 3.3 補助スクリプト（設計案）

以下は設計書レベルのスクリプト案。実行は別タスクで実装・検証する。

**set_skew.sh**:
```bash
#!/bin/bash
# 使い方: sudo ./set_skew.sh <seconds>
sudo systemctl stop systemd-timesyncd
sudo date -s "$(date -d "+$1 seconds" --utc --iso-8601=seconds)"
echo "skew set: +$1 seconds. current: $(date --iso-8601=seconds)"
```

**restore_clock.sh**:
```bash
#!/bin/bash
sudo systemctl start systemd-timesyncd
sudo chronyc -a 'makestep 0.1 -1' 2>/dev/null \
  || sudo systemctl restart chronyd 2>/dev/null \
  || echo "WARNING: NTP sync command failed — check clock manually"
echo "clock restored: $(date --iso-8601=seconds)"
```

---

## 4. シナリオ手順（5 ケース）

各 Case の構造は共通：

```
Phase 0: Office 側で時計をシフト（set_skew.sh）
Phase 1: Home / Office で交互に obs を 5件ずつ save（project=ntp-skew-test）
Phase 2: 両側で mesh-mem search、観測項目を記録
Phase 3: Office 側で時計を復元（restore_clock.sh）
Phase 4: 復元後の search で再確認
```

各 Case の推定所要時間: 約 5 分

### Case 1: skew = 100ms

```bash
# Phase 0 (Office)
sudo ./set_skew.sh 0.1   # 100ms = 0.1 秒

# Phase 1 (Home + Office 交互)
for i in $(seq 1 5); do
  ZENOH_CONNECT=tcp/127.0.0.1:7447 mesh-mem save "home obs $i" --project ntp-skew-test --tags skew100ms,home
  # (Office 側で同様)
done

# Phase 2: search で created_at 順確認
ZENOH_CONNECT=tcp/127.0.0.1:7447 mesh-mem search "" --project ntp-skew-test --limit 20
```

### Case 2: skew = 1s

```bash
# Phase 0
sudo ./set_skew.sh 1
# Phase 1-4 は Case 1 と同様（タグ: skew1s）
```

### Case 3: skew = 10s

```bash
# Phase 0
sudo ./set_skew.sh 10
# Phase 2 追加確認: since_iso フィルタ
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SINCE=$(date -d "$NOW - 5 seconds" -u +%Y-%m-%dT%H:%M:%SZ)
mesh-mem search "" --project ntp-skew-test --since-iso "$SINCE" --limit 20
# 経路 A 検証: Office obs が since_iso 範囲外に落ちていないか確認
```

### Case 4: skew = 60s（era 境界跨ぎ）

```bash
# Phase 0
sudo ./set_skew.sh 60
# Phase 2 追加確認: zenohd ログで era 分類の差異を観察
# Home zenohd ログ: journalctl --user -u mesh-mem-zenohd -n 50 -f
# Office zenohd ログ: 同様
# 観察点: "hot" / "warm" の era ラベルが両側で食い違うか
```

### Case 5: skew = 600s（破綻確認、optional）

```bash
# Phase 0
sudo ./set_skew.sh 600
# Phase 2 追加確認: alignment の完了時間を計測
# zenohd 起動後に alignment が収束するまでの秒数を記録
# 破綻基準: 120秒待っても収束しない、または zenohd が crash する
```

---

## 5. 期待結果（仮説）

各 Case での経路 A/B/C 顕在化の予測：

| Case | skew | 経路 A (created_at) | 経路 B (era 不整合) | 経路 C (TTL) | 総合予測 |
|------|------|--------------------|--------------------|--------------|---------|
| 1 | 100ms | 影響なし | 影響なし | 影響なし | 正常 |
| 2 | 1s | created_at にずれ記録されるが機能は正常 | 影響なし | 影響なし | 正常 |
| 3 | 10s | `since_iso=now-5s` フィルタで Office obs が漏れる可能性 | 境界付近の obs が era 食い違う可能性 | 微小影響 | 軽微異常 |
| 4 | 60s | since_iso フィルタで確実に影響 | hot (0-60s) / warm (60-360s) の era 分類が逆転する可能性 | gc retention 判定に影響 | 機能劣化 |
| 5 | 600s | 大幅外れ | era 計算崩壊の可能性 | gc 誤判定の可能性 | 破綻候補 |

**補足（TASK-102 との整合）**:
- `initial_alignment()` は scouting 完了後即実行（~500ms）のため、
  zenohd 接続直後の alignment 自体は skew の影響を受けにくい
- era 不整合は「両側が常時接続中に行う定期 digest 比較（interval=10s ごと）」で
  問題になる。接続直後の一発 alignment では顕在化しにくい
- skew >= 60s で定期 digest の era 判定が食い違い、
  同一 obs の digest 粒度がずれると alignment loop が発散しうる（仮説）

---

## 6. 観測項目

各 Case で以下を記録する：

| # | 観測項目 | 確認方法 |
|---|---------|---------|
| O1 | zenohd 起動直後のログ（era 警告・エラー） | `journalctl --user -u mesh-mem-zenohd -n 100` |
| O2 | `mesh-mem search` の created_at 順序（Home/Office 側各1回） | `mesh-mem search "" --project ntp-skew-test --limit 20` |
| O3 | `since_iso` フィルタの動作（Case 3 以上） | `mesh-mem search "" --since-iso <now-5s>` |
| O4 | Home から見た Office obs の `created_at` と実測保存時刻の差 | O2 結果と `date` コマンド照合 |
| O5 | alignment 完了時間（zenohd 起動から最初の obs が見えるまで） | `mesh-mem search` でポーリングして計測 |
| O6 | tombstone 操作時の挙動（Case 3 以上でオプション） | `mesh-mem delete <obs_id>` 後に `mesh-mem search` |
| O7 | zenohd の CPU/RSS（Case 4/5、alignment loop 発散の兆候確認） | `ps aux --pid <zenohd_pid>` |

---

## 7. リスクと対策

| ID | リスク | 対策 |
|----|--------|------|
| R1 | systemd-timesyncd が自動で時計を戻す | Phase 0 で `timedatectl set-ntp false` を実行してから `date -s` でシフト |
| R2 | cron / systemd timer が誤発火する | 各 Case を 5 分以内に完了させる。`mesh-mem gc` の cron は事前に確認 |
| R3 | SSH セッションが timeout する | tmux / screen でアタッチして実行。最大 Case 5 分 |
| R4 | 時計の復元失敗 | Phase 3 で `restore_clock.sh` を必ず実行。失敗した場合は手動で `sudo ntpdate pool.ntp.org` |
| R5 | Home ホストへの誤操作 | スクリプトに `hostname` チェックを入れ、Home ホスト名の場合は abort |
| R6 | zenohd crash でデータ消失 | ベンチデータ（scale-bench 1000件）が存在する状態で実施する場合は事前バックアップ |
| R7 | Office ホストが LXC/VM でない場合の影響範囲 | 物理 Office で実施する場合は各 Case を独立した日に分けて実施（Case 4/5 は別日推奨） |

---

## 8. 検収条件 (Pass criteria)

各 Case 完了後に以下の表を埋める：

| Case | skew | O1 (ログ) | O2 (順序) | O3 (since_iso) | O5 (align時間) | 総合 |
|------|------|-----------|-----------|----------------|----------------|------|
| 1 | 100ms | | | N/A | | |
| 2 | 1s | | | N/A | | |
| 3 | 10s | | | | | |
| 4 | 60s | | | | | |
| 5 | 600s | | | | | （optional）|

**合格基準**:
- Case 1（skew=100ms）: 全観測項目で異常なし → G1 Pass
- Case 2（skew=1s）: alignment 収束 30秒以内 → G2 Pass
- Case 3-4: 経路 A/B が仮説通りに顕在化 → G3/G4 確認（Pass/Fail は仮説一致で判断）
- 致命的破綻（alignment 不収束、データ消失、zenohd crash）が再現可能 → BLOCKER Issue 起票

---

## 9. 補助スクリプト案

Section 3.3 に記載。実装は別タスクで行う。

**追加案: obs_check.sh（観測 O2/O3 自動化）**:
```bash
#!/bin/bash
# 引数: skew 値（ログ記録用）
SKEW="$1"
PROJECT="ntp-skew-test"
echo "=== skew=$SKEW obs check $(date --iso-8601=seconds) ==="
ZENOH_CONNECT=tcp/127.0.0.1:7447 mesh-mem search "" \
  --project "$PROJECT" --limit 20 2>&1
echo "=== since_iso check (now-30s) ==="
SINCE=$(date -d "now - 30 seconds" -u +%Y-%m-%dT%H:%M:%SZ)
ZENOH_CONNECT=tcp/127.0.0.1:7447 mesh-mem search "" \
  --project "$PROJECT" --since-iso "$SINCE" --limit 20 2>&1
```

---

## 10. 引き継ぎ事項

### 実行推奨順序

- **同日中に実行可能**: Case 1〜3（skew 100ms / 1s / 10s）
  - 影響軽微。Office PC または LXC コンテナで実施
  - restore_clock.sh で NTP 同期後は通常運用に戻る
- **別日推奨**: Case 4〜5（skew 60s / 600s）
  - 時刻ずれが大きく cron / systemd timer への影響が大きい
  - LXC/Podman での隔離が特に推奨
  - 実施前日に crontab を確認・一時無効化する

### 事前確認チェックリスト

- [ ] Office 側の zenohd バージョンが v1.9.0 以上
- [ ] `mesh-mem gc` の cron が Office 側に設定されている場合は一時 disable
- [ ] LXC/Podman 環境での Office 役コンテナ起動手順を確認
- [ ] Home 側の zenohd PID と DB サイズを記録（実行前）

### BLOCKER 候補仮説

実行前の仮説として、以下が BLOCKER になる可能性がある（実行で確認）：

| 仮説 | 顕在化条件 | 重要度 |
|------|-----------|--------|
| skew >= 10s で `since_iso` フィルタが対向 obs を確実に取りこぼす | G3 で確認 | IMPORTANT |
| skew >= 60s で alignment loop が定期的に大量通信を発生させる | G4 で確認 | BLOCKER 候補 |
| skew >= 600s で zenohd が era 計算でパニック・クラッシュする | G5 で確認 | BLOCKER 候補 |

### 関連ファイル

- `src/mesh_mem/models.py:28-30` — `_utc_now_iso()` の実装（client 側 created_at 生成）
- `memory/project_zenoh_replication_semantics.md` — hot/warm/cold era 定義と initial_alignment の動作
- `docs/poc-reports/SUMMARY.md §8.1` — TASK-102 の era 計算解明
- `scripts/bench_bulk_save.py` — save/search 性能計測スクリプト（Case 実行時の性能比較基準）
