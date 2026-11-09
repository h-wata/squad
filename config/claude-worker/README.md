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
