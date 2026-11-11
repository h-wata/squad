# ADR 0010: Claude worker は session・worker ごとの CLAUDE_CONFIG_DIR で ask を外して動かす

- **Status**: Accepted
- **Date**: 2026-11-11
- **Supersedes**: なし
- **Related**: #104、#106、#107、[ADR 0005](0005-worker-runs-the-local-model-directly.md)、
  [ADR 0006](0006-approval-deny-and-wall-clock-budget.md)

## Context

`~/.claude/settings.json` の `permissions.ask` には `Bash(git commit*)` `Bash(rm *)` など、worker が
必ず打つコマンドが並んでいる。`ask` は project 側の `permissions.allow`、
`--permission-mode bypassPermissions`、`--dangerously-skip-permissions` のどれでも上書きできない。
tmux の pane には承認する人がいないので、worker は無言で止まる。

ADR 0005 の claude-local worker は、`CLAUDE_CONFIG_DIR=config/claude-worker` で user 設定の出所ごと
差し替えてこれを避けていた。Claude モデルの worker は `~/.claude` の OAuth 認証に頼っていたため、
同じ方法が使えなかった。

## Decision

1. **Claude モデルの worker も、長期トークンを置いたときだけ専用の `CLAUDE_CONFIG_DIR` で動かす。**
   `claude setup-token` のトークンを `~/.config/squad/claude-oauth-token` に置くと、start.sh は
   `CLAUDE_CODE_OAUTH_TOKEN` で認証し、`bypassPermissions` で起動する。トークンが無い・読めない・
   空白だけのときは従来どおり `~/.claude` で起動する (#104)。
2. **`CLAUDE_CONFIG_DIR` は `config/claude-worker/<session>/w<n>/` に分ける。** Claude Code は
   `/model` などでその中の `settings.json` を書き換える。1 つを共有すると、1 人の切り替えが他の
   worker と別の squad session、repo の追跡ファイルに波及した。追跡するのはひな形
   `settings.base.json` だけにし、start.sh が起動のたびに各ディレクトリへ書き出す (#107)。
3. **session が終わってもディレクトリは消さない。** 中の `projects/` には transcript が溜まり、
   `measure_task_cost.py`、`squad trace`、作業の再開がそれを読む。
4. **起動時の確認はすべて start.sh が先に済ませる。** 初回オンボーディング、フォルダの信頼、
   CLAUDE.md の外部 import の確認 (読み込まない側) を `.claude.json` に書いておく (#106)。
5. **worker が使うものは squad 側で持つ。**
   - MCP は `~/.claude.json` から kioku_mesh だけを写す (#104)。
   - skill は squad repo の `.claude/skills/` にコピーして置く。worker は `--add-dir` で squad
     repo を読むので、作業ディレクトリやトークンの有無に関係なく見える (#106)。
6. **危険な操作は `settings.base.json` の deny で止める。** `bypassPermissions` では承認の画面が
   出ないので、ADR 0006 の watchd による保留 (`APPROVAL_DENY_DEFAULT`) は働かない。同じ操作
   (`rm -r`、`git push --force`、`git reset --hard`、`git clean -f`、`gh pr merge/close`、
   `gh repo/release delete`、`mkfs`、`dd if=`) を deny に書き、実行そのものを拒否させる (#107)。

## 採用しなかった案

- **`~/.claude/settings.json` の ask を外す。** 手元の対話で使う安全弁がなくなる。worker のためだけに
  変えたくない。
- **skill を `~/.claude/skills` へのリンクで見せる。** 手元の skill フォルダの変更に worker が
  引きずられる。必要なものだけコピーし、squad 側を正として更新する。codex-review はコードを外部に
  送るので入れていない。
- **起動のたびに一意な名前のディレクトリにする / session 終了時に消す。** 前回の会話を再開できなくなり、
  transcript による計測も失われる。同じ名前の tmux session は同時に立たないので、session 名で
  分ければ動いている間に衝突しない。

## Consequences

- deny は Bash のコマンド前方一致なので完全ではない。別のコマンドや書き方では抜けうる。トークンも
  worker の環境変数 `CLAUDE_CODE_OAUTH_TOKEN` から読めるので、worker からは読める前提で扱う。
  確実に防ぐには sandbox やコンテナが要る。
- watchd の危険操作リストと `settings.base.json` の deny は二重管理になる。片方を変えたらもう片方も
  直す。`drop table/database` は psql などの中で打たれるため deny では拾えず、穴として残る。
- `~/.claude` 側の plugins、自分用 `CLAUDE.md`、status line は worker から見えない。status line は
  #105 で squad 用のものを用意する。
- worker ごとのディレクトリは溜まり続ける。増えすぎたら、最終更新から日数の経ったものを消す掃除を
  足す。
- 動いている worker は、起動し直すまで古い共有ディレクトリ (`config/claude-worker/` 直下) を使い続ける。
