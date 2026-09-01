"""Human-readable formatting shared by forge statuses and the web UI."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    secs = int(seconds)
    if secs >= 3600:  # noqa: PLR2004
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    if secs >= 60:  # noqa: PLR2004
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"
