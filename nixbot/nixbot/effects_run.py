"""Effects execution: gated discovery after a successful build,
per-effect queue items, and running one queued effect with its own
row and log.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from nixbot_effects import EffectError

from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .db_gen import web as web_q
from .db_gen import work_queue as wq
from .effects import (
    EffectMeta,
    EffectsContext,
    effect_push_url,
    effects_context,
    list_effects,
    run_effect,
    should_run_effects,
)
from .events import effects_event_for_build
from .executor import failure_excerpt
from .workload_identity import identity_from_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent, RepoInfo
    from .gitrepo import FetchCredentials, RepoManager
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def maybe_run_effects(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None = None,
) -> None:
    repo = event.repo
    # Gating config comes from the default branch of the central
    # clone: the worktree is PR-controlled, so its nixbot.toml
    # could grant the PR effects (and deploy secrets).
    default_branch_config = await o.default_branch_repo_config(
        repo, credentials=credentials
    )
    if not should_run_effects(
        default_branch_config,
        repo.default_branch,
        event.branch,
        is_pull_request=event.pr_number is not None,
    ):
        return
    # The started-flag guards against auto-re-running effects on crash
    # recovery (deploys are not idempotent). Record the triggering ref:
    # effect items carry only build_id and must report on this commit,
    # not the build's stored commit_sha (a reused PR head).
    if (
        await builds_q.mark_effects_started(
            o.pool,
            id_=build.id,
            commit_sha=event.commit_sha,
            branch=event.branch,
            pr_number=event.pr_number,
        )
        is None
    ):
        return
    task_token = o.task_tokens.issue(build.project_id)
    ctx = effects_context(
        o.config,
        repo,
        worktree_path=worktree_path,
        rev=event.commit_sha,
        branch=event.branch,
        git_token=credentials.token if credentials is not None else None,
        task_token=task_token,
    )
    try:
        # A bad effect DAG (cycle, unknown dependency) fails discovery
        # here and its reason ends up in the log.
        effects = await list_effects(ctx)
    except (EffectError, OSError):
        # OSError: nix/git missing from PATH. Effects are best-effort
        # and must not fail the (already reported) build.
        logger.exception("effects discovery failed", extra={"build_id": build.id})
        return
    finally:
        o.task_tokens.revoke(task_token)
    # Effects removed from the flake since the last run would
    # otherwise linger as stale pending rows.
    await q.drop_removed_effects(o.pool, build_id=build.id, names=list(effects))
    await _enqueue_effects(o, event, build, effects)


def _dedup_key(build: BuildRecord, name: str, meta: EffectMeta) -> str:
    """Locked effects share a per-project key so runs serialize across
    builds; unlocked ones get per-effect keys so independent effects of
    one build run in parallel (the claim query holds back effects whose
    dependencies have not settled)."""
    if meta.lock is not None:
        return f"effect-lock-{build.project_id}-{meta.lock}"
    return f"build-{build.id}-effect-{name}"


async def _enqueue_effects(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    effects: dict[str, EffectMeta],
) -> None:
    """One queue item per effect."""
    if not effects:
        return
    names = list(effects)
    await builds_q.start_pending_effects(
        o.pool,
        build_id=build.id,
        names=names,
        deps=[json.dumps(list(effects[n].after)) for n in names],
    )
    await o.reporter.effects_started(event, build, len(names))
    await wq.enqueue_effect_items(
        o.pool,
        build_id=build.id,
        names=names,
        dedup_keys=[_dedup_key(build, n, effects[n]) for n in names],
    )


async def _skip_effect(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, name: str, failed_dep: str
) -> None:
    """Terminal 'skipped' row: the effect never ran because a dependency
    did not succeed. Counts as failed on the forge (a deploy that never
    ran must not look green)."""
    error = f"skipped: dependency '{failed_dep}' did not succeed"
    await builds_q.start_effect(o.pool, build_id=build.id, name=name, status="skipped")
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id,
        name=name,
        status="skipped",
        error=error,
        log_size=0,
        log_truncated=False,
    )
    await o.reporter.effect_finished(event, build, name, success=False, error=error)


@asynccontextmanager
async def effect_checkout(
    repos: RepoManager,
    info: RepoInfo,
    worktree_path: Path,
    commit: str,
    credentials: FetchCredentials | None,
) -> AsyncIterator[Path | None]:
    """Pushable clone for effects declaring __nixbot_effect_checkout,
    prepared next to the build worktree. None without a forge token
    (nothing the effect could push with)."""
    push_url = (
        effect_push_url(info.forge, info.clone_url, credentials.token)
        if credentials is not None and credentials.token is not None
        else None
    )
    if push_url is None:
        yield None
        return
    dest = worktree_path.with_name(f"{worktree_path.name}-checkout")
    await repos.clone_for_effect(info.key, dest, commit=commit, push_url=push_url)
    try:
        yield dest
    finally:
        repos.remove_effect_clone(dest)


async def run_effect_item(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    name: str,
    credentials: FetchCredentials | None = None,
) -> None:
    """Dispatcher entry for one queued effect: run it, or skip it when a
    dependency did not succeed."""
    row = await q.effect_status(o.pool, build_id=build.id, name=name)
    if row != "pending":
        # Swept after a crash mid-run, or already terminal. Started
        # effects never auto-re-run (deploys are not idempotent).
        return
    dep_rows = await builds_q.effect_dep_statuses(o.pool, build_id=build.id, name=name)
    unsettled = [d.name for d in dep_rows if d.status in ("pending", "running")]
    if unsettled:
        # Only when an effects restart reset the rows under a stale claim;
        # the restart enqueued fresh items, so defer to those.
        logger.warning(
            "effect %s of build %s claimed before %s settled; deferring",
            name,
            build.id,
            ", ".join(unsettled),
        )
        return
    failed_dep = next((d.name for d in dep_rows if d.status != "succeeded"), None)
    if failed_dep is not None:
        event = effects_event_for_build(info, build)
        await _skip_effect(o, event, build, name, failed_dep)
        await post_effects_summary(o, event, build)
        return
    async with o.rerun_worktree(info, build, "effect", credentials) as (
        worktree_event,
        worktree_path,
    ):
        # The checkout ref (build commit) differs from the report ref
        # (see effects_event_for_build).
        event = effects_event_for_build(worktree_event.repo, build)
        task_token = o.task_tokens.issue(
            build.project_id, identity_from_event(event, name, build.id)
        )
        try:
            async with effect_checkout(
                o.repos, info, worktree_path, event.commit_sha, credentials
            ) as checkout:
                ctx = effects_context(
                    o.config,
                    info,
                    worktree_path=worktree_path,
                    rev=event.commit_sha,
                    branch=event.branch,
                    git_token=credentials.token if credentials is not None else None,
                    task_token=task_token,
                )
                ctx.effect_checkout = checkout
                # Audiences come from the effect derivation, evaluated by
                # run_effect just before its sandbox starts.
                ctx.bind_id_token_audiences = partial(
                    o.task_tokens.bind_audiences, task_token
                )
                await _run_one_effect(o, event, ctx, build, name)
            await post_effects_summary(o, event, build)
        finally:
            o.task_tokens.revoke(task_token)


async def post_effects_summary(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord
) -> None:
    """Post the aggregate status once all effects settle. The items run
    independently, so the last to finish reports it."""
    statuses = [e.status for e in await web_q.web_effects(o.pool, build_id=build.id)]
    if any(s in ("pending", "running") for s in statuses):
        return
    await o.reporter.effects_finished(
        event,
        build,
        failed=sum(1 for s in statuses if s != "succeeded"),
        succeeded=sum(1 for s in statuses if s == "succeeded"),
    )


async def _run_one_effect(
    o: Orchestrator,
    event: ChangeEvent,
    ctx: EffectsContext,
    build: BuildRecord,
    name: str,
) -> None:
    """One effect with its own row and log."""
    # A rerun resets the existing effect row.
    await builds_q.start_effect(o.pool, build_id=build.id, name=name, status="running")
    # A green commit status on a failed deploy hides the failure. Report
    # per-effect status so the forge reflects the real outcome.
    await o.reporter.effect_started(event, build, name)
    async with o.open_log(build.id, f"effect:{name}") as writer:
        try:
            success = await run_effect(ctx, name, writer.write)
        except Exception as e:
            # Any escape would leave the row running forever
            # (nothing re-runs effects) and kill the loop for the
            # remaining effects.
            logger.exception(
                "effect crashed",
                extra={"build_id": build.id, "effect": name},
            )
            await writer.write(f"\n{e}\n".encode())
            success = False
    error = None
    if not success:
        logger.error("effect failed", extra={"build_id": build.id, "effect": name})
        error = failure_excerpt(writer.tail_lines()) or None
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id,
        name=name,
        status="succeeded" if success else "failed",
        error=error,
        log_size=writer.bytes_seen,
        log_truncated=writer.truncated,
    )
    await o.reporter.effect_finished(event, build, name, success=success, error=error)
