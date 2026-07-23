"""Postgres-backed failed-build cache (opt-in via cacheFailedBuilds).

Storage port of db/failed_builds.py onto the service schema. The skip
semantics live in build_scheduler.py (cached failures skip the build,
report with a link to the first failure, and propagate to dependents;
an explicit rebuild deletes the rows up front in service.py and
builds again).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from .build_scheduler import CachedFailure
from .db_gen import failed as q

if TYPE_CHECKING:
    import asyncpg


class PostgresFailedBuildCache:
    """Implements the scheduler's FailedBuildCache protocol, scoped
    per project: a global key would leak one project's build URL into
    another's status descriptions."""

    def __init__(self, pool: asyncpg.Pool, project_id: int) -> None:
        self.pool = pool
        self.project_id = project_id

    async def check(self, drv_path: str) -> CachedFailure | None:
        row = await q.failed_build_by_drv(
            self.pool, project_id=self.project_id, derivation=drv_path
        )
        if row is None:
            return None
        return CachedFailure(
            drv_path=row.derivation,
            time=datetime.fromtimestamp(row.timestamp, tz=UTC),
            url=row.url,
        )

    async def add(self, drv_path: str, url: str) -> None:
        await q.upsert_failed_build(
            self.pool,
            project_id=self.project_id,
            derivation=drv_path,
            timestamp=datetime.now(tz=UTC).timestamp(),
            url=url,
        )
