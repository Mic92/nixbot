"""Effect checks, stored as effect_runs of kind 'check'. Every build
evaluates the onPush/onEvent effects of its own commit and builds their
dependencies without running them, the way hercules-ci-agent treats a
`runIf false` effect. That turns a pull request red when it breaks an
effect, before the effect would run on the default branch. Check names
are `<kind>.<effect>`, kind being `push` or an event kind."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from nixbot_effects import EffectError

from .db_gen import builds as builds_q
from .db_gen import maintenance as q
from .db_gen import work_queue as wq
from .effects import EffectsContext, effects_context
from .executor import failure_excerpt

if TYPE_CHECKING:
    from pathlib import Path

    from .db import BuildRecord
    from .events import ChangeEvent, RepoInfo
    from .gitrepo import FetchCredentials
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

KIND = "check"


def build_context(
    o: Orchestrator, event: ChangeEvent, worktree_path: Path
) -> EffectsContext:
    """Context for evaluating and building only. The tree was already
    evaluated by the build, and nothing runs, so no tokens or secrets."""
    return effects_context(
        o.config,
        event.repo,
        worktree_path=worktree_path,
        rev=event.commit_sha,
        branch=event.branch,
        git_token=None,
        task_token=None,
    )


async def record_eval_error(
    o: Orchestrator, build: BuildRecord, source: str, error: Exception | None
) -> None:
    """Remember that `source` (onPush or onEvent) of the built commit
    failed to evaluate, or clear it with error=None."""
    if error is None:
        await builds_q.clear_effect_eval_error(
            o.pool, build_id=build.id_, source=source
        )
        return
    logger.info(
        "effect definitions failed to evaluate",
        extra={"build_id": build.id_, "source": source},
    )
    await builds_q.record_effect_eval_error(
        o.pool,
        build_id=build.id_,
        source=source,
        error=str(error)[-8000:],
        code_rev=None,
    )


async def discover_checks(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    gated_push_effects: list[str],
) -> list[str] | None:
    """Names of the checks this build should run: all onEvent effects of
    the built commit, plus the onPush effects that are gated off on this
    ref and so would otherwise go untested. None if onEvent does not
    evaluate."""
    ctx = build_context(o, event, worktree_path)
    try:
        listing = await o.effects.list_all_event_effects(ctx)
    except (EffectError, OSError) as e:
        await record_eval_error(o, build, "onEvent", e)
        return None
    await record_eval_error(o, build, "onEvent", None)
    names = [f"push.{n}" for n in gated_push_effects]
    names += [f"{kind}.{n}" for kind, effects in listing.items() for n in effects]
    return names


async def enqueue_checks(
    o: Orchestrator, event: ChangeEvent, build: BuildRecord, names: list[str]
) -> None:
    await builds_q.start_pending_checks(o.pool, build_id=build.id_, names=names)
    if not names:
        return
    await o.reporter.effects_started(event, build, len(names))
    await wq.enqueue_effect_items(
        o.pool,
        build_id=build.id_,
        kind=KIND,
        names=names,
        dedup_keys=[f"build-{build.id_}-check-{n}" for n in names],
    )


async def run_check_item(
    o: Orchestrator,
    info: RepoInfo,
    build: BuildRecord,
    name: str,
    credentials: FetchCredentials | None,
) -> None:
    row = await q.effect_run(o.pool, build_id=build.id_, kind=KIND, name=name)
    if row is None or row.status != "pending":
        return
    kind, _, effect = name.partition(".")
    run_id = await builds_q.start_effect(
        o.pool, build_id=build.id_, kind=KIND, name=name, status="running"
    )
    if run_id is None:
        return
    success = False
    async with o.open_effect_log(run_id) as writer:
        try:
            async with o.rerun_worktree(info, build, "check", credentials) as (
                event,
                worktree_path,
            ):
                ctx = build_context(o, event, worktree_path)
                ctx.log = writer.write
                if kind == "push":
                    await o.effects.check_effect(ctx, effect)
                else:
                    await o.effects.check_event_effect(ctx, kind, effect)
                success = True
        except EffectError as e:
            await writer.write(f"error: {e}\n".encode())
        except Exception as e:
            logger.exception(
                "effect check crashed", extra={"build_id": build.id_, "check": name}
            )
            await writer.write(f"\n{e}\n".encode())
    await builds_q.finish_effect(
        o.pool,
        build_id=build.id_,
        kind=KIND,
        name=name,
        status="succeeded" if success else "failed",
        error=None if success else failure_excerpt(writer.tail_lines()) or None,
        log_size=writer.bytes_seen,
        log_truncated=writer.truncated,
    )
