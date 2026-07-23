"""Strip the secrets we inject from effect logs before they are stored,
served in the web UI, or posted to a forge.

We only redact secrets we control (deploy secret values, git and task
tokens): matching them literally catches any forge's token by value and
never touches unrelated output. Token-shape patterns are deliberately
avoided. They cannot cover Gitea's prefix-less tokens and risk masking
commit SHAs and other legitimate text.
"""

from __future__ import annotations

import contextlib
import json
import re

REDACTED = b"***"

# Below this length a literal is too generic to redact safely (it would
# blank out ordinary words that happen to appear in the secret file).
_MIN_LITERAL_LEN = 6


def secret_literals(*values: str | None) -> list[bytes]:
    """Literal secrets to redact, including the leaf string values of
    any JSON secret payload. Too-short values are dropped."""
    found: set[str] = set()
    for value in values:
        if value is None:
            continue
        found.add(value)
        with contextlib.suppress(ValueError):
            _collect_json_strings(json.loads(value), found)
    return [v.encode() for v in found if len(v) >= _MIN_LITERAL_LEN]


def _collect_json_strings(value: object, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_json_strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _collect_json_strings(item, out)


class Redactor:
    """Masks a fixed set of literal secrets in a single regex pass."""

    def __init__(self, literals: list[bytes]) -> None:
        # Longest first: a literal containing another is masked whole,
        # since alternation matches left to right at each position. An
        # empty alternation would match the empty string everywhere, so
        # keep the pattern None and pass data through untouched.
        alternatives = sorted(map(re.escape, set(literals)), key=len, reverse=True)
        self._pattern = re.compile(b"|".join(alternatives)) if alternatives else None

    def __call__(self, data: bytes) -> bytes:
        return self._pattern.sub(REDACTED, data) if self._pattern else data
