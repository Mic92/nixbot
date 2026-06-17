"""Linked-build and dedup logic: attaching second contexts to an
in-flight build, replaying terminal statuses, reusing terminal builds
for identical content, and gcroots/outputs post-processing.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from .canceller import RegisterOutcome
from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .events import BuildResult
from .gitrepo import GitError, run_git

if TYPE_CHECKING:
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent
    from .gitrepo import FetchCredentials
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def attach_linked_event(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord
) -> None:
    """In-flight (or recovering) build shared with another context:
    attach for the final status fan-out."""
    o.linked_events.setdefault(build.id, []).append(event)
    await o.reporter.build_started(event, build)
    # The build may have turned terminal between the record fetch
    # and the attach: the final fan-out already happened and would
    # never cover this event. Replay the final status instead.
    current = await builds_q.get_build(o.pool, id_=build.id)
    if current is not None and current.status in BuildStatus.TERMINAL:
        with contextlib.suppress(KeyError, ValueError):
            o.linked_events[build.id].remove(event)
        await replay_terminal_status(o, event, current)


async def replay_terminal_status(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord
) -> None:
    """Re-post the final eval and build statuses of an already
    terminal build for a new context; without this the context's
    nix-eval/nix-build checks stay pending forever. A succeeded
    build with zero attributes is a genuine empty-but-green eval,
    not an eval failure."""
    if build.status == BuildStatus.CANCELLED:
        await o.reporter.eval_cancelled(event, build)
    else:
        eval_success = build.status == BuildStatus.SUCCEEDED or bool(
            await builds_q.attribute_statuses(o.pool, build_id=build.id)
        )
        await o.reporter.eval_finished(event, build, success=eval_success, warnings=[])
    await o.reporter.build_finished(
        event, build, BuildResult(build.status, build.status_generation, [])
    )


async def finish_linked(
    o: Orchestrator,
    build: BuildRecord,
    result: BuildResult,
    *,
    eval_success: bool | None = None,
) -> None:
    """Final status fan-out for second contexts attached to this
    build; eval_success is None when no eval result exists."""
    for linked in o.linked_events.pop(build.id, []):
        if eval_success is not None:
            await o.reporter.eval_finished(
                linked, build, success=eval_success, warnings=[]
            )
        elif result.status == BuildStatus.CANCELLED:
            # Cancel during eval: the linked contexts' nix-eval
            # status would otherwise stay pending forever.
            await o.reporter.eval_cancelled(linked, build)
        await o.reporter.build_finished(linked, build, result)


async def is_ancestor(
    o: Orchestrator, project_key: str, ancestor: str, descendant: str
) -> bool:
    try:
        await run_git(
            ["merge-base", "--is-ancestor", ancestor, descendant],
            cwd=o.repos.clone_path(project_key),
        )
    except GitError:
        return False
    return True


async def reuse_terminal_build(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    key: str,
    tree_hash: str,
    *,
    worktree_path: Path,
    credentials: FetchCredentials | None,
    incoming_stale: bool,
) -> None:
    """Same content already built in another context: only report
    the existing result for this context. Still register so an
    in-flight build of this context's previous content is
    superseded, and a push reusing a PR build gets its
    gcroots/outputs updates."""
    logger.info(
        "reusing build for tree hash",
        extra={"build_id": build.id, "tree_hash": tree_hash},
    )
    outcome = o.canceller.register(
        event.repo.id,
        key,
        build.id,
        tree_hash,
        event.commit_sha,
        asyncio.Event(),
        incoming_is_ancestor_of_running=incoming_stale,
    )
    if outcome == RegisterOutcome.STALE:
        # Redelivered out-of-order event: superseding the in-flight
        # newer build with this old result would cancel it.
        return
    o.canceller.complete(build.id)
    if build.status == BuildStatus.SUCCEEDED:
        # Guarded like in-build post-processing: a gcroots/outputs
        # failure must not strand this context without a status.
        try:
            await _post_process_existing(o, event, build)
            # A build that ran as a PR never started effects, so a
            # default-branch push reusing it must still deploy; the
            # effects_started flag prevents re-deploys.
            await o.maybe_run_effects(event, build, worktree_path, credentials)
            await o.refresh_schedules(event)
        except Exception:
            logger.exception(
                "post-processing reused build failed",
                extra={"build_id": build.id},
            )
    await replay_terminal_status(o, event, build)


async def _post_process_existing(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord
) -> None:
    """Gcroots/outputs updates for a context reusing an already
    succeeded build (e.g. default-branch push reusing a PR build)."""
    rows = await q.succeeded_attribute_outputs(o.pool, build_id=build.id)
    pairs = []
    for row in rows:
        out = (json.loads(row.outputs) if row.outputs else {}).get("out")
        if out:
            pairs.append((row.attr, out))
    await post_process_skipped(o, event, pairs)


async def post_process_skipped(
    o: Orchestrator, event: ChangeEvent, skipped: list[tuple[str, str]]
) -> None:
    branches = o.config.branches
    repo = event.repo
    if event.pr_number is not None:
        return  # push events only, matching current behavior
    for attr, out_path in skipped:
        if not out_path:
            continue
        # Forge-scoped paths: the same owner/repo on two forges
        # must not share gc-roots or outputs files.
        if branches.do_register_gcroot(repo.default_branch, event.branch):
            await o.register_gcroot(o.config.gcroots_dir, repo.key, attr, out_path)
        if o.config.outputs_path is not None and branches.do_update_outputs(
            repo.default_branch, event.branch
        ):
            o.write_output_path(
                o.config.outputs_path,
                repo.forge,
                repo.owner,
                repo.repo,
                event.branch,
                attr,
                out_path,
            )
