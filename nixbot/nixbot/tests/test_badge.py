"""Regression for the routing rule that keeps the greedy owner
convertor from swallowing the badge suffix and 404ing the route."""

from __future__ import annotations

import re

from nixbot.web.routing import SegmentConvertor


def test_badge_suffix_is_reserved() -> None:
    """`badge.svg` must not parse as a project name, or the owner
    convertor eats the repo name and the route 404s."""
    assert not re.fullmatch(SegmentConvertor.regex, "badge.svg")
    assert re.fullmatch(SegmentConvertor.regex, "widget")
