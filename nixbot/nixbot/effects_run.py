"""Effects execution: gated discovery after a successful build,
per-effect queue items, and running one queued effect with its own
row and log.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote

from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .db_gen import web as web_q
from .db_gen import work_queue as wq
from .effects import (
    EffectsContext,
    EffectsError,
    effects_context,
    list_effects,
    run_effect,
    should_run_effects,
)
from .executor import failure_excerpt
from .repo_config import CONFIG_FILENAMES, BranchConfig

if TYPE_CHECKING:
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent, RepoInfo
    from .gitrepo import FetchCredentials
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
    # refs/heads/ prefix: a bare branch name would resolve a tag
    # of the same name first (tags auto-follow into the clone).
    config_text = None
    for filename in CONFIG_FILENAMES:
        config_text = await o.repos.show_file(
            repo.key, f"refs/heads/{repo.default_branch}", filename
        )
        if config_text is not None:
            break
    default_branch_config = BranchConfig.loads(config_text)
    if not should_run_effects(
        default_branch_config,
        repo.default_branch,
        event.branch,
        is_pull_request=event.pr_number is not None,
    ):
        return
    # The started-flag guards against auto-re-running effects on
    # crash recovery (deploys are not idempotent).
    if await builds_q.mark_effects_started(o.pool, id_=build.id) is None:
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
        names = await list_effects(ctx)
    except (EffectsError, OSError):
        # OSError: nixbot-effects not installed; effects are
        # best-effort and must not fail the (already reported) build.
        logger.exception("effects discovery failed", extra={"build_id": build.id})
        return
    finally:
        o.task_tokens.revoke(task_token)
    # Effects removed from the flake since the last run would
    # otherwise linger as stale pending rows.
    await q.drop_removed_effects(o.pool, build_id=build.id, names=names)
    await _enqueue_effects(o, event, build, names)


async def _enqueue_effects(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, names: list[str]
) -> None:
    """One queue item per effect, on the build's dedup key."""
    # Effect names are repo-controlled; a duplicate inside one batch
    # would make ON CONFLICT DO UPDATE fail with "cannot affect row a
    # second time".
    names = list(dict.fromkeys(names))
    if not names:
        return
    await builds_q.start_pending_effects(o.pool, build_id=build.id, names=names)
    await o.reporter.effects_started(event, build, len(names))
    await wq.enqueue_effect_items(
        o.pool,
        dedup_key=f"build-{build.id}",
        build_id=build.id,
        names=names,
    )


async def run_effect_item(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    name: str,
    credentials: FetchCredentials | None = None,
) -> None:
    """Dispatcher entry for one queued effect."""
    row = await q.effect_status(o.pool, build_id=build.id, name=name)
    if row != "pending":
        # Swept after a crash mid-run, or already terminal; started
        # effects never auto-re-run (deploys are not idempotent).
        return
    async with o.rerun_worktree(info, build, "effect", credentials) as (
        event,
        worktree_path,
    ):
        task_token = o.task_tokens.issue(build.project_id)
        try:
            ctx = effects_context(
                o.config,
                info,
                worktree_path=worktree_path,
                rev=event.commit_sha,
                branch=event.branch,
                git_token=credentials.token if credentials is not None else None,
                task_token=task_token,
            )
            await _run_one_effect(o, event, ctx, build, name)
            await _maybe_post_effects_summary(o, event, build)
        finally:
            o.task_tokens.revoke(task_token)


async def _maybe_post_effects_summary(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord
) -> None:
    """Post the aggregate status once all effects settle; the items run
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
    # A green commit status on a failed deploy hides the failure; report
    # per-effect status so the forge reflects the real outcome.
    await o.reporter.effect_started(event, build, name)
    # Effect names come from untrusted flakes; percent-encode so
    # the log file cannot escape the log directory. The "effects/"
    # subdirectory keeps them apart from attribute logs (a flat
    # prefix would collide with an attribute named "effect-X").
    async with o.open_log(
        build.id, f"effect:{name}", f"effects/{quote(name, safe='')}.zst"
    ) as writer:
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
        log_path=str(writer.path.relative_to(o.config.state_dir)),
        log_size=writer.bytes_seen,
        log_truncated=writer.truncated,
    )
    await o.reporter.effect_finished(event, build, name, success=success, error=error)
