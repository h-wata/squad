---
name: safe-pathspec-commit
description: 並行 worker / 別作業の未コミット WIP を巻き込まずに、対象ファイルだけを安全に commit する。git status で並行 WIP を検出 → 対象パスのみ stage → pathspec で commit までの定型手順。
allowed-tools: Bash
---

複数 worker 並列環境や `git add .` を使えない状況で、
対象ファイルだけを正確に commit するためのスキル。

## なぜ必要か

mesh-mem PoC で、Worker 2 の `gc --project` commit に Worker 1 の Phase 3 in-progress
（`__main__.py`）が混入する事故があった。`git add` のスコープを誤ると、
別 worker の未コミット作業が自分の commit に取り込まれ、commit メッセージと内容の帰属がズレる。

このスキルは「**自分が触ったファイルだけ**を確実に拾い、それ以外は触らない」
手順を機械化する。

## 使うタイミング

- 並行 worker / 別 task が同じリポジトリで作業している
- working tree に modified / untracked ファイルが多数あり、自分の作業と他人の作業が混在
- pre-commit hook が複数ファイルを reformat する（自分の変更だけに絞りたい）
- 引継ぎ commit で前任者の cosmetic 差分を巻き込みたくない

## 引数

`$ARGUMENTS` でコミットしたいパスとメッセージを指定:

```
paths: <空白区切りのファイルパスリスト>
message: |
  <commit message body>
issue_ref: <例: "Closes #11" or "Refs #7">
push: true / false（default: true）
```

または短く `paths`/`issue_ref` のみ位置引数で指定し、message はインタラクティブ生成。

## 手順

### 1. 並行 WIP の検出

```bash
# working tree 全体の状態を把握
git status -s

# 自分が触る予定のパスと、それ以外を区別
echo "=== 自分が触る予定: ==="
echo "<paths>"
echo "=== その他の WIP（巻き込まないこと） ==="
git status -s | grep -vE "$(echo <paths> | sed 's/ /|/g')"
```

「その他の WIP」が空でなければ、それは別 worker / 別作業の状態。
**ユーザに警告し、そのまま続けて良いか確認**（auto mode でもここは確認推奨）。

### 2. 対象ファイルだけを stage

```bash
# git add . / git add -A は使わない
git add <paths>

# stage 内容を確認
git diff --cached --stat
```

`git status -s` で `M` (staged modified) になっているのは指定パスだけであることを確認。

### 3. pre-commit hook に対応

pre-commit hook が走って format 修正を入れる場合がある。その場合は:

```bash
# 1 回目の commit で hook が再 format
git commit -m "<message>" || {
    # hook が修正した内容を再 add（指定パスだけ）
    git add <paths>
    # 2 回目の commit
    git commit -m "<message>"
}
```

**重要**: hook が他のファイルを reformat した場合、そのファイルは stage しない。
`git diff --cached` で stage 内容が想定通りであることを再確認。

### 4. 最終 commit（pathspec を再指定）

完全に確実を期す場合は `git commit -- <paths>` で pathspec を渡す:

```bash
git commit --only <paths> -m "$(cat <<'EOF'
<conventional commit prefix>: <要約>

<本文>

<issue_ref>
EOF
)"
```

`--only` フラグは「指定パスだけを commit、他の stage 内容は維持」する挙動。
これにより hook 後の再 stage で漏れがあっても安全。

### 5. push（必要に応じて）

```bash
git fetch origin <branch>
git status -b -s | head -3   # ahead/behind 確認

# 並行作業があれば rebase
git pull --rebase origin <branch>

git push origin <branch>
```

**rebase で衝突したら停止**し、user に報告。自動マージしない。

### 6. 確認

```bash
git log --oneline -1
git show --stat HEAD   # 自分の commit に余計なファイルが入っていないか
git status -s          # working tree の他 WIP は維持されているか
```

「他 WIP は触らず維持」が確認できれば成功。

## 注意

- **`git add .` / `git add -A` を使わない**: 並行 worker のファイルを巻き込む
- **`git commit -a` を使わない**: 同上
- **hook が他ファイルを変更したら stage しない**: 自分の commit には含めない
- **rebase 後も pathspec を再確認**: rebase で working tree が変わる場合があるため
- **Co-Authored-By 行は付けない**: 明示指示がある場合のみ
- **commit メッセージで他 worker の作業を主張しない**: 自分が書いたものだけが対象

## アンチパターン

```bash
# NG: git add . で全部巻き込む
git add . && git commit -m "..."

# NG: git commit -a で modified を全部 commit
git commit -am "..."

# NG: 他 worker の WIP を「ついでに」コミット
git add my_file.py other_worker_wip.py && git commit -m "fix my thing"
```
