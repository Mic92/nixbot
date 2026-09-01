"""Prometheus /metrics endpoint.

Unauthenticated by design, therefore free of private repository names:
metrics are aggregated by status/state only, never labeled by project.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ..db_gen import web as gen  # noqa: TID252
from ..sql_util import expect  # noqa: TID252

if TYPE_CHECKING:
    import asyncpg


async def render_metrics(pool: asyncpg.Pool) -> str:
    # Everything here is a gauge: status transitions and retention
    # cleanup shrink the table-derived values, so they are not counters.
    lines: list[str] = []

    lines.append("# HELP nixbot_builds Builds by final status.")
    lines.append("# TYPE nixbot_builds gauge")
    lines.extend(
        f'nixbot_builds{{status="{row.status}"}} {row.count}'
        for row in await gen.metrics_build_counts(pool)
    )

    lines.append("# HELP nixbot_attributes Attribute results by status.")
    lines.append("# TYPE nixbot_attributes gauge")
    lines.extend(
        f'nixbot_attributes{{status="{row.status}"}} {row.count}'
        for row in await gen.metrics_attribute_counts(pool)
    )

    queue_depth = await gen.metrics_queue_depth(pool)
    lines.append("# HELP nixbot_queue_depth Builds pending or running.")
    lines.append("# TYPE nixbot_queue_depth gauge")
    lines.append(f"nixbot_queue_depth {queue_depth}")

    duration = expect(await gen.metrics_build_duration(pool))
    lines.append(
        "# HELP nixbot_build_duration_seconds_sum Total wall time of finished builds."
    )
    lines.append("# TYPE nixbot_build_duration_seconds_sum gauge")
    lines.append(f"nixbot_build_duration_seconds_sum {duration.total}")
    lines.append("# TYPE nixbot_build_duration_seconds_count gauge")
    lines.append(f"nixbot_build_duration_seconds_count {duration.count}")

    eval_duration = expect(await gen.metrics_eval_duration(pool))
    lines.append(
        "# HELP nixbot_eval_duration_seconds_sum Total nix-eval-jobs run time."
    )
    lines.append("# TYPE nixbot_eval_duration_seconds_sum gauge")
    lines.append(f"nixbot_eval_duration_seconds_sum {eval_duration.total}")
    lines.append("# TYPE nixbot_eval_duration_seconds_count gauge")
    lines.append(f"nixbot_eval_duration_seconds_count {eval_duration.count}")

    projects = expect(await gen.metrics_projects(pool))
    lines.append("# HELP nixbot_projects Projects known/enabled.")
    lines.append("# TYPE nixbot_projects gauge")
    lines.append(f'nixbot_projects{{state="enabled"}} {projects.enabled}')
    lines.append(f'nixbot_projects{{state="total"}} {projects.total}')

    return "\n".join(lines) + "\n"


# /metrics is unauthenticated: without a cache anyone could run the
# full-table aggregations in a loop.
CACHE_TTL = 15.0


def create_metrics_router(pool: asyncpg.Pool) -> APIRouter:
    router = APIRouter()
    cached: tuple[float, str] | None = None
    lock = asyncio.Lock()

    @router.get("/metrics", response_class=PlainTextResponse)
    async def metrics() -> PlainTextResponse:
        nonlocal cached
        async with lock:  # one query burst even under concurrent scrapes
            if cached is None or time.monotonic() - cached[0] > CACHE_TTL:
                cached = (time.monotonic(), await render_metrics(pool))
        return PlainTextResponse(
            cached[1],
            media_type="text/plain; version=0.0.4",
        )

    return router
