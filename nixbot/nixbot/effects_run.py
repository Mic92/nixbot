"""Effects execution: discovery after a successful build, gating,
per-effect queue items, and running one queued effect with its own
row and log.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from nixbot_effects import EffectError

from . import effect_checks
from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .db_gen import work_queue as wq
from .effects import (
    EffectMeta,
    EffectsContext,
    effect_push_url,
    effects_context,
    should_run_effects,
)
from .event_effects import cloned_checkout, run_event_effect_item
from .events import effects_event_for_build
from .executor import failure_excerpt
from .running_effect import RunningEffect
from .workload_identity import identity_from_event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent, RepoInfo
    from .gitrepo import FetchCredentials, RepoManager
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)


async def maybe_run_effects(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None = None,
    *,
    only: list[str] | None = None,
    force_run: bool = False,
) -> None:
    """The only producer of pending build-owned effect rows, each with
    its queue item. `only` narrows a rerun to the named effects."""
    fresh = await builds_q.get_build(o.pool, id_=build.id_)
    if fresh is None:
        return
    build = fresh
    allowed = await _effects_allowed(o, event, credentials)
    if not allowed and build.effects_commit_sha is not None:
        # A gated ref does not revoke a recorded allowed one.
        event = effects_event_for_build(event.repo, build)
        allowed = await _effects_allowed(o, event, credentials)
    await builds_q.record_effects_ref(
        o.pool,
        id_=build.id_,
        commit_sha=event.commit_sha,
        branch=event.branch,
        pr_number=event.pr_number,
        allowed=allowed,
    )
    if allowed and not await _claim_effects(o, build, only):
        return
    effects = await discover_effects(o, event, build, worktree_path)
    if only is None:
        # Dependencies of onEvent effects and of onPush effects that do
        # not run here are built as checks, so a PR breaking them is red.
        gated = [] if allowed or effects is None else list(effects)
        checks = await effect_checks.discover_checks(
            o, event, build, worktree_path, gated
        )
        await effect_checks.enqueue_checks(o, event, build, checks or [])
    if effects is None:
        return
    # Also drops rows a rerun selected that the flake no longer has.
    await q.drop_removed_effects(o.pool, build_id=build.id_, names=list(effects))
    if only is not None:
        effects = {n: m for n, m in effects.items() if n in only}
    await _record_effects(
        o, event, build, effects, allowed=allowed, force_run=force_run
    )


async def _claim_effects(
    o: Orchestrator, build: BuildRecord, only: list[str] | None
) -> bool:
    """Set the started-flag once per build: deploys are not idempotent,
    crash recovery must not re-run them."""
    if build.effects_started:
        return False
    if only is None:
        # Rows a gated pass left (skipped effects, its checks) go.
        await o.drop_effects(build.id_, None)
    return await builds_q.mark_effects_started(o.pool, id_=build.id_) is not None


async def _record_effects(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    effects: dict[str, EffectMeta],
    *,
    allowed: bool,
    force_run: bool = False,
) -> None:
    """Pending rows plus queue items, or skipped rows on a gated ref so
    the page lists what a merge would run."""
    if not effects:
        return
    names = list(effects)
    await builds_q.insert_build_effects(
        o.pool,
        build_id=build.id_,
        kind="push",
        status="pending" if allowed else "skipped",
        names=names,
        deps=[json.dumps(list(effects[n].after)) for n in names],
        changed=[_changed_json(effects[n]) for n in names],
        force_run=force_run,
    )
    if not allowed:
        return
    await o.reporter.effects_started(event, build, len(names))
    await wq.enqueue_effect_items(
        o.pool,
        build_id=build.id_,
        kind="push",
        names=names,
        dedup_keys=[_dedup_key(build, n, effects[n]) for n in names],
    )


def _changed_json(meta: EffectMeta) -> str:
    """ "" becomes NULL in the INSERT."""
    return "" if meta.changed is None else json.dumps(meta.changed, sort_keys=True)


async def _reuse_last_success(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, name: str
) -> bool:
    """Decided at claim time: the run that answers the question is
    often still ahead of this item in the same lock queue."""
    run = await q.effect_run(o.pool, build_id=build.id_, kind="push", name=name)
    if run is None or run.changed_inputs is None or run.force_run:
        return False
    source = await builds_q.last_real_run_with_inputs(
        o.pool,
        project_id=build.project_id,
        name=name,
        changed_inputs=run.changed_inputs,
        build_id=build.id_,
    )
    if source is None:
        return False
    if (
        await builds_q.reuse_effect_run(
            o.pool, reused_from=source.id_, build_id=build.id_, name=name
        )
        is None
    ):
        return False
    await o.reporter.effect_finished(event, build, name, success=True, error=None)
    return True


async def _effects_allowed(
    o: Orchestrator, event: ChangeEvent, credentials: FetchCredentials | None
) -> bool:
    repo = event.repo
    # Gating config comes from the default branch of the central
    # clone: the worktree is PR-controlled, so its nixbot.toml
    # could grant the PR effects (and deploy secrets).
    default_branch_config = await o.default_branch_repo_config(
        repo, credentials=credentials
    )
    return should_run_effects(
        default_branch_config,
        repo.default_branch,
        event.branch,
        is_pull_request=event.pr_number is not None,
    )


async def discover_effects(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
) -> dict[str, EffectMeta] | None:
    """The flake's effects, or None when discovery failed. A pure eval of
    a tree the build already evaluated, so it needs no tokens."""
    ctx = effects_context(
        o.config,
        event.repo,
        worktree_path=worktree_path,
        rev=event.commit_sha,
        branch=event.branch,
        git_token=None,
        task_token=None,
    )
    try:
        # A bad effect DAG (cycle, unknown dependency) fails discovery
        # here and its reason ends up in the log.
        effects = await o.effects.list_effects(ctx)
    except (EffectError, OSError) as e:
        # OSError: nix/git missing from PATH.
        await effect_checks.record_eval_error(o, build, "onPush", e)
        return None
    await effect_checks.record_eval_error(o, build, "onPush", None)
    return effects


def _dedup_key(build: BuildRecord, name: str, meta: EffectMeta) -> str:
    """Locked effects share a per-project key so runs serialize across
    builds; unlocked ones get per-effect keys so independent effects of
    one build run in parallel (the claim query holds back effects whose
    dependencies have not settled)."""
    if meta.lock is not None:
        return f"effect-lock-{build.project_id}-{meta.lock}"
    return f"build-{build.id_}-effect-{name}"


async def _skip_effect(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, name: str, failed_dep: str
) -> None:
    """Terminal row for an effect whose dependency did not succeed.
    Counts as failed on the forge (a deploy that never ran must not
    look green)."""
    error = f"dependency '{failed_dep}' did not succeed"
    if (
        await builds_q.claim_effect(
            o.pool,
            build_id=build.id_,
            kind="push",
            name=name,
            status="dependency_failed",
        )
        is None
    ):
        return
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id_,
        kind="push",
        name=name,
        status="dependency_failed",
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
    async with cloned_checkout(repos, info, worktree_path, commit, push_url) as dest:
        yield dest


async def run_effect_item(  # noqa: PLR0913
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    name: str,
    kind: str = "push",
    credentials: FetchCredentials | None = None,
) -> None:
    """Dispatcher entry for one queued effect: run it, or skip it when a
    dependency did not succeed."""
    task = asyncio.current_task()
    if task is None:
        msg = "run_effect_item must run inside a task"
        raise RuntimeError(msg)
    if kind not in ("push", effect_checks.KIND):
        await run_event_effect_item(o, info, build, kind, name, credentials)
        return
    running = RunningEffect(task=task)
    o.running_effects[(build.id_, kind, name)] = running
    try:
        if kind == effect_checks.KIND:
            await effect_checks.run_check_item(o, info, build, name, credentials)
            await post_effects_summary(o, effects_event_for_build(info, build), build)
        else:
            await _claimed_effect_item(o, info, build, name, credentials)
    except asyncio.CancelledError:
        # The restart deletes the row and log after `settled`.
        if not running.restart:
            raise
    finally:
        o.running_effects.pop((build.id_, kind, name), None)
        running.settled.set()


async def _claimed_effect_item(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    name: str,
    credentials: FetchCredentials | None,
) -> None:
    row = await q.effect_status(o.pool, build_id=build.id_, name=name)
    if row != "pending":
        # Swept after a crash mid-run, or already terminal. Started
        # effects never auto-re-run (deploys are not idempotent).
        return
    dep_rows = await builds_q.effect_dep_statuses(o.pool, build_id=build.id_, name=name)
    failed_dep = next((d.name for d in dep_rows if d.status != "succeeded"), None)
    if failed_dep is not None:
        event = effects_event_for_build(info, build)
        await _skip_effect(o, event, build, name, failed_dep)
        await post_effects_summary(o, event, build)
        return
    if await _reuse_last_success(o, effects_event_for_build(info, build), build, name):
        await post_effects_summary(o, effects_event_for_build(info, build), build)
        return
    async with o.rerun_worktree(info, build, "effect", credentials) as (
        worktree_event,
        worktree_path,
    ):
        # The checkout ref (build commit) differs from the report ref
        # (see effects_event_for_build).
        event = effects_event_for_build(worktree_event.repo, build)
        task_token = o.task_tokens.issue(
            build.project_id, identity_from_event(event, name, build.id_)
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
    summary = await builds_q.effects_summary(o.pool, build_id=build.id_)
    if summary is None or summary.status in ("pending", "running"):
        return
    await o.reporter.effects_finished(
        event, build, failed=summary.failed, succeeded=summary.succeeded
    )


async def _run_one_effect(
    o: Orchestrator,
    event: ChangeEvent,
    ctx: EffectsContext,
    build: BuildRecord,
    name: str,
) -> None:
    """One effect with its own row and log."""
    run_id = await builds_q.claim_effect(
        o.pool, build_id=build.id_, kind="push", name=name, status="running"
    )
    if run_id is None:  # deleted by a restart since the item was claimed
        return
    # A green commit status on a failed deploy hides the failure. Report
    # per-effect status so the forge reflects the real outcome.
    await o.reporter.effect_started(event, build, name)
    async with o.open_effect_log(run_id) as writer:
        try:
            success = await o.effects.run_effect(ctx, name, writer.write)
        except Exception as e:
            # Any escape would leave the row running forever
            # (nothing re-runs effects) and kill the loop for the
            # remaining effects.
            logger.exception(
                "effect crashed",
                extra={"build_id": build.id_, "effect": name},
            )
            await writer.write(f"\n{e}\n".encode())
            success = False
    error = None
    if not success:
        logger.error("effect failed", extra={"build_id": build.id_, "effect": name})
        error = failure_excerpt(writer.tail_lines()) or None
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id_,
        kind="push",
        name=name,
        status="succeeded" if success else "failed",
        error=error,
        log_size=writer.bytes_seen,
        log_truncated=writer.truncated,
    )
    await o.reporter.effect_finished(event, build, name, success=success, error=error)
