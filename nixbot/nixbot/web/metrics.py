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
    from collections.abc import Iterable, Mapping

    import asyncpg

Sample = tuple["Mapping[str, str]", float]


class _Gauges:
    # Everything here is a gauge: status transitions and retention
    # cleanup shrink the table-derived values, so they are not counters.
    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, name: str, help_: str, samples: Iterable[Sample]) -> None:
        self.lines.append(f"# HELP nixbot_{name} {help_}")
        self.lines.append(f"# TYPE nixbot_{name} gauge")
        for labels, value in samples:
            sel = ",".join(f'{k}="{v}"' for k, v in labels.items())
            self.lines.append(
                f"nixbot_{name}{{{sel}}} {value}" if sel else f"nixbot_{name} {value}"
            )

    def one(self, name: str, help_: str, value: float) -> None:
        self.add(name, help_, [({}, value)])


async def render_metrics(pool: asyncpg.Pool) -> str:
    g = _Gauges()

    g.add(
        "builds",
        "Builds by final status.",
        [({"status": r.status}, r.count) for r in await gen.metrics_build_counts(pool)],
    )
    g.add(
        "attributes",
        "Attribute results by status.",
        [
            ({"status": r.status}, r.count)
            for r in await gen.metrics_attribute_counts(pool)
        ],
    )
    g.one(
        "queue_depth",
        "Builds pending or running.",
        expect(await gen.metrics_queue_depth(pool)),
    )
    g.one(
        "builds_oldest_active_age_seconds",
        "Age of the oldest pending or running build.",
        expect(await gen.metrics_build_oldest_active(pool)),
    )

    duration = expect(await gen.metrics_build_duration(pool))
    g.one(
        "build_duration_seconds_sum",
        "Total wall time of finished builds.",
        duration.total,
    )
    g.one(
        "build_duration_seconds_count",
        "Finished builds with a duration.",
        duration.count,
    )
    eval_duration = expect(await gen.metrics_eval_duration(pool))
    g.one(
        "eval_duration_seconds_sum",
        "Total nix-eval-jobs run time.",
        eval_duration.total,
    )
    g.one(
        "eval_duration_seconds_count",
        "Builds with an eval duration.",
        eval_duration.count,
    )

    projects = expect(await gen.metrics_projects(pool))
    g.add(
        "projects",
        "Projects known/enabled.",
        [
            ({"state": "enabled"}, projects.enabled),
            ({"state": "total"}, projects.total),
        ],
    )

    g.add(
        "work_queue",
        "Dispatcher work items by kind and status.",
        [
            ({"kind": w.kind, "status": w.status}, w.count)
            for w in await gen.metrics_work_queue_counts(pool)
        ],
    )
    g.add(
        "work_queue_oldest_age_seconds",
        "Age of the oldest pending or running work item.",
        [
            ({"kind": w.kind, "status": w.status}, w.age)
            for w in await gen.metrics_work_queue_oldest(pool)
        ],
    )

    g.add(
        "effects",
        "Effect runs by owner and status.",
        [
            ({"owner": e.owner, "status": e.status}, e.count)
            for e in await gen.metrics_effect_counts(pool)
        ],
    )
    g.add(
        "effects_oldest_running_age_seconds",
        "Age of the longest pending or running effect.",
        [
            ({"owner": e.owner}, e.age)
            for e in await gen.metrics_effect_oldest_running(pool)
        ],
    )
    sched = expect(await gen.metrics_schedule_lag(pool))
    if sched.schedules:
        g.one(
            "scheduled_effect_lag_seconds",
            "Time since any scheduled effect last ran.",
            sched.lag,
        )

    uploads = await gen.metrics_upload_queue(pool)
    g.add(
        "upload_queue_depth",
        "Store paths waiting for a binary-cache push.",
        [({"uploader": u.uploader}, u.depth) for u in uploads],
    )
    g.add(
        "upload_queue_retrying",
        "Queued paths whose push failed at least once.",
        [({"uploader": u.uploader}, u.retrying) for u in uploads],
    )
    g.add(
        "upload_queue_oldest_age_seconds",
        "Age of the oldest queued path.",
        [({"uploader": u.uploader}, u.oldest_age) for u in uploads],
    )

    return "\n".join(g.lines) + "\n"


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
