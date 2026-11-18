---
name: inherit-wip
description: 前任 worker / 自分が中断したタスクの未コミット WIP を引き継いで完了させる。git diff レビュー → 要件チェックリスト照合 → 不足分補完 → tests/CHANGELOG → 単一 commit + push まで一気通貫。
allowed-tools: Bash, Read, Edit, Write, Glob, Grep
---

中断したタスクや別 worker の未コミット作業を引き継ぐためのスキル。

複数 worker 並列環境や、自分自身が context limit / セッション中断で作業を残した場合に、
git の working tree 差分から状態を逆算して仕上げる手順をパターン化する。

## 使うタイミング

- `git status` で modified ファイルがあるが、誰が / 何の目的で書いたか即座に分からない
- 「TASK-XXX が WIP のまま放置されている」と知っているが、conversation context は失われた
- 別 worker / 自分の前回セッションのコミットが未 push で残っている
- 設計書や task YAML は残っているが、実装の進捗状況を git diff から再構築する必要がある

## 引数

`$ARGUMENTS` には以下のいずれかを指定:
- 元タスクの YAML パス（例: `queue/tasks/worker2.yaml`）
- 関連 issue 番号（例: `#7 Phase 3`）
- 関連 commit hash（例: `8b06c14 直後の WIP`）

引数が無い場合は `git status` と `git log -10` から最近のコンテキストを推定。

## 手順

### 1. WIP 把握フェーズ

最初に、task YAML と同じ project に `queue/projects/<pj>/briefs/<task_id>.md` があれば先に読む。
brief は watchd がタスク途中で区切ったときに worker が書いた引継ぎで (ADR 0011)、「2. 今の意図と次の一手」と
「4. 再開の手がかり」が git の状態を逆算する出発点になる。brief と git が食い違えば git を正とする。

```bash
# 現在の working tree
git status -s

# 変更ファイルの差分（large diff は head で抑える）
git diff --stat
git diff -- <ファイル> | head -200

# 直近コミットと branch 状態
git log --oneline -10
git status -b -s | head -3   # ahead/behind 確認

# 元タスクが指定されていれば読む
cat $ARGUMENTS  # task YAML
```

**読む順序の鉄則**:
1. まず `git diff --stat` で **どのファイルがどれだけ変わったか** を俯瞰
2. 次に `cat <task.yaml>` で **何を達成すべきか** の acceptance_criteria を確認
3. 最後に各ファイルの diff を見て **要件のどこまで実装済みか** を照合

### 2. 要件チェックリスト照合

task YAML / 設計書の acceptance_criteria を読み下し、各項目を以下の 3 状態で分類:

- ✅ 実装済み（diff から確認できた）
- 🟡 部分実装（コードはあるが test / CHANGELOG / lint 未対応）
- ❌ 未着手（実装そのものがない）

不足項目をチェックリストとして書き出す（report に残す）。

### 3. 補完実装フェーズ

不足分を順次実装。ただし以下は厳守:

- **既存 WIP を尊重する**: 前任者の判断（命名・構造・抽象度）を理解してから書く。理由なく書き直さない
- **scope creep を避ける**: acceptance_criteria に無い改善は別タスク化を提案するに留める
- **コミット粒度は 1 本に揃える**: 引継ぎ作業を引継ぎ commit 1 本に集約。元 WIP と分けない

### 4. test / lint / CHANGELOG

```bash
# プロジェクトの規約に合わせて実行
ruff check --fix src/ 2>/dev/null || true
yapf -i <変更ファイル> 2>/dev/null || true
PYTHONPATH=src python -m pytest tests/ -v -x 2>&1 | tail -50

# CHANGELOG への entry 追加（プロジェクトに CHANGELOG.md があれば）
```

### 5. コミット + push（pathspec 限定）

並行 worker の他作業が working tree に紛れている可能性があるため、
**必ず pathspec で対象ファイルを限定** して commit する:

```bash
git fetch origin <main branch>
git pull --rebase origin <main branch>  # 衝突なきこと

# pathspec で限定（git add . は使わない）
git add <ファイル1> <ファイル2> ...

# または git commit -- pathspec
git commit -m "$(cat <<'EOF'
<conventional commit prefix>: <要約>

<経緯と引継ぎの位置付け（前任 commit hash を明記）>

Refs #<issue> または Closes #<issue>
EOF
)"

git push origin <branch>
```

**Co-Authored-By 行は付けない**（明示的に user 指示があった場合のみ）。

### 6. 報告

引継ぎ完了の報告には以下を必ず含める:

- 既に実装済みだった項目（変更不要、引継ぎ前の状態）
- 補完実装した項目（差分の要点）
- test / lint 結果
- commit hash と push 結果
- rebase の有無、衝突解決の概要
- acceptance_criteria の最終チェック結果

## 注意点

- **前任者の WIP を捨てない**: `git checkout <file>` で消すのは cosmetic な lint 差分など、
  pre-commit hook と衝突する明らかな雑音だけに留める
- **未トラッキングファイルの扱い**: `.claude/` のような worker 設定や lock ファイルは
  そのまま untracked で放置（リポに含めない）
- **rebase 衝突時は停止**: 自動マージせず user に報告する。merge conflict marker (`<<<<<<<`) が
  残ったまま commit しない
- **CHANGELOG への entry 追加**: 並行 worker も CHANGELOG を触る可能性が高い。push 直前に
  必ず `git fetch && git pull --rebase` を実行し、衝突したら両方の entry をマージ
