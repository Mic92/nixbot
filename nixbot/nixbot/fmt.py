"""Human-readable formatting shared by forge statuses and the web UI."""

from __future__ import annotations


def format_duration(seconds: float) -> str:
    secs = int(seconds)
    if secs >= 3600:  # noqa: PLR2004
        return f"{secs // 3600}h {secs % 3600 // 60}m"
    if secs >= 60:  # noqa: PLR2004
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


def format_ms(ms: int | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:  # noqa: PLR2004
        return f"{ms} ms"
    if ms < 60_000:  # noqa: PLR2004
        return f"{ms / 1000:.1f} s"
    return format_duration(ms / 1000)


def format_bytes(value: int) -> str:
    if value < 1 << 20:
        return f"{value / (1 << 10):.1f} KiB"
    if value < 1 << 30:
        return f"{value / (1 << 20):.1f} MiB"
    return f"{value / (1 << 30):.1f} GiB"
