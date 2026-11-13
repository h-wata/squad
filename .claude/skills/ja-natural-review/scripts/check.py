# ruff: noqa: CPY001  個人リポジトリなので会社の著作権表記は付けない
"""日本語の md から機械的な書き癖を拾う. 標準ライブラリだけで動く.

    python3 check.py 文書.md [...]
    python3 check.py --selftest

パターンは textlint-ja/textlint-rule-preset-ai-writing (MIT, azu) と
geonwoo-jeong/japanese-humanizer (MIT) の quick-rules を元に, 正規表現で拾える分だけ移した.
コードブロックとインラインコードは見ない. 拾ったものは見直す候補で, 全部直す義務はない.
"""

import re
import sys

HYPE = (
    '革命的',
    '究極の',
    '魔法のよう',
    'パラダイムシフト',
    '圧倒的',
    '劇的',
    '画期的',
    '完璧な',
    'シームレス',
    '包括的',
    '不可欠',
    '鍵となる',
    'ゲームチェンジャー',
    '最先端',
    '飛躍的',
)
CLOSING = ('このように', 'まとめると', '結論として', 'いかがでしたか', 'が期待されます', 'していきましょう')
# (ルール名, 正規表現, 説明). 1行ずつ当てる. 表の行にも当てる
LINE_RULES = [
    (
        'list-bold-label',
        re.compile(r'^\s*([-*+]|\d+\.)\s+\*\*[^*]+\*\*\s*[:：\-–—]'),
        '太字ラベル付きの箇条書き。表・文・見出しにできないか',
    ),
    ('list-emoji', re.compile(r'^\s*[-*+]\s*(✅|❌|💡|🔥|🚀|⭐|🎯|📝|⚠️|👉)'), '箇条書きの先頭の絵文字'),
    ('heading-bold', re.compile(r'^#+\s.*\*\*'), '見出しの中の太字'),
    ('inline-bold', re.compile(r'[^\s*|>-]\*\*[^*]+\*\*'), '文中の太字。強調しなくても読める文にできないか'),
    ('hype', re.compile('|'.join(HYPE)), '大げさな言葉。対象・条件・理由を前に出せないか'),
    (
        'redundant',
        re.compile(r'することができ|することが可能|(検討|実施|確認|対応|改善|作成|実行|調査)を行|まず最初に'),
        '冗長な言い回し（「できる」「検討する」に）',
    ),
    ('humble', re.compile(r'させていただ'), '「させていただく」。1か所に絞るか「します」に'),
    ('closing', re.compile('|'.join(CLOSING)), '型どおりの締め。本文の判断・条件・次の行動で終えられないか'),
]
# 述語のあとのコロンで, 次の行がリスト・表・コード・引用になっているもの
COLON = re.compile(r'(ます|です|した|する|ある|いる|例えば|以下|次のとおり|次の通り)\s*[:：]\s*$')
BLOCK = re.compile(r'^\s*([-*+]\s|\d+\.\s|\||```|>)')
SENTENCE_LIMIT = 100


def strip(line: str) -> str:
    return re.sub(r'`[^`]*`', '', line)


def check(text: str) -> list[tuple[int, str, str, str]]:
    found = []
    lines = text.splitlines()
    fenced = False
    for no, raw in enumerate(lines, 1):
        if raw.lstrip().startswith('```'):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = strip(raw)
        for name, pattern, why in LINE_RULES:
            if m := pattern.search(line):
                found.append((no, name, why, m.group(0).strip()))
        following = next((x for x in lines[no:] if x.strip()), '')
        if COLON.search(line) and BLOCK.search(following):
            found.append((no, 'colon-block', '述語のあとのコロンでリストや表につないでいる', line.strip()[-20:]))
        if '→' in line and not line.lstrip().startswith('|'):
            found.append((no, 'arrow', '文を矢印でつないでいる。「〜して、〜する」に', line.strip()[:30]))
        if not line.lstrip().startswith('|'):
            for sentence in re.split(r'(?<=[。！？])', line):
                if len(sentence.strip()) > SENTENCE_LIMIT:
                    found.append(
                        (
                            no,
                            'long-sentence',
                            f'{SENTENCE_LIMIT}字を超える文。意味の切れ目で分ける',
                            sentence.strip()[:30] + '…',
                        )
                    )
    return found


def selftest() -> None:
    hits = {
        name
        for _, name, _, _ in check(
            """- **結論**：使う
- ✅ 済み
## **重要**
これは**とても**大事です。
革命的な仕組みで、検討を行うことができます。
させていただきます。このように進めます。
次のとおりです：
- a
A → B
```
- **コードの中**：拾わない
```
"""
            + 'あ' * 101
            + '。'
        )
    }
    expected = {
        'list-bold-label',
        'list-emoji',
        'heading-bold',
        'inline-bold',
        'hype',
        'redundant',
        'humble',
        'closing',
        'colon-block',
        'arrow',
        'long-sentence',
    }
    assert hits == expected, hits ^ expected
    assert check('| 前 → 後 | 名詞：説明 |\n普通の文です。\n使い方:\n- a') == [], check('使い方:\n- a')
    print('selftest ok')


def main(paths: list[str]) -> int:
    total = 0
    for path in paths:
        with open(path, encoding='utf-8') as f:
            for no, name, why, excerpt in check(f.read()):
                print(f'{path}:{no}: [{name}] {why}: {excerpt}')
                total += 1
    print(f'{total} 件', file=sys.stderr)
    return 1 if total else 0


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        selftest()
    else:
        sys.exit(main(sys.argv[1:]))
