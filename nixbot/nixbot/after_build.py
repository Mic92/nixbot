"""Everything that follows a settled build: effects and their checks,
schedule refresh, onEvent deliveries. Fresh builds, reruns and reused
builds all go through here. A failing step is logged and the others
still run."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .db import BuildStatus
from .db_gen import builds as q

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent
    from .gitrepo import FetchCredentials
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def after_build(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    status: str,
    *,
    worktree_path: Path,
    credentials: FetchCredentials | None,
    reused: bool = False,
) -> None:
    if status == BuildStatus.SUCCEEDED:
        await _step(
            "effects",
            build,
            _effects(o, event, build, worktree_path, credentials, reused),
        )
        await _step("schedules", build, o.refresh_schedules(event))
    await _step("deliveries", build, _deliveries(o, event, build, reused))


async def _step(name: str, build: BuildRecord, step: Awaitable[None]) -> None:
    try:
        await step
    except Exception:
        logger.exception(
            "post-build step failed", extra={"build_id": build.id_, "step": name}
        )


async def _effects(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
    reused: bool,
) -> None:
    if reused and build.effects_started:
        # Effects already ran for this tree; only copy their statuses
        # onto the commit that reused the build.
        from .build_reuse import replay_effect_statuses  # noqa: PLC0415

        await replay_effect_statuses(o, event, build)
        return
    await o.maybe_run_effects(event, build, worktree_path, credentials)


async def _deliveries(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, reused: bool
) -> None:
    refreshed = await q.get_build(o.pool, id_=build.id_)
    if refreshed is not None:
        # A reused build had its build_finished delivered when it finished.
        await o.deliver_events(event, refreshed, finished=not reused)
