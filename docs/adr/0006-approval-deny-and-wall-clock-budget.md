# ADR 0006: 承認の自動受理から危険操作を外し、実行予算は「通知のみ」の wall-clock 1 本にする

- **Status**: Accepted
- **Date**: 2026-10-21
- **Supersedes**: なし
- **Related**: Issue #54、[ADR 0005](0005-worker-runs-the-local-model-directly.md)

## Context

watchd の `check_workers()` は、worker の pane に承認プロンプト (`APPROVAL_RE`) が出ると
`auto_answer()` で **内容を見ずに** y / Enter を送っていた。もともとは bypass 運用の
保険だったが、結果として `rm -rf` や `git push --force` も無条件に通る経路になっていた。

あわせて、task 単位の実行時間の上限が無かった。長時間止まった worker は Dispatcher が
目視で「想定の 2 倍を超えたら `ps` で確認」していた (`instructions/dispatcher.md`)。
pane が無変化なら停止検知 (`stall_cycles`) が拾うが、**動き続けて終わらない** worker は拾えない。

探索グラフや Trace 共有 (#55 / #56 / #57) で worker の自律度を上げる前に、ここを塞ぐ。

参考にした ARTEX (h-wata/ARTEX a9d11cb) の実装:

- 全ツール呼び出しを guard で包み、allow / deny / ask を判定する。ask は人間の決定まで待つ
  (`guard/guard.go`、`intercept/intercept.go`)。
- 予算はターン数と wall-clock の 2 本だけで、token / cost の上限による強制停止は持たない。

## Decision

1. **危険操作は自動受理しない。** 承認プロンプトの画面 (末尾 40 行) が
   `APPROVAL_DENY_DEFAULT` (`rm -r`、`git push --force/-f`、`git reset --hard`、`git clean -f`、
   `gh pr merge/close`、`gh repo/release delete`、`drop table/database`、`mkfs`、`dd if=`) に
   一致したら、y / Enter を送らずに `[APPROVAL-HOLD]` を priority=critical で Dispatcher に送る。
   同じ画面には 1 回だけ送る。保留中は停止通報もしない。
   - パターンは環境変数 `WATCH_APPROVAL_DENY` (正規表現) で置き換えられる。watchd の他の
     設定がすべて環境変数なので、それに揃えた (Issue 本文の案は `squad/config.json` だった)。
   - Claude Code の `permissions.deny` を置き換えるものではない。deny 済みの操作はそもそも
     プロンプトが出ないので、watchd 側は **自動受理という穴を塞ぐ追加の層** として働く。
2. **実行予算は wall-clock 1 本で、通知のみ。** task YAML の任意フィールド
   `max_wall_clock_min` (top-level) を、`created_at` (読めなければ task YAML の mtime) からの
   経過時間が超えたら `[BUDGET]` を 1 回だけ送る。**worker は止めない。**
   - 通知済みの task は `queue/.budget_notified` に追記して、watcher を再起動しても再通知しない
     (通知 queue の dedupe は ack 後に効かないため、別に永続化した)。
   - フィールドを top-level にしたのは、watchd が stdlib only の `parse_scalars()`
     (top-level のみ) で task を読むため。Issue 本文の案は `budget.max_wall_clock_min` だった。
3. **停止理由を report に残す。** report の任意フィールド `stop_reason`
   (`budget_exceeded | verify_exhausted | blocked_by_user | approval_denied | other`) を足し、
   `scripts/check_report_yaml.py` で値域を検査する。

## 採用しなかった案

- **予算超過で worker を kill する。** 外から止める手段は tmux の send-keys しかなく、作業途中の
  worktree を壊しうる。ARTEX は止める前に settlement ターンで事実を書き戻しているが、Squad で
  同じことをするには worker 側の協力が要る。まず通知だけで運用し、続行・打ち切りは Dispatcher
  / 人間が判断する。
- **token / cost / LLM 呼び出し回数 / tool call 数の上限。** 外部から強制する手段が無く、
  token は `scripts/measure_task_cost.py` の事後計測で足りている。ARTEX も token 上限による
  強制停止は持っていない。
- **画面の最終行だけを deny 判定する。** 誤検知は減るが、Claude Code の承認ダイアログは
  コマンドを数行上に表示するため、取りこぼしの方が危ない。直前に流れた出力に一致して保留に
  なる誤検知は、安全側の失敗として受け入れる。

## Consequences

- 危険操作の承認は、Dispatcher (最終的には人間) を通らないと進まなくなる。deny 一致の保留は
  critical なので、通知 queue を使っていても 300 秒で Pane 0 に再送される。
- 誤検知 (直前の出力に `rm -r` 等が写っているだけ) で worker が待たされることがある。
  頻発するようなら `WATCH_APPROVAL_DENY` を絞るか、判定範囲を狭める。
- `max_wall_clock_min` を書かない既存の task は、挙動が変わらない。
