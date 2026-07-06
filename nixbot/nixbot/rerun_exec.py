"""Rerun paths: pending-attribute restarts/crash recovery and
effects-only restarts, plus the shared rerun worktree setup.

Calls back into other concerns via Orchestrator methods; build_run is
imported directly since it has no runtime dependency on this module.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from . import build_run, db
from .canceller import branch_key
from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .events import ChangeEvent, EvalReport, event_for_build
from .gitrepo import pr_refspec

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .db import BuildRecord
    from .events import RepoInfo
    from .gitrepo import FetchCredentials
    from .models import NixEvalJobSuccess
    from .orchestrator import Orchestrator


@asynccontextmanager
async def rerun_worktree(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    prefix: str,
    credentials: FetchCredentials | None,
) -> AsyncIterator[tuple[ChangeEvent, Path]]:
    """Event reconstruction plus a fresh worktree at the recorded
    commit; shared by the rerun paths."""
    event = event_for_build(info, build)
    # PR head commits are only reachable via the PR refs.
    refspecs = ["+refs/heads/*:refs/heads/*"]
    if build.pr_number is not None:
        refspecs.append(pr_refspec(info.forge, build.pr_number))
    await o.repos.fetch(info.key, info.clone_url, refspecs, credentials)
    worktree = await o.repos.checkout_for_build(
        info.key,
        f"{prefix}-{build.id}",
        base_commit=build.commit_sha,
        credentials=credentials,
    )
    try:
        yield event, worktree.path
    finally:
        await o.repos.remove_worktree(worktree)


async def rerun_pending_attributes(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    pending_jobs: list[NixEvalJobSuccess],
    credentials: FetchCredentials | None = None,
) -> None:
    """Re-run only the pending attributes of an existing build using
    the stored eval results — no re-evaluation (attribute restarts
    and crash recovery)."""
    if build.id in o.cancel_events:
        # Already running; a concurrent rerun would double-write
        # attribute completions.
        return
    # Claim the slot before the first await; concurrent reruns
    # must not pass the guard together.
    cancel_event = o.cancel_events[build.id] = asyncio.Event()
    try:
        current = await builds_q.get_build(o.pool, id_=build.id)
        if current is not None and current.status == "cancelled":
            # Cancelled between scheduling the rerun and getting here.
            return
        # Pending rows for systems no longer in build_systems would
        # stay non-terminal forever: the scheduler drops their jobs.
        # Drop the rows too (same as never recording them).
        unsupported = [
            job for job in pending_jobs if job.system not in o.config.build_systems
        ]
        if unsupported:
            await q.delete_attributes_by_name(
                o.pool,
                build_id=build.id,
                attrs=[job.attr for job in unsupported],
            )
            pending_jobs = [
                job for job in pending_jobs if job.system in o.config.build_systems
            ]
        # No re-eval happens on this path; go straight to building.
        await db.set_build_status(o.pool, build.id, BuildStatus.BUILDING)
        # Register so supersede/PR-close cancellation also covers
        # recovered and restarted builds.
        o.canceller.register(
            info.id,
            branch_key(build.branch, build.pr_number),
            build.id,
            build.tree_hash or "",
            build.commit_sha,
            cancel_event,
        )
        async with rerun_worktree(o, info, build, "rerun", credentials) as (
            event,
            worktree_path,
        ):
            # No re-eval on this path: re-post the eval context green,
            # the previous run may have left it red or pending.
            await o.reporter.eval_finished(event, build, EvalReport(success=True))
            # cache_failures=False: see _ReadOnlyFailedBuildCache.
            status = await build_run.build_attributes(
                o,
                event,
                build,
                worktree_path,
                pending_jobs,
                cache_failures=False,
            )
            if status == BuildStatus.SUCCEEDED:
                # Crash recovery before effects started; the
                # started-flag keeps already-deployed builds from
                # re-deploying.
                await o.maybe_run_effects(event, build, worktree_path, credentials)
                await o.refresh_schedules(event)
    finally:
        o.canceller.complete(build.id)
        o.cancel_events.pop(build.id, None)


async def rerun_effects(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    credentials: FetchCredentials | None = None,
) -> None:
    """Effects-only restart: fresh worktree at the recorded commit,
    attributes untouched."""
    if build.id in o.cancel_events:
        # A concurrent rerun (or double click) would deploy twice.
        return
    o.cancel_events[build.id] = asyncio.Event()
    try:
        # Reset under the claim: resetting earlier (e.g. in the
        # service) could clobber a rerun already in flight. Drop the
        # previous run's logs here too, so pending rows show no stale
        # output.
        await q.reset_effects_state(o.pool, build_id=build.id)
        o.reset_effect_logs(build.id)
        async with rerun_worktree(o, info, build, "effects", credentials) as (
            event,
            worktree_path,
        ):
            await o.maybe_run_effects(event, build, worktree_path, credentials)
            await o.refresh_schedules(event)
        # The enqueued effect items share this build's key and only
        # become claimable once this item finishes.
    finally:
        o.cancel_events.pop(build.id, None)
