# Ponytail 連携 (任意)

ponytail プラグインの squad での扱い。README から移した。

[ponytail](https://github.com/DietrichGebert/ponytail) は AI エージェントに「怠惰な
シニア開発者」規範 (YAGNI、stdlib 優先、最小差分) を注入するプラグイン。squad は
ロール別に組み込み済みで、プラグインを導入するだけで有効になる。未導入なら何も
起きない (環境変数が無視されるだけ)。

導入 (一度だけ、claude 内で):

```
/plugin marketplace add DietrichGebert/ponytail
/plugin install ponytail@ponytail
```

ロール別の挙動 (`start.sh` が `PONYTAIL_DEFAULT_MODE` で制御):

- Worker 1-3 (Claude): `full` - ladder (YAGNI → 既存コード → stdlib → native → 1行) を
  適用して実装する。レベルを変えたい場合は `start.sh` の該当行を編集 (lite/full/ultra)
- Dispatcher: `off` - コードを書かないためルールセット注入はコンテキストの無駄
- Worker 4 (Codex): プラグイン機構がないため `instructions/worker-codex.md` に規範を直接
  記載済み。cross-review でも過剰設計を指摘する

補足: ponytail のモードフラグ (`~/.claude/.ponytail-active`) は Claude セッション間で
共有されるが、影響するのはサブエージェント注入とステータスライン表示のみ。各 window の
メインルールセットは起動時に環境変数から個別に決まる。なお `off` の Dispatcher window で
`/clear` すると SessionStart が再発火してこのフラグが削除され、Worker 側のサブエージェント
注入も Worker 側で次に SessionStart (起動 / `/clear`) が走るまで止まる (メインルール
セットは影響を受けない)。Worker が意図的簡略化に残す
`ponytail:` コメントは `/ponytail-debt` で台帳化できる。
