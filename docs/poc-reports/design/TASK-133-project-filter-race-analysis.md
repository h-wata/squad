# `--project` filter race 再現＋根本原因分析 設計書

| 項目 | 値 |
|------|-----|
| TASK | TASK-133 |
| 作成日 | 2026-04-27 |
| Issue | https://github.com/h-wata/mesh-mem/issues/8 |
| 起票元 | TASK-119 DR 24h test（`docs/poc-reports/raw/TASK-119-dr-1day-result.yaml`） |
| 関連 | `docs/poc-reports/SUMMARY.md` §8.3、Issue #7（線形 full scan）、`docs/poc-reports/design/TASK-131-search-server-side-filtering-design.md` |
| ステータス | 設計のみ。実装は別タスク |

---

## 1. 観測事象の整理

### 1.1 Issue #8 の記述

DR 24h test (TASK-119, 2026-04-27) で Office 側 zenohd 再起動後の数分間、同一 CLI / 同一ホスト / 同一 backend にも関わらず以下の不整合が観測された。

```bash
# Office 側、再起動後 1.5〜4.7 分の窓内
mm search 'dr-test' --project dr-test --limit 2000 | wc -l   # → 0
mm search ''        --project dr-test --limit 2000 | wc -l   # → 1193
mm search 'dr-test'                   --limit 2000 | wc -l   # → 1193
```

データそのものは存在している（empty-keyword query が rows を返している）が、`keyword + --project` の組み合わせだけが 0 件を返す状態が継続した。

### 1.2 SUMMARY §8.3 で記録された別パターン

`SUMMARY.md` §8.3 の記述は Issue #8 と少し異なる:

```bash
mesh-mem search "" --project dr-test --limit 2000   # → 0
mesh-mem search "" --limit 20                        # → dr-test の obs を含む
```

つまり **empty keyword + project=dr-test = 0**、**empty keyword (no project) = N (dr-test を含む)**、という別のフィルタ組み合わせでも一時的に 0 になっていた。Issue 本文と SUMMARY で微妙に違う組み合わせが報告されているのは、**手で何度か叩いた際のスナップショットを部分的にメモしている**可能性が高い。本設計書では「フィルタ組み合わせ次第で一時的に 0 件が返る不整合」と一般化する。

### 1.3 時系列（TASK-119 raw より）

| 時刻 | Office 件数 | Home 件数 | 備考 |
|------|------------|----------|------|
| `08:46:48` ESTAB | 0 | 1,188-1,190 | 再起動完了直後 |
| `08:47:19 - 08:48:25` poll1 | 0 | 1,188-1,190 | 70 秒間 0 件 |
| `08:50:18 - 08:51:14` poll2 | 0 | 1,191-1,193 | さらに 56 秒間 0 件 |
| `08:51:30` spot check | **1,193** | 1,193 | step-function jump |

→ ESTAB 後 **97-282 秒**（中央値 ~3 分）の間、Office 側 search は 0 件の状態。最後にいきなり 1193 件に jump（cold-era step-function 収束、§8.3 主要発見 1）。Issue #8 の「1.5〜4.7 分」窓はこの帯と一致する。

### 1.4 同一窓内でのフィルタ別挙動

ここが本設計書の主問題：cold-era 収束の窓 (T+97s〜T+282s) のうち、**`empty keyword + project` だけが 0 を返し、片方だけだと 1193 を返す**ような瞬間が存在したのか、それとも **3 つのコマンドが順次叩かれ、それぞれ違う時刻のスナップショットを観測しただけ**なのかが切り分けられていない。

---

## 2. コードパス調査

### 2.1 `search_observations` の実装（`src/mesh_mem/store.py:173-236`）

要約フロー:

1. selector を組み立てる
   - `parts = ['mem/obs', agent_family or '*', client_id or '*', pc_id or '*', session_id or '*', '**']`
   - `tomb_expr = key_expr.replace('mem/obs/', 'mem/tomb/', 1)`
   - **`project` / `since` / `query` は selector に乗らない**（identity 系のみ key 階層に含まれる）
2. tombstone scan（`session.get(tomb_expr)`）→ tombstone の observation_id を `set` に集める
3. obs scan（`session.get(key_expr)`）→ payload を `Observation.from_json` でデコード
4. Python 側で逐次 filter
   - `if obs.observation_id in tombs: continue`
   - `if project and obs.project != project: continue`
   - `if since_dt: ...`
   - `if q and q not in content and q not in project_lower and not any(q in tag for ...): continue`
5. `created_at` 降順 sort、`[:limit]`

### 2.2 重要な観察

| 観察 | 影響 |
|------|------|
| `project` / keyword は zenoh selector ではなく Python フィルタ。`--project` 指定の有無で **送る query は同一**。 | H2「別 selector で別の hydration を見る」は **棄却** |
| `--project` フィルタは `obs.project != project` で部分集合の中身に依存。空集合なら 0 件返す | H1（部分集合 race）と整合 |
| keyword は OR 条件（content / project / tags のいずれか一致でヒット）。q='dr-test' かつ obs.project='dr-test' なら必ずヒット | filter ロジック単独で 0 件にはならない（同一 obs リストの場合）|
| `_iter_ok_replies` は err reply で `QueryErrorReply` を raise → `with_retry` が一度 retry → 失敗で `RuntimeError` | H3（exception fallthrough）の経路 |
| `with_retry` decorator は `search_observations` 全体を包む。retry は **同じ引数で全体再実行** | filter args（keyword/project）の違いで retry 経路は変わらない |
| `_iter_ok_replies` が **err を raise せず部分集合だけ返す** ケース：alignment 中に Zenoh storage が「そこまで届いた分」を返答する場合 | H1 を直接生む |

### 2.3 keyword と project の評価順

```python
if obs.observation_id in tombs:    # ① tombstone
    continue
if project and obs.project != project:  # ② project
    continue
if since_dt: ...                   # ③ since
if (q and not (q in content or q in project_lower or any(q in t for t in tags))):  # ④ keyword
    continue
results.append(obs)
```

評価順は ① → ② → ③ → ④。**完全 obs リスト**の場合、`project='dr-test' & q='dr-test'` でも `obs.project='dr-test'` なら ② を通り、④ で `q in project_lower` が真なのでヒット。**filter ロジック単体で keyword + project 0 件にはならない**。

→ つまり問題は **入力リスト（zenoh から返る obs 集合）が一時的に部分／空集合**になることが必要条件。

---

## 3. 仮説（複数立案）

### H1（最有力）: cold-era step-function alignment による snapshot timing race

**主張**：`session.get(...)` が Office 側 RocksDB の **hydration 進行中スナップショット**を返している。3 つの search コマンドは順次叩かれるため、それぞれ別の進行度で返答される。
- empty keyword + project: 部分集合のうち project=dr-test のものを返す
- keyword + project: 同上で AND 評価（部分集合の状態によっては 0）
- keyword only: 部分集合のうち keyword 一致を返す

支持根拠
- TASK-119 raw の polling から「step-function (0 → 1193 jump)」が確実に観測されている
- `_iter_ok_replies` は err 以外は素通しなので、partial set でも例外なく yield される
- 部分集合のサイズが 0 のときは empty / keyword / project のどれでも 0 になる
- `search_observations` のロジックは **入力 obs リスト次第で結果が大きく変わる**

否定材料
- Issue #8 の同時 3 コマンド観測では「empty + project = 1193、keyword + project = 0」と「同時刻なら集合差が無いはず」が観測されている → ただし「同時」と言っても CLI 1 回ごとに新規 zenoh session を張り直すので、各 invocation は別の get になる
- 1193 件と 0 件の極端な差を、3 コマンド間の数秒のタイムラグだけで説明可能か？ → step-function なら可能（窓の前後で 0 ↔ 1193 が一瞬で入れ替わる）

### H2: zenoh selector / queryable に keyword が乗っており別 hydration 経路

**主張**：将来 Issue #7 の方向で keyword を zenoh selector に push down した場合、project と keyword で異なる selector を張ることになり、別の hydration timing を観測する可能性。

支持根拠
- 現状実装では発生しないが、将来 Issue #7 の B / C 案を採ると新たに発生し得る
- 「同じ get なら結果が同じはず」を裏返せば「違う get なら結果が違って当然」

否定材料（現状実装）
- 現在の `search_observations` は project / keyword に依存せず同じ key_expr で get
- → **現時点では棄却**。将来の警告として記録

### H3: `_iter_ok_replies` の中途エラーで `RuntimeError`、CLI が空 stdout（`wc -l = 0`）

**主張**：alignment 中の partial replication で zenoh が err reply を返し、`_iter_ok_replies` が `QueryErrorReply` を raise → `with_retry` が一度だけ再試行 → 再試行も同状態で失敗 → `RuntimeError` → `_cmd_search` で uncaught → traceback が stderr へ、stdout は空 → `wc -l = 0`。

支持根拠
- Issue #8 の `wc -l = 0` という観測値は「『該当するメモリはありません。』が 1 行 print されない」ことを意味する。もし print されていれば `wc -l = 1`
- exception で stdout 空という整合性は強い

否定材料
- もし RuntimeError なら、`empty keyword + project` でも同じ get を呼ぶので同じく失敗するはず（観測では 1193 件返る）
- → 同窓内で直前は err、直後は ok という**過渡的な失敗**であれば説明可能。確率的なバグとなる

### H4: search_observations 全体が `with_retry` で包まれているため、partial の途中で例外 → 全体 retry → 別 timing でリスト取得

**主張**：tomb scan 中／obs scan 中に部分エラー → with_retry が wrap 全体を retry → 2 回目で結果が戻る。**2 回叩いている**ため、1 回目と 2 回目の hydration timing 差で結果が変わる可能性。

支持根拠
- with_retry は decorator で全関数を包んでいる（`store.py:88-109`）
- retry 間に `time.sleep(0.2 * attempt)` が入る → 200ms 経過後に session を再作成して再試行

否定材料
- retry 経路は keyword/project と無関係 → **「特定 filter だけ retry がトリガーされる」ことは無い**
- ただし「retry 経路に入った場合に 200ms の余分な hydration 時間が稼げるので結果が変わる」は副作用として有り得る

### H5: zenoh storage backend の wildcard query が alignment 中に **partial-empty** 応答を返す

**主張**：rocksdb backend が alignment 中に `**` selector に対し空集合を返してしまう実装上の癖がある。obs scan が空集合を返したら results は []、CLI は「該当するメモリはありません」を print → wc -l = 1（観測の 0 とは整合しない）。

否定材料
- wc -l = 0 と整合しない → H3 と組み合わせない限り棄却

---

## 4. 再現手順（ローカル最短シーケンス）

目的：H1 を裏付けるため、**alignment 進行窓内で同時に複数フィルタを叩いて結果差分を観測**する。

### 4.1 想定環境

- 単一 PC で 2 router を `tcp/localhost:7448` / `tcp/localhost:7449` で立てる
- それぞれ独立した RocksDB ディレクトリを使う
- replication は `mem/**` 全体に対し有効

### 4.2 手順スクリプト案 (`scripts/repro_issue_8.sh`、擬似コード)

```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT=dr-test-repro
ROUTER_A_DB=$HOME/.local/share/zenoh-mem-repro-a
ROUTER_B_DB=$HOME/.local/share/zenoh-mem-repro-b
ROUTER_A_CONF=config/zenohd_localhost_a.json5
ROUTER_B_CONF=config/zenohd_localhost_b.json5

# Phase 0: cleanup
rm -rf "$ROUTER_A_DB" "$ROUTER_B_DB"

# Phase 1: start both routers
ZENOH_BACKEND_ROCKSDB_ROOT=$ROUTER_A_DB nohup zenohd -c $ROUTER_A_CONF > /tmp/repro_a.log 2>&1 &
ZENOH_BACKEND_ROCKSDB_ROOT=$ROUTER_B_DB nohup zenohd -c $ROUTER_B_CONF > /tmp/repro_b.log 2>&1 &
sleep 5

# Phase 2: bulk put 1000 obs from router A
ZENOH_CONNECT=tcp/localhost:7448 \
  python scripts/bulk_save.py --project "$PROJECT" --count 1000 --workers 10

sleep 10  # let replication settle

# Phase 3: stop router B (simulate restart)
pkill -TERM -f "zenohd_localhost_b"
sleep 30  # B is down

# Phase 4: put another 100 obs from A
ZENOH_CONNECT=tcp/localhost:7448 \
  python scripts/bulk_save.py --project "$PROJECT" --count 100 --workers 10

# Phase 5: restart router B
ZENOH_BACKEND_ROCKSDB_ROOT=$ROUTER_B_DB nohup zenohd -c $ROUTER_B_CONF > /tmp/repro_b.log 2>&1 &

# Phase 6: poll all 3 filter modes from B side, in tight loop (every 100ms for 5min)
ZENOH_CONNECT=tcp/localhost:7449
for i in $(seq 1 3000); do
  T_NOW=$(date +%s.%N)
  C1=$(mesh-mem search "$PROJECT" --project "$PROJECT" --limit 2000 2>/dev/null | grep -c '<id=' || echo "ERR")
  C2=$(mesh-mem search ""         --project "$PROJECT" --limit 2000 2>/dev/null | grep -c '<id=' || echo "ERR")
  C3=$(mesh-mem search "$PROJECT"                      --limit 2000 2>/dev/null | grep -c '<id=' || echo "ERR")
  echo "$T_NOW c1_kw_proj=$C1 c2_empty_proj=$C2 c3_kw_only=$C3"
  sleep 0.1
done | tee /tmp/repro_issue8_poll.log
```

### 4.3 期待される計測

| 観測項目 | 期待値 |
|----------|--------|
| 不整合継続時間 | T+0 〜 T+(数十秒)、cold-era で 100s+ になり得る |
| (c1, c2, c3) のパターン | 主に `(0,0,0) → (1100,1100,1100)` の jump が観測されるはず |
| もし `(0, 1100, 1100)` のような **同一 100ms 内** スナップショットが取れたら | filter ロジック側に何かある証拠 → H2/H3 の精査へ |
| ESTAB 後最初に non-zero になる時刻 | hot/warm era では数秒、cold era では数十秒〜 |

### 4.4 ステップ数

- 6 phase（0:cleanup, 1:start, 2:bulk put, 3:stop B, 4:put more, 5:restart B, 6:tight poll）
- スクリプト 1 本 + bulk_save.py の 2 ファイル

---

## 5. 根本原因の最有力候補

### 5.1 推定

**最有力：H1（snapshot timing race）+ H4（with_retry の retry 経路で別 timing を観測）の合算**

理由
- TASK-119 で記録された step-function 収束（0 → 1193 jump）の窓と、Issue #8 の不整合窓が完全に一致する
- `search_observations` の filter ロジック単体では「同一入力リスト」で keyword + project だけが 0 件になることは**起こり得ない**（§2.3 評価順の検証）
- 入力リスト自体が hydration 進行で変動していれば、3 コマンドの順次実行で結果が大きく分かれることはあり得る

### 5.2 代替仮説の可能性

- H3（exception fallthrough）も否定しきれない。`wc -l = 0` の挙動を厳密に説明するには「stdout が空 = exception または fall-through」のいずれか
  - 検証手段：Office 側で `mesh-mem search ... ; echo "exit=$?"` を叩き、exit code が 0 か非 0 かで切り分け
- 実機再現の poll log で `(0, N, N)` のような同時刻ヘテロパターンが観測されれば、H1 単体では説明できなくなる → コード再調査

### 5.3 ソースコード行レベルの根拠

- `store.py:174-236` の `search_observations`：filter は Python 側、入力依存
- `store.py:128` の `_iter_ok_replies`：err reply で raise、それ以外は yield → partial set はそのまま流れる
- `store.py:88-109` の `with_retry`：関数全体を 2 回まで実行 → retry 中は session 再作成

---

## 6. 修正方針の比較

| 案 | 概要 | 利点 | 欠点 | DoD | test 観点 |
|----|------|------|------|------|-----------|
| 案 1：filter 順序組み替え | Python 側 filter の評価順を「project → keyword → since」に明示し、空入力時に `RuntimeError` を上げる代わりに warning を返す | 実装容易、既存 API 不変 | 根本原因（partial set）を直さない | filter ロジックに対する単体テストが PASS | H1 への打ち手としては不十分 |
| 案 2：alignment 中の retry 拡張 | `_iter_ok_replies` が空集合を返した直後の N 秒は「未収束」とみなし、最大 5s まで sleep + retry。または `search_observations` 自体に `wait_for_alignment=True` オプションを追加 | partial set の影響を緩和 | 「収束」の判定が困難（empty が正解の場合と区別がつかない）、レスポンス遅延悪化 | 再起動後 5s 以内なら retry でリカバリ | 再現スクリプトで結果の安定化を確認 |
| 案 3：README に運用回避を明記 | 「zenohd 再起動後 5 分は filter 結果が揺れるため、polling を empty keyword で行うこと」を明記 | 即効性、コード変更ゼロ | 運用負荷を user 側に押し付ける、根本未解決 | README 更新 1 コミット | 文書のみ |
| 案 4：Issue #7 の SQLite local index 導入 | 別途設計済の `TASK-131` 案 B を採用すれば、search は **SQLite snapshot** に対する一貫した query になり、zenoh hydration 進行とは独立になる | 根本解決、PoC スコープと整合 | 実装コスト中（5 phase）、Issue #7 の決着待ち | TASK-131 §6 の 5 phase をすべて完了 | TASK-131 §7 の test 観点を流用 |

### 6.1 案 1 の詳細イメージ

```python
# src/mesh_mem/store.py の search_observations 内
results: list[Observation] = []
all_obs: list[Observation] = []
for ok in _iter_ok_replies(session, key_expr):
    try:
        obs = Observation.from_json(ok.payload.to_string())
    except Exception as e:
        log.warning('skip malformed payload at %s: %s', ok.key_expr, e)
        continue
    all_obs.append(obs)

if not all_obs:
    log.info('search returned 0 raw obs (possibly mid-alignment); empty result')
    return []

# project 先、keyword 後（変えなくても順序変更そのものに意味はない）
filtered = [o for o in all_obs if o.observation_id not in tombs]
if project:
    filtered = [o for o in filtered if o.project == project]
if since_dt:
    filtered = [o for o in filtered if (_parse_iso(o.created_at) or epoch) >= since_dt]
if q:
    filtered = [o for o in filtered if (q in o.content.lower() or q in o.project.lower() or any(q in t.lower() for t in o.tags))]
...
```

→ ただし**入力リストが partial であれば結果も partial**は変わらない。順序変更だけでは race 解消には至らない。

---

## 7. 推奨と Issue #7 との関係

### 7.1 推奨組み合わせ

- **短期（数日以内）**：**案 3（README 運用回避）+ 案 1（filter 順序の明示と warning ログ）**
  - 案 3：「zenohd 再起動後数分は cold-era alignment が走るため、search が 0 件を返すことがある」を README に追記
  - 案 1：filter ロジックを「all_obs を一括収集 → 順次絞り込み」に書き換え、empty 時は warning ログを残して空 list を返す。表面的なデバッグ性向上
- **長期（PoC 完遂）**：**案 4（Issue #7 = TASK-131 案 B の SQLite local index 導入）**
  - SQLite に hydration 完了済みの snapshot を保持できれば、search は zenoh の hydration timing と独立 → race 自体が消える
  - subscriber が replication を SQLite に書き込むまでは「まだ届いていない」ことが SQLite 側で明示される（`SELECT count(*)` で hydration 進行を可観測）

### 7.2 Issue #7 との関係マトリクス

| 観点 | Issue #7 (search server-side filtering) | Issue #8 (project filter race) |
|------|-----------------------------------------|--------------------------------|
| 症状 | DB 件数が増えると search が遅くなる | zenohd 再起動直後にフィルタ結果が一瞬 0 になる |
| 根本原因 | Python 側 full scan + filter | zenoh storage の hydration が進行中 |
| 影響範囲 | 平常時の latency | 障害復旧時の polling |
| #7 の修正で #8 は解決するか | **YES（案 B / D を採れば snapshot は SQLite 側で一貫、race 消滅）** | — |
| #8 単独で対処すべきこと | — | 短期：README 警告。長期：#7 の解決待ち |

### 7.3 各案の DoD まとめ

| 案 | DoD | 並行可能性 |
|----|------|-----------|
| 案 1 | filter リファクタ → 既存テスト全 PASS、新規テスト「partial set 入力で部分結果を返す」追加 | TASK-131 と独立、いつでも実施可 |
| 案 2 | retry オプション追加 → 再現スクリプトで安定化を確認 | 副作用が大きいため TASK-131 と相互チェック必要 |
| 案 3 | README 1 コミット、Issue #8 にコメントで方針共有 | 即時 |
| 案 4 | TASK-131 設計の Phase 1〜5 を完遂 | TASK-131 のロードマップそのまま |

---

## 8. 受け入れ条件への対応

| Issue #8 の AC | 本設計書での対応 |
|----------------|------------------|
| ローカルで再現可能（zenohd 再起動 + alignment 中 search） | §4 に手順を記述。`scripts/repro_issue_8.sh` 案を提示 |
| 原因分類 (a/b/c) | a (CLI argument handling race) → 棄却（§2.3）／ b (MCP/store 層の stale cache) → cache は無いので棄却／ c (Zenoh queryable selector が partial-storage を観測) → **採用候補（§3 H1）** |
| fix or 運用文書化 | §6 修正方針 + §7 推奨組み合わせで案を明示。短期＝案 3+1、長期＝案 4 |

---

## 9. 残課題と次タスク

### 9.1 残課題

| 項目 | 詳細 | 担当推奨 |
|------|------|----------|
| 実機再現 | `scripts/repro_issue_8.sh` の実装と poll 結果取得 | 別タスクで Worker（コード変更を伴うため Worker 1 / 2 のいずれか）|
| 仮説 H1 vs H3 の切り分け | poll log で「同一 100ms 内に (0, N, N) パターンが観測されるか」を確認 | 同上 |
| 案 1 の filter リファクタ | filter 順序の明示と warning 追加 | 別タスク |
| 案 3 の README 追記 | 運用回避を明記 | 別タスク |
| Issue #7 (TASK-131) の進捗連動 | Phase 1 spike で SQLite race 解消確認まで | TASK-131 のロードマップに従う |

### 9.2 次タスクの起票候補

| 候補タスク | 内容 | 前提 |
|-----------|------|------|
| TASK-XXX: Issue #8 ローカル再現スクリプト実装 | scripts/repro_issue_8.sh を作って poll 結果を raw に保存 | localhost 2-router 構成必要 |
| TASK-XXX: search_observations filter リファクタ（案 1） | all_obs 一括収集 → 順次絞り込みに整理、warning 追加 | 既存テスト互換 |
| TASK-XXX: README 運用ガイド追記（案 3） | zenohd 再起動後の polling 推奨方法を記載 | TASK-131 進捗と連動 |
| TASK-131 Phase 1 spike の優先実施 | 案 4（SQLite local index）で race の根本解消を実証 | TASK-131 設計書 §6 |

---

*設計書のみ。コード変更なし、commit/push なし。実装は別タスクで実施する。最有力仮説 H1 の確証は §4 再現スクリプトの実機計測で得る。*
