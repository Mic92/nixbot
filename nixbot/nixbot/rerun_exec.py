"""Rerun paths: pending-attribute restarts/crash recovery and
effects-only restarts, plus the shared rerun worktree setup.

Calls back into other concerns via Orchestrator methods; build_run is
imported directly since it has no runtime dependency on this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from . import build_reuse, build_run, db
from .canceller import branch_key
from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .events import ChangeEvent, EvalReport, event_for_build
from .gitrepo import pr_refspec

logger = logging.getLogger(__name__)

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
    commit. Shared by the rerun paths."""
    event = event_for_build(info, build)
    # PR head commits are only reachable via the PR refs.
    refspecs = ["+refs/heads/*:refs/heads/*"]
    if build.pr_number is not None:
        refspecs.append(pr_refspec(info.forge, build.pr_number))
    await o.repos.fetch(info.key, info.clone_url, refspecs, credentials)
    worktree = await o.repos.checkout_for_build(
        info.key,
        f"{prefix}-{build.id_}",
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
    if build.id_ in o.cancel_events:
        msg = f"build {build.id_} still has a live run"
        raise RuntimeError(msg)
    cancel_event = o.cancel_events[build.id_] = asyncio.Event()
    try:
        # Pending rows for systems no longer in build_systems would
        # stay non-terminal forever: the scheduler drops their jobs.
        # Drop the rows too (same as never recording them).
        unsupported = [
            job for job in pending_jobs if job.system not in o.config.build_systems
        ]
        if unsupported:
            await q.delete_attributes_by_name(
                o.pool,
                build_id=build.id_,
                attrs=[job.attr for job in unsupported],
            )
            pending_jobs = [
                job for job in pending_jobs if job.system in o.config.build_systems
            ]
        # No re-eval happens on this path. Go straight to building.
        await db.set_build_status(o.pool, build.id_, BuildStatus.BUILDING)
        # Register so supersede/PR-close cancellation also covers
        # recovered and restarted builds.
        o.canceller.register(
            info.id,
            branch_key(build.branch, build.pr_number),
            build.id_,
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
            await build_reuse.report_eval_finished(
                o, event, build, EvalReport(success=True)
            )
            # cache_failures=False: see _ReadOnlyFailedBuildCache.
            # effects_started keeps a recovered, already-deployed
            # build from re-deploying.
            await build_run.build_attributes(
                o,
                event,
                build,
                worktree_path,
                pending_jobs,
                credentials=credentials,
                cache_failures=False,
            )
    finally:
        o.canceller.complete(build.id_)
        o.release_run(build.id_)


async def _rerun_names(
    o: Orchestrator, build: BuildRecord, only: str
) -> list[str] | None:
    """The effect plus transitive dependents that cannot settle on their
    own. Returns None when `only` is not a row of this build."""
    rows = await builds_q.effects_for_build(o.pool, build_id=build.id_)
    if all(r.name != only for r in rows):
        return None
    names = {only}
    changed = True
    while changed:
        changed = False
        for r in rows:
            deps = set(json.loads(r.deps)) if r.deps else set()
            # A still-pending dependent would be stranded behind the
            # rerun row it waits on.
            stranded = r.status in ("dependency_failed", "pending")
            if stranded and r.name not in names and deps & names:
                names.add(r.name)
                changed = True
    return sorted(names)


async def cancel_running_effects(
    o: Orchestrator, build_id: int, names: list[str] | None
) -> None:
    """Cancel the build's push and check tasks (`names`: only these push
    effects) and wait until they let go of their rows. A hung run would
    otherwise hold its dedup key forever (issue #139)."""
    if names is None:
        await cancel_running(o, build_id, None, None)
    else:
        await cancel_running(o, build_id, "push", names)


async def cancel_running(
    o: Orchestrator, build_id: int, kind: str | None, names: list[str] | None
) -> None:
    """kind None: the build-owned kinds (push, check)."""
    running = [
        r
        for (bid, k, name), r in o.running_effects.items()
        if bid == build_id
        and (k == kind if kind is not None else k in ("push", "check"))
        and (names is None or name in names)
    ]
    for r in running:
        r.cancel()
    await asyncio.gather(*(r.settled.wait() for r in running))


async def rerun_effects(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    credentials: FetchCredentials | None = None,
    only: str | None = None,
) -> None:
    """Effects-only restart: fresh worktree at the recorded commit,
    attributes untouched. `only` narrows the rerun to one effect and
    its dependency_failed dependents."""
    if build.id_ in o.cancel_events:
        # A concurrent rerun (or double click) would deploy twice.
        return
    o.cancel_events[build.id_] = asyncio.Event()
    try:
        names: list[str] | None = None
        if only is not None:
            names = await _rerun_names(o, build, only)
            if names is None:
                logger.warning(
                    "rerun of unknown effect ignored",
                    extra={"build_id": build.id_, "effect": only},
                )
                return
        # Under the claim: an earlier reset could clobber a rerun in flight.
        await o.drop_effects(build.id_, names)
        async with rerun_worktree(o, info, build, "effects", credentials) as (
            event,
            worktree_path,
        ):
            await o.maybe_run_effects(
                event, build, worktree_path, credentials, only=names
            )
            await o.refresh_schedules(event)
        # The enqueued effect items share this build's key and only
        # become claimable once this item finishes.
    finally:
        o.release_run(build.id_)
