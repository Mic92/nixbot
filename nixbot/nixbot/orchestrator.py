"""Build orchestrator: change event → build record → eval → attribute
builds → aggregate result.

Wires together the repo manager (clone/worktree + PR merge), the eval
runner, the dependency-aware scheduler, the build executor, gcroots/
outputs updates, post-build steps, and effects. Status reporting is a
callback so forge integration (4.4) and the web frontend can subscribe
without coupling.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from . import (
    build_reuse,
    build_run,
    db,
    effects_run,
    gcroots,
    outputs,
    recovery,
    rerun_exec,
)
from .canceller import (
    CancellationManager,
    RegisterOutcome,
    branch_key,
    has_skip_ci_marker,
)
from .db import BuildStatus
from .effects_state import TaskTokens
from .events import (
    BuildResult,
    ChangeEvent,
    NullStatusReporter,
    RepoInfo,
    StatusReporter,
)
from .executor import LogWriter
from .gitrepo import MergeConflictError, pr_refspec
from .web.logs import LogRegistry
from .work_queue import WorkQueue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from contextlib import AbstractAsyncContextManager
    from pathlib import Path

    import asyncpg

    from .build_scheduler import BuildOutcome, FailedBuildCache
    from .config import Config
    from .db import BuildRecord
    from .gitrepo import FetchCredentials, RepoManager
    from .models import NixEvalJobSuccess
    from .nix_eval import (
        EvalResult,
        EvalSettings,
        JobBatchCallback,
        StderrLineCallback,
    )
    from .repo_config import BranchConfig

    GcrootRegistrar = Callable[[Path, str, str, str], Awaitable[None]]
    OutputWriter = Callable[[Path, str, str, str, str, str, str], Path]

logger = logging.getLogger(__name__)


class EvalRunnerLike(Protocol):
    """What the orchestrator needs from nix_eval.EvalRunner."""

    async def run(
        self,
        worktree_path: Path,
        branch_config: BranchConfig,
        settings: EvalSettings,
        on_jobs: JobBatchCallback | None = None,
        on_stderr_line: StderrLineCallback | None = None,
    ) -> EvalResult: ...


class AttributeExecutor(Protocol):
    """What the orchestrator needs from executor.NixBuildExecutor."""

    async def build_attribute(
        self,
        build_key: object,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event | None = None,
    ) -> BuildOutcome: ...


@dataclass
class Orchestrator:
    config: Config
    pool: asyncpg.Pool
    repos: RepoManager
    eval_runner: EvalRunnerLike
    executor: AttributeExecutor
    reporter: StatusReporter = field(default_factory=NullStatusReporter)
    # Project id -> cache; scoped so one project's failures cannot
    # affect another's builds.
    failed_build_cache: Callable[[int], FailedBuildCache] | None = None
    # build id -> cancel event, set by the cancellation manager.
    cancel_events: dict[int, asyncio.Event] = field(default_factory=dict)
    # (build id, attr) -> cancel event for a single queued/running
    # attribute; registered for the lifetime of the executor job.
    attr_cancel_events: dict[tuple[int, str], asyncio.Event] = field(
        default_factory=dict
    )
    # Injectable for tests; defaults to the real implementations.
    register_gcroot: GcrootRegistrar = gcroots.register_gcroot
    write_output_path: OutputWriter = outputs.write_output_path
    # Store-path validity probe for eval reuse; injectable for tests.
    check_store_paths: Callable[[list[str]], Awaitable[set[str]]] = (
        recovery.check_store_paths
    )
    canceller: CancellationManager = field(default_factory=CancellationManager)
    # Live log fan-out for the web frontend's SSE endpoints.
    log_registry: LogRegistry = field(default_factory=LogRegistry)
    # Per-run bearer tokens for the hercules state API.
    task_tokens: TaskTokens = field(default_factory=TaskTokens)
    # Second contexts attached to an in-flight build, for status fan-out.
    linked_events: dict[int, list[ChangeEvent]] = field(default_factory=dict)

    def _log_dir(self, build_id: int) -> Path:
        return self.config.state_dir / "logs" / str(build_id)

    def gcroots_dir(self, build: BuildRecord) -> Path:
        return self.config.state_dir / "eval-gcroots" / str(build.id)

    async def handle_change_event(
        self, event: ChangeEvent, credentials: FetchCredentials | None = None
    ) -> BuildRecord | None:
        """Full lifecycle for one change event. Returns the build record
        (None when the checkout failed before a build existed)."""
        repo = event.repo

        if has_skip_ci_marker(event.commit_message):
            logger.info(
                "skipping build due to [skip ci] marker",
                extra={"repo": repo.name, "commit": event.commit_sha},
            )
            return None

        # Fetch and create the per-build worktree; PR head is merged
        # into the base branch locally. PR refs are fetched per PR:
        # fetching all of them is unbounded on PR-heavy repos.
        refspecs = ["+refs/heads/*:refs/heads/*"]
        if event.pr_number is not None:
            refspecs.append(pr_refspec(repo.forge, event.pr_number))
        await self.repos.fetch(repo.key, repo.clone_url, refspecs, credentials)
        try:
            # Unique token: concurrent events for the same commit must
            # not share (and destroy) one checkout.
            worktree = await self.repos.checkout_for_build(
                repo.key,
                f"{repo.id}-{event.commit_sha[:12]}-{uuid.uuid4().hex[:8]}",
                base_commit=event.base_sha or event.commit_sha,
                head_commit=event.commit_sha if event.base_sha else None,
            )
        except MergeConflictError as e:
            # Merge conflict: failed build, status on the head SHA.
            build = await db.create_failed_build(
                self.pool,
                repo.id,
                event.commit_sha,
                event.branch,
                str(e),
                pr_number=event.pr_number,
                pr_author=event.pr_author,
            )
            await self.reporter.eval_finished(event, build, success=False, warnings=[])
            await self.reporter.build_finished(
                event, build, BuildResult(BuildStatus.FAILED, 0, [])
            )
            return build

        try:
            tree_hash = await worktree.tree_hash()
            build, created = await db.get_or_create_build(
                self.pool,
                repo.id,
                tree_hash,
                event.commit_sha,
                event.branch,
                pr_number=event.pr_number,
                pr_author=event.pr_author,
            )
            await self._dispatch_build(
                event,
                build,
                created=created,
                tree_hash=tree_hash,
                worktree_path=worktree.path,
                credentials=credentials,
            )
            return build
        finally:
            await self.repos.remove_worktree(worktree)

    async def _dispatch_build(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        created: bool,
        tree_hash: str,
        worktree_path: Path,
        credentials: FetchCredentials | None,
    ) -> None:
        """Decide what this event means for the (possibly shared) build:
        reuse a terminal result, drop a stale event, attach to an
        in-flight build, or run it."""
        repo = event.repo
        key = branch_key(event.branch, event.pr_number)
        # A branch push reusing a PR build already shed the PR identity
        # (and took over the branch field) in get_or_create_build.
        # Out-of-order delivery check: an event whose commit is an
        # ancestor of the context's running build is stale. Checked
        # before the reuse branch too: a stale redelivery matching an
        # old terminal build must not supersede the in-flight build.
        incoming_stale = False
        running_commit = self.canceller.running_commit_for(repo.id, key)
        if running_commit is not None and running_commit != event.commit_sha:
            incoming_stale = await build_reuse.is_ancestor(
                self, repo.key, event.commit_sha, running_commit
            )

        if not created and build.status in (
            BuildStatus.SUCCEEDED,
            BuildStatus.FAILED,
        ):
            await build_reuse.reuse_terminal_build(
                self,
                event,
                build,
                key,
                tree_hash,
                worktree_path=worktree_path,
                credentials=credentials,
                incoming_stale=incoming_stale,
            )
            return

        in_flight = build.id in self.cancel_events
        rebuild = created or (not in_flight and build.status == BuildStatus.CANCELLED)
        if rebuild or in_flight:
            cancel_event = self.cancel_events.setdefault(build.id, asyncio.Event())
        else:
            # Not running here (e.g. crashed build awaiting recovery):
            # a stored entry would block its rerun.
            cancel_event = asyncio.Event()
        outcome = self.canceller.register(
            repo.id,
            key,
            build.id,
            tree_hash,
            event.commit_sha,
            cancel_event,
            incoming_is_ancestor_of_running=incoming_stale,
        )
        if outcome == RegisterOutcome.STALE:
            if rebuild:
                self.cancel_events.pop(build.id, None)
            if created:
                await db.set_build_status(self.pool, build.id, BuildStatus.CANCELLED)
                await self.reporter.build_finished(
                    event,
                    build,
                    BuildResult(BuildStatus.CANCELLED, build.status_generation, []),
                )
            return
        if not rebuild:
            await build_reuse.attach_linked_event(self, event, build)
            return
        try:
            await self.run_build(event, build, worktree_path, credentials)
        finally:
            self.canceller.complete(build.id)
            self.cancel_events.pop(build.id, None)
            self.linked_events.pop(build.id, None)

    async def run_build(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Evaluate and build; every attribute completion is one
        transactional DB write, then the result is re-aggregated."""
        await build_run.run_build(self, event, build, worktree_path, credentials)

    async def refresh_schedules(self, event: ChangeEvent) -> None:
        """Queue `onSchedule` re-discovery after a successful
        default-branch build; the service's scheduled-effects loop only
        sweeps what the executor stores."""
        if event.pr_number is not None or event.branch != event.repo.default_branch:
            return
        await WorkQueue(self.pool).enqueue(
            "refresh-schedules",
            f"schedules-{event.repo.id}",
            {"project_id": event.repo.id, "rev": event.commit_sha},
        )

    def rerun_worktree(
        self,
        info: RepoInfo,
        build: BuildRecord,
        prefix: str,
        credentials: FetchCredentials | None,
    ) -> AbstractAsyncContextManager[tuple[ChangeEvent, Path]]:
        """Event reconstruction plus a fresh worktree at the recorded
        commit; shared by the rerun paths."""
        return rerun_exec.rerun_worktree(self, info, build, prefix, credentials)

    async def rerun_pending_attributes(
        self,
        info: RepoInfo,
        build: BuildRecord,
        pending_jobs: list[NixEvalJobSuccess],
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Re-run only the pending attributes of an existing build using
        the stored eval results — no re-evaluation (attribute restarts
        and crash recovery)."""
        await rerun_exec.rerun_pending_attributes(
            self, info, build, pending_jobs, credentials
        )

    async def rerun_effects(
        self,
        info: RepoInfo,
        build: BuildRecord,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Effects-only restart: fresh worktree at the recorded commit,
        attributes untouched."""
        await rerun_exec.rerun_effects(self, info, build, credentials)

    async def maybe_run_effects(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        worktree_path: Path,
        credentials: FetchCredentials | None = None,
    ) -> None:
        await effects_run.maybe_run_effects(
            self, event, build, worktree_path, credentials
        )

    async def run_effect_item(
        self,
        info: RepoInfo,
        build: BuildRecord,
        name: str,
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Dispatcher entry for one queued effect."""
        await effects_run.run_effect_item(self, info, build, name, credentials)

    @asynccontextmanager
    async def open_log(
        self, build_id: int, key: str, filename: str
    ) -> AsyncIterator[LogWriter]:
        """LogWriter registered for live streaming; closed and
        unregistered on exit. Shared by attribute and effect runs."""
        log_path = self._log_dir(build_id) / filename
        writer = LogWriter(path=log_path, size_limit=self.config.log_size_limit)
        self.log_registry.register(build_id, key, writer)
        try:
            yield writer
        finally:
            await writer.close()
            self.log_registry.unregister(build_id, key)

    async def finish_linked(
        self,
        build: BuildRecord,
        result: BuildResult,
        *,
        eval_success: bool | None = None,
    ) -> None:
        """Final status fan-out for second contexts attached to this
        build; eval_success is None when no eval result exists."""
        await build_reuse.finish_linked(self, build, result, eval_success=eval_success)

    async def post_process_skipped(
        self, event: ChangeEvent, skipped: list[tuple[str, str]]
    ) -> None:
        """Gcroots/outputs updates for built or skipped-as-local
        attributes (push events only)."""
        await build_reuse.post_process_skipped(self, event, skipped)
