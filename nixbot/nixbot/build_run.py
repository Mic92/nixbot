"""Build execution: evaluation, attribute scheduling, result
persistence, and the scheduler executor adapter.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from . import db
from .build_scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
    JobScheduler,
)
from .db import BuildStatus
from .db_gen import builds as q
from .events import BuildResult
from .executor import failure_excerpt
from .live_warnings import LiveWarningAggregator
from .memory import calculate_eval_workers
from .models import CacheStatus, NixEvalJobSuccess
from .nix_eval import EvalError, EvalSettings
from .post_build import build_props, run_post_build_steps
from .repo_config import BranchConfig

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    import asyncpg

    from .build_scheduler import CachedFailure, FailedBuildCache
    from .db import BuildRecord
    from .events import ChangeEvent
    from .gitrepo import FetchCredentials
    from .models import NixEvalJob
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

LIVE_WARNINGS_FLUSH_INTERVAL = 2.0


async def record_attributes(
    pool: asyncpg.Pool, build_id: int, jobs: Sequence[NixEvalJob]
) -> None:
    """Persist eval results as pending rows (with statically-known
    outputs) so crash recovery can resume without a re-eval; eval
    failures are settled by the scheduler."""
    successes = [job for job in jobs if isinstance(job, NixEvalJobSuccess)]
    if not successes:
        return
    await q.record_attributes(
        pool,
        build_id=build_id,
        attrs=[job.attr for job in successes],
        systems=[job.system for job in successes],
        drv_paths=[job.drv_path for job in successes],
        outputs=[json.dumps(job.outputs) for job in successes],
    )


async def get_eval_jobs(
    pool: asyncpg.Pool, build_id: int
) -> list[NixEvalJobSuccess] | None:
    """Reconstruct the eval job set from the build's attribute rows;
    None when any row lacks a drv_path (eval failures must be
    reproduced by a fresh evaluation). Reconstructed jobs carry no
    dependency closures, like the crash-recovery rerun path."""
    rows = await q.eval_job_rows(pool, build_id=build_id)
    jobs = []
    for row in rows:
        if not row.drv_path:
            return None
        outputs = json.loads(row.outputs) if row.outputs else {}
        jobs.append(
            NixEvalJobSuccess(
                attr=row.attr,
                attr_path=row.attr.split("."),
                cache_status=CacheStatus.not_built,
                needed_builds=[],
                needed_substitutes=[],
                drv_path=row.drv_path,
                name=row.attr,
                outputs=outputs or {"out": None},
                system=row.system or "",
            )
        )
    return jobs


async def run_build(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None = None,
) -> None:
    """Evaluate and build; every attribute completion is one
    transactional DB write, then the result is re-aggregated."""
    try:
        await _run_build_inner(o, event, build, worktree_path, credentials)
    except Exception as e:
        # Catch-all: a DB outage or GitError mid-eval would
        # otherwise wedge the build in 'evaluating' with no
        # terminal forge status.
        if isinstance(e, EvalError):
            logger.warning(
                "evaluation failed",
                extra={"build_id": build.id, "error": str(e)},
            )
        else:
            logger.exception(
                "build failed with unexpected error", extra={"build_id": build.id}
            )
        # Skip settling when the final fan-out already happened
        # (e.g. the effects phase failed): the build's aggregated
        # result must not be overwritten with a failure.
        current = await q.get_build(o.pool, id_=build.id)
        if current is None or current.status not in BuildStatus.TERMINAL:
            await _settle_aborted(o, event, build, BuildStatus.FAILED, error=str(e))
    finally:
        # Eval gc-roots only need to outlive the build; without
        # cleanup the nix store grows unboundedly.
        shutil.rmtree(o.gcroots_dir(build), ignore_errors=True)


def _eval_settings(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    credentials: FetchCredentials | None,
) -> EvalSettings:
    # Auto-sized workers come with a matching per-worker memory
    # limit; the configured limit acts as a ceiling. An explicit
    # worker count keeps the configured limit as-is.
    if o.config.eval_worker_count:
        worker_count = o.config.eval_worker_count
        eval_max_memory = o.config.eval_max_memory_size
    else:
        worker_config = calculate_eval_workers()
        worker_count = worker_config.count
        eval_max_memory = min(
            o.config.eval_max_memory_size, worker_config.max_memory_mib
        )
    # PR-controlled eval can fetch arbitrary flake inputs with the
    # netrc; an instance-wide token (Gitea/GitLab) would let a
    # malicious PR read any private repo on the forge. Only
    # repo-scoped tokens (GitHub) reach PR evals.
    netrc_file = None
    if credentials is not None and (event.pr_number is None or credentials.repo_scoped):
        netrc_file = credentials.netrc_file
    return EvalSettings(
        gc_roots_dir=o.gcroots_dir(build),
        timeout=o.config.eval_timeout,
        worker_count=worker_count,
        max_memory_size_mib=eval_max_memory,
        show_trace=o.config.show_trace_on_failure,
        netrc_file=netrc_file,
        # The worktree's .git points into the central clone; the
        # sandboxed evaluator needs to read it.
        extra_ro_paths=[o.repos.clone_path(event.repo.key)],
    )


async def _settle_aborted(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    status: str,
    *,
    error: str | None = None,
) -> None:
    """Terminal bookkeeping and status fan-out for a build that
    ended without a normal aggregation (failure or cancellation)."""
    # Otherwise pending/building attribute rows would look like they
    # are still running.
    await q.settle_unfinished_attributes(o.pool, build_id=build.id)
    await db.set_build_status(o.pool, build.id, status, error=error)
    if status == BuildStatus.CANCELLED:
        await o.reporter.eval_cancelled(event, build)
    else:
        await o.reporter.eval_finished(event, build, success=False, warnings=[])
    await o.reporter.build_finished(
        event, build, BuildResult(status, build.status_generation, [])
    )
    await o.finish_linked(
        build,
        BuildResult(status, build.status_generation, []),
        eval_success=None if status == BuildStatus.CANCELLED else False,
    )


async def _reap(*tasks: asyncio.Future[Any]) -> None:
    """Cancel and await tasks, swallowing their errors."""
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


async def _run_build_inner(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
) -> None:
    await o.reporter.build_started(event, build)
    await db.set_build_status(o.pool, build.id, BuildStatus.EVALUATING)

    if await _try_reuse_eval(o, event, build, worktree_path, credentials):
        return

    branch_config = BranchConfig.load(worktree_path)
    eval_settings = _eval_settings(o, event, build, credentials)
    # Race the evaluation against the cancel event: a superseded
    # build must not hold the eval slot to completion.
    cancel_event = o.cancel_events.setdefault(build.id, asyncio.Event())

    jobs_queue: asyncio.Queue[list[NixEvalJob] | None] = asyncio.Queue()

    async def record_job_batch(jobs: list[NixEvalJob]) -> None:
        # Pending rows appear in the UI while the eval is running.
        await record_attributes(
            o.pool,
            build.id,
            [
                job
                for job in jobs
                if isinstance(job, NixEvalJobSuccess)
                and job.system in o.config.build_systems
            ],
        )
        await jobs_queue.put(jobs)

    # Deduplicated warnings appear on the build page while the eval
    # runs; DB writes are throttled since retry storms emit one
    # line per narinfo.
    live_warnings = LiveWarningAggregator()
    last_flush = 0.0

    async def record_stderr_line(line: str) -> None:
        nonlocal last_flush
        if not live_warnings.add(line):
            return
        now = time.monotonic()
        if now - last_flush >= LIVE_WARNINGS_FLUSH_INTERVAL:
            last_flush = now
            await q.set_eval_warnings(
                o.pool, id_=build.id, warnings=json.dumps(live_warnings.snapshot())
            )

    eval_task = asyncio.ensure_future(
        o.eval_runner.run(
            worktree_path,
            branch_config,
            eval_settings,
            on_jobs=record_job_batch,
            on_stderr_line=record_stderr_line,
        )
    )
    # Builds start as soon as the first eval batch arrives.
    build_task = asyncio.create_task(
        build_attributes(o, event, build, worktree_path, jobs_queue)
    )
    cancel_wait = asyncio.ensure_future(cancel_event.wait())
    try:
        try:
            await asyncio.wait(
                {eval_task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            cancel_wait.cancel()
        if cancel_event.is_set() and not eval_task.done():
            await _reap(eval_task, build_task)
            await _settle_aborted(o, event, build, BuildStatus.CANCELLED)
            return
        # EvalError and anything else propagate to run_build,
        # which settles the build as failed. Flush warnings dropped
        # by the throttle either way (run_build settles failures).
        try:
            eval_result = await eval_task
        finally:
            if live_warnings:
                await q.set_eval_warnings(
                    o.pool, id_=build.id, warnings=json.dumps(live_warnings.snapshot())
                )
        await db.set_build_status(o.pool, build.id, BuildStatus.BUILDING)
        # Idempotent backstop for the streaming inserts above;
        # pending rows are what crash recovery resumes from. The
        # scheduler drops unsupported systems; their pending rows
        # would never turn terminal, so don't record them.
        buildable = [
            job
            for job in eval_result.jobs
            if isinstance(job, NixEvalJobSuccess)
            and job.system in o.config.build_systems
        ]
        await record_attributes(o.pool, build.id, buildable)
        # The full eval result is recorded in build_attributes; a later
        # build of the same tree may reuse it instead of re-evaluating.
        await q.mark_eval_completed(o.pool, id_=build.id)
        await o.reporter.eval_finished(
            event,
            build,
            success=True,
            warnings=[str(g["message"]) for g in live_warnings.snapshot()],
            jobs=buildable,
        )

        # Re-send the complete eval result: the scheduler dedupes
        # by attr, so this only schedules jobs a streamed batch
        # missed (e.g. eval runners without on_jobs support).
        await jobs_queue.put(list(eval_result.jobs))
        await jobs_queue.put(None)
        status = await build_task
    except BaseException:
        # Reap both tasks or the build task leaks forever blocked
        # on the jobs queue and the evaluator process outlives the
        # build (nix_eval kills the evaluator on cancellation).
        await _reap(eval_task, build_task)
        raise

    if status == BuildStatus.SUCCEEDED:
        await o.maybe_run_effects(event, build, worktree_path, credentials)


async def _reusable_eval_jobs(
    o: Orchestrator, build: BuildRecord
) -> list[NixEvalJobSuccess] | None:
    """Eval result of another build of the same tree (e.g. cancelled
    after evaluation, retried later), reusable when its derivations
    are still in the store; None means evaluate afresh."""
    if build.tree_hash is None:
        return None
    source_id = await q.find_completed_eval(
        o.pool,
        project_id=build.project_id,
        tree_hash=build.tree_hash,
        exclude_build_id=build.id,
    )
    if source_id is None:
        return None
    jobs = await get_eval_jobs(o.pool, source_id)
    if jobs is None:
        return None
    # The recorded set may predate a build_systems config change.
    jobs = [job for job in jobs if job.system in o.config.build_systems]
    valid = await o.check_store_paths([job.drv_path for job in jobs])
    if any(job.drv_path not in valid for job in jobs):
        return None  # garbage-collected since the eval
    logger.info(
        "reusing eval results from earlier build",
        extra={"build_id": build.id, "source_build_id": source_id},
    )
    return jobs


async def _try_reuse_eval(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
) -> bool:
    """Skip nix-eval-jobs and build from a reused eval result; False
    when a fresh evaluation is needed."""
    reused = await _reusable_eval_jobs(o, build)
    if reused is None:
        return False
    await record_attributes(o.pool, build.id, reused)
    await q.mark_eval_completed(o.pool, id_=build.id)
    await db.set_build_status(o.pool, build.id, BuildStatus.BUILDING)
    await o.reporter.eval_finished(event, build, success=True, warnings=[], jobs=reused)
    # cache_failures=False: see _ReadOnlyFailedBuildCache.
    status = await build_attributes(
        o, event, build, worktree_path, reused, cache_failures=False
    )
    if status == BuildStatus.SUCCEEDED:
        await o.maybe_run_effects(event, build, worktree_path, credentials)
    return True


async def build_attributes(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    jobs: Sequence[NixEvalJob] | asyncio.Queue[list[NixEvalJob] | None],
    *,
    cache_failures: bool = True,
) -> str:
    """Schedule the attribute builds, persist their results, and
    re-aggregate the build (shared by fresh builds and reruns).
    Accepts either a complete job list or a queue fed during an
    ongoing evaluation. Returns the aggregated build status."""
    cancel_event = o.cancel_events.setdefault(build.id, asyncio.Event())

    async def record_early(result: AttributeResult) -> None:
        """Persist skips and dependency failures as they happen;
        otherwise they stay pending until the whole build ends."""
        await db.complete_attribute(o.pool, build.id, result, if_unfinished=True)

    failed_build_cache: FailedBuildCache | None = (
        o.failed_build_cache(build.project_id)
        if o.failed_build_cache is not None and o.config.cache_failed_builds
        else None
    )
    if failed_build_cache is not None and not cache_failures:
        failed_build_cache = _ReadOnlyFailedBuildCache(failed_build_cache)
    scheduler = JobScheduler(
        _OrchestratorExecutor(o, event, build, worktree_path, cancel_event),
        o.config.build_systems,
        failed_build_cache=failed_build_cache,
        build_url=f"{o.config.url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}",
        on_result=record_early,
    )
    if isinstance(jobs, asyncio.Queue):
        schedule_result = await scheduler.run_incremental(jobs)
    else:
        schedule_result = await scheduler.run(list(jobs))

    # Persist results the executor adapter didn't already write
    # (failed_eval, dependency_failed, cached_failure, skips).
    for result in schedule_result.results:
        await db.complete_attribute(o.pool, build.id, result, if_unfinished=True)

    # Skipped-as-local attributes still get gcroots/outputs
    # updates. A filesystem error here must not skip the final
    # aggregation and status fan-out below.
    post_process_error: str | None = None
    try:
        await o.post_process_skipped(event, schedule_result.skipped_out_paths)
    except Exception as e:
        logger.exception(
            "post-processing skipped attributes failed",
            extra={"build_id": build.id},
        )
        post_process_error = str(e)

    status, generation = await db.aggregate_build(o.pool, build.id)
    if post_process_error is not None:
        status = BuildStatus.FAILED
        await db.set_build_status(
            o.pool, build.id, BuildStatus.FAILED, error=post_process_error
        )
    await o.reporter.build_finished(
        event,
        build,
        BuildResult(
            status,
            generation,
            schedule_result.results,
            attr_statuses={
                r.attr: r.status
                for r in await q.attribute_statuses(o.pool, build_id=build.id)
            },
            attr_prefix=BranchConfig.load(worktree_path).attribute,
        ),
    )
    await o.finish_linked(
        build,
        BuildResult(status, generation, schedule_result.results),
        eval_success=True,
    )
    o.cancel_events.pop(build.id, None)

    if status == BuildStatus.SUCCEEDED:
        await o.refresh_schedules(event)
    return status


class _ReadOnlyFailedBuildCache:
    """Failed-build cache that skips known failures but records none.

    Recovery/restart reruns rebuild jobs from DB rows without dependency
    closures, so dependents of one broken drv fail with their own build
    error; recording those would poison the cache."""

    def __init__(self, inner: FailedBuildCache) -> None:
        self._inner = inner

    async def check(self, drv_path: str) -> CachedFailure | None:
        return await self._inner.check(drv_path)

    async def add(self, drv_path: str, url: str) -> None:
        pass


class _OrchestratorExecutor:
    """Scheduler executor adapter: runs the build, then post-build
    steps, gcroots, outputs, and writes the attribute completion as one
    transactional write."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        cancel_event: asyncio.Event,
    ) -> None:
        self.o = orchestrator
        self.event = event
        self.build_record = build
        self.worktree_path = worktree_path
        self.cancel_event = cancel_event

    async def build(self, job: NixEvalJobSuccess) -> BuildOutcome:
        try:
            return await self._build_inner(job)
        except Exception:
            logger.exception(
                "unexpected error building attribute",
                extra={"build_id": self.build_record.id, "attr": job.attr},
            )
            result = AttributeResult(
                attr=job.attr,
                status=AttributeStatus.failed,
                job=job,
                error="internal error, see service logs",
                drv_path=job.drv_path,
                system=job.system,
            )
            await db.complete_attribute(self.o.pool, self.build_record.id, result)
            # Internal errors are not derivation failures: don't cache.
            return BuildOutcome.failure_no_cache

    async def _build_inner(self, job: NixEvalJobSuccess) -> BuildOutcome:
        # Flip to 'building' and stamp started_at so the web UI can
        # distinguish running attributes from queued ones; returns no
        # row when the attribute is already terminal.
        marked = await q.mark_attribute_building(
            self.o.pool,
            build_id=self.build_record.id,
            attr=job.attr,
            system=job.system,
            drv_path=job.drv_path,
        )
        if marked is None:
            # Cancelled externally while waiting on dependencies: do
            # not resurrect the row by building it anyway.
            return BuildOutcome.cancelled
        # Per-attribute cancellation: the executor watches one event, so
        # mirror the build-level cancel into the attribute's own event.
        attr_cancel = asyncio.Event()
        self.o.attr_cancel_events[(self.build_record.id, job.attr)] = attr_cancel

        async def _mirror_build_cancel() -> None:
            await self.cancel_event.wait()
            attr_cancel.set()

        mirror = asyncio.create_task(_mirror_build_cancel())
        # Attribute names come from untrusted flakes; percent-encode
        # so the log file cannot escape the log directory.
        try:
            async with self.o.open_log(
                self.build_record.id, job.attr, f"{quote(job.attr, safe='')}.zst"
            ) as writer:
                outcome = await self.o.executor.build_attribute(
                    self.build_record.id,
                    job,
                    writer,
                    self.worktree_path,
                    attr_cancel,
                )
                if outcome == BuildOutcome.success and self.o.config.post_build_steps:
                    props = build_props(self.event, job)
                    step_results = await run_post_build_steps(
                        self.o.config.post_build_steps, props, self.worktree_path
                    )
                    for step in step_results:
                        await writer.write(
                            f"\npost-build step {step.name}: "
                            f"{'ok' if step.success else 'failed'}\n".encode()
                        )
                        await writer.write(step.output.encode())
                    if any(step.failed for step in step_results):
                        # The derivation built: fail the attribute without
                        # poisoning the failed-build cache.
                        outcome = BuildOutcome.post_build_failure
        finally:
            mirror.cancel()
            self.o.attr_cancel_events.pop((self.build_record.id, job.attr), None)

        status = {
            BuildOutcome.success: AttributeStatus.succeeded,
            BuildOutcome.failure: AttributeStatus.failed,
            BuildOutcome.failure_no_cache: AttributeStatus.failed,
            BuildOutcome.post_build_failure: AttributeStatus.failed,
            BuildOutcome.cancelled: AttributeStatus.cancelled,
        }[outcome]
        # Failed attributes carry a log-tail excerpt so the build page
        # answers "why" without a click into the log. ANSI stays: the
        # web layer renders it, the API strips it.
        error = None
        if status == AttributeStatus.failed:
            error = failure_excerpt(writer.tail_lines()) or None
        result = AttributeResult(
            attr=job.attr,
            status=status,
            job=job,
            error=error,
            out_path=job.outputs.get("out"),
            drv_path=job.drv_path,
            system=job.system,
        )
        await db.complete_attribute(
            self.o.pool,
            self.build_record.id,
            result,
            log_path=str(writer.path.relative_to(self.o.config.state_dir)),
            log_size=writer.bytes_seen,
            log_truncated=writer.truncated,
        )
        if outcome == BuildOutcome.success:
            try:
                await self.o.post_process_skipped(
                    self.event, [(job.attr, job.outputs.get("out") or "")]
                )
            except Exception:
                # Must not overwrite the recorded success or poison
                # the failed-build cache.
                logger.exception(
                    "post-processing failed",
                    extra={"build_id": self.build_record.id, "attr": job.attr},
                )
        return outcome
