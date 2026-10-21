#!/usr/bin/env python3
# ruff: noqa: CPY001
"""squad/mask.py のテスト. scripts/tests/test_pi_log_triage_masking.sh と同じ入力を使う."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'squad'))
from mask import mask  # noqa: E402

CASES = [
    (
        'QWxhZGRpbjpvcGVuIHNlc2FtZQ==',
        ['2026-08-19T16:18:00Z AUTHORIZATION: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ=='],
    ),  # pragma: allowlist secret
    (
        'dGVzdC1sb3dlcmNhc2UtdmFsdWU=',
        ['curl -H "authorization: Basic dGVzdC1sb3dlcmNhc2UtdmFsdWU="'],
    ),  # pragma: allowlist secret
    (
        'sk-should-be-masked-1234567890',
        ['2026-08-20T00:00:01Z token=Bearer sk-should-be-masked-1234567890'],
    ),  # pragma: allowlist secret
    (
        'ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
        ['export TOKEN=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'],
    ),  # pragma: allowlist secret
    (
        'gho_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB',
        ['export TOKEN=gho_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB'],
    ),  # pragma: allowlist secret
    (
        'ghu_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC',
        ['export TOKEN=ghu_CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC'],
    ),  # pragma: allowlist secret
    (
        'ghs_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD',
        ['export TOKEN=ghs_DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD'],
    ),  # pragma: allowlist secret
    (
        'github_pat_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE',  # pragma: allowlist secret
        [
            'export TOKEN=github_pat_EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE'
        ],  # pragma: allowlist secret
    ),
    ('AKIAABCDEFGHIJKLMNOP', ['AKIAABCDEFGHIJKLMNOP is the key id']),  # pragma: allowlist secret
    (
        'abcd1234EXAMPLEsecretkeyvalue0000000000',  # pragma: allowlist secret
        ['aws_secret_access_key=abcd1234EXAMPLEsecretkeyvalue0000000000'],  # pragma: allowlist secret
    ),
    (
        's3cr3tpassw0rd',
        ['git clone https://alice:s3cr3tpassw0rd@github.com/example/repo.git'],  # pragma: allowlist secret
    ),
    (
        'b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY',  # pragma: allowlist secret
        [
            '-----BEGIN OPENSSH PRIVATE KEY-----',  # pragma: allowlist secret
            'b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY',  # pragma: allowlist secret
            '-----END OPENSSH PRIVATE KEY-----',
        ],
    ),
    ('json-value+/=', ['{"authorization":"Basic json-value+/="}']),  # pragma: allowlist secret
    ('Basic folded-value+/=', ['Authorization:', ' Basic folded-value+/=']),  # pragma: allowlist secret
]


@pytest.mark.parametrize(('secret', 'lines'), CASES)
def test_secret_is_masked(secret: str, lines: list[str]) -> None:
    assert secret not in mask('\n'.join(lines))


def test_structure_is_kept() -> None:
    text = '\n'.join(
        [
            'AUTHORIZATION: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==',  # pragma: allowlist secret
            '-----BEGIN OPENSSH PRIVATE KEY-----',  # pragma: allowlist secret
            'b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY',  # pragma: allowlist secret
            '-----END OPENSSH PRIVATE KEY-----',
            'FAILED tests/test_x.py::test_y',
            '  indented line after a normal line',
        ]
    )
    out = mask(text)
    assert out.count('\n') == text.count('\n')
    assert out.splitlines()[-2:] == ['FAILED tests/test_x.py::test_y', '  indented line after a normal line']


def test_private_key_on_one_line_is_masked() -> None:
    r"""Transcript の tool 入力は json.dumps されるので、鍵の改行が \n になり 1 行に詰まる."""
    body = 'b3BlbnNzaC1rZXktdjEAAAAAB3NzaC1yc2EAAAADAQABAAAB000EXAMPLEBODY'  # pragma: allowlist secret
    begin, end = '-----BEGIN OPENSSH PRIVATE KEY-----', '-----END OPENSSH PRIVATE KEY-----'  # pragma: allowlist secret
    out = mask(f'{{"content": "{begin}\\n{body}\\n{end}\\n"}}\nnext line')
    assert body not in out
    assert out.endswith('\nnext line')  # END まで同じ行にあれば、次の行は伏せない
    unterminated = mask(f'x {begin}{body}\n{body}\n{end}\nok')
    assert body not in unterminated
    assert unterminated.endswith('\nok')
