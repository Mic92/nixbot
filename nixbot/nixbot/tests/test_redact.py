"""Log secret redaction: literal deploy secrets and tokens we inject."""

from __future__ import annotations

from nixbot.redact import Redactor, secret_literals


def test_secret_literals_extracts_json_values_over_threshold() -> None:
    literals = secret_literals('{"token": "supersecret", "n": 1, "x": "ab"}')
    # Full payload plus the long leaf value; short "ab" and non-strings
    # are dropped.
    assert b"supersecret" in literals
    assert b"ab" not in literals


def test_redactor_masks_literals() -> None:
    redactor = Redactor(secret_literals("deploytoken123", None))
    assert redactor(b"key=deploytoken123 end") == b"key=*** end"


def test_redactor_masks_longest_overlapping_literal() -> None:
    redactor = Redactor([b"secretvalue", b"secretvalue-extended"])
    assert redactor(b"x=secretvalue-extended") == b"x=***"


def test_redactor_without_literals_is_a_noop() -> None:
    # An empty alternation would otherwise match the empty string
    # between every byte.
    assert Redactor([])(b"hello world") == b"hello world"
