"""Service composition: wires every component into
one running process — database, orchestrator, forge clients, webhook
ingestion, web frontend, pollers, and background maintenance loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import db, discovery, restarts, scheduled_runs
from .config import ScheduleWhen
from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import failed as failed_q
from .db_gen import maintenance as q
from .events import ChangeEvent, StatusReporter
from .gitrepo import (
    CredentialsProvider,
    FetchCredentials,
    StaticCredentialsProvider,
)
from .recovery import (
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    fail_interrupted_effects,
    find_unfinished_builds,
    settle_already_built,
)
from .repos import repo_info
from .scheduled import DueEffect
from .webhooks import (
    ChangeRequest,
    CheckRerequested,
    PrClosed,
    WebhookEvent,
    should_build_branch,
)
from .work_queue import WorkItem, WorkQueue

if TYPE_CHECKING:
    from collections.abc import Coroutine

    import asyncpg

    from .config import Config
    from .db import BuildRecord
    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .orchestrator import Orchestrator
    from .polling import PolledRepository
    from .repos import RepoStore
    from .scheduler import AttributeResult

logger = logging.getLogger(__name__)

_STATIC_CREDENTIALS = StaticCredentialsProvider()

# Repo metadata rarely changes; the UI refresh button covers the
# "I just created a repo" case without waiting for the next tick.
DISCOVERY_INTERVAL = 60 * 60
REFRESH_COOLDOWN = 60
MAINTENANCE_INTERVAL = 60 * 60


class PullBasedCredentialsProvider:
    """Per-repo SSH credentials for pull-based repositories."""

    def __init__(self, repos: list[PolledRepository]) -> None:
        self._by_url = {repo.url: repo for repo in repos}

    async def get(self, repo_url: str) -> FetchCredentials:
        repo = self._by_url.get(repo_url)
        if repo is None:
            return FetchCredentials()
        return FetchCredentials(
            ssh_private_key_file=repo.ssh_private_key_file,
            ssh_known_hosts_file=repo.ssh_known_hosts_file,
        )


MAX_REPORT_ATTEMPTS = 5
REPORT_BACKOFF_SECONDS = 30
MAX_REPORT_DELAY_SECONDS = 3600


def _report_payload(build_id: int, attempt: int, error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"build_id": build_id, "attempt": attempt}
    # Forges send Retry-After on rate limits; honoring it is required
    # (e.g. GitHub secondary limits escalate when ignored).
    retry_after = getattr(error, "retry_after", None)
    if retry_after is not None:
        payload["retry_at"] = time.time() + min(retry_after, MAX_REPORT_DELAY_SECONDS)
    return payload


def _report_delay(attempt: int, retry_at: float | None) -> float:
    backoff = min(REPORT_BACKOFF_SECONDS * (attempt - 1), 300)
    hinted = max(0.0, retry_at - time.time()) if retry_at is not None else 0.0
    return min(max(backoff, hinted), MAX_REPORT_DELAY_SECONDS)


@dataclass
class RetryingReporter:
    """Wraps the forge reporter: a failed terminal status post becomes
    a queued retry instead of a stale pending commit status."""

    inner: StatusReporter
    service: CIService

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.build_started(event, build)

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
    ) -> None:
        await self.inner.eval_finished(event, build, success=success, warnings=warnings)

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.eval_cancelled(event, build)

    async def build_finished(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        generation: int,
        results: list[AttributeResult],
        *,
        attr_statuses: dict[str, str] | None = None,
        attr_prefix: str = "checks",
    ) -> None:
        try:
            await self.inner.build_finished(
                event,
                build,
                status,
                generation,
                results,
                attr_statuses=attr_statuses,
                attr_prefix=attr_prefix,
            )
        except Exception as e:
            logger.exception(
                "status post failed; queueing a retry", extra={"build_id": build.id}
            )
            await self.service.enqueue_work(
                "report", f"report-{build.id}", _report_payload(build.id, 1, e)
            )


@dataclass
class CIService:
    config: Config
    pool: asyncpg.Pool
    orchestrator: Orchestrator
    repo_store: RepoStore
    github: GitHubAppClient | None = None
    gitea: GiteaClient | None = None
    gitlab: GitlabClient | None = None
    credentials_providers: dict[str, CredentialsProvider] = field(default_factory=dict)
    # Strong references to fire-and-forget tasks: the event loop only
    # keeps weak references, so an unreferenced running build could be
    # garbage-collected mid-flight.
    _tasks: set[asyncio.Task] = field(default_factory=set)
    # Discovery must not run concurrently (upserts, webhook
    # registration); the timestamp debounces the UI refresh button,
    # which any logged-in user can press.
    _discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_discovery: float = 0.0
    # Wakes the dispatcher early on local enqueues and completions.
    _work_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Process start (DB clock when constructed via bootstrap): effect
    # rows started after this are live deploys, not crash leftovers.
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def credentials_provider(self, forge: str) -> CredentialsProvider:
        return self.credentials_providers.get(forge, _STATIC_CREDENTIALS)

    def _spawn(self, coro: Coroutine[None, None, object]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("background task failed", exc_info=task.exception())

    # -- change ingestion (ChangeSink for webhooks/reconciliation) -------

    async def submit(self, event: WebhookEvent) -> None:
        if isinstance(event, PrClosed):
            project = await self.repo_store.by_forge_id(
                event.forge, event.forge_repo_id
            )
            if project is not None:
                self.orchestrator.canceller.cancel_pr(project.id, event.pr_number)
            # Queued events for the PR would build it after the close.
            await q.supersede_pending_changes(
                self.pool,
                forge=event.forge,
                forge_repo_id=event.forge_repo_id,
                pr_number=event.pr_number,
            )
            return
        if isinstance(event, CheckRerequested):
            await self._submit_rerequest(event)
            return
        await self._submit_change(event)

    async def _submit_rerequest(self, event: CheckRerequested) -> None:
        """GitHub "Re-run" button → existing restart paths. Per-attr
        runs restart that attr only; summary runs and check_suite
        restart the whole build."""
        project = await self.repo_store.by_forge_id(event.forge, event.forge_repo_id)
        if project is None:
            return
        build_id = event.build_id
        if build_id is not None:
            build = await builds_q.get_build(self.pool, id_=build_id)
            # external_id is attacker-influenced (set by whichever app
            # created the run); never restart another project's build.
            if build is None or build.project_id != project.id:
                build_id = None
        if build_id is None:
            build_id = await failed_q.latest_build_for_sha(
                self.pool, project_id=project.id, commit_sha=event.head_sha
            )
        if build_id is None:
            return
        if event.name is not None:
            row = await failed_q.check_run_attr(
                self.pool, project_id=project.id, sha=event.head_sha, name=event.name
            )
            if row is not None and row.attr is not None:
                await self.restart_attribute(build_id, row.attr)
                return
        await self.restart_build(build_id)

    async def _submit_change(self, change: ChangeRequest) -> None:
        """Enqueue only; the dispatcher runs _process_change. The key
        serializes deliveries of one commit, not of one branch:
        supersede needs newer commits to run concurrently."""
        await self.enqueue_work(
            "change",
            f"change-{change.forge}-{change.forge_repo_id}-{change.commit_sha}",
            dataclasses.asdict(change),
        )

    async def _process_change(self, change: ChangeRequest) -> None:
        project = await self.repo_store.by_forge_id(change.forge, change.forge_repo_id)
        if project is None or not project.enabled:
            return
        if change.pr_number is None and not should_build_branch(
            self.config.branches, project.default_branch, change.branch
        ):
            return
        info = repo_info(project)
        credentials = await self.credentials_provider(info.forge).get(info.clone_url)
        event = ChangeEvent(
            repo=info,
            branch=change.branch,
            commit_sha=change.commit_sha,
            pr_number=change.pr_number,
            pr_author=change.pr_author,
            base_sha=change.base_sha,
            commit_message=change.commit_message,
        )
        await self.orchestrator.handle_change_event(event, credentials)

    # -- ControlBackend ---------------------------------------------------

    async def refresh_projects(self) -> None:
        async with self._discovery_lock:
            if time.monotonic() - self._last_discovery < REFRESH_COOLDOWN:
                return
            await self.discover_once()
            self._last_discovery = time.monotonic()

    async def restart_build(self, build_id: int) -> None:
        await self.enqueue_work("restart", f"build-{build_id}", {"build_id": build_id})

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        await self.enqueue_work(
            "restart", f"build-{build_id}", {"build_id": build_id, "attr": attr}
        )

    async def restart_effects(self, build_id: int) -> None:
        await self.enqueue_work("effects", f"build-{build_id}", {"build_id": build_id})

    async def enqueue_work(
        self, kind: str, dedup_key: str, payload: dict[str, Any]
    ) -> None:
        await WorkQueue(self.pool).enqueue(kind, dedup_key, payload)
        self._work_event.set()

    async def work_loop(self) -> None:
        """Single dispatcher: claims queued intent and executes it."""
        queue = WorkQueue(self.pool)
        while True:
            try:
                item = await queue.claim_next()
            except Exception:
                logger.exception("work claim failed")
                item = None
            if item is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._work_event.wait(), timeout=5)
                self._work_event.clear()
                continue
            self._spawn(self._execute_work(queue, item))

    async def drain_work(self) -> None:
        """Execute claimable work to completion (tests)."""
        queue = WorkQueue(self.pool)
        while (item := await queue.claim_next()) is not None:
            await self._execute_work(queue, item)

    async def _execute_work(self, queue: WorkQueue, item: WorkItem) -> None:
        try:
            await self._dispatch_work(item)
        except Exception as e:
            logger.exception("work item failed", extra={"work_id": item.id})
            await queue.finish(item.id, error=str(e) or type(e).__name__)
        else:
            await queue.finish(item.id)
        finally:
            # A deferred same-key item may be claimable now.
            self._work_event.set()

    async def _dispatch_work(self, item: WorkItem) -> None:
        payload = item.payload
        if item.kind == "change":
            await self._process_change(ChangeRequest(**payload))
        elif item.kind == "restart":
            retry = await restarts.restart(
                self, payload["build_id"], payload.get("attr")
            )
            if retry:
                # Still running (e.g. restart right after a cancel,
                # while the old run unwinds): retry, don't drop.
                await self.enqueue_work("restart", item.dedup_key, payload)
        elif item.kind == "rerun":
            # Crash recovery: resume pending attributes, no reset.
            await self._rerun(payload["build_id"])
        elif item.kind == "effects":
            await restarts.restart_effects(self, payload["build_id"])
        elif item.kind == "effect":
            await self._run_effect_item(payload["build_id"], payload["name"])
        elif item.kind == "report":
            await self._re_report(
                payload["build_id"],
                payload.get("attempt", 1),
                payload.get("retry_at"),
            )
        elif item.kind == "refresh-schedules":
            await scheduled_runs.refresh_schedules(
                self, payload["project_id"], payload["rev"]
            )
        elif item.kind == "scheduled":
            await scheduled_runs.run_scheduled(
                self,
                DueEffect(
                    project_id=payload["project_id"],
                    schedule_name=payload["schedule_name"],
                    effect=payload["effect"],
                    when=ScheduleWhen.model_validate(payload["when"]),
                ),
            )
        else:
            msg = f"unknown work kind {item.kind!r}"
            raise ValueError(msg)

    async def _re_report(
        self, build_id: int, attempt: int, retry_at: float | None = None
    ) -> None:
        """Re-post the build summary from database state. Waits for the
        larger of the attempt backoff and the forge's Retry-After."""
        delay = _report_delay(attempt, retry_at)
        if delay > 0:
            await asyncio.sleep(delay)
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        event = ChangeEvent(
            repo=repo_info(project),
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        rows = await builds_q.attribute_statuses(self.pool, build_id=build_id)
        reporter = self.orchestrator.reporter
        if isinstance(reporter, RetryingReporter):
            # Post via the inner reporter: the wrapper would enqueue a
            # competing attempt-1 item on failure.
            reporter = reporter.inner
        try:
            # Empty results: only the summary is re-posted; per-attribute
            # statuses were already posted (or cached) inline.
            await reporter.build_finished(
                event,
                build,
                build.status,
                build.status_generation,
                [],
                attr_statuses={row.attr: row.status for row in rows},
            )
        except Exception as e:
            if attempt < MAX_REPORT_ATTEMPTS:
                await self.enqueue_work(
                    "report",
                    f"report-{build_id}",
                    _report_payload(build_id, attempt + 1, e),
                )
            raise

    async def _run_effect_item(self, build_id: int, name: str) -> None:
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        info = repo_info(project)
        credentials = await self.credentials_provider(info.forge).get(info.clone_url)
        try:
            await self.orchestrator.run_effect_item(info, build, name, credentials)
        except Exception as e:
            # Setup failures (fetch/checkout) happen before the
            # runner settles the row.
            await builds_q.finish_effect(
                self.pool,
                build_id=build_id,
                name=name,
                status="failed",
                error=str(e) or type(e).__name__,
                log_path=None,
                log_size=0,
                log_truncated=False,
            )
            raise

    async def _rerun(self, build_id: int) -> None:
        await restarts.rerun(self, build_id)

    async def recover_unfinished_builds(self) -> None:
        """Crash recovery: settle already-built attributes, then queue
        reruns for the rest. Builds interrupted mid-eval (no attribute
        rows) re-evaluate via the rerun path."""
        await fail_interrupted_effects(self.pool, self._started_at)
        for resumable in await find_unfinished_builds(self.pool):
            remaining, settled = await settle_already_built(self.pool, resumable)
            if settled:
                # Recovered results still need gcroots/outputs updates.
                event = await restarts.change_event_for(self, resumable)
                if event is not None:
                    await self.orchestrator.post_process_skipped(event, settled)
            logger.info(
                "recovering build",
                extra={"build_id": resumable.build_id, "remaining": len(remaining)},
            )
            await self.enqueue_work(
                "rerun",
                f"build-{resumable.build_id}",
                {"build_id": resumable.build_id},
            )

    async def cancel_attribute(self, build_id: int, attr: str) -> None:
        event = self.orchestrator.attr_cancel_events.get((build_id, attr))
        if event is not None:
            event.set()
            return
        # Not queued or running (e.g. leftover from an interrupted
        # build): mark it cancelled directly.
        cancelled = await q.cancel_attribute(self.pool, build_id=build_id, attr=attr)
        if cancelled != 1:
            return
        # No running pipeline re-aggregates for us; without this the
        # build stays non-terminal forever once all rows are settled.
        status, generation = await db.aggregate_build(self.pool, build_id)
        if status in BuildStatus.TERMINAL:
            await self._report_direct_finish(build_id, status, generation)

    async def cancel_build(self, build_id: int) -> None:
        event = self.orchestrator.cancel_events.get(build_id)
        if event is not None:
            event.set()
            return
        # Not running: mark cancelled directly.
        generation = await q.cancel_build(self.pool, id_=build_id)
        if generation is None:
            return
        # CancelBuild also settled leftover pending/building attribute
        # rows in the same statement.
        await self._report_direct_finish(build_id, BuildStatus.CANCELLED, generation)

    async def _report_direct_finish(
        self, build_id: int, status: str, generation: int
    ) -> None:
        """Post the terminal forge status for a build settled outside a
        running pipeline; otherwise the commit status stays pending
        forever."""
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        change = ChangeEvent(
            repo=repo_info(project),
            branch=build.branch,
            commit_sha=build.commit_sha,
            pr_number=build.pr_number,
        )
        await self.orchestrator.reporter.build_finished(
            change, build, status, generation, []
        )

    # -- background loops ---------------------------------------------------

    async def discovery_loop(self) -> None:
        reconciled = False
        while True:
            try:
                async with self._discovery_lock:
                    await self.discover_once()
                    self._last_discovery = time.monotonic()
                if not reconciled:
                    # Startup reconciliation needs discovery first
                    # (GitHub installation tokens are learned during
                    # discovery); retried until one pass succeeds so a
                    # forge outage at startup does not skip it.
                    await self.reconcile_once()
                    reconciled = True
            except Exception:
                logger.exception("project discovery failed")
            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def reconcile_once(self) -> None:
        await discovery.reconcile_once(self)

    async def discover_once(self) -> None:
        await discovery.discover_once(self)

    async def _register_hooks(self) -> None:
        await discovery.register_hooks(self)

    async def maintenance_loop(self) -> None:
        while True:
            try:
                await cleanup_old_builds(
                    self.pool, self.config.state_dir, self.config.retention_days
                )
                await cleanup_orphan_log_dirs(self.pool, self.config.state_dir)
                await WorkQueue(self.pool).cleanup(self.config.retention_days)
                await self.orchestrator.repos.cleanup()
                await self.orchestrator.repos.gc()
            except Exception:
                logger.exception("maintenance run failed")
            await asyncio.sleep(MAINTENANCE_INTERVAL)

    async def scheduled_effects_loop(self) -> None:
        await scheduled_runs.scheduled_effects_loop(self)
