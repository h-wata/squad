# ADR 0005: worker 自体をローカルモデルで動かし、local-coder を廃止する

- **Status**: Accepted
- **Date**: 2026-09-01
- **Supersedes**: [ADR 0003](0003-local-llm-as-default-executor.md),
  [ADR 0004](0004-flash-next-as-local-coder-backend.md)
- **Related**: [ADR 0001](0001-multi-session-isolation-by-project-ownership.md),
  [ADR 0002](0002-squad-cli-wrapper.md)

## Context

ADR 0003 / 0004 は「**高価な Claude worker が、条件に合う局所タスクだけを無料の
ローカル LLM に外注する**」という構図だった。worker はあくまで Claude で、
local-coder は worker のターンの中から呼ぶ外部プロセス。委譲条件 (仕様確定 /
`verify:` あり / 1〜数ファイル) は、この非対称性を前提に引かれている。

2026-09-01 に、その前提が崩れた。**worker 自体をローカルモデルで動かせることが
実測で確認できた。**

同一 Issue (#34: `next_on_pass` / `next_on_fail` の遷移ルール実装。複数ファイル
横断 + 新規関数の設計 + 後方互換の担保 + テスト 5 ケース) を、条件を揃えて 3 本
流した (worktree はいずれも同じ main から、task YAML の差分は worker / agent /
パスのみ)。

| 構成 | テスト | 受け入れ条件の矛盾 | コスト |
|---|---:|---|---|
| Opencode + Qwen3.8-Flash-Next | 297 pass | 報告せず自分で解釈 | 0 |
| Claude Code + Sonnet | 298 pass | 報告した | 課金 |
| **Claude Code + Qwen3.8-Flash-Next** | **301 pass** | **報告した** | **0** |

Claude Code を `ANTHROPIC_BASE_URL` で LAN の LiteLLM に向けると、Anthropic
Messages API 互換のため **サブエージェント / hook / Skill / `--append-system-prompt`
がそのまま動く**。つまり worker として不足がない。

さらに、Issue #34 の受け入れ条件には矛盾があった (「通知本文が 1 バイトも変わらない」
と「`[NEXT none]` を付ける」は両立しない)。これを `issues:` に書いて報告したのは
Sonnet と **Claude Code + Qwen** の 2 本で、Opencode + Qwen は黙って解釈した。
**同じモデルでもハーネスを変えると開示するようになった**ため、「曖昧さを黙って
解釈する」は当初 Qwen の性質と診断したが、実際はハーネスの差だった。

## Decision

**`local-coder` agent とその委譲フローを廃止する。** worker が必要ならローカル
モデルで直接動かす (`SQUAD_W*_AGENT=claude-local`)。

理由:

1. **抽象が二重になった。** 「ローカルモデルに書かせる」手段が worker 自体と
   local-coder の 2 つになり、worker はどちらを使うか毎回判断することになる。
   実測でその判断は実際に外れた: 5〜6 ファイル横断のタスク (委譲条件を満たさない)
   を local-coder に委譲しようとした試行があった。
2. **委譲は失敗しうる経路を 1 つ増やすだけになった。** 同じ試行で local-coder
   呼び出しは 401 で失敗し、worker は自前実装に切り替えた。worker 自身がローカル
   モデルなら、この経路も失敗も存在しない。
3. **委譲条件そのものが不要になった。** ADR 0003/0004 の条件 (仕様確定 /
   `verify:` あり / 1〜数ファイル) は「Claude の文脈をローカルに切り出す」ための
   線引きであって、worker 自体がローカルモデルなら引く意味がない。実際 Issue #34
   は「複数ファイル横断」で委譲条件を外れるが、worker として動かせば完走した。

## Consequences

- `.claude/agents/local-coder.md` を削除。`instructions/worker.md` /
  `dispatcher.md` の委譲節と routing 行も削除する。
- `scripts/link-pi-config.sh` と `config/pi/models.json.template` は残す。pi は
  `scripts/pi-log-triage.sh` 等が引き続き使う。
- **検証は author と別系統のモデルで行う** という制約が新たに要る。ローカル
  モデル同士だと死角を共有し、実測でも意味論の欠陥 (壊れた YAML を
  `task_id_mismatch` と報告する等) を素通しした。`scripts/verify-task.sh` の既定は
  sonnet。最終ゲートは従来どおり Codex の cross-review。
- Anthropic は**非 Claude モデルへの routing を公式にはサポートしない**
  (`llm-gateway-rollout.md`)。Claude Code の更新で壊れうる前提で使う。壊れた場合の
  退避先として Opencode 経路 (`SQUAD_W*_AGENT=opencode`) を残してある。
- ADR 0003 / 0004 の実測値 (39/39 PASS、p50 30 秒等) は Qwen3.8-Flash-Next の能力
  評価としては引き続き有効。無効になったのは「委譲という構図」のほう。
