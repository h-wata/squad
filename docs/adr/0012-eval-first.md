# ADR 0012: squad の改善は、評価を先に作って数字で確かめる。Journal と Tool Guard は評価で測ってから決める

- **Status**: Accepted
- **Date**: 2026-11-21
- **Supersedes**: なし
- **Related**: #130、[ADR 0005](0005-worker-runs-the-local-model-directly.md)、
  [ADR 0007](0007-exploration-graph-as-derived-index.md)、[ADR 0008](0008-trace-search-over-existing-transcripts.md)、
  [ADR 0011](0011-brief-clear-resume.md)

## Context

squad の改善 (instruction、skill、検査スクリプトの変更) が本当に効いたかを、いまは数字で確かめられない。
改善の案としては、実行の記録 (Execution Journal)、実行前の検査 (Tool Guard)、失敗から改善までの追跡、
改善ループの自動化などが挙がった。設計検討 SQUAD-347 でこれらを現状と照らして調べた。要点は次のとおり。

- squad の実行単位は task YAML 1 枚で、ツール呼び出しは Claude Code / Codex の内側で起きる。squad が
  個々のツール呼び出しを仲介する位置にはいない。hook は Stop / Notification / StopFailure /
  UserPromptSubmit の 4 つだけ。
- 操作の記録は transcript がすでに持っている。`squad trace` (ADR 0008) が tool_use と tool_result を対にして
  取り出せる。transcript の保持は Claude Code 任せで、Codex / Opencode には無い。
- 実行前の制約は、deny リスト、watchd の承認保留 (ADR 0006)、グローバル設定の ask ルール、外部送信の人間承認の
  4 層がすでにある。
- 比較実験の枠は `docs/benchmark.md` (suite / config / trial) と `bench_report.py` にある。足りないのは、
  実行過程と制約違反の指標、Memory を確実に切る手段、介入の記録の形式。
- 重複した外部操作や制約違反が実際に何件起きているかは、まだ測っていない。測る道具が評価なので、評価が先になる。
- 依頼にあった「Context Compiler」に当たる独立した部品は、どちらのリポジトリにも無い。

## Decision

### 1. 評価を先に作る

transcript から決定論的に指標を数えるスクリプトを 1 本足し、`docs/benchmark.md` の枠 (suite / config / trial)
で集計する。新しい基盤や runner は作らない。

- 評価は決定論的な指標から始める。LLM による判定は新しく足さない (verifier と cross-review がすでにその役)。
- 評価スクリプト・suite・verifier の定義は改善の対象にしない。変更は改善案と別の PR にし、人間が merge する。

### 2. Journal と Tool Guard は今は作らない

評価で次のどちらかが 1 件でも出たら、PreToolUse hook で外部書き込みだけを記録・照合する設計に進む。

- 同じ外部書き込み (`gh pr create` など) の重複した実行
- 既存の deny や承認保留をすり抜けて実行された制約違反

判定は `metrics/task_evals.jsonl` で行う。

- 重複: `external_write_dups` が 1 以上。
- 制約違反: `deny_pattern_matches` は deny パターンに一致した試行の数で、実行されたかどうかは示さない。1 以上なら
  transcript と突き合わせ、実際に実行された (deny・承認保留をすり抜けた) かを手で確かめる。

どちらも 0 件なら作らない。全ツール呼び出しを包む実行基盤 (Execution Engine) は、どちらの場合も作らない。
着手するかどうかは、P0-P2 の評価の結果を見て判断する (#130 の P4)。

### 3. 探索グラフ (ADR 0007) は読み取り元のまま

探索グラフは評価の読み取り元として使う。Journal の書き込み先にはしない。YAML が正本のままで、
`graph.sync()` が毎回作り直す派生索引という位置づけも変えない。したがって ADR 0007 は維持する
(Supersedes は「なし」)。ツール呼び出し単位の記録は transcript から取る。

### 4. 最初の比較実験は Memory ON / OFF の 2 条件

- Memory OFF は task 文で禁じるのではなく、起動時に worker から kioku-mesh の MCP を外して作る。
- Context Compiler は条件に入れない。
- 題材は merge 済みの squad の Issue を、元の commit から解き直す。正解と verify を書けて、PR が重複しない。

### 5. 並列の価値は別の軸で測る

同じ題材を「単一 worker + verify」と「Dispatcher + 複数 worker (cross-review なし)」で流し、成功率・所要時間・
トークン・人の介入回数を比べる。題材には同じファイルを触る task を混ぜる。

### 6. 改善ループはベンチマークの題材の上だけで回す

同じ題材で候補を同時に試す形 (google-research/harness2 型) は、ベンチマークの題材の上だけで回す。本番の task は
分担のまま。

## 採用しなかった案

- **Execution Journal / Tool Guard の自前実装**: 記録は transcript に、実行前の制約は既存の 4 層にある。
  重複や違反が実際に起きているかが未計測のまま作ると、使われない部品が増える。判断は評価の後 (Decision 2)。
- **探索グラフを Journal の書き込み先にする**: sync が毎回全件を消して作り直すため、グラフにだけ書いた記録は
  消える。差分更新と migration が要り、ADR 0007 が退けた案と、ledger と分けた理由の両方を覆す。
- **Context Compiler を作って 5 条件で比較する**: 独立した部品が無く、作るなら最小構成ではなくなる。
  Memory の一種で、Memory と独立には切れない。探索グラフを読むのは Dispatcher で worker ではないため、
  worker の 1 task の成否には効果が出ない。
- **外部の実行基盤 (ADK の Runner、Temporal など)**: ツールを呼ぶ主体が Claude Code / Codex の CLI である
  squad とは前提が合わない。入れるなら CLI の置き換えになる。
- **LLM-as-a-Judge の追加**: verifier と cross-review がすでにある。足す前に、verifier の判定と人間の判断の
  一致を既存の verdict で数える。
- **本番の task での候補の同時試行**: 試行の本数ぶんコストが増え、PR が重複する。bench の task は最初から
  push / PR を禁じているので、この問題が無い。
- **google-research/harness2 のコードをそのまま使う**: Claude Code に対応しておらず、Codex のサンドボックスを
  切る動かし方が squad の deny リストと承認保留に合わない。

## Consequences

- 良い面: 改善の効果を、成功数・制約違反・トークンの数字で比べられる。Journal / Guard を作るかを、件数を
  見て決められる。正本 (YAML、ledger、transcript、Observation) は 1 つも変わらない。
- 引き受けるコスト: 評価結果の正本 (`metrics/` の追記のみのファイル) が 1 つ増える。transcript が消えると
  再計算できないため。題材の用意が、コードより手間がかかる。Memory ON / OFF の比較は 1 条件あたり
  複数 run で、5h / 7d の上限を他の作業と共有する。trial 数が少ないので有意差は主張しない。
- 残るリスク: 実行前に止める用途には transcript の後追いでは足りない。それが要るかは、評価で測るまで分からない。
- 次に起きること: #130 の P0 (評価スクリプトと手採点との突き合わせ) と P1 (完了済み task の再実行防止) を
  並行で進める。P2 以降は前の段の結果を見てから着手する。
