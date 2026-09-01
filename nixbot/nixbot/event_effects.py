"""Runs one queued onEvent effect. The effect code comes from a
worktree of the default branch at the row's code_rev; PR events also
get an untrusted clone of the PR head. The payload was stored on the
row at delivery time. Event effects post no forge statuses, see
docs/EFFECTS.md."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from .db_gen import builds as builds_q
from .db_gen import events as ev_q
from .db_gen import maintenance as q
from .db_gen import work_queue as wq
from .deliver import event_dedup_key
from .effects import effects_context
from .executor import failure_excerpt
from .gitrepo import GitError, pr_refspec
from .running_effect import RunningEffect
from .workload_identity import EffectIdentity

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .db import BuildRecord
    from .db_gen.models import EffectRun
    from .events import RepoInfo
    from .gitrepo import FetchCredentials, RepoManager
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


@asynccontextmanager
async def cloned_checkout(
    repos: RepoManager, info: RepoInfo, worktree_path: Path, commit: str, url: str
) -> AsyncIterator[Path]:
    """Standalone clone at `commit` next to the worktree, removed on exit."""
    dest = worktree_path.with_name(f"{worktree_path.name}-checkout")
    await repos.clone_for_effect(info.key, dest, commit=commit, push_url=url)
    try:
        yield dest
    finally:
        repos.remove_effect_clone(dest)


async def restart_event_effect(
    o: Orchestrator, build: BuildRecord, kind: str, name: str
) -> None:
    """Restart one event effect from the UI/API with its stored payload.
    If it is still running it is cancelled first, which also frees its
    queue key."""
    row = await q.effect_run(o.pool, build_id=build.id_, kind=kind, name=name)
    if row is None:
        return
    running = o.running_event_effects.get(row.id_)
    if running is not None:
        running.cancel()
        await running.settled.wait()
    reset = await ev_q.reset_event_effect(
        o.pool, build_id=build.id_, kind=kind, name=name
    )
    if reset is None:
        return
    await o.reset_effect_run_log(reset.id_)
    await wq.enqueue_effect_items(
        o.pool,
        build_id=build.id_,
        kind=kind,
        names=[name],
        dedup_keys=[
            event_dedup_key(build.project_id, build.id_, kind, name, reset.lock)
        ],
    )


async def run_event_effect_item(  # noqa: PLR0913
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    kind: str,
    name: str,
    credentials: FetchCredentials | None,
) -> None:
    row = await q.effect_run(o.pool, build_id=build.id_, kind=kind, name=name)
    if row is None or row.status != "pending" or row.code_rev is None:
        return
    run_id = await builds_q.start_effect(
        o.pool, build_id=build.id_, kind=kind, name=name, status="running"
    )
    if run_id is None:
        return
    task = asyncio.current_task()
    assert task is not None  # noqa: S101
    running = RunningEffect(task=task)
    o.running_event_effects[run_id] = running
    try:
        await _run(o, info, build, row, credentials)
    except asyncio.CancelledError:
        # Cancelled by a UI restart, which re-queues the row once
        # `settled` fires. Anything else cancelling us is a shutdown.
        if not running.restart:
            raise
    finally:
        o.running_event_effects.pop(run_id, None)
        running.settled.set()


async def _run(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    row: EffectRun,
    credentials: FetchCredentials | None,
) -> None:
    assert row.code_rev is not None  # noqa: S101
    payload = json.loads(row.payload) if row.payload else {}
    pr = payload.get("pullRequest")
    success = False
    error: str | None = None
    async with o.open_effect_log(row.id_) as writer:
        try:
            worktree = await o.repos.checkout_for_build(
                info.key,
                f"event-{row.id_}",
                base_commit=row.code_rev,
                credentials=credentials,
            )
            async with AsyncExitStack() as stack:
                stack.push_async_callback(o.repos.remove_worktree, worktree)
                checkout = None
                if pr is not None:
                    try:
                        await o.repos.fetch(
                            info.key,
                            info.clone_url,
                            [pr_refspec(info.forge, pr["number"])],
                            credentials,
                        )
                    except GitError:
                        # A closed PR may have lost its ref, but its head
                        # is usually still in the mirror from the build.
                        logger.info("PR ref fetch failed", extra={"pr": pr["number"]})
                    # The PR tree is untrusted: no push token in its remote.
                    checkout = await stack.enter_async_context(
                        cloned_checkout(
                            o.repos, info, worktree.path, pr["headRev"], info.clone_url
                        )
                    )
                task_token = o.task_tokens.issue(
                    build.project_id,
                    EffectIdentity(
                        forge=info.forge,
                        owner=info.owner,
                        repo=info.repo,
                        event=row.kind,
                        effect=row.name,
                        build_id=build.id_,
                        sha=row.code_rev,
                        branch=info.default_branch,
                        pr_number=pr["number"] if pr else None,
                        actor=row.actor,
                    ),
                )
                stack.callback(o.task_tokens.revoke, task_token)
                ctx = effects_context(
                    o.config,
                    info,
                    worktree_path=worktree.path,
                    rev=row.code_rev,
                    branch=info.default_branch,
                    git_token=credentials.token if credentials is not None else None,
                    task_token=task_token,
                )
                ctx.effect_checkout = checkout
                ctx.bind_id_token_audiences = partial(
                    o.task_tokens.bind_audiences, task_token
                )
                success = await o.effects.run_event_effect(
                    ctx, row.kind, row.name, payload, writer.write
                )
        except Exception as e:
            logger.exception(
                "event effect crashed",
                extra={"build_id": build.id_, "kind": row.kind, "effect": row.name},
            )
            await writer.write(f"\n{e}\n".encode())
        if not success:
            error = failure_excerpt(writer.tail_lines()) or None
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id_,
        kind=row.kind,
        name=row.name,
        status="succeeded" if success else "failed",
        error=error,
        log_size=writer.bytes_seen,
        log_truncated=writer.truncated,
    )
