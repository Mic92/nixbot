"""Everything that follows a settled build: effects and their checks,
schedule refresh, onEvent deliveries. Fresh builds, reruns and reused
builds all go through here. A failing step is logged and the others
still run."""

from __future__ import annotations

import asyncio
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
        if event.tag is not None:
            # Not the build's once-only onPush run, which belongs to
            # whichever branch built the tree.
            await _step("tag", build, o.deliver_tag(event, build, status))
        else:
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
        if await _redeploy(o, event, build, worktree_path, credentials):
            return
        # Effects already ran for this tree; only copy their statuses
        # onto the commit that reused the build.
        from .build_reuse import replay_effect_statuses  # noqa: PLC0415

        logger.info(
            "reused build: replaying effect statuses",
            extra={"build_id": build.id_, "sha": event.commit_sha},
        )
        await replay_effect_statuses(o, event, build)
        return
    await o.maybe_run_effects(event, build, worktree_path, credentials)


async def _redeploy(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
) -> bool:
    """Run a reused build's effects again when a later build's effects
    ran on this branch since: a revert back to a deployed tree must
    deploy it again, the targets run the newer build. Replaces the old
    runs like an effects restart, now reported on the reusing commit."""
    if event.pr_number is not None or not await q.effects_superseded(
        o.pool, id_=build.id_, branch=event.branch
    ):
        return False
    if build.id_ in o.cancel_events:
        # A run of this build is in flight and deploys this tree anyway.
        return False
    logger.info(
        "reused build superseded on its branch: running effects again",
        extra={"build_id": build.id_, "sha": event.commit_sha},
    )
    o.cancel_events[build.id_] = asyncio.Event()
    try:
        await o.drop_effects(build.id_, None)
        await o.maybe_run_effects(event, build, worktree_path, credentials)
    finally:
        o.release_run(build.id_)
    return True


async def _deliveries(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, reused: bool
) -> None:
    refreshed = await q.get_build(o.pool, id_=build.id_)
    if refreshed is not None:
        # A reused build's build_finished went to the context that built it,
        # often a PR. A branch push needs its own.
        finished = not reused or (event.pr_number is None and event.tag is None)
        await o.deliver_events(event, refreshed, finished=finished)
