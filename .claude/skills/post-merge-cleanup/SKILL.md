---
name: post-merge-cleanup
description: PR が merge された後の局所的なクリーンアップを一発で済ませる skill。merge 確認 → main fast-forward → worktree remove → branch -D まで、必要なら dispatcher の dashboard 更新 + 元 author worker への ack も。並列 worker / worktree で開発している環境で merge ごとに繰り返す定型手順を機械化する。「merge した」「PR merged」「PR #N を入れた後」のタイミングで使う。
allowed-tools: Bash, Read, Edit
---

並列 worker + 専用 worktree で複数 PR を回している環境で、merge 後に必ず踏む
定型手順を 1 コマンドにまとめる skill。

## なぜ必要か

worktree 分離（`<repo>-wt-<key>` パターン）で並列開発していると、PR が merge
されるたび同じ 5-6 ステップを踏む:

1. **merge 確認**: 本当に MERGED 状態か（gh pr view で state)
2. **main 同期**: `git fetch --prune && git checkout main && git pull --ff-only`
3. **worktree remove**: `git worktree remove <path>`（残骸防止）
4. **branch -D**: squash & merge 後の branch は `git branch -d` だと「not fully
   merged」で失敗するので `-D` 強制削除が必要
5. （任意）**dashboard 更新**: dispatcher の `dashboards/<project>.md` 上の
   Worker 状態行 + アクティブタスク表を編集
6. （任意）**worker ack**: tmux で元 author の pane に「merged at <time>」を送信

ステップは機械的だが、手で打つと枝刈り漏れや「-d で失敗 → 諦め → 後で `git branch
-a` で気付く」が起きる。2026-05-21 の Wave 3 で 1 日 5 回繰り返したため skill 化。

## 使うタイミング

- ユーザーが「merge した」「PR merged」「入れた」「マージしといて」と言ったとき
- `gh pr merge` を実行した直後
- 並列 worker 環境（squad 等）で、特定の worktree を使い切ったとき
- 単独開発でも `git worktree add` で作業した branch を merge した後
- 複数 PR を順次 merge していて、一括で後処理したいとき
- main の `git status` に残った変更や `git worktree list` の枝刈りミスに気付いたとき

## 前提

- リポジトリは GitHub（`gh` コマンドが使える、認証済み）
- 作業ディレクトリは PR 元 repo の main worktree
- ユーザーが merge 番号（PR 番号）と、対応する worktree path / branch 名を伝えられる
- （Dispatcher 環境のみ）`<squad のルート>/dashboards/<project>.md` が存在

## 必要な情報

skill 起動時、以下を **ユーザー or 会話文脈から特定** する。曖昧なら
AskUserQuestion で確認:

| 必須 | 情報 | 例 |
|---|---|---|
| ✓ | PR 番号 | `103` |
| ✓ | repo | `h-wata/mesh-mem`（main worktree の cwd の origin remote から自動取得可） |
| ✓ | worktree path | `/home/gisen/work/mesh-mem-wt-issue51` |
| ✓ | branch 名 | `feat/issue-51-mcp-proactive-save` |
|   | 元 author worker pane | `ros-agents:0.1`（dispatcher 環境） |
|   | project 名（dashboard 更新時） | `mesh-mem` |

複数 PR を一括処理する場合、(PR, worktree, branch) のセットを並べる。

## 手順

### Step 1: merge 確認

```bash
gh pr view <PR> --repo <owner>/<repo> --json state,mergedAt
# → {"state":"MERGED","mergedAt":"2026-05-21T..."} を確認
```

`state` が `MERGED` でなければ **そこで止める**。OPEN や CLOSED なら原因を確認:
- OPEN: まだ merge されていない → merge 待ち or ユーザー実行依頼
- CLOSED + mergedAt=null: closed without merge → cleanup の意図を再確認

### Step 2: main 同期

main worktree（PR 元 repo の主 working tree）で:

```bash
cd <main-worktree-path>
git fetch origin --prune
git checkout main
git pull --ff-only origin main
```

`fetch --prune` で remote 側で消えた branch ref も同時に枝刈りされる。
`--ff-only` で local commit が混ざっている場合は止まる（安全）。

### Step 3: worktree remove

```bash
git worktree remove <worktree-path>
```

worktree に dirty な変更が残っていれば失敗する。その場合:
1. 残っている変更が必要か確認（cherry-pick が必要なら拾う）
2. 不要なら `git worktree remove --force <worktree-path>`

### Step 4: branch 削除

```bash
git branch -d <branch-name> 2>&1 || git branch -D <branch-name>
```

squash & merge 後の branch は `-d` で「not fully merged」エラーが出る
（merge commit が無いため）。`-D` 強制削除が必要。**先に `-d` を試して、
失敗時のみ `-D`** にすれば、本当に merged 済かを git 経由で確認できて安全。

remote 側 branch は GitHub の `--delete-branch` フラグで削除されているはず
だが、念のため `git fetch --prune` 後に `git branch -a` で remote tracking
の残骸を確認するとよい。

### Step 5: 状態確認

```bash
git worktree list
git branch
```

期待: 削除したはずの worktree / branch が両方消えていること、main は
`origin/main` と一致（`## main` だけ表示）。

### Step 6: （任意）dashboard 更新

Dispatcher 環境 (squad) の場合:

```bash
DASHBOARD="<squad のルート>/dashboards/<project>.md"
INDEX="<squad のルート>/dashboard.md"
```

更新対象:
- 該当 Worker の状態列を「実装中」「待機 (merge 待ち)」→ 「待機 (merge 済)」
  または「待機中」に
- 「Wave X 残タスク」表で当該 PR を「✅ MERGED: …」に書き換え
- 「Codex cross-review 履歴」表に最終 verdict 追加（あれば）
- 必要なら index 側の Worker ステータス表も同期

これは `Edit` ツールで行単位置換するのが楽。複数 PR を一括処理する場合は
1 ファイル開いて連続 Edit する。

### Step 7: （任意）元 worker への ack

Dispatcher 環境で tmux session に worker pane がある場合:

```bash
tmux send-keys -t <session>:0.<pane> "[merge 完了] PR #<N> (<title>) merged at <HH:MM>。お疲れさまでした。引き続き待機していてください。"
sleep 0.5
tmux send-keys -t <session>:0.<pane> Enter
```

**重要**: メッセージと Enter は別コマンドで sleep を挟む。同一 `send-keys
"text" Enter` だと Enter が届かず次メッセージと連結するバグあり
（squad 運用知見）。

### Step 8: 複数 PR の場合

(PR_A, wt_A, br_A) と (PR_B, wt_B, br_B) を順次処理するときは、Step 1-4
を各セットでループ、Step 5-7 は最後に一括でやると効率的。

main 同期 (Step 2) は最初の 1 回だけでよい（merge は GitHub 側で順序付き、
fetch --prune で最新化される）。

## エラーパターンと対処

### `git worktree remove` が失敗

- **「contains modified or untracked files」**: 残った dirty 変更を確認。
  必要なものは cherry-pick で拾い、不要なら `--force` で削除
- **「is a main working tree」**: main worktree を削除しようとしている。
  worktree path を再確認（誤って main を指定していないか）

### `git pull --ff-only` が失敗

- local main に commit が積まれている → ユーザー判断を仰ぐ
  - rebase してから pull するか
  - local commit を別 branch に退避するか
- ネットワーク / 認証エラー → `gh auth status` で確認

### branch 削除が `-d` も `-D` も失敗

- branch が checked out のままになっている可能性 → 別 worktree が使ってないか
  `git worktree list` で確認
- ref が壊れている → `git reflog show <branch>` で状態確認

## アンチパターン

- **`git add -A` 的な雑な後始末をしない**: 削除対象を明示。worktree / branch
  名を会話文脈から特定できない場合は AskUserQuestion で確認する
- **main worktree を `worktree remove` しない**: skill の Step 3 は **作業 worktree
  のみ** が対象。誤って main を指定すると repo が使えなくなる
- **dashboard 更新を skip しない**（Dispatcher 環境のみ）: 後でユーザーが状況を
  見たときに「これもう merge 済？」と聞き直す手間が発生するため、必ず反映する
- **Step 1 (merge 確認) を飛ばさない**: 「merge した」と聞いたら本当に MERGED か
  必ず確認。OPEN のまま cleanup すると WIP を失う

## 例: 単一 PR の最小フロー

```bash
# Step 1
gh pr view 103 --repo h-wata/mesh-mem --json state,mergedAt
# → MERGED

# Step 2-5
cd /home/gisen/work/mesh-mem
git fetch origin --prune
git checkout main
git pull --ff-only origin main
git worktree remove /home/gisen/work/mesh-mem-wt-issue51
git branch -d feat/issue-51-mcp-proactive-save || git branch -D feat/issue-51-mcp-proactive-save
git worktree list
git branch
```

ここまでが skill の **必須範囲**。Step 6-7 は dispatcher / 並列 worker 環境
のみ。

## 例: 並列 2 PR を一括 cleanup（Dispatcher 環境）

```bash
# Step 1 (両方)
gh pr view 102 --repo h-wata/mesh-mem --json state,mergedAt  # MERGED
gh pr view 103 --repo h-wata/mesh-mem --json state,mergedAt  # MERGED

# Step 2 (1 回だけ)
cd /home/gisen/work/mesh-mem
git fetch origin --prune
git checkout main
git pull --ff-only origin main

# Step 3-4 (各 PR)
git worktree remove /home/gisen/work/mesh-mem-wt-issue75
git worktree remove /home/gisen/work/mesh-mem-wt-issue51
git branch -D fix/issue-75-forward-compat-extras feat/issue-51-mcp-proactive-save

# Step 5
git worktree list
git branch

# Step 6: dashboard.md / dashboards/<project>.md を Edit
# Step 7: tmux で W1, W2 に ack 送信
```

## 出力サマリ

skill 完了時、以下をユーザーに 3-5 行で報告:

- merge した PR 番号と時刻
- main の新しい HEAD（短 SHA）
- 削除した worktree / branch
- dashboard を更新したかどうか
- 残っている worktree がある場合、その path（残置の意図確認のため）
