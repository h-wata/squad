# claude-worker: worker 用の CLAUDE_CONFIG_DIR

`SQUAD_W*_AGENT=claude-local` の worker が `CLAUDE_CONFIG_DIR` として使うディレクトリ。

## なぜ要るか

`~/.claude/settings.json` の `permissions.ask` には `Bash(git commit*)` `Bash(rm *)`
`Bash(git push*)` など、worker が必ず打つコマンドが並んでいる。対話で使う分には妥当な
安全弁だが、`ask` ルールは

- project 側の `permissions.allow`
- `--permission-mode bypassPermissions`
- `--dangerously-skip-permissions`

のいずれでも**上書きできない**（3 つとも実測で確認済み）。tmux の pane には承認する人が
いないため、worker は無言で停止し、Dispatcher からは「作業中」に見えたまま止まり続ける。

`CLAUDE_CONFIG_DIR` をこのディレクトリに向けると user 設定の出所ごと差し替わるため、
**手元の対話用設定に一切触れずに** worker だけ ask を外せる。

## 認証について

`CLAUDE_CONFIG_DIR` は認証情報の置き場も切り替える。

- **ローカルモデル worker** (`claude-local`): `ANTHROPIC_BASE_URL` /
  `ANTHROPIC_AUTH_TOKEN` で認証するため、このディレクトリに credentials は要らない。
- **Claude モデルの worker** (`claude`): `claude setup-token` で発行した長期トークンを
  `~/.config/squad/claude-oauth-token` (`SQUAD_CLAUDE_TOKEN_FILE` で変更可、権限 600) に置くと、
  start.sh は claude worker もこのディレクトリで起動し、`CLAUDE_CODE_OAUTH_TOKEN` で認証する。
  トークンは pane のコマンド行に出さず、起動時に `$(cat ...)` で読む。ファイルが無ければ従来どおり
  `~/.claude` の設定 (ask あり) で起動する。

## 既知の制約 (Claude モデルの worker)

- `~/.claude` 側の skills / plugins / 自分用 `CLAUDE.md` / user MCP は見えない。worker が使う
  kioku_mesh だけは、start.sh が起動のたびに `~/.claude.json` からこのディレクトリの
  `.claude.json` に写す。worker が使う skill は squad repo の `.claude/skills/` に置いてあり、
  `--add-dir` で読み込むので、こちらはトークンの有無に関係なく見える。
- `settings.json` で起動時の既定モデルを `sonnet` にしている。タスクごとの切り替えは今までどおり
  Dispatcher が `/model` で行う。
- `settings.json` の deny はトークンを守りきれない。Bash の deny はコマンドの前方一致なので
  `head` や `python3` では読めるし、トークンは worker の環境変数 `CLAUDE_CODE_OAUTH_TOKEN` にも
  入っている。worker からは読める前提で扱うこと。

## status line

`settings.json` の `statusLine` は squad 用の `scripts/statusline.py` を指す (#105)。画面の最下行に
`w1 | Opus 5.5 | ctx 12% | SQUAD-301` (worker ID・モデル・ctx 使用率・作業中の task_id) を出し、同じ値を
`squad/state/<worker>.json` に取得元 `statusline` と時刻つきで書く。`squad ls` と `squad web` の model・ctx は
これを第一の取得元にする。

- パスは `${CLAUDE_CONFIG_DIR}/../../scripts/statusline.py`。command はシェル経由で動くので環境変数が展開される
- start.sh は worker と Dispatcher の `--settings` にも同じ statusLine を足す。`--settings` の方が優先されるので、
  `~/.claude` で起動する worker (トークン無し) と Dispatcher にも squad の status line が出る。
  `~/.claude/settings.json` の statusLine は書き換えない
- ctx が取れないとき (応答前など) は `ctx 不明` と出す。0% にはしない
- 受け取る JSON の項目は `docs/statusline-stdin-json.md` (実測)
