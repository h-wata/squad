# status line コマンドが stdin で受け取る JSON (実測)

`scripts/statusline.py` が読む JSON の項目を、実セッションで描画させて確かめた結果 (#105)。

- 実測日時: 2026-11-11 11:57 (JST)
- Claude Code: 2.1.322 (`claude --version`)
- モデル: Haiku 5.5 (`--model haiku`)
- fixture: `tests/fixtures/statusline_stdin_before_reply.json` (起動直後) と
  `tests/fixtures/statusline_stdin_after_reply.json` (1 往復後)。パスと ID は伏せてある

## 取り方

本物の設定を変えないように、一時ディレクトリだけで行った。

1. `mktemp -d` の下に、stdin をファイルに書いて 1 行返すだけのスクリプトを置く
   (`cat > "$SCR/stdin-NNN.json"; echo probe`)
2. 同じ場所に `{"statusLine": {"type": "command", "command": "<スクリプトの絶対パス>"}}` だけの settings を置く
3. 本物とは別名の tmux セッション (`probe105`) で `claude --model haiku --settings <その settings>` を対話起動し、
   起動直後に 1 回、`Reply with just the word OK.` に答えた後に 1 回、描画させる
4. 終わったら `tmux kill-session -t probe105`。`~/.claude/settings.json` と `config/claude-worker/settings.json`
   は変えていない

## キー一覧

| キー | 例 | 意味 |
|---|---|---|
| `session_id` | (UUID) | セッション ID |
| `transcript_path` | `"~/.claude/projects/<cwd>/<session_id>.jsonl"` | transcript。`CLAUDE_CONFIG_DIR` を変えるとその下になる |
| `cwd` | | 起動ディレクトリ |
| `scratchpad_dir` | | セッションの scratchpad |
| `prompt_id` | | 1 往復後だけある |
| `session_name` | | 1 往復後だけある (自動で付く題名) |
| `effort.level` | `"medium"` | |
| `model.id` | `"claude-haiku-5-5"` | |
| `model.display_name` | `"Haiku 5.5"` | 画面に出す名前。status line はこれを使う |
| `workspace.current_dir` / `project_dir` / `added_dirs` | | `--add-dir` の一覧が `added_dirs` |
| `version` | `"2.1.322"` | |
| `output_style.name` | `"default"` | |
| `cost.total_cost_usd` / `total_duration_ms` / `total_api_duration_ms` / `total_lines_added` / `total_lines_removed` | | 累計 |
| `context_window.total_input_tokens` / `total_output_tokens` | `62321` / `253` | 下の `current_usage` の入力側合計と同じ値だった |
| `context_window.context_window_size` | `1000000` | window の大きさ (トークン数) |
| `context_window.current_usage` | 下記 | 直近の API 応答の usage。起動直後は `null` |
| `context_window.used_percentage` | `6` | 使用済みの割合 (整数の %)。起動直後は `null` |
| `context_window.remaining_percentage` | `94` | 残りの割合。起動直後は `null` |
| `exceeds_200k_tokens` | `false` | |
| `prompt_cache.*` | | キャッシュの状態。1 往復後だけある |
| `fast_mode` | `false` | |
| `thinking.enabled` | `true` | |
| `rate_limits.five_hour` / `seven_day` | `{"used_percentage": 11, "resets_at": ...}` | 1 往復後だけある |

`context_window.current_usage` の中身 (1 往復後):

```json
{"input_tokens": 2, "output_tokens": 253, "cache_creation_input_tokens": 34678, "cache_read_input_tokens": 27641}
```

## ctx として何を使うか

- `used_percentage` は **使用済み** の割合。`(2 + 34678 + 27641) / 1000000 = 6.2%` に対して `6` だった。
  丸め方 (切り捨てか四捨五入か) は、この 1 例では区別できない (未確認)
- `remaining_percentage` は `100 - used_percentage`
- 起動直後と、応答がまだ無いときは `current_usage` も `used_percentage` も `null`。0 ではない
- transcript の最後の assistant メッセージの `usage` (input + cache read + cache creation) は 62321 で、
  `current_usage` の同じ 3 項目の合計と一致した

`scripts/statusline.py` は `used_percentage` を ctx、`current_usage` の 3 項目の合計をトークン数、
`context_window_size` を window の大きさとして `squad/state/<worker>.json` に書く。`null` は `null` のまま書き、
画面と `squad ls` / `squad web` には「不明」と出す。

## Issue #105 の想定との差

- 想定どおり、JSON にモデル名と ctx 使用率とトークン数が入っていた。transcript から計算しなくても使用率が取れる
- 使用率は応答が 1 回あるまで `null`。`/clear` 直後も同じと考えられるが、`/clear` は実測していない (未確認)
- transcript だけでは window の大きさが分からない。status line が書いた `context_window_size` があるときだけ、
  transcript のトークン数から使用率を出す
- Stop hook の時点では、そのターンの最後の assistant メッセージがまだ transcript に書かれていなかった
  (2 ターン目の Stop で読んだ値が 1 ターン目のものだった)。transcript 由来の値は 1 ターン遅れることがある

## 起動のしかたで分かったこと

- `--settings` に渡した statusLine は `~/.claude/settings.json` の statusLine より優先された
- command はシェル経由で動き、環境変数が展開された (`"${PROBE_ROOT}/probe.sh"` が動いた)。
  `config/claude-worker/settings.json` はこれを使い `"${CLAUDE_CONFIG_DIR}/../../scripts/statusline.py"` を指す
- pane に設定した `SQUAD_WORKER_ID` などの環境変数は command にも渡る
