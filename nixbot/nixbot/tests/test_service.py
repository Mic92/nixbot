"""Service composition regression tests (service.py)."""

# ruff: noqa: PLR2004, ARG001, ARG002 (stub callbacks ignore arguments)

from __future__ import annotations

import asyncio
import contextlib
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from nixbot.bootstrap import _startup, build_service, run_service
from nixbot.config import (
    PullBasedConfig,
    PullBasedRepository,
    resolve_credential_path,
)
from nixbot.events import BuildResult, NullStatusReporter
from nixbot.forge import DiscoveredRepo
from nixbot.schedule_runner import scheduled_worktree_id
from nixbot.schedules import DueEffect, ScheduleWhen
from nixbot.status import CheckRunStore
from nixbot.webhooks import ChangeRequest, CheckRerequested, PrClosed
from nixbot.work_queue import WorkQueue

from .support import FakeGitlab, git, insert_build, insert_project, make_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from fastapi import FastAPI

    from nixbot.service import CIService

pytestmark = pytest.mark.usefixtures("fresh_work_queue")


@pytest.fixture
def git_repo(upstream: Path) -> tuple[Path, str]:
    return upstream, git(upstream, "rev-parse", "HEAD")


type ServiceFactory = Callable[..., Awaitable[tuple[CIService, FastAPI]]]


@pytest.fixture
async def make_service(
    postgres_dsn: str, tmp_path: Path
) -> AsyncIterator[ServiceFactory]:
    services: list[CIService] = []

    async def make(**kwargs: Any) -> tuple[CIService, FastAPI]:
        service, app = await build_service(
            make_config(postgres_dsn, tmp_path / "state", **kwargs)
        )
        services.append(service)
        return service, app

    yield make
    for service in services:
        await service.pool.close()


@pytest.fixture
async def service(make_service: ServiceFactory) -> CIService:
    service, _app = await make_service()
    return service


# --- pure helpers ------------------------------------------------------


def test_scheduled_worktree_id_distinct_per_effect() -> None:
    when = ScheduleWhen()
    a = DueEffect(project_id=1, schedule_name="s", effect="deploy", when=when)
    b = DueEffect(project_id=1, schedule_name="s", effect="notify", when=when)
    assert scheduled_worktree_id(a, 1) != scheduled_worktree_id(b, 1)


def test_scheduled_worktree_id_sanitizes_traversal() -> None:
    due = DueEffect(
        project_id=1,
        schedule_name="../../../etc",
        effect="x/../../y",
        when=ScheduleWhen(),
    )
    wid = scheduled_worktree_id(due, 1)
    assert "/" not in wid
    assert ".." not in wid


def test_resolve_credential_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert resolve_credential_path(Path("name")) == Path("name")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/x")
    assert resolve_credential_path(Path("name")) == Path("/run/credentials/x/name")
    assert resolve_credential_path(Path("/abs/key")) == Path("/abs/key")
    assert resolve_credential_path(None) is None


# --- composition -------------------------------------------------------


async def test_build_service_accepts_asyncpg_dsn(
    postgres_dsn: str, tmp_path: Path
) -> None:
    """SQLAlchemy-style URLs must be normalized before apply_migrations
    too, not only for the pool."""

    dsn = postgres_dsn.replace("postgresql://", "postgresql+asyncpg://")
    service, _app = await build_service(make_config(dsn, tmp_path / "state"))
    try:
        assert await service.pool.fetchval("SELECT 1") == 1
    finally:
        await service.pool.close()


async def test_visibility_fetcher_and_cache_ttl_wired(
    make_service: ServiceFactory,
) -> None:
    _service, app = await make_service(repo_acl_cache_ttl=123)
    visibility = app.state.web_context.visibility
    assert visibility.fetcher is not None
    assert visibility.cache.ttl == 123


# --- restart semantics --------------------------------------------------


async def seed_project(pool: Any, url: str) -> int:
    return await insert_project(
        pool, forge_repo_id=f"svc-{time.monotonic_ns()}", url=url
    )


async def test_aclose_cancels_in_flight_tasks(service: CIService) -> None:
    """Shutdown must cancel spawned build tasks (and await their
    cleanup), not orphan them, so an interrupted build unwinds and
    leaves itself resumable instead of being killed mid-write."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def long_running() -> None:
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = service._spawn(long_running())  # noqa: SLF001
    await started.wait()
    await service.aclose()
    assert task.cancelled()
    assert cancelled.is_set()
    assert not service._tasks  # noqa: SLF001


async def test_pr_close_discards_queued_changes(service: CIService) -> None:
    """A queued change event for a closed PR must not build it later."""

    pool = service.pool
    project_id = await seed_project(pool, "http://x")
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    await service.submit(
        ChangeRequest(
            forge="github",
            forge_repo_id=forge_repo_id,
            branch="refs/pull/12/head",
            commit_sha="abc",
            pr_number=12,
        )
    )
    await service.submit(
        PrClosed(forge="github", forge_repo_id=forge_repo_id, pr_number=12)
    )
    handled: list[Any] = []

    async def fake_handle(event: Any, credentials: Any = None) -> None:
        handled.append((event, credentials))

    service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
    await service.drain_work()
    assert handled == []
    status = await pool.fetchval("SELECT status FROM work_queue WHERE kind = 'change'")
    assert status == "done"


async def test_restart_gitlab_mr_build_fetches_mr_refs(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """The re-eval restart path must fetch GitLab MR heads via
    refs/merge-requests/*, not refs/pull/*."""
    repo, _sha = git_repo

    pool = service.pool
    try:
        project_id = await insert_project(
            pool,
            forge="gitlab",
            forge_repo_id=f"svc-{time.monotonic_ns()}",
            url=f"file://{repo}",
        )
        # file:// forces the full transfer protocol; clone before
        # the MR ref exists so only the fetch refspec can bring it
        # in (see test_orchestrator.make_gitlab_mr_env).
        await service.orchestrator.repos.fetch(
            "gitlab/acme/widget", f"file://{repo}", ["+refs/heads/*:refs/heads/*"]
        )
        git(repo, "checkout", "-b", "mrsrc")
        (repo / "mr").write_text("x")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "mr")
        mr_sha = git(repo, "rev-parse", "HEAD")
        git(repo, "update-ref", "refs/merge-requests/9/head", mr_sha)
        git(repo, "checkout", "main")
        git(repo, "branch", "-D", "mrsrc")
        build_id = await insert_build(
            pool,
            project_id,
            commit_sha=mr_sha,
            status="failed",
            pr_number=9,
            error="eval boom",
        )

        reevals: list[int] = []

        async def fake_run_build(
            event: Any,
            build: Any,
            worktree_path: Path,
            credentials: Any = None,
        ) -> None:
            reevals.append(build.id)

        service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
        await service.restart_build(build_id)
        await service.drain_work()
        await asyncio.gather(*service._tasks)  # noqa: SLF001
        assert reevals == [build_id]
    finally:
        # Module-shared database: an enabled gitlab project would
        # leak into the discovery/hook-registration tests.
        await pool.execute("DELETE FROM projects WHERE id = $1", project_id)


async def test_restart_eval_failed_build_reevaluates(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """A build that failed before eval produced attributes has nothing
    to resume; restarting it must re-evaluate instead of aggregating an
    empty attribute set to 'succeeded'."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="failed", error="eval boom"
    )

    reevals: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        assert await asyncio.to_thread(worktree_path.exists)
        reevals.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
    # A restart only queues; the rerun sets the real status.
    status = await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
    assert status == "pending"
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    assert reevals == [build_id]
    status = await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
    assert status != "succeeded"


async def test_restart_clears_stale_error_and_warnings(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """A successful restart must not keep showing the old failure
    banner: builds.error / eval_warnings are cleared on the claim."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="failed", error="eval boom"
    )
    await pool.execute(
        "UPDATE builds SET eval_warnings = '[\"w\"]'::jsonb WHERE id = $1",
        build_id,
    )

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        pass

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    row = await pool.fetchrow(
        "SELECT error, eval_warnings FROM builds WHERE id = $1", build_id
    )
    assert row["error"] is None
    assert row["eval_warnings"] is None


async def test_restart_unknown_attribute_is_a_noop(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """Restarting a nonexistent attribute must not reset the build row,
    settle attributes, or spawn a rerun."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="failed", error="boom"
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "drv_path) VALUES ($1, 'real', 'x86_64-linux', 'succeeded', '/d')",
        build_id,
    )

    reruns: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reruns.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await service.restart_attribute(build_id, "ghost")
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    assert reruns == []
    row = await pool.fetchrow(
        "SELECT status, error FROM builds WHERE id = $1", build_id
    )
    assert (row["status"], row["error"]) == ("failed", "boom")
    attr_status = await pool.fetchval(
        "SELECT status FROM build_attributes WHERE build_id = $1", build_id
    )
    assert attr_status == "succeeded"


async def test_restart_failed_eval_attribute_reevaluates(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """failed_eval attributes have no drv_path; resetting them to
    pending must trigger a re-eval, not wedge the build in 'building'."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="failed")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "error) VALUES ($1, 'broken', 'x86_64-linux', 'failed_eval', 'e')",
        build_id,
    )

    reevals: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reevals.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
    # A restart only queues; the rerun sets the real status.
    status = await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
    assert status == "pending"
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    assert reevals == [build_id]
    # Stale NULL-drv rows are cleared so the re-eval result is
    # authoritative.
    count = await pool.fetchval(
        "SELECT count(*) FROM build_attributes WHERE build_id = $1",
        build_id,
    )
    assert count == 0


async def test_restart_cancelled_build_reschedules_attributes(
    service: CIService,
    git_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting a cancelled build must rebuild its cancelled
    attributes from the stored eval results."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="cancelled")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, drv_path) "
        "VALUES ($1, 'a', 'x86_64-linux', 'cancelled', '/nix/store/a.drv'), "
        "($1, 'b', 'x86_64-linux', 'cancelled', '/nix/store/b.drv')",
        build_id,
    )

    async def fake_check_store_paths(drvs: list[str]) -> set[str]:
        return set(drvs)

    monkeypatch.setattr(
        "nixbot.restart_dispatch.check_store_paths", fake_check_store_paths
    )

    rescheduled: list[list[str]] = []

    async def fake_rerun_pending_attributes(
        info: Any, build: Any, pending_jobs: Any, credentials: Any = None
    ) -> None:
        rescheduled.append(sorted(job.attr for job in pending_jobs))

    service.orchestrator.rerun_pending_attributes = fake_rerun_pending_attributes  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    assert rescheduled == [["a", "b"]]


async def test_restart_while_running_is_requeued_not_dropped(
    service: CIService,
    git_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart clicked while the build is still in flight (e.g. right
    after a cancel, while the old run unwinds) must stay queued and
    run once the build is gone, not be dropped silently."""
    repo, sha = git_repo
    monkeypatch.setattr("nixbot.restart_dispatch.RESTART_RETRY_SECONDS", 0.0)

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="cancelled")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, drv_path) "
        "VALUES ($1, 'a', 'x86_64-linux', 'cancelled', '/nix/store/a.drv')",
        build_id,
    )

    async def fake_check_store_paths(drvs: list[str]) -> set[str]:
        return set(drvs)

    monkeypatch.setattr(
        "nixbot.restart_dispatch.check_store_paths", fake_check_store_paths
    )

    rescheduled: list[int] = []

    async def fake_rerun_pending_attributes(
        info: Any, build: Any, pending_jobs: Any, credentials: Any = None
    ) -> None:
        rescheduled.append(build.id)

    service.orchestrator.rerun_pending_attributes = fake_rerun_pending_attributes  # type: ignore[method-assign]

    # Simulate the cancelled run still unwinding.
    service.orchestrator.cancel_events[build_id] = asyncio.Event()
    await service.restart_build(build_id)
    queue = WorkQueue(pool)
    item = await queue.claim_next()
    assert item is not None
    await service._execute_work(queue, item)  # noqa: SLF001
    assert rescheduled == []  # deferred, not executed
    pending = await pool.fetchval(
        "SELECT count(*) FROM work_queue WHERE kind = 'restart' AND status = 'pending'"
    )
    assert pending == 1  # the intent survived

    # Old run finished; the queued restart now goes through.
    del service.orchestrator.cancel_events[build_id]
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001
    assert rescheduled == [build_id]


# --- cancel of a non-running build ---------------------------------------


class RecordingReporter(NullStatusReporter):
    def __init__(self) -> None:
        self.finished: list[tuple[int, str, int]] = []

    async def build_finished(self, event: Any, build: Any, result: BuildResult) -> None:
        self.finished.append((build.id, result.status, result.generation))


async def test_cancel_not_running_posts_forge_status(service: CIService) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(pool, project_id, commit_sha="c1", status="building")
    reporter = RecordingReporter()
    service.orchestrator.reporter = reporter

    await service.cancel_build(build_id)

    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "cancelled"
    )
    assert len(reporter.finished) == 1
    reported_id, status, generation = reporter.finished[0]
    assert (reported_id, status) == (build_id, "cancelled")
    assert generation == 1  # bumped so stale posts lose

    # Cancelling again is a no-op: no duplicate forge status.
    await service.cancel_build(build_id)
    assert len(reporter.finished) == 1


async def test_check_rerequested_dispatch(service: CIService) -> None:
    """GitHub Re-run button: per-attr name -> attribute restart,
    summary / suite -> full restart, foreign external_id falls back
    to the head_sha lookup."""
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    build_id = await insert_build(pool, project_id, commit_sha="deadbeef")
    store = CheckRunStore(pool)
    await store.set(project_id, "deadbeef", "nixbot/nix-build", None, 1)
    await store.set(project_id, "deadbeef", "nixbot/nix-build a", "flaky", 2)

    enqueued: list[tuple[str, dict]] = []

    async def fake_enqueue(kind: str, key: str, payload: dict) -> None:
        enqueued.append((kind, payload))

    service.enqueue_work = fake_enqueue  # type: ignore[method-assign,assignment]

    base = {"forge": "github", "forge_repo_id": forge_repo_id, "head_sha": "deadbeef"}
    await service.submit(
        CheckRerequested(**base, build_id=build_id, name="nixbot/nix-build a")
    )
    await service.submit(
        CheckRerequested(**base, build_id=build_id, name="nixbot/nix-build")
    )
    # check_suite: no build_id, no name; resolved via LatestBuildForSha.
    await service.submit(CheckRerequested(**base))
    # external_id from another project's app must not be honoured.
    await service.submit(CheckRerequested(**base, build_id=99999999))
    assert enqueued == [
        ("restart", {"build_id": build_id, "attr": "flaky"}),
        ("restart", {"build_id": build_id}),
        ("restart", {"build_id": build_id}),
        ("restart", {"build_id": build_id}),
    ]


async def test_cancel_not_running_settles_attribute_rows(service: CIService) -> None:
    """Direct cancel (no running task) must not leave pending/building
    attribute rows non-terminal forever."""

    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(pool, project_id, commit_sha="c2", status="building")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status) "
        "VALUES ($1, 'p', 'x', 'pending'), ($1, 'b', 'x', 'building'), "
        "($1, 'ok', 'x', 'succeeded')",
        build_id,
    )
    service.orchestrator.reporter = RecordingReporter()

    await service.cancel_build(build_id)

    rows = await pool.fetch(
        "SELECT attr, status FROM build_attributes WHERE build_id = $1",
        build_id,
    )
    statuses = {row["attr"]: row["status"] for row in rows}
    assert statuses == {
        "p": "cancelled",
        "b": "cancelled",
        "ok": "succeeded",
    }


async def test_cancel_attribute_not_running_reaggregates_build(
    service: CIService,
) -> None:
    """Direct attribute cancel must re-aggregate the build; otherwise
    the build stays 'building' forever with all rows terminal."""

    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(pool, project_id, commit_sha="c3", status="building")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status) "
        "VALUES ($1, 'only', 'x', 'pending')",
        build_id,
    )
    service.orchestrator.reporter = RecordingReporter()

    await service.cancel_attribute(build_id, "only")

    assert (
        await pool.fetchval(
            "SELECT status FROM build_attributes WHERE build_id = $1 AND attr = 'only'",
            build_id,
        )
        == "cancelled"
    )
    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "cancelled"
    )


# --- pull-based projects --------------------------------------------------


async def test_pull_based_projects_synced_and_buildable(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    """Polled head changes are dropped unless an enabled projects row
    exists for forge='pull_based'."""

    key = tmp_path / "id_ed25519"
    key.write_text("fake-key")
    service, _app = await make_service(
        pull_based=PullBasedConfig(
            repositories={
                "myrepo": PullBasedRepository(
                    name="myrepo",
                    default_branch="main",
                    url="ssh://git@example.com/x/y.git",
                    ssh_private_key_file=key,
                )
            }
        )
    )
    pool = service.pool
    await service.discover_once()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE forge = 'pull_based' AND forge_repo_id = 'myrepo'"
    )
    assert row is not None
    assert row["enabled"] is True
    assert row["default_branch"] == "main"

    events: list[Any] = []

    async def fake_handle(event: Any, credentials: Any = None) -> None:
        events.append((event, credentials))

    service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
    await service.submit(
        ChangeRequest(
            forge="pull_based",
            forge_repo_id="myrepo",
            branch="main",
            commit_sha="abc",
        )
    )
    await service.drain_work()
    assert len(events) == 1
    event, credentials = events[0]
    assert event.repo.forge == "pull_based"
    # SSH credentials resolved for the polled repository.
    assert credentials.ssh_private_key_file == key


# --- discovery topic handling ----------------------------------------------


class StubGitHub:
    def __init__(self, repos: list[DiscoveredRepo]) -> None:
        self.repos = repos

    async def discover_repos(self) -> list[DiscoveredRepo]:
        return self.repos


async def test_topic_does_not_hard_filter_discovery(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    """The topic is a one-shot legacy import aid; repos without it must
    still be discovered (disabled) so admins can enable them in the UI."""

    secret = tmp_path / "gh.pem"
    secret.write_text("k")
    webhook_secret = tmp_path / "webhook-secret"
    webhook_secret.write_text("s")
    service, _app = await make_service(
        github={
            "id": 1,
            "secret_key_file": str(secret),
            "webhook_secret_file": str(webhook_secret),
            "filters": {"topic": "build-with-buildbot"},
        }
    )
    pool = service.pool
    service.github = StubGitHub(  # type: ignore[assignment]
        [
            DiscoveredRepo(
                forge="github",
                forge_repo_id="999001",
                owner="acme",
                repo="untagged",
                default_branch="main",
                clone_url="http://example/acme/untagged",
                private=False,
                topics=(),
            )
        ]
    )
    await service.discover_once()
    row = await pool.fetchrow(
        "SELECT * FROM projects WHERE forge = 'github' AND forge_repo_id = '999001'"
    )
    assert row is not None
    assert row["name"] == "untagged"


async def test_startup_reevaluates_interrupted_eval(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """A build interrupted mid-eval (no attribute rows) re-evaluates at
    startup instead of being marked failed."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="evaluating")

    reevals: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reevals.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await _startup(service)
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    # The shared database may hold other tests' unfinished builds.
    assert build_id in reevals
    status = await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
    assert status != "failed"


async def test_rerun_of_interrupted_eval_reevaluates(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """A crash mid-evaluation leaves a partial attribute set; resuming
    only those rows would report success for a build that never
    finished evaluating. It must re-evaluate."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="evaluating")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "drv_path) VALUES ($1, 'partial', 'x86_64-linux', 'pending', "
        "'/nix/store/p.drv')",
        build_id,
    )

    reevals: list[int] = []
    resumes: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reevals.append(build.id)

    async def fake_resume(
        info: Any, build: Any, jobs: Any, credentials: Any = None
    ) -> None:
        resumes.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    service.orchestrator.rerun_pending_attributes = fake_resume  # type: ignore[method-assign, assignment]
    await service._rerun(build_id)  # noqa: SLF001

    assert resumes == []
    assert reevals == [build_id]


async def test_rerun_resumes_building_rows_and_keeps_finished(
    service: CIService, git_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that crashed with an attribute in 'building' resumes that
    attribute; already-finished rows are kept, not re-evaluated away."""
    repo, sha = git_repo

    async def all_valid(paths: list[str]) -> set[str]:
        return set(paths)

    monkeypatch.setattr("nixbot.restart_dispatch.check_store_paths", all_valid)

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="building")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "drv_path) VALUES "
        "($1, 'done', 'x86_64-linux', 'succeeded', '/nix/store/d.drv'), "
        "($1, 'mid', 'x86_64-linux', 'building', '/nix/store/m.drv')",
        build_id,
    )

    reevals: list[int] = []
    resumed_attrs: list[str] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reevals.append(build.id)

    async def fake_resume(
        info: Any, build: Any, jobs: Any, credentials: Any = None
    ) -> None:
        resumed_attrs.extend(job.attr for job in jobs)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    service.orchestrator.rerun_pending_attributes = fake_resume  # type: ignore[method-assign, assignment]
    await service._rerun(build_id)  # noqa: SLF001

    assert reevals == []
    assert resumed_attrs == ["mid"]
    kept = await pool.fetchval(
        "SELECT status FROM build_attributes WHERE build_id = $1 AND attr = 'done'",
        build_id,
    )
    assert kept == "succeeded"


async def test_rerun_reevaluates_when_drv_paths_were_garbage_collected(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """Stored drv paths can be GC'd between the build and its restart;
    blindly rerunning them fails with "path does not exist". Missing
    drvs must fall back to a re-evaluation."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(pool, project_id, commit_sha=sha, status="building")
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "drv_path) VALUES ($1, 'gone', 'x86_64-linux', 'pending', "
        "'/nix/store/gcd.drv')",
        build_id,
    )

    reevals: list[int] = []
    resumes: list[int] = []

    async def fake_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        reevals.append(build.id)

    async def fake_resume(
        info: Any, build: Any, jobs: Any, credentials: Any = None
    ) -> None:
        resumes.append(build.id)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    service.orchestrator.rerun_pending_attributes = fake_resume  # type: ignore[method-assign, assignment]
    # The real path checker: /nix/store/gcd.drv does not exist.
    await service._rerun(build_id)  # noqa: SLF001

    assert resumes == []
    assert reevals == [build_id]


async def test_reeval_failure_marks_build_failed(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """A failed re-eval marks the build failed instead of leaving it
    pending."""
    repo, sha = git_repo

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="failed", error="boom"
    )

    async def broken_run_build(
        event: Any,
        build: Any,
        worktree_path: Path,
        credentials: Any = None,
    ) -> None:
        msg = "eval exploded"
        raise RuntimeError(msg)

    service.orchestrator.run_build = broken_run_build  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
    await asyncio.gather(*service._tasks, return_exceptions=True)  # noqa: SLF001

    row = await pool.fetchrow(
        "SELECT status, error FROM builds WHERE id = $1", build_id
    )
    assert row["status"] == "failed"
    assert "re-evaluation" in row["error"]


async def test_gitlab_discovery_and_hook_registration(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    forge = FakeGitlab(
        [
            {
                "id": 41,
                "path_with_namespace": "Mic92/dotfiles",
                "default_branch": "main",
                "http_url_to_repo": "https://gitlab.com/Mic92/dotfiles.git",
                "visibility": "public",
            }
        ],
        token="glpat-x",  # noqa: S106 (test credential)
    )

    token = tmp_path / "gitlab-token"
    token.write_text("glpat-x")
    service, _app = await make_service(gitlab={"token_file": str(token)})
    pool = service.pool
    service.gitlab = forge.client(base_url="https://gitlab.com")
    await service.discover_once()
    project = await pool.fetchrow(
        "SELECT * FROM projects WHERE forge = 'gitlab' AND forge_repo_id = '41'"
    )
    assert project is not None
    assert project["name"] == "dotfiles"
    assert not forge.created  # disabled projects get no hook

    await pool.execute(
        "UPDATE projects SET enabled = TRUE WHERE id = $1", project["id"]
    )
    await service._register_hooks()  # noqa: SLF001
    assert forge.created[0]["url"] == "http://ci.test/webhooks/gitlab"
    secret = await pool.fetchval(
        "SELECT secret FROM webhook_secrets WHERE project_id = $1",
        project["id"],
    )
    assert forge.created[0]["token"] == secret


async def test_health_serves_while_startup_blocks(
    postgres_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP server binds while discovery/recovery still run."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = make_config(postgres_dsn, tmp_path / "state", http_port=port)

    started = asyncio.Event()

    async def stalled_startup(service: object) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("nixbot.bootstrap._startup", stalled_startup)
    runner = asyncio.create_task(run_service(config))
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/health")
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            else:
                msg = "server did not bind while startup was blocked"
                raise AssertionError(msg)
        assert response.status_code == 200
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
