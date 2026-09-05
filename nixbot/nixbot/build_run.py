"""Build execution: evaluation, attribute scheduling, result
persistence, and the scheduler executor adapter.

Calls back into other concerns only via Orchestrator methods, which
keeps the module dependency graph acyclic.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import shutil
import time
from typing import TYPE_CHECKING, Any

from . import db
from .after_build import after_build
from .build_scheduler import (
    AttributeResult,
    AttributeStatus,
    BuildOutcome,
    JobScheduler,
    outcome_status,
)
from .db import BuildStatus
from .db_gen import builds as q
from .db_gen import maintenance as mq
from .events import BuildResult, EvalReport
from .executor import LogWriter, failure_excerpt
from .flake_prefetch import PrefetchError, prefetch_flake_inputs
from .live_warnings import LiveWarningAggregator
from .memory import calculate_eval_workers
from .models import CacheStatus, NixEvalJobSuccess
from .nix_eval import EvalError, EvalResult, EvalSettings
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


@dataclasses.dataclass
class _JobColumns:
    attrs: list[str]
    systems: list[str]
    drv_paths: list[str]
    outputs: list[str]
    eval_warnings: list[str]
    # -1 stands for NULL so the arrays stay int-typed for asyncpg.
    eval_wall_ms: list[int]
    eval_alloc_bytes: list[int]


def _job_columns(jobs: Sequence[NixEvalJob]) -> _JobColumns:
    successes = [job for job in jobs if isinstance(job, NixEvalJobSuccess)]
    return _JobColumns(
        attrs=[job.attr for job in successes],
        systems=[job.system for job in successes],
        drv_paths=[job.drv_path for job in successes],
        outputs=[json.dumps(job.outputs) for job in successes],
        eval_warnings=[json.dumps(job.warnings or None) for job in successes],
        eval_wall_ms=[job.stats.wall_ms if job.stats else -1 for job in successes],
        eval_alloc_bytes=[
            job.stats.alloc_bytes if job.stats else -1 for job in successes
        ],
    )


async def record_attributes(
    pool: asyncpg.Pool, build_id: int, jobs: Sequence[NixEvalJob]
) -> None:
    """Persist eval results as pending rows so crash recovery can
    resume without a re-eval."""
    cols = _job_columns(jobs)
    if not cols.attrs:
        return
    await q.record_attributes(pool, build_id=build_id, **dataclasses.asdict(cols))


async def commit_eval_result(
    pool: asyncpg.Pool,
    build_id: int,
    jobs: Sequence[NixEvalJob],
    duration_ms: int | None = None,
) -> None:
    """Publish a completed eval: the only operation that may shrink
    the attribute set. duration_ms is None when the jobs were not
    produced by this build (eval reuse)."""
    cols = _job_columns(jobs)
    await mq.commit_eval_result(
        pool,
        build_id=build_id,
        eval_duration_ms=duration_ms,
        **dataclasses.asdict(cols),
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
    """Evaluate and build. Every attribute completion is one
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
                extra={"build_id": build.id_, "error": str(e)},
            )
        else:
            logger.exception(
                "build failed with unexpected error", extra={"build_id": build.id_}
            )
        # Skip settling when the final fan-out already happened
        # (e.g. the effects phase failed): the build's aggregated
        # result must not be overwritten with a failure.
        current = await q.get_build(o.pool, id_=build.id_)
        if current is None or current.status not in BuildStatus.TERMINAL:
            await _settle_aborted(o, event, build, BuildStatus.FAILED, error=str(e))
    finally:
        # Eval gc-roots only need to outlive the build. Without
        # cleanup the nix store grows unboundedly.
        shutil.rmtree(o.gcroots_dir(build), ignore_errors=True)


def _eval_settings(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    credentials: FetchCredentials | None,
) -> EvalSettings:
    # Auto-sized workers come with a matching per-worker memory
    # limit. The configured limit acts as a ceiling. An explicit
    # worker count keeps the configured limit as-is.
    worker_config = calculate_eval_workers()
    if o.config.eval_worker_count:
        worker_count = o.config.eval_worker_count
        eval_max_memory = o.config.eval_max_memory_size
    else:
        worker_count = worker_config.count
        eval_max_memory = min(
            o.config.eval_max_memory_size, worker_config.max_memory_mib
        )
    # PR-controlled eval can fetch arbitrary flake inputs with the
    # netrc. An instance-wide token (Gitea/GitLab) would let a
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
        cgroup_limit_mib=worker_config.cgroup_limit_mib,
        show_trace=o.config.show_trace_on_failure,
        netrc_file=netrc_file,
        # The worktree's .git points into the central clone. The
        # sandboxed evaluator needs to read it.
        extra_ro_paths=[o.repos.clone_path(event.repo.key)],
        eval_systems=o.config.eval_systems,
        legacy_attr_prefix=o.config.legacy_attr_prefix,
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
    await q.settle_unfinished_attributes(o.pool, build_id=build.id_)
    await db.set_build_status(o.pool, build.id_, status, error=error)
    if status == BuildStatus.CANCELLED:
        await o.reporter.eval_cancelled(event, build)
    else:
        await o.reporter.eval_finished(
            event, build, EvalReport(success=False, error=error)
        )
    await o.reporter.build_finished(
        event, build, BuildResult(status, build.status_generation, [])
    )
    await o.finish_linked(
        build,
        BuildResult(status, build.status_generation, []),
        eval_success=None if status == BuildStatus.CANCELLED else False,
    )
    refreshed = await q.get_build(o.pool, id_=build.id_)
    if refreshed is not None:
        await o.deliver_events(event, refreshed)


async def _record_eval_success(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    result: EvalResult,
    live_warnings: LiveWarningAggregator,
) -> None:
    """Persist a completed evaluation and report it to the forge."""
    # The scheduler drops unsupported systems, their rows would stay
    # pending forever.
    buildable = [
        job
        for job in result.jobs
        if isinstance(job, NixEvalJobSuccess) and job.system in o.config.build_systems
    ]
    await commit_eval_result(o.pool, build.id_, buildable, result.duration_ms)
    await o.reporter.eval_finished(
        event,
        build,
        EvalReport(
            success=True,
            warnings=[str(g["message"]) for g in live_warnings.snapshot()],
            jobs=buildable,
            duration_ms=result.duration_ms,
        ),
    )


async def _prefetch_inputs(
    o: Orchestrator,
    build: BuildRecord,
    worktree_path: Path,
    branch_config: BranchConfig,
    credentials: FetchCredentials | None,
) -> None:
    """Fetch flake inputs into the store outside the eval sandbox, with
    the same credentials as the git fetch, so private ssh:// inputs
    work even though the sandboxed evaluator has no SSH key (#86)."""
    try:
        async with asyncio.timeout(o.config.eval_timeout):
            await prefetch_flake_inputs(
                worktree_path,
                branch_config,
                o.gcroots_dir(build),
                credentials=credentials,
                cache_dir=o.config.state_dir / "cache" / "nix",
            )
    except TimeoutError:
        msg = (
            "prefetching flake inputs timed out after "
            f"{o.config.eval_timeout:.0f} seconds"
        )
        raise PrefetchError(msg) from None


async def _reap(*tasks: asyncio.Future[Any]) -> None:
    """Cancel and await tasks, swallowing their errors."""
    for task in tasks:
        if not task.done():
            task.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await task


async def _evaluate(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
    jobs_queue: asyncio.Queue[list[NixEvalJob] | None],
    live_warnings: LiveWarningAggregator,
) -> EvalResult:
    """Wait for an eval slot, then prefetch and evaluate, streaming
    jobs into jobs_queue and warnings into live_warnings."""

    async def record_job_batch(jobs: list[NixEvalJob]) -> None:
        # Pending rows appear in the UI while the eval is running.
        await record_attributes(
            o.pool,
            build.id_,
            [
                job
                for job in jobs
                if isinstance(job, NixEvalJobSuccess)
                and job.system in o.config.build_systems
            ],
        )
        await jobs_queue.put(jobs)

    # DB writes are throttled since retry storms emit one line per
    # narinfo.
    last_flush = 0.0

    async def record_stderr_line(line: str) -> None:
        nonlocal last_flush
        if not live_warnings.add(line):
            return
        now = time.monotonic()
        if now - last_flush >= LIVE_WARNINGS_FLUSH_INTERVAL:
            last_flush = now
            await q.set_eval_warnings(
                o.pool, id_=build.id_, warnings=json.dumps(live_warnings.snapshot())
            )

    branch_config = BranchConfig.load(worktree_path)
    async with o.eval_slots:
        await db.set_build_status(o.pool, build.id_, BuildStatus.EVALUATING)
        await _prefetch_inputs(o, build, worktree_path, branch_config, credentials)
        return await o.eval_runner.run(
            worktree_path,
            branch_config,
            _eval_settings(o, event, build, credentials),
            on_jobs=record_job_batch,
            on_stderr_line=record_stderr_line,
        )


async def _run_build_inner(
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    credentials: FetchCredentials | None,
) -> None:
    await o.reporter.build_started(event, build)
    # Reruns enter with a terminal row. Stay pending until a slot is held.
    await db.set_build_status(o.pool, build.id_, BuildStatus.PENDING)

    if await _try_reuse_eval(o, event, build, worktree_path, credentials):
        return

    jobs_queue: asyncio.Queue[list[NixEvalJob] | None] = asyncio.Queue()
    live_warnings = LiveWarningAggregator()
    # Race slot wait and evaluation against the cancel event. A
    # superseded build must not hold or wait for the slot to completion.
    cancel_event = o.cancel_events.setdefault(build.id_, asyncio.Event())
    eval_task = asyncio.ensure_future(
        _evaluate(
            o, event, build, worktree_path, credentials, jobs_queue, live_warnings
        )
    )
    # Builds start as soon as the first eval batch arrives.
    build_task = asyncio.create_task(
        build_attributes(
            o, event, build, worktree_path, jobs_queue, credentials=credentials
        )
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
                    o.pool, id_=build.id_, warnings=json.dumps(live_warnings.snapshot())
                )
        await db.set_build_status(o.pool, build.id_, BuildStatus.BUILDING)
        await _record_eval_success(o, event, build, eval_result, live_warnings)

        # Re-send the complete eval result: the scheduler dedupes
        # by attr, so this only schedules jobs a streamed batch
        # missed (e.g. eval runners without on_jobs support).
        await jobs_queue.put(list(eval_result.jobs))
        await jobs_queue.put(None)
        await build_task
    except BaseException:
        # Reap both tasks or the build task leaks forever blocked
        # on the jobs queue and the evaluator process outlives the
        # build (nix_eval kills the evaluator on cancellation).
        await _reap(eval_task, build_task)
        raise


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
        exclude_build_id=build.id_,
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
        extra={"build_id": build.id_, "source_build_id": source_id},
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
    await commit_eval_result(o.pool, build.id_, reused)
    await db.set_build_status(o.pool, build.id_, BuildStatus.BUILDING)
    await o.reporter.eval_finished(event, build, EvalReport(success=True, jobs=reused))
    # cache_failures=False: see _ReadOnlyFailedBuildCache.
    await build_attributes(
        o,
        event,
        build,
        worktree_path,
        reused,
        credentials=credentials,
        cache_failures=False,
    )
    return True


async def build_attributes(  # noqa: PLR0913
    o: Orchestrator,
    event: ChangeEvent,
    build: BuildRecord,
    worktree_path: Path,
    jobs: Sequence[NixEvalJob] | asyncio.Queue[list[NixEvalJob] | None],
    *,
    credentials: FetchCredentials | None,
    cache_failures: bool = True,
) -> str:
    """Schedule the attribute builds, persist their results, and
    re-aggregate the build (shared by fresh builds and reruns).
    Accepts either a complete job list or a queue fed during an
    ongoing evaluation. Returns the aggregated build status."""
    cancel_event = o.cancel_events.setdefault(build.id_, asyncio.Event())

    async def record_early(result: AttributeResult) -> None:
        """Persist skips and dependency failures as they happen;
        otherwise they stay pending until the whole build ends."""
        await db.complete_attribute(o.pool, build.id_, result, if_unfinished=True)

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
        await db.complete_attribute(o.pool, build.id_, result, if_unfinished=True)

    # Skipped-as-local attributes still get gcroots/outputs
    # updates. A filesystem error here must not skip the final
    # aggregation and status fan-out below.
    post_process_error: str | None = None
    try:
        await o.post_process_skipped(event, schedule_result.skipped_out_paths)
    except Exception as e:
        logger.exception(
            "post-processing skipped attributes failed",
            extra={"build_id": build.id_},
        )
        post_process_error = str(e)

    status, generation = await db.aggregate_build(o.pool, build.id_)
    if post_process_error is not None:
        status = BuildStatus.FAILED
        await db.set_build_status(
            o.pool, build.id_, BuildStatus.FAILED, error=post_process_error
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
                for r in await q.attribute_statuses(o.pool, build_id=build.id_)
            },
            attr_prefix=BranchConfig.load(worktree_path).attribute,
        ),
    )
    await o.finish_linked(
        build,
        BuildResult(status, generation, schedule_result.results),
        eval_success=True,
    )
    o.release_run(build.id_)
    await after_build(
        o, event, build, status, worktree_path=worktree_path, credentials=credentials
    )
    return status


class _ReadOnlyFailedBuildCache:
    """Failed-build cache that skips known failures but records none.

    Recovery/restart reruns rebuild jobs from DB rows without dependency
    closures, so dependents of one broken drv fail with their own build
    error. Recording those would poison the cache."""

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
                extra={"build_id": self.build_record.id_, "attr": job.attr},
            )
            result = AttributeResult(
                attr=job.attr,
                status=AttributeStatus.failed,
                job=job,
                error="internal error, see service logs",
                drv_path=job.drv_path,
                system=job.system,
            )
            await db.complete_attribute(self.o.pool, self.build_record.id_, result)
            # Internal errors are not derivation failures: don't cache.
            return BuildOutcome.failure_no_cache

    async def _after_success(
        self, job: NixEvalJobSuccess, writer: LogWriter
    ) -> BuildOutcome:
        """Uploads (warn-only) and post-build steps for a built attribute."""
        paths = (
            []
            if job.build_dependencies_only
            else [p for p in job.outputs.values() if p]
        )
        results = await asyncio.gather(*(u.upload(paths) for u in self.o.uploaders))
        for uploader, result in zip(self.o.uploaders, results, strict=True):
            await writer.write(
                f"\nupload {uploader.name}: "
                f"{'ok' if result.success else 'failed'}\n".encode()
            )
            await writer.write(result.output.encode())
        step_results = await run_post_build_steps(
            self.o.config.post_build_steps,
            build_props(self.event, job),
            self.worktree_path,
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
            return BuildOutcome.post_build_failure
        return BuildOutcome.success

    async def _build_inner(self, job: NixEvalJobSuccess) -> BuildOutcome:
        # Runs after slot acquisition so started_at (and the shown
        # duration) excludes queue wait. False: row already terminal.
        async def mark_building() -> bool:
            marked = await q.mark_attribute_building(
                self.o.pool,
                build_id=self.build_record.id_,
                attr=job.attr,
                system=job.system,
                drv_path=job.drv_path,
            )
            return marked is not None

        # Per-attribute cancellation: the executor watches one event, so
        # mirror the build-level cancel into the attribute's own event.
        attr_cancel = asyncio.Event()
        self.o.attr_cancel_events[(self.build_record.id_, job.attr)] = attr_cancel

        async def _mirror_build_cancel() -> None:
            await self.cancel_event.wait()
            attr_cancel.set()

        def on_built(drv: str) -> None:
            for uploader in self.o.uploaders:
                uploader.enqueue_nowait(drv)

        mirror = asyncio.create_task(_mirror_build_cancel())
        try:
            async with self.o.open_log(self.build_record.id_, job.attr) as writer:
                outcome = await self.o.executor.build_attribute(
                    self.build_record.id_,
                    job,
                    writer,
                    self.worktree_path,
                    attr_cancel,
                    on_start=mark_building,
                    on_built=on_built if self.o.uploaders else None,
                )
                if outcome == BuildOutcome.success:
                    outcome = await self._after_success(job, writer)
        finally:
            mirror.cancel()
            self.o.attr_cancel_events.pop((self.build_record.id_, job.attr), None)

        status, error = outcome_status(job, outcome)
        # Failed attributes carry a log-tail excerpt so the build page
        # answers "why" without a click into the log. ANSI stays: the
        # web layer renders it, the API strips it.
        failure = None
        if error is None and status in (
            AttributeStatus.failed,
            AttributeStatus.ignored_failure,
        ):
            failure = writer.capture.build_failure() if writer.capture else None
            error = (
                (failure.as_text() if failure else "")
                or failure_excerpt(writer.tail_lines())
                or None
            )
        result = AttributeResult(
            attr=job.attr,
            status=status,
            job=job,
            error=error,
            failure=failure,
            out_path=None if job.build_dependencies_only else job.outputs.get("out"),
            drv_path=job.drv_path,
            system=job.system,
        )
        await db.complete_attribute(
            self.o.pool,
            self.build_record.id_,
            result,
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
                    extra={"build_id": self.build_record.id_, "attr": job.attr},
                )
        return outcome
