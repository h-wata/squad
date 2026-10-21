#!/usr/bin/env python3
# ruff: noqa: CPY001
"""secret のマスク (scripts/pi-log-triage.sh の mask_secrets() と同じ規則, Issue #56).

行数は変えない (行番号で参照されても壊れないように)。shell 版を直したらこちらも直すこと。
"""

from __future__ import annotations

import re

_AUTH_HEADER = re.compile(r'authorization"?[ \t]*:', re.IGNORECASE)
_KEY_BEGIN = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
_KEY_END = re.compile(r'-----END [A-Z ]*PRIVATE KEY-----')
# BEGIN と同じ行に続く本文 (json.dumps で改行が \n になった鍵など)。END が無ければ行末まで
_KEY_INLINE = re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----).*?(-----END [A-Z ]*PRIVATE KEY-----|$)')
_RULES = (
    (re.compile(r'(Authorization"?\s*:\s*).*', re.IGNORECASE), r'\1<redacted>'),
    (re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]+'), 'Bearer <redacted>'),
    (re.compile(r'\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b'), r'\1_<redacted>'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'), 'github_pat_<redacted>'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), 'AKIA<redacted>'),
    (re.compile(r'(aws_secret_access_key\s*[:=]\s*)\S+', re.IGNORECASE), r'\1<redacted>'),
    (re.compile(r'(https?://)[^/@\s]+:[^/@\s]+@'), r'\1<redacted>@'),
)


def mask(text: str) -> str:
    out: list[str] = []
    fold = False  # 直前が Authorization ヘッダ (次の字下げ行は折り返された値)
    in_key = False  # PRIVATE KEY ブロックの中
    for line in text.split('\n'):
        if fold and line[:1] in (' ', '\t'):
            out.append('[REDACTED FOLDED HEADER VALUE]')
            continue
        fold = bool(_AUTH_HEADER.search(line))
        if _KEY_BEGIN.search(line):
            # shell 版より強い: 1 行に詰まった鍵も伏せる (transcript は JSON なので鍵が 1 行になる)
            # 最後の BEGIN が END で閉じていなければ次の行も鍵 (同じ行に END...BEGIN と続く場合も)
            in_key = not list(_KEY_INLINE.finditer(line))[-1].group(2)
            line = _KEY_INLINE.sub(r'\1[REDACTED PRIVATE KEY]\2', line)
        elif _KEY_END.search(line):
            in_key = False
        elif in_key:
            out.append('[REDACTED PRIVATE KEY LINE]')
            continue
        for pattern, repl in _RULES:
            line = pattern.sub(repl, line)
        out.append(line)
    return '\n'.join(out)
