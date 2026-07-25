"""Sanitizing of build log output before it is re-emitted to a terminal.

The escape-sequence grammar mirrors nixbot.ansi (the server keeps the
colored original, the CLI is standalone and cannot import it).
"""

from __future__ import annotations

import re

# One token per escape sequence. Only the named SGR group is allowed
# through; every other form (cursor moves, clear-screen, OSC titles,
# charset selects from BIOS/boot logs) is consumed and dropped.
ANSI_TOKEN_RE = re.compile(
    r"\x1b\[(?P<sgr>[0-9;:]*)m"
    r"|\x1b\](?P<osc>[^\x07\x1b]*)(?:\x07|\x1b\\)"
    r"|\x1b\[[0-9;:?<=>]*[ -/]*[@-~]"
    r"|\x1b[()*+]."
    r"|\x1b[^\[\]]"
)
# C0 controls that mean nothing in a log line (tabs are expanded first).
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]")
# Mega-lines (minified JS etc.) choke terminals.
MAX_LINE_LEN = 4096
SGR_RESET = "\x1b[0m"
_TRAILING_PARTIAL_SGR_RE = re.compile(r"\x1b[^m]*$")


def sanitize_line(s: str) -> str:
    """Make one captured log line safe to re-emit.

    Whitelist approach: only SGR color sequences and printable text
    survive. Carriage-return overwrite is emulated, the length is
    capped, and a kept color always ends with a reset so it cannot
    bleed into our own output.
    """
    if "\r" in s:
        s = next((p for p in reversed(s.split("\r")) if p), "")
    kept_sgr = False

    def repl(m: re.Match[str]) -> str:
        nonlocal kept_sgr
        if m.group("sgr") is None:
            return ""
        kept_sgr = True
        return m.group(0)

    s = CTRL_RE.sub("", ANSI_TOKEN_RE.sub(repl, s)).expandtabs(4)
    if len(s) > MAX_LINE_LEN:
        # Do not cut an SGR in half: an unterminated CSI makes the
        # terminal swallow the text that follows.
        s = _TRAILING_PARTIAL_SGR_RE.sub("", s[:MAX_LINE_LEN])
        s += " …[line truncated]"
    if kept_sgr:
        s += SGR_RESET
    return s


def sanitize_block(text: str) -> str:
    """sanitize_line applied to every line of a multi-line string."""
    return "\n".join(sanitize_line(line) for line in text.splitlines())
