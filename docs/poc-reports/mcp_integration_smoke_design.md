# MCP Integration Smoke Test 設計書

| 項目 | 値 |
|------|-----|
| 作成日 | 2026-04-25 |
| Issue | https://github.com/h-wata/mesh-mem/issues/5 |
| 参照 | /home/gisen/work/mesh-mem/src/mesh_mem/mcp_server.py |
| 参照 | /home/gisen/work/mesh-mem/tests/test_mcp_server.py |

---

## 1. 目的

### 既存テストとの違い

既存の `tests/test_mcp_server.py` および `tests/test_mcp_cli.py`（TASK-111 で fastmcp を追加済み、47 passed / 0 skipped）は、`fastmcp.Client` を使って **Python コード上から** MCP サーバーを直接呼び出す自動テストである。これは「MCP サーバー実装の正しさ」を検証する。

本テストが検証するのは **実際の Claude Code CLI を MCP client として起動したときの end-to-end 動作**であり、既存テストでは確認できない以下の要素を検証対象とする：

| 確認項目 | 既存テスト | 本テスト |
|---------|-----------|---------|
| tool 登録・discovery | fastmcp Client (Python) | Claude Code MCP client |
| tool call の発火 | Python コード呼び出し | LLM が自然言語から tool use を判断 |
| stdio transport 実装 | subprocess 経由 (test_mcp_cli.py) | Claude Code の stdio MCP client |
| 環境変数の解決 | テスト環境 | 実運用と同じ env 設定 |

具体的な目的：
- Claude Code に mesh-mem MCP server を登録し、`claude mcp list` で確認できること
- 対話セッション中に `save_observation` / `search_memory` / `delete_memory` / `get_memory_status` が LLM によって呼び出されること
- 異なる MCP client（Claude Desktop, Cursor 等）での動作確認は out-of-scope（別 Issue 候補）

---

## 2. 検証目標 (Goals)

| ID | ゴール | 判定方法 |
|----|--------|---------|
| G1 | Claude Code に mesh-mem MCP server を登録できる | `claude mcp add` が正常終了し、`~/.claude.json` に登録が入る |
| G2 | `claude mcp list` で mesh-mem が `Connected` 状態で表示される | 出力に `mesh_mem: ... - Connected` が含まれる |
| G3 | 対話セッションで「保存して」と依頼すると `save_observation` が呼ばれる | tool use ログ + `mesh-mem search` で 1 件 hit |
| G4 | 対話セッションで「検索して」と依頼すると `search_memory` が呼ばれる | tool call 発生 + 結果がセッション内に表示 |
| G5 | 「削除して」と依頼すると `delete_memory` が呼ばれ、再検索で 0 件になる | tool call 発生 + 再 search で 0 件 |

---

## 3. 環境とツール

### 必要なもの

| コンポーネント | 状態 |
|-------------|------|
| Claude Code CLI (`claude`) | インストール済み想定 |
| mesh-mem パッケージ | `pip install -e .` 済み |
| `mesh-mem-mcp` コンソールスクリプト | `/home/gisen/.local/bin/mesh-mem-mcp` |
| zenohd | ローカルで稼働中（Home ホストのみで十分） |
| Zenoh RocksDB storage | `~/.local/share/zenoh-mem/agent_mem/` |

### 検証ホスト

Home ホスト（192.168.134.28）のみ。`ZENOH_CONNECT=tcp/127.0.0.1:7447` のローカル接続で十分。

### 公開ツール一覧（`mcp_server.py` より）

| ツール名 | 引数 | 説明 |
|--------|------|------|
| `save_observation` | `content`, `project?`, `tags?` | メモを保存。ID は自動生成、identity は env から解決 |
| `search_memory` | `query?`, `project?`, `limit?`, 他フィルタ | メモを検索（limit デフォルト 50） |
| `delete_memory` | `observation_id` (32 文字), `reason?` | soft delete（tombstone 発行） |
| `get_memory_status` | なし | バージョン・件数・pc_id 等のサマリーを返す |

注: `save_observation` の identity フィールド（agent_family, client_id 等）は **LLM から引数で渡せない設計**。サーバー側の環境変数（`MESH_MEM_AGENT_FAMILY`, `MESH_MEM_CLIENT_ID` 等）から解決される。

---

## 4. 事前準備手順

### Step 1. zenohd の healthcheck

```bash
# zenohd プロセスが起動しているか確認
ps aux | grep zenohd | grep -v grep

# 起動していない場合
zenohd -c /home/gisen/work/mesh-mem/config/zenohd_localhost.json5 &
# または
systemctl --user start mesh-mem-zenohd
```

### Step 2. mesh-mem-mcp の動作確認

```bash
# インストール済みか確認
which mesh-mem-mcp
# 期待値: /home/gisen/.local/bin/mesh-mem-mcp

# stdio から起動して応答があるか確認（Ctrl+C で終了）
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | mesh-mem-mcp
```

### Step 3. Claude Code への MCP 登録

```bash
# claude mcp add で登録（~/.claude.json に書き込まれる）
# IMPORTANT: ~/.claude/settings.json の mcpServers は Claude Code CLI に無視される
#   Claude Code は ~/.claude.json (top-level または projects[<path>].mcpServers) のみ読む
#   (README.md "MCP registration" セクション / memory に記録済みの既知動作)

claude mcp add mesh_mem -s user \
  -e ZENOH_CONNECT=tcp/127.0.0.1:7447 \
  -e MESH_MEM_AGENT_FAMILY=claude \
  -e MESH_MEM_CLIENT_ID=claude-code \
  -- /home/gisen/.local/bin/mesh-mem-mcp
```

環境変数は `-e KEY=VALUE` 形式で複数指定可能。

### Step 4. 登録確認

```bash
claude mcp list
# 期待値: mesh_mem: /home/gisen/.local/bin/mesh-mem-mcp - Connected
```

`Connected` が表示されない場合はトラブルシューティング（セクション 7 参照）。

---

## 5. シナリオ手順

各 Case は新規 `claude` セッション（または同一セッション内の継続）で実施する。
tool call が発生したかは Claude Code の tool use ログ（ターミナル出力）で確認。

### Case 1: MCP registration（G1-G2）

```
実行コマンド: claude mcp list
期待出力: mesh_mem: ... - Connected
記録: コマンド出力を poc-reports/raw/ に保存
```

### Case 2: save_observation via LLM（G3）

```
プロンプト例: "mesh-mem に保存して: 設計書テスト用メモ 2026-04-25"

期待動作:
  1. Claude が save_observation tool を呼ぶ（ツール呼び出しログが表示）
  2. tool result: "保存完了: <32文字ID>"
  3. 検証: mesh-mem search "設計書テスト" --limit 100 で 1 件 hit

記録: tool call 引数・結果、observation_id
```

注: `project` や `tags` を指定したい場合はプロンプトに含めると Claude が引数として渡す。

### Case 3: search_memory via LLM（G4）

```
プロンプト例: "mesh-mem から「設計書テスト」を検索して"

期待動作:
  1. Claude が search_memory tool を呼ぶ
  2. tool result: "[claude/claude-code] ... 設計書テスト用メモ <id=...>"
  3. Claude がセッション内に結果を表示

記録: tool call の query 引数・返却件数
```

### Case 4: get_memory_status via LLM

```
プロンプト例: "mesh-mem の状態を見せて"

期待動作:
  1. Claude が get_memory_status tool を呼ぶ
  2. tool result に以下が含まれる:
     - "mesh-mem version: ..."
     - "pc_id: ..."
     - "session_id: ..."
     - "件数 (上限 10000 内): ..."

記録: tool result の出力全文
```

### Case 5: delete_memory via LLM（G5）

```
前提: Case 2 で保存した observation_id を使用

プロンプト例: "observation_id <Case 2 の 32 文字 ID> を削除して"
  または: "さっき保存した設計書テストのメモを削除して"
  ※ LLM が ID を把握していない場合は先に Case 3 を実行して ID を特定する

期待動作:
  1. Claude が delete_memory tool を呼ぶ（observation_id 32 文字を正確に渡す）
  2. tool result: "削除（tombstone）完了: <observation_id>"
  3. 検証: mesh-mem search "設計書テスト" --limit 100 → 0 件

記録: tool call の observation_id 引数・結果
```

注: `delete_memory` は **32 文字の完全一致 ID が必須**（短縮 ID は拒否される）。LLM が ID を正確に渡せるかが確認ポイント。

---

## 6. 期待結果（仮説）

- **G1-G5 全 Pass**: mesh-mem の MCP server が正しく実装されており、stdio transport での Claude Code 接続が機能するはず（`test_mcp_cli.py` で subprocess 接続は検証済み）
- **tool call の発火**: 明確な「保存して」「検索して」のプロンプトであれば LLM が tool を使うはず。ただし LLM が直接回答しようとして tool を呼ばないケースは failure 扱い
- **ID の取り扱い**: `delete_memory` で 32 文字 ID を正確に渡せるかは LLM の能力依存。Case 3（search）で ID を表示させてから Case 5 を実行するフローが安全

### 設定ミスで失敗する可能性が高いパターン

| 症状 | 原因候補 |
|------|---------|
| `claude mcp list` に mesh_mem が出ない | 登録先が `~/.claude/settings.json` になっている |
| `Not Connected` または `Error` | zenohd が未起動、または ZENOH_CONNECT の設定ミス |
| `save_observation` の identity が unknown | MESH_MEM_AGENT_FAMILY / MESH_MEM_CLIENT_ID が未設定 |
| `delete_memory` で「32 文字が必要」エラー | LLM が ID を短縮して渡した |

---

## 7. 観測項目

各 Case で以下を記録する：

| 観測項目 | 確認方法 |
|---------|---------|
| tool call が発生したか | Claude Code の tool use ログ（ターミナル） |
| tool の引数が正確か | ログに表示される JSON 引数 |
| tool result が返ったか | ログの tool result 表示 |
| レイテンシ（主観） | 体感 1 秒未満なら OK |
| エラー出力 | stderr または `journalctl --user -u mesh-mem-zenohd` |

---

## 8. リスクと対策

| ID | リスク | 評価 | 対策 |
|----|--------|------|------|
| R1 | `~/.claude.json` と `~/.claude/settings.json` の混同 | 高 | Step 3 で必ず `claude mcp add` を使う。手動編集するなら `~/.claude.json` の top-level `mcpServers` に記載 |
| R2 | stdio transport で zombie process が残る | 低〜中 | テスト完了後に `pkill -f mesh-mem-mcp` で後始末 |
| R3 | zenohd が落ちると全 Case 失敗 | 中 | Step 1 で healthcheck を必ず実施 |
| R4 | LLM が tool を呼ばず自己生成で回答 | 中 | プロンプトを「mesh-mem ツールを使って保存して」と明示。それでも呼ばない場合は failure として記録 |
| R5 | `mesh-mem-mcp` のパスが環境によって異なる | 低 | `which mesh-mem-mcp` で確認してから登録 |

---

## 9. 検収条件 (Pass criteria)

### 全体判定

| 基準 | 判定 |
|------|------|
| Case 1-5 全 Case で tool call が観測される | Pass |
| 1 Case でも tool call なし（LLM が自己生成で回答）| Failure → レポート + 別 Issue 起票 |
| 1 Case でも tool call エラー（MCP server 側の例外） | Failure → ログを添付して Issue 起票 |

### 詳細基準

| Case | Pass 条件 |
|------|----------|
| Case 1 | `claude mcp list` で `mesh_mem: ... Connected` が表示される |
| Case 2 | `save_observation` tool call 発生 + `mesh-mem search` で 1 件 hit |
| Case 3 | `search_memory` tool call 発生 + 結果が表示される |
| Case 4 | `get_memory_status` tool call 発生 + version/pc_id が含まれる |
| Case 5 | `delete_memory` tool call 発生（32 文字 ID 正確）+ 再 search 0 件 |

---

## 10. 引き継ぎ事項

### 人間オペレーターへの依頼事項

1. **実行者**: Claude Code の対話セッションが必要なため、人間オペレーターが必須
2. **所要時間**: 5 Case すべて実施で 15〜30 分程度
3. **ログ保存**: 各 Case のターミナル出力をコピーして `docs/poc-reports/raw/` に保存

```
推奨ファイル名:
  docs/poc-reports/raw/mcp-integration-case1-registration.txt
  docs/poc-reports/raw/mcp-integration-case2-save.txt
  docs/poc-reports/raw/mcp-integration-case3-search.txt
  docs/poc-reports/raw/mcp-integration-case4-status.txt
  docs/poc-reports/raw/mcp-integration-case5-delete.txt
```

### 自動化の検討（将来 Issue）

Claude Code の対話セッションを自動化するのは難易度が高い。代替案として：
- `mcp-cli`（non-LLM の CLI MCP client）を使って tool call を直接テストする
- `test_mcp_cli.py` の fastmcp Client を使った自動テストで代替する（既にある）

この代替案が十分か、または「実際の LLM client でのテスト」が必要かは別 Issue で判断する。

### 登録解除（後片付け）

```bash
# 登録を削除する場合
claude mcp remove mesh_mem
```

---

*設計書のみ。実行は人間オペレーターによる Claude Code 対話セッションが必要。mesh-mem 側のコード変更なし。*
