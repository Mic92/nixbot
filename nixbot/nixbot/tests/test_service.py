"""Service composition regression tests (service.py)."""

# ruff: noqa: PLR2004, ARG001, ARG002 (stub callbacks ignore arguments)

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from nixbot import build_reuse, restart_dispatch
from nixbot.bootstrap import _startup, build_service, run_service
from nixbot.config import (
    PrApprovalConfig,
    PullBasedConfig,
    PullBasedRepository,
    resolve_credential_path,
)
from nixbot.db_gen import approvals as approvals_q
from nixbot.db_gen import builds as builds_q
from nixbot.events import BuildResult, ChangeEvent, EvalReport, NullStatusReporter
from nixbot.forge import DiscoveredRepo
from nixbot.repos import repo_info
from nixbot.schedule_runner import scheduled_worktree_id
from nixbot.schedules import DueEffect, ScheduleWhen
from nixbot.status import CheckPermissionError, CheckRunStore
from nixbot.webhooks import ChangeRequest, CheckRerequested, PrApproved, PrClosed
from nixbot.work_queue import WorkQueue

from .support import (
    FakeGitlab,
    git,
    insert_build,
    insert_project,
    make_config,
    mk_job,
)

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


def capture_run_build(service: CIService) -> list[int]:
    """Replace orchestrator.run_build with a stub recording build ids."""
    build_ids: list[int] = []

    async def fake_run_build(
        event: Any, build: Any, worktree_path: Path, credentials: Any = None
    ) -> None:
        build_ids.append(build.id_)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    return build_ids


def capture_resume(service: CIService) -> list[int]:
    """Replace orchestrator.rerun_pending_attributes with a stub
    recording build ids."""
    build_ids: list[int] = []

    async def fake_resume(
        info: Any, build: Any, jobs: Any, credentials: Any = None
    ) -> None:
        build_ids.append(build.id_)

    service.orchestrator.rerun_pending_attributes = fake_resume  # type: ignore[method-assign, assignment]
    return build_ids


# --- pure helpers ------------------------------------------------------


def test_scheduled_worktree_id_distinct_per_effect() -> None:
    when = ScheduleWhen()
    a = DueEffect(project_id=1, schedule_name="s", effect="deploy", when=when)
    b = DueEffect(project_id=1, schedule_name="s", effect="notify", when=when)
    assert scheduled_worktree_id(a) != scheduled_worktree_id(b)


def test_scheduled_worktree_id_sanitizes_traversal() -> None:
    due = DueEffect(
        project_id=1,
        schedule_name="../../../etc",
        effect="x/../../y",
        when=ScheduleWhen(),
    )
    wid = scheduled_worktree_id(due)
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


async def test_build_branches_gates_branch_pushes(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """`build_branches` in the default branch's nixbot.toml decides
    which non-default branches build."""
    repo, sha = git_repo
    (repo / "nixbot.toml").write_text('build_branches = ["release-*"]\n')
    git(repo, "add", "nixbot.toml")
    git(repo, "commit", "-m", "opt release branches into CI")

    pool = service.pool
    project_id = await seed_project(pool, f"file://{repo}")
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    handled: list[Any] = []

    async def fake_handle(event: Any, credentials: Any = None) -> None:
        handled.append(event.branch)

    service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]

    for branch in ("release-1.0", "random-feature"):
        await service.submit(
            ChangeRequest(
                forge="github",
                forge_repo_id=forge_repo_id,
                branch=branch,
                commit_sha=sha,
            )
        )
    await service.drain_work()
    assert handled == ["release-1.0"]


async def test_pr_approval_gate(
    make_service: ServiceFactory, git_repo: tuple[Path, str]
) -> None:
    """Untrusted GitHub PRs are held until approved. Approval builds
    the held head and unlocks later pushes. Trusted authors and the
    per-repo `require_approval = false` opt-out bypass the gate."""
    repo, sha = git_repo
    service, _app = await make_service(pr_approval=PrApprovalConfig(enable=True))
    pool = service.pool
    project_id = await seed_project(pool, f"file://{repo}")
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    handled: list[tuple[int | None, str]] = []
    gates: list[tuple[str, int]] = []

    async def fake_handle(event: Any, credentials: Any = None) -> None:
        handled.append((event.pr_number, event.commit_sha))

    class FakeGate:
        async def post_gate(
            self, owner: str, repo: str, sha: str, pr_number: int, details_url: str
        ) -> None:
            gates.append((sha, pr_number))

    service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
    service.gate_poster = FakeGate()

    def pr(number: int, commit: str, association: str) -> ChangeRequest:
        return ChangeRequest(
            forge="github",
            forge_repo_id=forge_repo_id,
            branch="main",
            commit_sha=commit,
            pr_number=number,
            pr_author="github:eve",
            author_association=association,
        )

    # App/bot PRs report NONE but push to the base repo: trusted.
    await service.submit(
        dataclasses.replace(pr(4, "c0", "NONE"), head_in_base_repo=True)
    )
    await service.drain_work()
    assert handled == [(4, "c0")]
    handled.clear()

    await service.submit(pr(5, sha, "MEMBER"))
    await service.submit(pr(6, "c1", "FIRST_TIME_CONTRIBUTOR"))
    await service.submit(pr(6, "c2", "FIRST_TIME_CONTRIBUTOR"))
    await service.drain_work()
    assert handled == [(5, sha)]
    assert gates == [("c1", 6), ("c2", 6)]
    pending = await approvals_q.pending_approvals(pool, project_id=project_id)
    assert [(p.pr_number, p.commit_sha) for p in pending] == [(6, "c2")]

    await service.submit(
        PrApproved(forge="github", forge_repo_id=forge_repo_id, pr_number=6)
    )
    await service.drain_work()
    assert handled[-1] == (6, "c2")
    assert not await approvals_q.pending_approvals(pool, project_id=project_id)
    # Double click: nothing pending, no second build.
    await service.approve_pr(project_id, 6, "github:maint")
    await service.drain_work()
    assert len(handled) == 2

    await service.submit(pr(6, "c3", "FIRST_TIME_CONTRIBUTOR"))
    await service.drain_work()
    assert handled[-1] == (6, "c3")
    assert len(gates) == 2

    # Pre-approval (no held change yet) still unlocks the PR.
    await service.approve_pr(project_id, 8, "github:maint")
    await service.submit(pr(8, "c5", "NONE"))
    await service.drain_work()
    assert handled[-1] == (8, "c5")

    (repo / "nixbot.toml").write_text("require_approval = false\n")
    git(repo, "add", "nixbot.toml")
    git(repo, "commit", "-m", "opt out of the contributor gate")
    await service.submit(pr(7, "c4", "NONE"))
    await service.drain_work()
    assert handled[-1] == (7, "c4")


async def test_pr_approval_disabled_by_default(service: CIService) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://x")
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    handled: list[Any] = []

    async def fake_handle(event: Any, credentials: Any = None) -> None:
        handled.append(event.pr_number)

    service.orchestrator.handle_change_event = fake_handle  # type: ignore[method-assign]
    await service.submit(
        ChangeRequest(
            forge="github",
            forge_repo_id=forge_repo_id,
            branch="main",
            commit_sha="c1",
            pr_number=3,
            author_association="NONE",
        )
    )
    await service.drain_work()
    assert handled == [3]


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
        # file:// forces the full transfer protocol. Clone before
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

        reevals = capture_run_build(service)
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
    to resume. Restarting it must re-evaluate instead of aggregating an
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
        reevals.append(build.id_)

    service.orchestrator.run_build = fake_run_build  # type: ignore[method-assign]
    await service.restart_build(build_id)
    await service.drain_work()
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

    capture_run_build(service)
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

    reruns = capture_run_build(service)
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
    """failed_eval attributes have no drv_path. Resetting them to
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

    reevals = capture_run_build(service)
    await service.restart_build(build_id)
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    assert reevals == [build_id]
    status = await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
    assert status == "pending"
    # The stale row survives until a successful eval commits its
    # result. Interrupted re-evals must never lose rows.
    count = await pool.fetchval(
        "SELECT count(*) FROM build_attributes WHERE build_id = $1",
        build_id,
    )
    assert count == 1


async def test_interrupted_reeval_keeps_attribute_rows(
    service: CIService, git_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An attribute restart falls back to re-evaluation when the stored
    derivations were garbage-collected. A re-eval that never completes
    (cancel, crash) must not lose any attribute rows."""
    repo, sha = git_repo

    async def all_gcd(paths: list[str]) -> set[str]:
        return set()

    monkeypatch.setattr("nixbot.restart_dispatch.check_store_paths", all_gcd)

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="failed", eval_completed=True
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, drv_path) "
        "VALUES ($1, 'ok', 'x86_64-linux', 'succeeded', '/nix/store/aaa.drv'), "
        "($1, 'bad', 'x86_64-linux', 'failed', '/nix/store/bbb.drv')",
        build_id,
    )

    capture_run_build(service)  # the re-eval never records a result
    await service.restart_attribute(build_id, "bad")
    await service.drain_work()
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    attrs = {
        r["attr"]: r["status"]
        for r in await pool.fetch(
            "SELECT attr, status FROM build_attributes WHERE build_id = $1", build_id
        )
    }
    assert set(attrs) == {"ok", "bad"}
    assert attrs["ok"] == "succeeded"


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


async def test_restart_cancelled_mid_eval_reevaluates(
    service: CIService, git_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build cancelled mid-eval has a partial attribute set with
    eval_completed unset. Restarting must re-evaluate, not resume the
    partial set, even when its derivations are still in the store."""
    repo, sha = git_repo

    async def all_valid(paths: list[str]) -> set[str]:
        return set(paths)

    monkeypatch.setattr("nixbot.restart_dispatch.check_store_paths", all_valid)

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    # Post-reset state: status and attrs back to pending, but a mid-eval
    # cancellation never set eval_completed.
    build_id = await insert_build(
        pool,
        project_id,
        commit_sha=sha,
        status="pending",
        eval_completed=False,
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, "
        "drv_path) VALUES ($1, 'partial', 'x86_64-linux', 'pending', "
        "'/nix/store/p.drv')",
        build_id,
    )

    reevals = capture_run_build(service)
    resumes = capture_resume(service)
    await restart_dispatch.rerun(service, build_id)

    assert resumes == []
    assert reevals == [build_id]


async def test_restart_while_unwinding_waits_then_runs(
    service: CIService,
    git_repo: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart clicked while the old run is still unwinding (e.g.
    right after a cancel) must neither reset rows under that run nor
    drop the rerun: it waits for the run to release the build."""
    repo, sha = git_repo

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
        rescheduled.append(build.id_)

    service.orchestrator.rerun_pending_attributes = fake_rerun_pending_attributes  # type: ignore[method-assign]

    # Simulate the cancelled run still unwinding.
    service.orchestrator.cancel_events[build_id] = asyncio.Event()
    await service.restart_build(build_id)
    drain = asyncio.create_task(service.drain_work())
    await asyncio.sleep(0.05)
    assert not drain.done()
    assert rescheduled == []
    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "cancelled"
    )

    # Old run finished. The waiting rerun now resets and goes through.
    service.orchestrator.release_run(build_id)
    await drain
    await asyncio.gather(*service._tasks)  # noqa: SLF001
    assert rescheduled == [build_id]
    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "pending"
    )


async def test_restart_attribute_while_build_running(
    service: CIService, git_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restarting one attribute of a build that is still running must
    not reset rows under the live run. Otherwise the run finishes onto
    the reset rows: nothing re-executes and the build ends terminal
    with started_at NULL ("took \u2014" in the UI)."""
    from nixbot.repos import repo_info  # noqa: PLC0415

    from .test_orchestrator import FakeExecutor  # noqa: PLC0415

    repo, sha = git_repo

    async def all_valid(paths: list[str]) -> set[str]:
        return set(paths)

    monkeypatch.setattr("nixbot.restart_dispatch.check_store_paths", all_valid)

    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="pending", eval_completed=True
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status, drv_path) "
        "VALUES ($1, 'a', 'x86_64-linux', 'pending', '/nix/store/a.drv'), "
        "($1, 'b', 'x86_64-linux', 'pending', '/nix/store/b.drv')",
        build_id,
    )
    executor = FakeExecutor(gate=asyncio.Event())
    service.orchestrator.executor = executor
    project = await service.repo_store.by_id(project_id)
    assert project is not None
    build = await builds_q.get_build(pool, id_=build_id)
    assert build is not None

    run = asyncio.create_task(
        service.orchestrator.rerun_pending_attributes(
            repo_info(project), build, [mk_job("a"), mk_job("b")]
        )
    )
    await asyncio.wait_for(executor.started.wait(), timeout=10)

    await service.restart_attribute(build_id, "a")
    drain = asyncio.create_task(service.drain_work())
    # The restart must not have touched rows owned by the live run.
    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "building"
    )
    assert executor.gate is not None
    executor.gate.set()
    await run
    await drain
    await asyncio.gather(*service._tasks)  # noqa: SLF001

    row = await pool.fetchrow(
        "SELECT status, started_at, finished_at FROM builds WHERE id = $1", build_id
    )
    assert row["status"] == "succeeded"
    assert row["started_at"] is not None
    assert row["finished_at"] >= row["started_at"]
    attr = await pool.fetchrow(
        "SELECT status, started_at FROM build_attributes "
        "WHERE build_id = $1 AND attr = 'a'",
        build_id,
    )
    assert (attr["status"], attr["started_at"] is not None) == ("succeeded", True)
    # 'a' ran in the original run and again for the restart.
    assert sorted(executor.built) == ["a", "a", "b"]


async def test_build_persistence_queues_only_terminal_actionable_failures(
    service: CIService, git_repo: tuple[Path, str]
) -> None:
    """The scheduler callback queues after persistence, while cancelled and
    explicitly ignored failures stay out of early reporting."""
    from nixbot.build_scheduler import BuildOutcome  # noqa: PLC0415
    from nixbot.repos import repo_info  # noqa: PLC0415

    from .test_orchestrator import FakeExecutor  # noqa: PLC0415

    repo, sha = git_repo
    pool = service.pool
    project_id = await seed_project(pool, str(repo))
    build_id = await insert_build(
        pool, project_id, commit_sha=sha, status="pending", eval_completed=True
    )
    jobs = [
        mk_job("broken"),
        mk_job("cancelled"),
        mk_job("ignored").model_copy(update={"extra_value": {"ignoreFailure": True}}),
    ]
    await pool.executemany(
        "INSERT INTO build_attributes "
        "(build_id, attr, system, status, drv_path) "
        "VALUES ($1, $2, 'x86_64-linux', 'pending', $3)",
        [(build_id, job.attr, job.drv_path) for job in jobs],
    )
    service.orchestrator.executor = FakeExecutor(
        outcomes={
            "broken": BuildOutcome.failure,
            "cancelled": BuildOutcome.cancelled,
            "ignored": BuildOutcome.failure,
        }
    )
    service.orchestrator.request_attribute_report = service.request_attribute_report
    project = await service.repo_store.by_id(project_id)
    build = await builds_q.get_build(pool, id_=build_id)
    assert project is not None
    assert build is not None

    await service.orchestrator.rerun_pending_attributes(repo_info(project), build, jobs)

    items = await pool.fetch(
        "SELECT payload->>'attr' AS attr FROM work_queue "
        "WHERE kind = 'attribute-report'"
    )
    assert [row["attr"] for row in items] == ["broken"]
    assert "fake build output" in (
        await pool.fetchval(
            "SELECT error FROM build_attributes WHERE build_id = $1 AND attr = 'broken'",
            build_id,
        )
    )


# --- cancel of a non-running build ---------------------------------------


class RecordingReporter(NullStatusReporter):
    def __init__(self) -> None:
        self.finished: list[tuple[int, str, int]] = []

    async def build_finished(self, event: Any, build: Any, result: BuildResult) -> None:
        self.finished.append((build.id_, result.status, result.generation))


class AttributeRecordingReporter(NullStatusReporter):
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls = 0
        self.failures: list[tuple[str, str, str | None, str, int, str]] = []
        self.finished: list[tuple[str, str | None]] = []

    async def attribute_failed(  # noqa: PLR0913
        self,
        event: Any,
        build: Any,
        result: Any,
        *,
        attempt: str,
        generation: int,
        attr_prefix: str = "checks",
    ) -> None:
        self.calls += 1
        if self.fail_once:
            self.fail_once = False
            msg = "forge unavailable"
            raise httpx.ConnectError(msg)
        self.failures.append(
            (
                event.commit_sha,
                result.attr,
                result.error,
                attempt,
                generation,
                attr_prefix,
            )
        )

    async def build_finished(self, event: Any, build: Any, result: BuildResult) -> None:
        self.finished.extend((event.commit_sha, item.error) for item in result.results)


class TargetRecordingReporter(AttributeRecordingReporter):
    def __init__(self) -> None:
        super().__init__()
        self.restarted: list[tuple[str, str]] = []
        self.final_results: list[tuple[str, BuildResult]] = []
        self.eval_results: list[tuple[str, bool]] = []
        self.eval_reports: list[tuple[str, EvalReport]] = []
        self.eval_cancellations: list[str] = []

    async def build_restarted(
        self,
        event: Any,
        build: Any,
        attr: str | None,
        attr_prefix: str = "checks",
    ) -> None:
        self.restarted.append((event.commit_sha, attr_prefix))

    async def build_finished(self, event: Any, build: Any, result: BuildResult) -> None:
        self.final_results.append((event.commit_sha, result))

    async def eval_finished(
        self, event: Any, build: Any, report: EvalReport
    ) -> None:
        self.eval_results.append((event.commit_sha, report.success))
        self.eval_reports.append((event.commit_sha, report))

    async def eval_cancelled(self, event: Any, build: Any) -> None:
        self.eval_cancellations.append(event.commit_sha)


async def test_restart_and_final_report_use_persisted_targets_after_restart(
    service: CIService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="failed"
    )
    for commit_sha, branch, pr_number in (
        ("pr-sha", "feature", 7),
        ("main-sha", "main", None),
    ):
        await builds_q.record_build_report_target(
            pool,
            build_id=build_id,
            commit_sha=commit_sha,
            branch=branch,
            pr_number=pr_number,
        )
    await builds_q.set_build_attribute_prefix(
        pool, build_id=build_id, attribute_prefix="hydraJobs"
    )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter
    service.orchestrator.request_build_report = service.request_build_report

    async def no_resumable_builds(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        restart_dispatch, "find_unfinished_builds", no_resumable_builds
    )
    await restart_dispatch.rerun(service, build_id, restart=True)

    assert set(reporter.restarted) == {
        ("pr-sha", "hydraJobs"),
        ("main-sha", "hydraJobs"),
    }

    build = await builds_q.get_build(pool, id_=build_id)
    project = await service.repo_store.by_id(project_id)
    assert build is not None
    assert project is not None
    source_event = ChangeEvent(
        repo=repo_info(project), branch="feature", commit_sha="pr-sha", pr_number=7
    )
    await build_reuse.report_eval_finished(
        service.orchestrator, source_event, build, EvalReport(success=True)
    )
    await build_reuse.report_eval_finished(
        service.orchestrator, source_event, build, EvalReport(success=False)
    )
    await build_reuse.report_eval_cancelled(
        service.orchestrator, source_event, build
    )
    assert set(reporter.eval_results) == {
        ("pr-sha", True),
        ("main-sha", True),
        ("pr-sha", False),
        ("main-sha", False),
    }
    assert set(reporter.eval_cancellations) == {"pr-sha", "main-sha"}

    # No linked_events survive a process restart; durable targets still receive
    # the authoritative completion.
    service.orchestrator.linked_events.clear()
    await pool.execute(
        "UPDATE builds SET status = 'succeeded', finished_at = now() WHERE id = $1",
        build_id,
    )
    build = await builds_q.get_build(pool, id_=build_id)
    assert build is not None
    await service.orchestrator.report_build_finished(
        source_event,
        build,
        BuildResult("succeeded", build.status_generation, []),
    )
    assert {commit for commit, _ in reporter.final_results} == {
        "pr-sha",
        "main-sha",
    }


async def test_old_terminal_report_does_not_post_after_restart_reset(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="failed-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="failed-sha",
        branch="main",
        pr_number=None,
    )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter
    await service.enqueue_work(
        "report", f"report-{build_id}", {"build_id": build_id}
    )

    await service.orchestrator.reset_build_for_restart(build_id, None)
    await service.drain_work()

    build = await builds_q.get_build(pool, id_=build_id)
    assert build is not None
    assert build.status == "pending"
    assert build.status_generation == 1
    assert reporter.eval_results == []
    assert reporter.eval_cancellations == []
    assert reporter.final_results == []


async def test_terminal_eval_api_failure_blocks_ack_until_retry(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="failed"
    )
    for commit_sha, branch, pr_number in (
        ("pr-sha", "feature", 8),
        ("main-sha", "main", None),
    ):
        await builds_q.record_build_report_target(
            pool,
            build_id=build_id,
            commit_sha=commit_sha,
            branch=branch,
            pr_number=pr_number,
        )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, status, finished_at) "
        "VALUES ($1, 'broken', 'failed', now())",
        build_id,
    )

    class FlakyTerminalEvalReporter(TargetRecordingReporter):
        fail_terminal_eval = True

        async def terminal_eval_finished(
            self, event: Any, build: Any, report: EvalReport
        ) -> None:
            if self.fail_terminal_eval:
                self.fail_terminal_eval = False
                msg = "forge unavailable during terminal eval"
                raise httpx.ConnectError(msg)
            await super().terminal_eval_finished(event, build, report)

    reporter = FlakyTerminalEvalReporter()
    service.orchestrator.reporter = reporter
    service.orchestrator.request_build_report = service.request_build_report

    await service.orchestrator.request_build_report(build_id)
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) is None
    assert reporter.final_results == []

    await service.drain_work()

    assert set(reporter.eval_results) == {("pr-sha", True), ("main-sha", True)}
    assert {commit for commit, _ in reporter.final_results} == {
        "pr-sha",
        "main-sha",
    }
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) == 0


async def test_terminal_report_treats_partial_eval_with_attribute_as_failed(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool,
        project_id,
        commit_sha="partial-sha",
        status="failed",
        error="evaluation crashed",
        eval_completed=False,
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="partial-sha",
        branch="main",
        pr_number=None,
    )
    await pool.execute(
        "UPDATE builds SET eval_warnings = '[\"deprecated input\"]'::jsonb, "
        "eval_duration_ms = 1234 WHERE id = $1",
        build_id,
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, status, finished_at) "
        "VALUES ($1, 'already-emitted', 'dependency_failed', now())",
        build_id,
    )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter

    await service.request_build_report(build_id)

    assert reporter.eval_cancellations == []
    assert reporter.eval_reports == [
        (
            "partial-sha",
            EvalReport(
                success=False,
                warnings=["deprecated input"],
                error="evaluation crashed",
                duration_ms=1234,
            ),
        )
    ]


async def test_terminal_report_distinguishes_completed_and_incomplete_cancellation(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool,
        project_id,
        commit_sha="cancelled-sha",
        status="cancelled",
        eval_completed=True,
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="cancelled-sha",
        branch="main",
        pr_number=None,
    )
    await pool.execute(
        "UPDATE builds SET eval_warnings = '[\"deprecated input\"]'::jsonb, "
        "eval_duration_ms = 4321 WHERE id = $1",
        build_id,
    )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter

    await service.request_build_report(build_id)

    assert reporter.eval_cancellations == []
    assert reporter.eval_reports == [
        (
            "cancelled-sha",
            EvalReport(
                success=True,
                warnings=["deprecated input"],
                duration_ms=4321,
            ),
        )
    ]

    incomplete_id = await insert_build(
        pool,
        project_id,
        number=2,
        commit_sha="cancelled-during-eval-sha",
        status="cancelled",
        eval_completed=False,
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=incomplete_id,
        commit_sha="cancelled-during-eval-sha",
        branch="main",
        pr_number=None,
    )

    await service.request_build_report(incomplete_id)

    assert reporter.eval_cancellations == ["cancelled-during-eval-sha"]
    assert len(reporter.eval_reports) == 1


async def test_late_target_crash_recovers_terminal_eval_and_final(
    service: CIService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="pr-sha",
        branch="feature",
        pr_number=11,
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, status, finished_at) "
        "VALUES ($1, 'broken', 'failed', now())",
        build_id,
    )
    build = await builds_q.get_build(pool, id_=build_id)
    project = await service.repo_store.by_id(project_id)
    assert build is not None
    assert project is not None
    late = ChangeEvent(repo=repo_info(project), branch="main", commit_sha="main-sha")
    await service.orchestrator.record_report_target(late, build)
    # Simulated crash here: the target is durable, but terminal replay never ran.

    async def no_early_failures(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def only_this_terminal(*args: Any, **kwargs: Any) -> list[int]:
        return [build_id]

    async def no_unfinished_builds(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(
        builds_q, "reportable_attribute_failures", no_early_failures
    )
    monkeypatch.setattr(
        builds_q, "unreconciled_terminal_builds", only_this_terminal
    )
    monkeypatch.setattr("nixbot.service.find_unfinished_builds", no_unfinished_builds)
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter

    await service.recover_unfinished_builds()
    await service.drain_work()

    assert set(reporter.eval_results) == {("pr-sha", True), ("main-sha", True)}
    assert {commit for commit, _ in reporter.final_results} == {
        "pr-sha",
        "main-sha",
    }
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) == 0


async def test_late_terminal_target_gets_rich_failures_and_final_status(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="pr-sha",
        branch="feature",
        pr_number=9,
    )
    await builds_q.set_build_attribute_prefix(
        pool, build_id=build_id, attribute_prefix="hydraJobs"
    )
    await pool.execute(
        "INSERT INTO build_attributes "
        "(build_id, attr, system, drv_path, status, error, finished_at) "
        "VALUES ($1, 'x86_64-linux.broken', 'x86_64-linux', "
        "'/nix/store/broken.drv', 'failed', $2, now())",
        build_id,
        "error: persisted rich diagnostic",
    )
    build = await builds_q.get_build(pool, id_=build_id)
    project = await service.repo_store.by_id(project_id)
    assert build is not None
    assert project is not None
    event = ChangeEvent(repo=repo_info(project), branch="main", commit_sha="main-sha")
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter
    service.orchestrator.request_attribute_report = service.request_attribute_report
    service.orchestrator.request_build_report = service.request_build_report

    await service.orchestrator.record_report_target(event, build)
    await build_reuse.replay_terminal_status(service.orchestrator, event, build)
    await service.drain_work()

    main_results = [
        result for commit, result in reporter.final_results if commit == "main-sha"
    ]
    assert len(main_results) == 1
    assert main_results[0].attr_prefix == "hydraJobs"
    assert main_results[0].results[0].error == "error: persisted rich diagnostic"
    main_failures = [failure for failure in reporter.failures if failure[0] == "main-sha"]
    assert len(main_failures) == 1
    assert main_failures[0][2] == "error: persisted rich diagnostic"
    assert main_failures[0][5] == "hydraJobs"


async def test_attribute_report_hint_is_best_effort_but_preserves_cancellation(
    service: CIService,
) -> None:
    async def broken_enqueue(*args: Any, **kwargs: Any) -> None:
        msg = "database unavailable"
        raise RuntimeError(msg)

    service.enqueue_work = broken_enqueue  # type: ignore[method-assign]
    await service.request_attribute_report(1, "a")

    async def cancelled_enqueue(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    service.enqueue_work = cancelled_enqueue  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await service.request_attribute_report(1, "a")


async def test_live_reconciliation_repairs_queue_write_and_retry_update_failures(
    service: CIService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="terminal-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="terminal-sha",
        branch="main",
        pr_number=None,
    )

    class RecoveringReporter(TargetRecordingReporter):
        available = False

        async def build_finished(
            self, event: Any, build: Any, result: BuildResult
        ) -> None:
            if not self.available:
                msg = "GitHub Checks permission unavailable"
                raise CheckPermissionError(msg)
            await super().build_finished(event, build, result)

    reporter = RecoveringReporter()
    service.orchestrator.reporter = reporter
    service.orchestrator.request_build_report = service.request_build_report
    unreconciled_terminal_builds = builds_q.unreconciled_terminal_builds

    async def terminal_for_this_build(*args: Any, **kwargs: Any) -> list[int]:
        return [
            candidate
            for candidate in await unreconciled_terminal_builds(*args, **kwargs)
            if candidate == build_id
        ]

    monkeypatch.setattr(
        builds_q, "unreconciled_terminal_builds", terminal_for_this_build
    )

    original_enqueue = service.enqueue_work

    async def failed_enqueue(*args: Any, **kwargs: Any) -> None:
        msg = "queue database write failed"
        raise RuntimeError(msg)

    service.enqueue_work = failed_enqueue  # type: ignore[method-assign]
    await service.request_build_report(build_id)
    service.enqueue_work = original_enqueue  # type: ignore[method-assign]
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) is None
    assert await pool.fetchval(
        "SELECT count(*) FROM work_queue WHERE kind = 'report'"
    ) == 0

    queue = WorkQueue(pool)
    await service._reconcile_terminal_reports(queue)  # noqa: SLF001
    item = await queue.claim_next()
    assert item is not None
    original_retry = queue.retry

    async def failed_retry(*args: Any, **kwargs: Any) -> bool:
        msg = "retry update failed"
        raise RuntimeError(msg)

    queue.retry = failed_retry  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="retry update failed"):
        await service._execute_work(queue, item)  # noqa: SLF001
    queue.retry = original_retry  # type: ignore[method-assign]
    assert await pool.fetchval(
        "SELECT status FROM work_queue WHERE id = $1", item.id
    ) == "running"

    # The idle-loop lease sweep repairs the otherwise unleased running row
    # without requiring a process restart.
    await pool.execute(
        "UPDATE work_queue SET claimed_at = now() - interval '1 hour' "
        "WHERE id = $1",
        item.id,
    )
    reporter.available = True
    await service._reconcile_terminal_reports(queue)  # noqa: SLF001
    await service.drain_work()

    assert {commit for commit, _ in reporter.final_results} == {"terminal-sha"}
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) == 0


async def test_attribute_report_reloads_persisted_failure_retries_and_fans_out(
    service: CIService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="pr-sha",
        branch="feature",
        pr_number=7,
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="main-sha",
        branch="main",
        pr_number=None,
    )
    await builds_q.set_build_attribute_prefix(
        pool, build_id=build_id, attribute_prefix="hydraJobs"
    )
    await pool.execute(
        "INSERT INTO build_attributes "
        "(build_id, attr, system, drv_path, status, error, finished_at) "
        "VALUES ($1, 'x86_64-linux.broken', 'x86_64-linux', "
        "'/nix/store/broken.drv', 'failed', $2, now())",
        build_id,
        "error: rich executor diagnostic\nlast line",
    )
    reporter = AttributeRecordingReporter(fail_once=True)
    service.orchestrator.reporter = reporter

    # Startup recovery is intentionally database-wide. Keep this assertion
    # isolated from reportable rows created concurrently by other xdist workers.
    reportable_attribute_failures = builds_q.reportable_attribute_failures

    async def reportable_for_this_build(*args: Any, **kwargs: Any) -> list[Any]:
        return [
            row
            for row in await reportable_attribute_failures(*args, **kwargs)
            if row.build_id == build_id
        ]

    monkeypatch.setattr(
        builds_q, "reportable_attribute_failures", reportable_for_this_build
    )
    unreconciled_terminal_builds = builds_q.unreconciled_terminal_builds

    async def terminal_for_this_build(*args: Any, **kwargs: Any) -> list[int]:
        return [
            candidate
            for candidate in await unreconciled_terminal_builds(*args, **kwargs)
            if candidate == build_id
        ]

    monkeypatch.setattr(
        builds_q, "unreconciled_terminal_builds", terminal_for_this_build
    )

    await service.recover_unfinished_builds()
    await service.drain_work()

    assert reporter.calls == 3  # failed delivery, then both durable targets
    assert {failure[0] for failure in reporter.failures} == {"pr-sha", "main-sha"}
    assert all(
        "rich executor diagnostic" in (failure[2] or "")
        for failure in reporter.failures
    )
    assert all(failure[5] == "hydraJobs" for failure in reporter.failures)
    attempts = await pool.fetchval(
        "SELECT max(attempts) FROM work_queue WHERE kind = 'attribute-report'"
    )
    assert attempts == 1

    # Final/recovery reconciliation also uses persisted rich rows and durable
    # targets; it does not depend on the live linked-events list.
    await pool.execute(
        "UPDATE builds SET status = 'failed', status_generation = 1 WHERE id = $1",
        build_id,
    )
    await service._re_report(build_id)  # noqa: SLF001
    assert set(reporter.finished) == {
        ("pr-sha", "error: rich executor diagnostic\nlast line"),
        ("main-sha", "error: rich executor diagnostic\nlast line"),
    }

    # Supersession may be visible in memory just before DB cancellation
    # settlement; do not publish a stale red in that window.
    cancel_event = asyncio.Event()
    cancel_event.set()
    service.orchestrator.cancel_events[build_id] = cancel_event
    await service.request_attribute_report(build_id, "x86_64-linux.broken")
    await service.drain_work()
    assert reporter.calls == 3


async def test_terminal_recovery_reconciles_failure_beyond_early_slice(
    service: CIService, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="terminal-sha", status="failed"
    )
    await builds_q.record_build_report_target(
        pool,
        build_id=build_id,
        commit_sha="terminal-sha",
        branch="main",
        pr_number=None,
    )
    await builds_q.set_build_attribute_prefix(
        pool, build_id=build_id, attribute_prefix="hydraJobs"
    )
    attrs = [
        f"x86_64-linux.failure-{index:03d}"
        for index in range(service.config.failed_build_report_limit + 1)
    ]
    await pool.executemany(
        "INSERT INTO build_attributes "
        "(build_id, attr, status, error, finished_at) "
        "VALUES ($1, $2, 'failed', $3, now())",
        [(build_id, attr, f"rich diagnostic for {attr}") for attr in attrs],
    )
    early = await builds_q.reportable_attribute_failures(
        pool, report_limit=service.config.failed_build_report_limit
    )
    assert attrs[-1] not in {
        row.attr for row in early if row.build_id == build_id
    }

    async def no_early_failures(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def only_terminal_build(*args: Any, **kwargs: Any) -> list[int]:
        return [build_id]

    monkeypatch.setattr(
        builds_q, "reportable_attribute_failures", no_early_failures
    )
    monkeypatch.setattr(
        builds_q, "unreconciled_terminal_builds", only_terminal_build
    )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter

    await service.recover_unfinished_builds()
    await service.drain_work()

    assert len(reporter.final_results) == 1
    result = reporter.final_results[0][1]
    assert result.attr_prefix == "hydraJobs"
    assert {item.attr for item in result.results} == set(attrs)
    assert next(item for item in result.results if item.attr == attrs[-1]).error == (
        f"rich diagnostic for {attrs[-1]}"
    )
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) == await pool.fetchval(
        "SELECT status_generation FROM builds WHERE id = $1", build_id
    )


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


async def test_direct_cancel_fans_out_and_acknowledges_durable_targets(
    service: CIService,
) -> None:
    pool = service.pool
    project_id = await seed_project(pool, "http://example/repo")
    build_id = await insert_build(
        pool, project_id, commit_sha="pr-sha", status="building"
    )
    for commit_sha, branch, pr_number in (
        ("pr-sha", "feature", 4),
        ("main-sha", "main", None),
    ):
        await builds_q.record_build_report_target(
            pool,
            build_id=build_id,
            commit_sha=commit_sha,
            branch=branch,
            pr_number=pr_number,
        )
    reporter = TargetRecordingReporter()
    service.orchestrator.reporter = reporter
    service.orchestrator.request_build_report = service.request_build_report

    await service.cancel_build(build_id)

    assert {commit for commit, _ in reporter.final_results} == {
        "pr-sha",
        "main-sha",
    }
    generation = await pool.fetchval(
        "SELECT status_generation FROM builds WHERE id = $1", build_id
    )
    assert await pool.fetchval(
        "SELECT reported_generation FROM build_reporting WHERE build_id = $1",
        build_id,
    ) == generation


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
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, system, status) "
        "VALUES ($1, 'flaky', 'x86_64-linux', 'failed')",
        build_id,
    )
    store = CheckRunStore(pool)
    await store.set(project_id, "deadbeef", "nixbot/nix-build", None, 1)
    await store.set(project_id, "deadbeef", "nixbot/nix-build a", "flaky", 2)

    calls: list[tuple[int, str | None]] = []

    async def fake_restart(build_id: int, *, attr: str | None) -> None:
        calls.append((build_id, attr))

    service._restart = fake_restart  # type: ignore[method-assign,assignment]  # noqa: SLF001

    base = {"forge": "github", "forge_repo_id": forge_repo_id, "head_sha": "deadbeef"}
    await service.submit(
        CheckRerequested(**base, build_id=build_id, name="nixbot/nix-build a")
    )
    await service.submit(
        CheckRerequested(**base, build_id=build_id, name="nixbot/nix-build")
    )
    # check_suite: no build_id, no name. Resolved via LatestBuildForSha.
    await service.submit(CheckRerequested(**base))
    # external_id from another project's app must not be honoured.
    await service.submit(CheckRerequested(**base, build_id=99999999))
    assert calls == [
        (build_id, "flaky"),
        (build_id, None),
        (build_id, None),
        (build_id, None),
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
    """Direct attribute cancel must re-aggregate the build. Otherwise
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

    def config_with(names: list[str]) -> PullBasedConfig:
        return PullBasedConfig(
            repositories={
                name: PullBasedRepository(
                    name=name,
                    default_branch="main",
                    url=f"ssh://git@example.com/x/{name}.git",
                    ssh_private_key_file=key,
                )
                for name in names
            }
        )

    service, _app = await make_service(pull_based=config_with(["myrepo", "obsolete"]))
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

    # Disabled rows of repos removed from the config are pruned (issue #76)
    await pool.execute(
        "UPDATE projects SET enabled = FALSE "
        "WHERE forge = 'pull_based' AND forge_repo_id = 'obsolete'"
    )
    service.config = service.config.model_copy(
        update={"pull_based": config_with(["myrepo"])}
    )
    await service.discover_once()
    rows = {
        r["forge_repo_id"]: r["enabled"]
        for r in await pool.fetch(
            "SELECT * FROM projects WHERE forge = 'pull_based' "
            "AND forge_repo_id IN ('myrepo', 'obsolete')"
        )
    }
    assert rows == {"myrepo": True}


# --- discovery topic handling ----------------------------------------------


class StubGitHub:
    def __init__(self, repos: list[DiscoveredRepo]) -> None:
        self.repos = repos

    async def discover_repos(self) -> list[DiscoveredRepo]:
        return self.repos


async def test_topic_does_not_hard_filter_discovery(
    make_service: ServiceFactory, tmp_path: Path
) -> None:
    """The topic is a one-shot legacy import aid. Repos without it must
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

    reevals = capture_run_build(service)
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
    """A crash mid-evaluation leaves a partial attribute set. Resuming
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

    reevals = capture_run_build(service)
    resumes = capture_resume(service)
    await restart_dispatch.rerun(service, build_id)

    assert resumes == []
    assert reevals == [build_id]


async def test_rerun_resumes_building_rows_and_keeps_finished(
    service: CIService, git_repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that crashed with an attribute in 'building' resumes that
    attribute. Already-finished rows are kept, not re-evaluated away."""
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

    reevals = capture_run_build(service)
    resumed_attrs: list[str] = []

    async def fake_resume(
        info: Any, build: Any, jobs: Any, credentials: Any = None
    ) -> None:
        resumed_attrs.extend(job.attr for job in jobs)

    service.orchestrator.rerun_pending_attributes = fake_resume  # type: ignore[method-assign, assignment]
    await restart_dispatch.rerun(service, build_id)

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

    reevals = capture_run_build(service)
    resumes = capture_resume(service)
    # The real path checker: /nix/store/gcd.drv does not exist.
    await restart_dispatch.rerun(service, build_id)

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
        token="glpat-x",
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
