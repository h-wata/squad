# プロジェクトダッシュボード

**最終更新**: 2026-04-27 15:43

## 現在のステータス

| ワーカー | Pane | 状態 | 現在のタスク |
|---------|------|------|------------|
| Worker 1 | 1 | 作業中 | TASK-127 (#9 Phase 2: MCP ツール拡張) |
| Worker 2 | 2 | 待機中 | - |
| Worker 3 | 3 | 待機中 | - |

## DR 24h テスト 完了（24.45h）

| Goal | 結果 | 備考 |
|------|------|------|
| G1 writer 1440 件 | PARTIAL (1192 件) | writer 起動 4h40m 遅延、システム障害ではない |
| G2 収束 30s 以内 | **FAIL** (実測 97-282s) | cold-era step-function 一括転送、設計仮説 §5.2 の通り |
| G3 loss 0 | **PASS** | 両側 md5 完全一致 (1194件) |
| G4 DB<50MB / RSS<200MB | **PASS** | DB 26MB / RSS 82MB |
| Tombstone 5件 | **PASS** | 5s 以内に Office 伝播、md5 再一致 |

詳細: docs/poc-reports/raw/TASK-119-dr-1day-result.yaml

## アクティブタスク

なし

## GitHub Issues (mesh-mem 現状)

### クローズ済み
- ~~[#1 Unify default limit across CLI/MCP/API](https://github.com/h-wata/mesh-mem/issues/1)~~ → c0f5194
- ~~[#2 Align ZENOH_BACKEND_ROCKSDB_ROOT path](https://github.com/h-wata/mesh-mem/issues/2)~~ → 2a39ff5
- ~~[#3 Provide systemd override for zenohd](https://github.com/h-wata/mesh-mem/issues/3)~~ → c4cfaee
- ~~[#4 Add fastmcp as test dependency](https://github.com/h-wata/mesh-mem/issues/4)~~ → e5768c4 (47 passed / 0 skipped)

### オープン
- [#5 Expand benchmark and DR test coverage](https://github.com/h-wata/mesh-mem/issues/5) (Tier-2/3 ✅、DR ✅、NTP partial、MCP/GC 設計済、Case 4-5 実機 defer)
- [#6 Prepare v0.2.0 release](https://github.com/h-wata/mesh-mem/issues/6) (#5/#7 解消後)
- [#7 search_observations: server-side filtering](https://github.com/h-wata/mesh-mem/issues/7) ← Tier-3 で発覚 (16k obs で 2.2s)
- [#8 --project filter race after zenohd restart](https://github.com/h-wata/mesh-mem/issues/8) ← DR 24h で発見
- [#9 Observation 構造化（長記憶向け）](https://github.com/h-wata/mesh-mem/issues/9) ← user 提起
- [#10 NTP setup advisory (chrony recommended)](https://github.com/h-wata/mesh-mem/issues/10) ← NTP skew test で発見
- [#11 Add --project filter to mesh-mem gc](https://github.com/h-wata/mesh-mem/issues/11) ← GC test で発見

### 解消済み（Codex レビュー対応、push 済み）
- ~~BLOCKER: config 実 IP ハードコード~~ → commit 36c12b7
- ~~IMPORTANT: test_search_respects_since_iso_filter date-dependent~~ → commit 40b1fe9

## 待機中タスク

なし

## 完了タスク

| タスクID | 担当 | タイトル | 完了日時 |
|---------|------|---------|---------|
| TASK-126 | Worker 3 | SUMMARY.md §8.5 GC 結果追記（+92 行）、§3/§6 N-7 更新 | 2026-04-27 16:00 |
| TASK-125 | Worker 2 | Issue #5 GC/retention 実機実行（G1-G3 PASS、G4 defer、新 Issue #11 起票） | 2026-04-27 15:51 |
| TASK-124 | Worker 1 | Issue #9 Phase 1: Observation schema 拡張（6 フィールド、5 tests、commit 7a5ccd3） | 2026-04-27 15:53 |
| TASK-123 | Worker 2 | SUMMARY.md §8.4 NTP skew 結果追記（+97 行）、§3/§6 更新 | 2026-04-27 15:42 |
| TASK-122 | Dispatcher | Issue #5 NTP skew Case Re-1/2/3 実施（G2/G3 PASS、G1 NOT_VERIFIABLE、G4/G5 DEFERRED） | 2026-04-27 15:40 |
| TASK-121 | Worker 3 | Issue #5 GC/retention 設計書（413 行、実装事実調査済み） | 2026-04-27 14:45 |
| TASK-120 | Worker 2 | SUMMARY.md §8.3 DR 24h 追記（+94 行） | 2026-04-27 09:05 |
| TASK-119 | Dispatcher | Issue #5 DR 24h Phase 3-4: 再接続 + ID diff + tombstone（G3/G4/Tombstone PASS、G2 仮説どおり FAIL） | 2026-04-27 08:58 |
| TASK-118 | Worker 1 | Issue #5 DR 24h Phase 2: run_dr_writer.sh + nohup 起動（PID 2799943） | 2026-04-26 13:01 |
| TASK-117 | Worker 3 | Issue #5-E: MCP integration smoke 設計書（5 Case、305 行） | 2026-04-25 18:15 |
| TASK-116 | Worker 2 | Issue #5-D: NTP skew 100ms-600s 設計書（298 行） | 2026-04-25 18:15 |
| TASK-115 | Worker 1 | Issue #5-C: Tier-3 ベンチ実行（save 42k ops/s、search 16k 件で 2.2s → 新 Issue #7） | 2026-04-25 18:15 |
| TASK-114 | Worker 3 | Issue #5-B: DR(24h) 分断テスト設計書（447行、TASK-102 整合済み） | 2026-04-25 18:10 |
| TASK-113 | Worker 2 | Issue #5-A: Tier-2 ベンチ合格（37k ops/s、search 50-87ms、1000件 intact） | 2026-04-25 18:07 |
| TASK-112 | Worker 1 | Issue #3: systemd override example.conf + README 手順（commit c4cfaee） | 2026-04-25 18:12 |
| TASK-111 | Worker 3 | Issue #4: fastmcp を test extras に追加（47 passed / 0 skipped、commit e5768c4） | 2026-04-25 17:12 |
| TASK-110 | Worker 2 | Issue #2: rocksdb path 統一 + migration note（commit 2a39ff5） | 2026-04-25 17:10 |
| TASK-109 | Worker 1 | Issue #1: search default limit を 50 に統一（commit c0f5194） | 2026-04-25 17:15 |
| Bench | Dispatcher | F2 Cold-entry 30分分断（ESTAB+11s 収束、TASK-102 理論を実証） | 2026-04-25 16:57 |
| TASK-108 | Worker 3 | GitHub Issue Template 作成 (commit 9bd3def) | 2026-04-25 16:51 |
| TASK-107 | Worker 2 | SUMMARY.md に TASK-102/103 追記（459 行に拡張） | 2026-04-25 17:10 |
| TASK-105 | Worker 3 | Codex BLOCKER 実装（config IP placeholder、commit 36c12b7） | 2026-04-25 16:45 |
| TASK-106 | Worker 1 | Codex IMPORTANT 実装（test created_at 固定、commit 40b1fe9） | 2026-04-25 16:45 |
| TASK-104 | Worker 2 | SUMMARY.md 作成（260 行、7 セクション） | 2026-04-25 17:00 |
| TASK-103 | Worker 3 | Codex BLOCKER + IMPORTANT 修正方針設計（推奨案 A,A） | 2026-04-25 16:40 |
| TASK-102 | Worker 1 | zenoh hot/warm/cold semantics 調査（5秒収束の理論解明） | 2026-04-25 16:38 |
| TASK-101 | Worker 2 | mesh-mem: CLI search default limit 仕様確認（CLI=20, MCP=20, API=50） | 2026-04-25 16:25 |
| TASK-100 | Dispatcher | mesh-mem: Mid-hot 分断（65秒、20件）合格、ESTAB+5s 収束 | 2026-04-25 15:59 |
| TASK-099 | Dispatcher | mesh-mem: Short 再測（クリーン）30秒、10件、ESTAB+5s 収束 | 2026-04-25 15:57 |
| TASK-098 | Dispatcher | mesh-mem: Short 分断テスト（30秒分断、10件、両側10件揃った） | 2026-04-25 15:55 |
| TASK-097 | Worker 1 | mesh-mem: Tier-1 ベンチマーク実装＆実行（合格） | 2026-04-25 15:55 |
| TASK-096 | Worker 1 | mesh-mem: 大規模 + 長期分断シナリオ設計（推奨: Tier-1 + Short） | 2026-04-25 15:50 |
| TASK-095 | Worker 2 | mesh-mem: 未コミット変更の整理コミット（3コミット、push なし） | 2026-04-25 15:35 |
| TASK-094 | Worker 2 | mesh-mem: Split-brain 復旧テスト（設計 + 実行、Goal 4/5 達成） | 2026-04-25 15:27 |
| TASK-093 | Worker 2 | mesh-mem: Home/Office Zenoh クラスタのスモークテスト（全通過） | 2026-04-25 10:15 |
| TASK-092 | Worker 1 | Home zenohd v1.9 + rocksdb plugin 導入計画（実態確認で不要判明） | 2026-04-24 21:00 |
| TASK-091 | Worker 2 | mesh-mem: Office PC bring-up 時 Search 可否調査 | 2026-04-24 21:00 |
| TASK-090 | Worker 2 | mesh-mem: Home IP typo 修正 (128.28→134.28) | 2026-04-24 20:52 |
| TASK-089 | Worker 2 | mesh-mem: Zenoh Home/Office IP 設定整合 | 2026-04-24 20:47 |
| TASK-088 | Worker 2 | mesh-mem: plan.md vs 実装のギャップ分析と追加実装 | 2026-04-24 20:45 |
| TASK-087 | Worker 1 | Gaussian Splatting 実装と説明の追記 | 2026-03-03 10:50 |
| TASK-086 | Worker 3 | 差分SLAMセクション追記 | 2026-03-03 09:15 |
| TASK-085 | Worker 2 | 差分SLAM / インクリメンタルマップ更新 OSS調査 | 2026-03-02 19:30 |
| TASK-084 | Worker 3 | Open-RMF×MEC + Map管理 統合レポート作成 | 2026-03-02 18:35 |
| TASK-083 | Worker 3 | Open-RMF × MEC 活用パターン調査 | 2026-03-02 17:30 |
| TASK-082 | Worker 2 | MECを使ったMap管理調査 | 2026-03-02 17:45 |
| TASK-081 | Worker 3 | MECオフロード用途セクション追記 | 2026-03-02 16:55 |
| TASK-080 | Worker 3 | ROS2/MEC調査レポート Zenohセクション追記 | 2026-03-02 16:40 |
| TASK-079 | Worker 2 | Zenoh + MEC/Cloud ロボットソリューション調査 | 2026-03-02 16:20 |
| TASK-078 | Worker 3 | ROS2 + Cloud/MEC 調査レポート作成 | 2026-03-02 16:05 |
| TASK-077 | Worker 2 | ROS2 + Cloud/MEC連携プロジェクト調査 | 2026-03-02 15:45 |
| TASK-076 | Worker 3 | FY25下期報告会アジェンダ フォワード案件追加 | 2026-03-02 14:50 |
| TASK-075 | Worker 3 | FY25下期 社内報告会 項目リストアップ | 2026-03-02 14:30 |
| TASK-074 | Worker 2 | Obsidian 要確認ファイルの最終処理 | 2026-03-02 14:00 |
| TASK-073 | Worker 2 | Obsidian ファイル整理・移動実行 | 2026-03-02 11:40 |
