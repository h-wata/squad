# github-writing skill を有効にする

このリポジトリを clone してシンボリックリンクを張っただけでは自動発火しない。発火の
トリガーは `~/.claude/CLAUDE.md` 側にあり、このリポジトリには含まれないため。

## 手順

1. シンボリックリンクを張る:
   `ln -s $(pwd)/skills/github-writing ~/.claude/skills/github-writing`
2. `~/.claude/CLAUDE.md` に次の2行を追加する:

   ```
   ## GitHub 向け文章

   - PR 本文・Issue/PR スレッドのコメント・レビューコメントを書く前に `github-writing` skill を読む
   ```

2 を追加しないと、skill 自体は存在していても自動発火しない。
