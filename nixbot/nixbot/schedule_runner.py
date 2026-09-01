"""Scheduled-effects execution: the due-effect sweep loop, running a
due effect in a fresh worktree, and refreshing a project's stored
onSchedule definitions at a commit.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from functools import partial
from typing import TYPE_CHECKING

from .effects import EffectsContext, effects_context, run_scheduled_effect
from .effects_run import effect_checkout
from .executor import failure_excerpt
from .repos import repo_info
from .schedules import (
    DueEffect,
    ScheduledEffectsStore,
    discover_schedules,
)
from .workload_identity import EffectIdentity

if TYPE_CHECKING:
    from .events import RepoInfo
    from .executor import LogWriter
    from .service import CIService

logger = logging.getLogger(__name__)


def scheduled_worktree_id(due: DueEffect) -> str:
    """Worktree id for one scheduled effect run.
    Schedule/effect names are repo-controlled,
    so sanitize against path traversal."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{due.schedule_name}-{due.effect}")
    return f"scheduled-{due.project_id}-{safe}"


async def scheduled_effects_loop(s: CIService) -> None:
    store = ScheduledEffectsStore(s.pool)
    while True:
        try:
            for due in await store.due_effects():
                logger.info(
                    "scheduled effect due",
                    extra={
                        "schedule": due.schedule_name,
                        "effect": due.effect,
                    },
                )
                # Enqueue before mark_run: the reverse order loses
                # the occurrence when we crash in between.
                await s.enqueue_work(
                    "scheduled",
                    f"scheduled-{due.project_id}-{due.schedule_name}-{due.effect}",
                    {
                        "project_id": due.project_id,
                        "schedule_name": due.schedule_name,
                        "effect": due.effect,
                        "when": due.when.model_dump(exclude_none=True),
                    },
                )
                await store.mark_run(due)
        except Exception:
            logger.exception("scheduled-effects sweep failed")
        # Sleep to the next minute boundary: is_due matches exactly
        # one wall-clock minute, so a fixed 60s sleep after the sweep
        # work would drift and silently skip minutes.
        await asyncio.sleep(60 - (time.time() % 60))


async def refresh_schedules(s: CIService, project_id: int, rev: str) -> None:
    """Discover and store onSchedule definitions at the commit."""
    project = await s.repo_store.by_id(project_id)
    if project is None or not project.enabled:
        return
    info = repo_info(project)
    credentials = await s.credentials_provider(info.forge).get(info.clone_url)
    await s.orchestrator.repos.fetch(
        info.key, info.clone_url, ["+refs/heads/*:refs/heads/*"], credentials
    )
    worktree = await s.orchestrator.repos.checkout_for_build(
        info.key,
        f"schedules-{project_id}",
        base_commit=rev,
        credentials=credentials,
    )
    try:
        ctx = EffectsContext(
            path=worktree.path,
            rev=rev,
            branch=info.default_branch,
            repo=info.name,
            extra_sandbox_paths=s.config.effects_extra_sandbox_paths,
        )
        schedules = await discover_schedules(ctx)
        await ScheduledEffectsStore(s.pool).replace_schedules(project_id, schedules)
    finally:
        await s.orchestrator.repos.remove_worktree(worktree)


async def run_scheduled(
    s: CIService, due: DueEffect, run_id: int | None = None
) -> None:
    store = ScheduledEffectsStore(s.pool)
    project = await s.repo_store.by_id(due.project_id)
    if project is None or not project.enabled:
        # Manual runs pass a pre-created row. Close it instead of
        # leaving it stuck running.
        if run_id is not None:
            await store.finish_run(run_id, success=False, error="project disabled")
        return
    info = repo_info(project)
    # Manual runs pre-create the row. The sweep loop does not.
    if run_id is None:
        run_id = await store.start_run(due)
    async with s.orchestrator.open_effect_log(run_id) as log:
        try:
            success = await _run_scheduled_inner(s, due, info, log)
            error = None if success else (failure_excerpt(log.tail_lines()) or None)
        except Exception as e:
            # Spawned task: an exception would only surface as "Task
            # exception was never retrieved" and leave the row running.
            logger.exception("scheduled effect crashed", extra={"run_id": run_id})
            success, error = False, str(e)
    await store.finish_run(
        run_id,
        success=success,
        error=error,
        log_size=log.bytes_seen,
        log_truncated=log.truncated,
    )


async def _run_scheduled_inner(
    s: CIService, due: DueEffect, info: RepoInfo, log: LogWriter
) -> bool:
    credentials = await s.credentials_provider(info.forge).get(info.clone_url)
    await s.orchestrator.repos.fetch(
        info.key, info.clone_url, ["+refs/heads/*:refs/heads/*"], credentials
    )
    worktree = await s.orchestrator.repos.checkout_for_build(
        info.key,
        scheduled_worktree_id(due),
        base_commit=info.default_branch,
        credentials=credentials,
    )
    task_token = s.orchestrator.task_tokens.issue(
        due.project_id,
        EffectIdentity(
            forge=info.forge,
            owner=info.owner,
            repo=info.repo,
            event="schedule",
            effect=due.effect,
            schedule=due.schedule_name,
        ),
    )
    try:
        rev = await worktree.rev_parse("HEAD")
        async with effect_checkout(
            s.orchestrator.repos, info, worktree.path, rev, credentials
        ) as checkout:
            ctx = effects_context(
                s.config,
                info,
                worktree_path=worktree.path,
                rev=rev,
                branch=info.default_branch,
                git_token=credentials.token if credentials is not None else None,
                task_token=task_token,
            )
            ctx.effect_checkout = checkout
            ctx.bind_id_token_audiences = partial(
                s.orchestrator.task_tokens.bind_audiences, task_token
            )
            return await run_scheduled_effect(
                ctx, due.schedule_name, due.effect, log.write
            )
    finally:
        s.orchestrator.task_tokens.revoke(task_token)
        await s.orchestrator.repos.remove_worktree(worktree)
