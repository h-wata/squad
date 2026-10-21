# ADR 0008: Worker の実行 trace は新しく保存せず、既存の Claude Code transcript をマスクして検索する

- **Status**: Accepted
- **Date**: 2026-10-21
- **Supersedes**: なし
- **Related**: Issue #56、[ADR 0007](0007-exploration-graph-as-derived-index.md)

## Context

report に書かれるのは結論だけで、途中で失敗したコマンドや観察は残らない。別の worker が
同じ project で同じ失敗を繰り返しても、Dispatcher が blocked の原因を確かめたくても、
worker の transcript を手で開くしかなかった。

一方で、Claude Code worker の全実行過程は既に `~/.claude/projects/<cwd>/<session_id>.jsonl`
に残っている。report には任意の `session_id` があり、`scripts/measure_task_cost.py` が
transcript を特定する関数（exact / 時間窓による近似）を持っている。

参考にした ARTEX (h-wata/ARTEX a9d11cb) の実装:

- trace を `activity` テーブルに保存し、`search_all_worker_traces`（要約のキーワード検索）→
  `get_worker_trace`（ID 指定で詳細）の 2 段階で引かせる (`agent/tools.go`)。一覧は要約だけに
  して、コンテキストの消費を抑えている。
- trace と HTTP traffic は平文で保存していて、マスクは無い (`traffic/traffic.go`)。

## Decision

1. **保存先は増やさない。** `squad trace search / get` は transcript を毎回直接読む。保持期間や
   容量は Claude Code の管理に任せ、squad 側で複製しない。
2. **ARTEX の 2 段階は採る。** `search` は 1 件 1 行（trace_id・task・worker・時刻・tool・
   ERROR・本文 120 文字）、`get <trace_id>` で tool の入力と結果を全文（4000 文字まで）返す。
   `trace_id = <session_id>:<行番号>`。検索の単位は tool_result（入力と結果をまとめる）と
   assistant の text。
3. **出力は必ずマスクする。ARTEX の平文保存は採らない。** `scripts/pi-log-triage.sh` の
   `mask_secrets()` と同じ規則を `squad/mask.py` に移し、同じ入力でテストする。検索もマスク後の
   本文に対して行うので、secret の文字列そのもので引くこともできない。
4. **transcript の特定は `measure_task_cost.py` の関数を import して使う。** `session_id` が
   無い report は時間窓の近似になり、同じ cwd の他 session が混ざりうるので、出力の行頭に `~`
   を付けて区別する。
5. **モジュール名は `trace_search.py`。** Python 標準ライブラリの `trace` を隠さないため
   （Issue 本文の案は `trace.py`）。

## 採用しなかった案

- **trace を sqlite / FTS に取り込む。** 実 queue の全 report で 1 回の検索が約 3.5 秒で足りて
  いる。遅くなってから検討する。
- **MCP ツールにする。** worker も Dispatcher も Bash から CLI を呼べる。
- **Opencode worker の trace。** 形式が違う。必要になったら別 Issue にする。

## Consequences

- 実 queue では report 325 件のうち `session_id` を持つのは 52 件で、残りは近似になる。
  Claude Code が古い transcript を消すため、古い task は「transcript が見つからない」と出る。
- マスクの規則を変えるときは、shell 版と Python 版の両方を直す必要がある。
- 探索グラフ (ADR 0007) の result / fact から trace_id へのリンクは、この上に後から足せる。
