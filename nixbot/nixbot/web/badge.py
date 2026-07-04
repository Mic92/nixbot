"""Shields-style status badges for README embedding.

A self-contained flat SVG (templates/badge.svg) so no external badge
service is needed and badges render without leaking traffic to a third
party.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jinja2 import Environment

# Overall build status -> (badge message, right-side color).
_STATUS = {
    "succeeded": ("passing", "#4c1"),
    "failed": ("failing", "#e05d44"),
    "cancelled": ("cancelled", "#9f9f9f"),
    "building": ("building", "#dfb317"),
    "evaluating": ("evaluating", "#dfb317"),
    "pending": ("pending", "#dfb317"),
}
_UNKNOWN = ("unknown", "#9f9f9f")


def message_for(status: str | None) -> tuple[str, str]:
    """Badge message and color for a build status (None = no build)."""
    return _STATUS.get(status or "", _UNKNOWN)


# Average glyph advance for 11px Verdana, in px. Approximate: badges
# only need consistent, not pixel-perfect, widths.
_CHAR_PX = 6.5
_PAD = 10


def _section_width(text: str) -> int:
    return round(len(text) * _CHAR_PX) + 2 * _PAD


def render(env: Environment, label: str, message: str, color: str) -> str:
    """Render a two-part flat badge as SVG."""
    lw = _section_width(label)
    mw = _section_width(message)
    return env.get_template("badge.svg").render(
        label=label,
        message=message,
        color=color,
        lw=lw,
        mw=mw,
        total=lw + mw,
        # Text is positioned at 10x scale (centered in each section) and
        # stretched to the section's inner width.
        lx=lw * 5,
        mx=lw * 10 + mw * 5,
        lt=(lw - 2 * _PAD) * 10,
        mt=(mw - 2 * _PAD) * 10,
    )
