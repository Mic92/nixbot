"""Everything that follows a settled build: effects and their checks,
schedule refresh, onEvent deliveries, build-finished steps. Fresh
builds, reruns and reused builds all go through here. A failing step is
logged and the others still run."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .db import BuildStatus
from .db_gen import builds as q
from .db_gen import maintenance as mq
from .post_build import build_finished_props, run_build_finished_steps

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
    await _step(
        "build finished steps",
        build,
        _build_finished_steps(o, event, build, status, worktree_path),
    )


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

        logger.info(
            "reused build: replaying effect statuses",
            extra={"build_id": build.id_, "sha": event.commit_sha},
        )
        await replay_effect_statuses(o, event, build)
        return
    await o.maybe_run_effects(event, build, worktree_path, credentials)


async def _deliveries(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, reused: bool
) -> None:
    refreshed = await q.get_build(o.pool, id_=build.id_)
    if refreshed is not None:
        # A reused build's build_finished went to the context that built it,
        # often a PR. A branch push needs its own.
        finished = not reused or (event.pr_number is None and event.tag is None)
        await o.deliver_events(event, refreshed, finished=finished)


async def _build_finished_steps(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    status: str,
    worktree_path: Path,
) -> None:
    steps = o.config.build_finished_steps
    if not steps:
        return
    rows = await mq.attributes_for_builds(o.pool, build_ids=[build.id_])
    attributes = [
        {
            "attr": row.attr,
            "system": row.system,
            "drv_path": row.drv_path,
            "outputs": json.loads(row.outputs) if row.outputs else {},
            "status": row.status,
        }
        for row in sorted(rows, key=lambda row: row.attr)
    ]
    build_url = (
        f"{o.config.url}/repos/{event.repo.forge}/{event.repo.name}"
        f"/builds/{build.number}"
    )
    await run_build_finished_steps(
        steps,
        build_finished_props(event, status, build_url),
        attributes,
        worktree_path,
    )
