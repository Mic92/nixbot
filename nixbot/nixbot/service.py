"""Service composition: wires every component into
one running process — database, orchestrator, forge clients, webhook
ingestion, web frontend, pollers, and background maintenance loops.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from typing import TYPE_CHECKING, Any

from . import db, discovery, restart_dispatch, schedule_runner
from .config import ScheduleWhen
from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import events as ev_q
from .db_gen import failed as failed_q
from .db_gen import maintenance as q
from .deliver import Delivery, EventListingCache, deliver
from .event_effects import restart_event_effect
from .events import (
    BuildResult,
    ChangeEvent,
    EvalReport,
    StatusReporter,
    effects_event_for_build,
    event_for_build,
)
from .forge_pr import ForgePrClient
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
from .schedules import DueEffect, ScheduledEffectsStore
from .webhooks import (
    ChangeRequest,
    CheckRerequested,
    PrClosed,
    PrComment,
    PrLabeled,
    WebhookEvent,
    is_merge_queue_branch,
    should_build_branch,
)
from .work_queue import (
    MAX_WORK_ATTEMPTS,
    TransientError,
    WorkItem,
    WorkQueue,
    work_retry_delay,
)

if TYPE_CHECKING:
    from collections.abc import Coroutine, Sequence

    import asyncpg

    from .config import Config
    from .db import BuildRecord
    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .orchestrator import Orchestrator
    from .polling import PolledRepository
    from .repos import RepoStore

logger = logging.getLogger(__name__)

_STATIC_CREDENTIALS = StaticCredentialsProvider()

# Repo metadata rarely changes. The UI refresh button covers the
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


@dataclass
class RetryingReporter:
    """Wraps the forge reporter: a failed terminal status post becomes
    a queued retry instead of a stale pending commit status."""

    inner: StatusReporter
    service: CIService

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.build_started(event, build)

    async def eval_finished(
        self, event: ChangeEvent, build: BuildRecord, report: EvalReport
    ) -> None:
        await self.inner.eval_finished(event, build, report)

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self.inner.eval_cancelled(event, build)

    async def build_restarted(
        self, event: ChangeEvent, build: BuildRecord, attr: str | None
    ) -> None:
        await self.inner.build_restarted(event, build, attr)

    async def effect_started(
        self, event: ChangeEvent, build: BuildRecord, name: str
    ) -> None:
        await self.inner.effect_started(event, build, name)

    async def effect_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        await self.inner.effect_finished(
            event, build, name, success=success, error=error
        )

    async def effects_started(
        self, event: ChangeEvent, build: BuildRecord, total: int
    ) -> None:
        await self.inner.effects_started(event, build, total)

    async def effects_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        failed: int,
        succeeded: int,
    ) -> None:
        await self.inner.effects_finished(
            event, build, failed=failed, succeeded=succeeded
        )

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        try:
            await self.inner.build_finished(event, build, result)
        except Exception as e:
            logger.exception(
                "status post failed; queueing a retry", extra={"build_id": build.id_}
            )
            await self.service.enqueue_work(
                "report",
                f"report-{build.id_}",
                {"build_id": build.id_},
                delay=work_retry_delay(
                    1,
                    getattr(e, "retry_after", None),
                    self.service.config.work_retry_backoff,
                ),
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
    # registration). The timestamp debounces the UI refresh button,
    # which any logged-in user can press.
    _discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_discovery: float = 0.0
    # Wakes the dispatcher early on local enqueues and completions.
    _work_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Process start (DB clock when constructed via bootstrap): effect
    # rows started after this are live deploys, not crash leftovers.
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    event_listings: EventListingCache = field(default_factory=EventListingCache)

    @property
    def forge_pr(self) -> ForgePrClient:
        return ForgePrClient(self.github, self.gitea, self.gitlab)

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

    async def aclose(self) -> None:
        """Cancel in-flight work and await its cleanup before exit. A
        cancelled build unwinds through the scheduler, which reaps its
        nix children without writing a terminal status, so the build
        stays resumable — shutdown behaves like a crash and recovery
        resumes it. Needs systemd KillMode=mixed so the children outlive
        the stop signal."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- change ingestion (ChangeSink for webhooks/reconciliation) -------

    async def submit(self, event: WebhookEvent) -> None:
        if isinstance(event, PrClosed):
            await self._submit_pr_closed(event)
        elif isinstance(event, PrComment):
            await self._deliver_for_pr(
                event, "comment", command=event.command, args=event.args
            )
        elif isinstance(event, PrLabeled):
            await self._deliver_for_pr(event, "pull_request")
        elif isinstance(event, CheckRerequested):
            await self._submit_rerequest(event)
        else:
            await self._submit_change(event)

    async def _submit_pr_closed(self, event: PrClosed) -> None:
        if not event.merged:
            # A merged PR is not cancelled: the merge commit has the same
            # tree, so its push reuses the PR build.
            project = await self.repo_store.by_forge_id(
                event.forge, event.forge_repo_id
            )
            if project is not None:
                self.orchestrator.canceller.cancel_pr(project.id_, event.pr_number)
            # Queued events for the PR would build it after the close.
            await q.supersede_pending_changes(
                self.pool,
                forge=event.forge,
                forge_repo_id=event.forge_repo_id,
                pr_number=event.pr_number,
            )
        await self._deliver_for_pr(event, "pull_request_closed")

    async def _deliver_for_pr(
        self,
        event: PrClosed | PrComment | PrLabeled,
        kind: str,
        *,
        command: str | None = None,
        args: str | None = None,
    ) -> None:
        """Queue a PR event against the PR's latest build. PRs nixbot
        never built (filtered, disabled) get no event effects."""
        project = await self.repo_store.by_forge_id(event.forge, event.forge_repo_id)
        if project is None or not project.enabled:
            return
        build = await ev_q.latest_build_for_pr(
            self.pool, project_id=project.id_, pr_number=event.pr_number
        )
        if build is None:
            return
        # pull_request means "this PR head built green" (docs/EFFECTS.md).
        if kind == "pull_request" and build.status != BuildStatus.SUCCEEDED:
            return
        d = Delivery(
            kind=kind,
            build_id=build.id_,
            actor=event.actor,
            pr_number=event.pr_number,
            command=command,
            args=args,
        )
        # One queue key per PR so its events are matched in order.
        await self.enqueue_work(
            "deliver", f"deliver-{project.id_}-pr-{event.pr_number}", d.as_payload()
        )

    async def _submit_rerequest(self, event: CheckRerequested) -> None:
        """GitHub "Re-run" button → existing restart paths. Per-attr
        runs restart that attr only. Summary runs and check_suite
        restart the whole build."""
        project = await self.repo_store.by_forge_id(event.forge, event.forge_repo_id)
        if project is None:
            return
        build_id = event.build_id
        if build_id is not None:
            build = await builds_q.get_build(self.pool, id_=build_id)
            # external_id is attacker-influenced (set by whichever app
            # created the run). Never restart another project's build.
            if build is None or build.project_id != project.id_:
                build_id = None
        if build_id is None:
            build_id = await failed_q.latest_build_for_sha(
                self.pool, project_id=project.id_, commit_sha=event.head_sha
            )
        if build_id is None:
            return
        if event.name is not None:
            row = await failed_q.check_run_attr(
                self.pool, project_id=project.id_, sha=event.head_sha, name=event.name
            )
            if row is not None and row.attr is not None:
                await self.restart_attribute(build_id, row.attr)
                return
        await self.restart_build(build_id)

    async def _submit_change(self, change: ChangeRequest) -> None:
        """Enqueue only. The dispatcher runs _process_change. The key
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
        info = repo_info(project)
        credentials = await self.credentials_provider(info.forge).get(info.clone_url)
        if change.pr_number is None and not (
            change.branch == project.default_branch
            or is_merge_queue_branch(change.branch)
        ):
            # `build_branches` in the default branch's nixbot.toml
            # decides which extra branches build. Without it the
            # globally configured branch globs apply.
            repo_config = await self.orchestrator.default_branch_repo_config(
                info, credentials, fetch=True
            )
            if repo_config.build_branches is None:
                if not should_build_branch(
                    self.config.branches, project.default_branch, change.branch
                ):
                    return
            elif not any(
                fnmatch(change.branch, glob) for glob in repo_config.build_branches
            ):
                return
        event = ChangeEvent(
            repo=info,
            branch=change.branch,
            commit_sha=change.commit_sha,
            pr_number=change.pr_number,
            pr_author=change.pr_author,
            base_sha=change.base_sha,
            commit_message=change.commit_message,
            actor=change.actor,
        )
        await self.orchestrator.handle_change_event(event, credentials)

    # -- ControlBackend ---------------------------------------------------

    async def project_activated(self, project_id: int) -> None:
        await discovery.activate_project(self, project_id)

    async def refresh_projects(self) -> None:
        async with self._discovery_lock:
            if time.monotonic() - self._last_discovery < REFRESH_COOLDOWN:
                return
            await self.discover_once()
            self._last_discovery = time.monotonic()

    async def restart_build(self, build_id: int) -> None:
        await self._restart(build_id, attr=None)

    async def restart_attribute(self, build_id: int, attr: str) -> None:
        # A stale attr (e.g. after a re-eval renamed it) must not
        # reset the build row and spawn an empty rerun.
        if await q.attribute_known(self.pool, build_id=build_id, attr=attr) is None:
            logger.warning(
                "restart of unknown attribute ignored",
                extra={"build_id": build_id, "attr": attr},
            )
            return
        await self._restart(build_id, attr=attr)

    async def _restart(self, build_id: int, *, attr: str | None) -> None:
        # Cancel a live run. The work item resets once it released the build.
        cancel = (
            self.orchestrator.cancel_events.get(build_id)
            if attr is None
            else self.orchestrator.attr_cancel_events.get((build_id, attr))
        )
        if cancel is not None:
            cancel.set()
        await self.enqueue_work(
            "rerun",
            f"build-{build_id}",
            {"build_id": build_id, "restart": True, "attr": attr},
        )

    async def restart_effects(
        self,
        build_id: int,
        name: str | None = None,
        kind: str = "push",
    ) -> None:
        if kind != "push" and name is not None:
            await self.enqueue_work(
                "event-restart",
                f"build-{build_id}-{kind}-{name}-restart",
                {"build_id": build_id, "kind": kind, "name": name},
            )
            return
        # Per-build dedup key: single-effect and full reruns serialize.
        await self.enqueue_work(
            "effects", f"build-{build_id}", {"build_id": build_id, "name": name}
        )

    async def run_scheduled_now(
        self, project_id: int, schedule_name: str, effect: str, when_spec: str
    ) -> None:
        """Manually trigger an onSchedule effect from the UI.

        The run row is created synchronously so the UI shows it running
        before the dispatcher claims the work. The dedup key carries
        run_id to stay distinct from a sweep-loop due item, and this
        path never touches last_run, so the regular schedule is
        unaffected."""
        due = DueEffect(
            project_id=project_id,
            schedule_name=schedule_name,
            effect=effect,
            when=ScheduleWhen.model_validate(json.loads(when_spec)),
        )
        run_id = await ScheduledEffectsStore(self.pool).start_run(due)
        await self.enqueue_work(
            "scheduled",
            f"manual-{project_id}-{schedule_name}-{effect}-{run_id}",
            {
                "project_id": project_id,
                "schedule_name": schedule_name,
                "effect": effect,
                "when": due.when.model_dump(exclude_none=True),
                "run_id": run_id,
            },
        )

    async def enqueue_work(
        self, kind: str, dedup_key: str, payload: dict[str, Any], *, delay: float = 0
    ) -> None:
        await WorkQueue(self.pool).enqueue(kind, dedup_key, payload, delay=delay)
        self._work_event.set()

    def wake_work(self) -> None:
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
        except TransientError as e:
            delay = work_retry_delay(
                item.attempts + 1, e.retry_after, self.config.work_retry_backoff
            )
            if item.attempts + 1 < MAX_WORK_ATTEMPTS and await queue.retry(
                item.id, delay=delay, error=str(e)
            ):
                logger.warning(
                    "work item failed, retrying",
                    extra={
                        "work_id": item.id,
                        "kind": item.kind,
                        "in": delay,
                        "error": str(e),
                    },
                )
            else:
                logger.exception(
                    "work item failed, giving up",
                    extra={"work_id": item.id, "kind": item.kind, "error": str(e)},
                )
                await queue.finish(item.id, error=str(e))
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
        elif item.kind == "rerun":
            await restart_dispatch.rerun(
                self,
                payload["build_id"],
                restart=payload.get("restart", False),
                attr=payload.get("attr"),
            )
        elif item.kind == "effects":
            await restart_dispatch.restart_effects(
                self, payload["build_id"], payload.get("name")
            )
        elif item.kind == "event-restart":
            await self._restart_event_effect(payload)
        elif item.kind == "effect":
            await self._run_effect_item(
                payload["build_id"], payload["name"], payload.get("kind", "push")
            )
        elif item.kind == "deliver":
            await deliver(
                self,
                Delivery(**payload),
                last_attempt=item.attempts + 1 >= MAX_WORK_ATTEMPTS,
            )
        elif item.kind == "report":
            await self._re_report(payload["build_id"])
        elif item.kind == "refresh-schedules":
            await schedule_runner.refresh_schedules(
                self, payload["project_id"], payload["rev"]
            )
        elif item.kind == "scheduled":
            await schedule_runner.run_scheduled(
                self,
                DueEffect(
                    project_id=payload["project_id"],
                    schedule_name=payload["schedule_name"],
                    effect=payload["effect"],
                    when=ScheduleWhen.model_validate(payload["when"]),
                ),
                run_id=payload.get("run_id"),
            )
        else:
            msg = f"unknown work kind {item.kind!r}"
            raise ValueError(msg)

    async def _re_report(self, build_id: int) -> None:
        """Re-post the build summary from database state."""
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        event = event_for_build(repo_info(project), build)
        rows = await builds_q.attribute_statuses(self.pool, build_id=build_id)
        reporter = self.orchestrator.reporter
        if isinstance(reporter, RetryingReporter):
            # The wrapper would enqueue a competing item on failure.
            reporter = reporter.inner
        try:
            # Per-attribute statuses were already posted (or cached) inline.
            await reporter.build_finished(
                event,
                build,
                BuildResult(
                    build.status,
                    build.status_generation,
                    [],
                    attr_statuses={row.attr: row.status for row in rows},
                ),
            )
        except Exception as e:
            raise TransientError(
                str(e), retry_after=getattr(e, "retry_after", None)
            ) from e

    async def _restart_event_effect(self, payload: dict[str, Any]) -> None:
        build = await builds_q.get_build(self.pool, id_=payload["build_id"])
        if build is None:
            return
        await restart_event_effect(
            self.orchestrator, build, payload["kind"], payload["name"]
        )
        self.wake_work()

    async def _run_effect_item(self, build_id: int, name: str, kind: str) -> None:
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        info = repo_info(project)
        credentials = await self.credentials_provider(info.forge).get(info.clone_url)
        try:
            await self.orchestrator.run_effect_item(
                info, build, name, kind, credentials
            )
        except Exception as e:
            # Setup failures (fetch/checkout) happen before the
            # runner settles the row.
            await builds_q.finish_effect(
                self.pool,
                build_id=build_id,
                kind=kind,
                name=name,
                status="failed",
                error=str(e) or type(e).__name__,
                log_size=0,
                log_truncated=False,
            )
            raise

    async def recover_unfinished_builds(self) -> None:
        """Crash recovery: settle already-built attributes, then queue
        reruns for the rest. Builds interrupted mid-eval (no attribute
        rows) re-evaluate via the rerun path."""
        settled_effects = await fail_interrupted_effects(self.pool, self._started_at)
        await self._report_interrupted_effects(settled_effects)
        for resumable in await find_unfinished_builds(self.pool):
            remaining, settled = await settle_already_built(self.pool, resumable)
            if settled:
                # Recovered results still need gcroots/outputs updates.
                event = await restart_dispatch.change_event_for(self, resumable)
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

    async def _report_interrupted_effects(
        self, settled: Sequence[q.FailInterruptedEffectsRow]
    ) -> None:
        """Post the failure of effects settled by crash recovery: the
        forge holds the pending status from effect_started, and nothing
        re-runs a settled effect to clear it."""
        by_build: dict[int, list[str]] = {}
        for row in settled:
            if row.build_id is not None:
                by_build.setdefault(row.build_id, []).append(row.name)
        for build_id, names in by_build.items():
            build = await builds_q.get_build(self.pool, id_=build_id)
            if build is None:
                continue
            project = await self.repo_store.by_id(build.project_id)
            if project is None:
                continue
            event = effects_event_for_build(repo_info(project), build)
            for name in names:
                await self.orchestrator.reporter.effect_finished(
                    event,
                    build,
                    name,
                    success=False,
                    error="interrupted by a service restart",
                )
            await self.orchestrator.post_effects_summary(event, build)

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
        # No running pipeline re-aggregates for us. Without this the
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
        running pipeline. Otherwise the commit status stays pending
        forever."""
        build = await builds_q.get_build(self.orchestrator.pool, id_=build_id)
        if build is None:
            return
        project = await self.repo_store.by_id(build.project_id)
        if project is None:
            return
        change = event_for_build(repo_info(project), build)
        await self.orchestrator.reporter.build_finished(
            change, build, BuildResult(status, generation, [])
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
                    # discovery). Retried until one pass succeeds so a
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
        await schedule_runner.scheduled_effects_loop(self)
