#!/usr/bin/env python3
# ruff: noqa: CPY001
r"""instructions/*.md 内の {PLACEHOLDER} を実値へ安全に展開する.

sed の s/// や bash の ${var//pattern/replacement} は、置換値に & | \\ 等の
シェル/正規表現メタ文字が含まれると壊れる（& は「マッチ全体」として再解釈される
などの理由で、実際にどちらも同じ壊れ方をする）。str.replace() は常にリテラル文字列
一致による置換のみを行うため、クローン先パスに任意の文字が含まれても安全に展開できる。

使い方: render_prompt.py <template_path> [KEY=VALUE ...]
  各 KEY=VALUE について、テンプレート内の "{KEY}" を VALUE に置換して標準出力へ書く。
"""

import sys


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: render_prompt.py <template> [KEY=VALUE ...]', file=sys.stderr)
        return 1

    template_path = sys.argv[1]
    with open(template_path, 'r', encoding='utf-8') as f:
        content = f.read()

    for pair in sys.argv[2:]:
        key, sep, value = pair.partition('=')
        if not sep:
            print(f'invalid KEY=VALUE pair: {pair!r}', file=sys.stderr)
            return 1
        content = content.replace('{' + key + '}', value)

    sys.stdout.write(content)
    return 0


if __name__ == '__main__':
    sys.exit(main())
