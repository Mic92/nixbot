"""Tangled support: the Buildkite-compatible API tack drives and the
webhooks nixbot sends back (tangled.py, web/tangled_routes.py)."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)
from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from pydantic import ValidationError

from nixbot import db
from nixbot.bootstrap import build_service
from nixbot.config import TangledConfig, TangledRepository
from nixbot.db import BuildStatus
from nixbot.db_gen import builds as builds_q
from nixbot.events import BuildResult, event_for_build
from nixbot.repos import repo_info
from nixbot.tangled import owner_and_name

from .support import git, make_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nixbot.events import ChangeEvent
    from nixbot.service import CIService

pytestmark = [
    pytest.mark.usefixtures("fresh_work_queue"),
    pytest.mark.skipif(shutil.which("git") is None, reason="git not available"),
]

SECRET = "hook-secret"
BUILDS = "/buildkite/v2/organizations/nixbot/pipelines/widget/builds"
META = {"tack:knot": "knot.test", "tack:pipeline_rkey": "3k", "tack:workflow": "ci.yml"}


@dataclass
class Tack:
    """A nixbot serving one Tangled repository, seen from tack's side."""

    service: CIService
    api: httpx.AsyncClient
    upstream: Path
    # Webhook deliveries as (X-Buildkite-Event, payload).
    hooks: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Events of the builds that ran.
    built: list[ChangeEvent] = field(default_factory=list)
    outcome: str = BuildStatus.SUCCEEDED
    hook_status: int = 200

    @property
    def head(self) -> str:
        return git(self.upstream, "rev-parse", "HEAD")

    def commit_on(self, branch: str) -> None:
        git(self.upstream, "checkout", "-b", branch)
        (self.upstream / "flake.nix").write_text("{ outputs = _: { x = 1; }; }")
        git(self.upstream, "commit", "-am", branch)

    async def create(self, commit: str, branch: str = "main", **extra: Any) -> dict:
        response = await self.api.post(
            BUILDS,
            json={"commit": commit, "branch": branch, "meta_data": META, **extra},
        )
        assert response.status_code == 201, response.text
        return response.json()

    async def get(self, number: int) -> dict:
        response = await self.api.get(f"{BUILDS}/{number}")
        assert response.status_code == 200, response.text
        return response.json()

    async def settle(self) -> None:
        """Run the queued work and deliver the webhooks it caused."""
        assert self.service.tangled is not None
        await self.service.drain_work()
        await self.service.tangled.deliver_pending()


@pytest.fixture
async def tack(
    postgres_dsn: str, tmp_path: Path, upstream: Path
) -> AsyncIterator[Tack]:
    (tmp_path / "token").write_text("api-token\n")
    (tmp_path / "secret").write_text(SECRET)
    service, app = await build_service(
        make_config(
            postgres_dsn,
            tmp_path / "state",
            tangled=TangledConfig(
                api_token_file=tmp_path / "token",
                webhook_url="http://tack.test/webhooks/buildkite",
                webhook_secret_file=tmp_path / "secret",
                repositories={"widget": TangledRepository(url=str(upstream))},
            ),
        )
    )
    await service.pool.execute("DELETE FROM projects WHERE forge = 'tangled'")
    await service.discover_once()
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ci.test",
        headers={"Authorization": "Bearer api-token"},
    )
    t = Tack(service, api, upstream)

    def webhook(request: httpx.Request) -> httpx.Response:
        if t.hook_status != 200:
            return httpx.Response(t.hook_status)
        # What tack's VerifySignature checks.
        parts = dict(
            p.split("=", 1) for p in request.headers["X-Buildkite-Signature"].split(",")
        )
        expected = hmac.new(
            SECRET.encode(),
            parts["timestamp"].encode() + b"." + request.content,
            hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(parts["signature"], expected)
        t.hooks.append(
            (request.headers["X-Buildkite-Event"], json.loads(request.content))
        )
        return httpx.Response(200)

    assert service.tangled is not None
    service.tangled.http = httpx.AsyncClient(transport=httpx.MockTransport(webhook))

    async def run_build(
        event: ChangeEvent,
        build: Any,
        worktree_path: Path,  # noqa: ARG001
        credentials: Any = None,  # noqa: ARG001
    ) -> None:
        t.built.append(event)
        reporter = service.orchestrator.reporter
        await reporter.build_started(event, build)
        await db.set_build_status(service.pool, build.id_, t.outcome)
        await reporter.build_finished(event, build, BuildResult(t.outcome, 0, []))

    service.orchestrator.run_build = run_build  # type: ignore[method-assign]
    yield t
    await api.aclose()
    await service.pool.close()


async def test_push_is_built_and_reported(tack: Tack) -> None:
    created = await tack.create(tack.head)
    assert created["state"] == "scheduled"
    assert created["meta_data"] == META

    await tack.settle()

    assert [e.commit_sha for e in tack.built] == [tack.head]
    # Tack only needs the state the build is in now.
    assert [event for event, _ in tack.hooks] == ["build.finished"]
    payload = tack.hooks[0][1]
    assert payload["organization"] == {"slug": "nixbot"}
    build = payload["build"]
    assert build["id"] == created["id"]
    assert build["state"] == "passed"
    # Tack recovers its pipeline from these when its own record is late.
    assert build["meta_data"] == META
    assert build["pipeline"]["slug"] == "widget"

    fetched = await tack.get(created["number"])
    assert fetched["state"] == "passed"
    assert fetched["web_url"].startswith("http://ci.test/repos/tangled/")
    assert fetched["web_url"].endswith("/builds/1")
    assert [job["name"] for job in fetched["jobs"]] == ["nix-eval"]

    # Nothing left to deliver.
    await tack.settle()
    assert len(tack.hooks) == 1


async def test_api_requires_the_token_and_a_configured_pipeline(tack: Tack) -> None:
    body = {"commit": tack.head, "branch": "main"}
    for headers in ({"Authorization": "Bearer wrong"}, {"Authorization": ""}):
        response = await tack.api.post(BUILDS, json=body, headers=headers)
        assert response.status_code == 401
    for url in (
        BUILDS.replace("widget", "other"),
        BUILDS.replace("nixbot", "other-org"),
    ):
        assert (await tack.api.post(url, json=body)).status_code == 404
    assert (await tack.api.get(f"{BUILDS}/9999")).status_code == 404
    # An abbreviated commit could name another object later.
    response = await tack.api.post(BUILDS, json={**body, "commit": "abc123"})
    assert response.status_code == 422


async def test_failed_build_is_reported_and_retry_runs_it_again(tack: Tack) -> None:
    tack.outcome = BuildStatus.FAILED
    first = await tack.create(tack.head)
    await tack.settle()
    assert tack.hooks[-1][1]["build"]["state"] == "failed"

    restarted: list[int] = []

    async def restart_build(build_id: int) -> None:
        restarted.append(build_id)

    tack.service.restart_build = restart_build  # type: ignore[method-assign]
    # Tack's retry is a second build for the same commit.
    second = await tack.create(tack.head)
    assert second["id"] != first["id"]
    await tack.settle()
    assert len(restarted) == 1
    assert len(tack.built) == 1

    # The rerun reports with the identity stored on the nixbot build.
    pool = tack.service.pool
    build = await builds_q.get_build(pool, id_=restarted[0])
    assert build is not None
    project = await tack.service.repo_store.by_id(build.project_id)
    assert project is not None
    event = event_for_build(repo_info(project), build)
    reporter = tack.service.orchestrator.reporter
    await reporter.build_restarted(event, build, None)
    await tack.settle()
    assert tack.hooks[-1][0] == "build.running"
    assert tack.hooks[-1][1]["build"]["id"] == second["id"]
    await reporter.build_finished(
        event, build, BuildResult(BuildStatus.SUCCEEDED, 1, [])
    )
    await tack.settle()
    assert (await tack.get(second["number"]))["state"] == "passed"


async def test_branches_build_without_a_build_branches_entry(tack: Tack) -> None:
    """The workflow's `when:` chose the branch. nixbot's own branch
    filter would leave the pipeline pending forever."""
    tack.commit_on("feature")

    await tack.create(tack.head, "feature")
    await tack.settle()

    assert [(e.branch, e.pr_number) for e in tack.built] == [("feature", None)]


async def test_pull_request_builds_the_merge_with_its_target(tack: Tack) -> None:
    tack.commit_on("feature")

    created = await tack.create(tack.head, "feature", pull_request_base_branch="main")
    assert created["pull_request"] == {"id": "1", "base": "main"}
    await tack.settle()

    (event,) = tack.built
    assert (event.branch, event.pr_number) == ("main", 1)
    assert event.base_sha == "refs/heads/main"
    assert tack.hooks[-1][1]["build"]["state"] == "passed"
    # Tack gets the branch back as it sent it.
    assert tack.hooks[-1][1]["build"]["branch"] == "feature"

    # The same pull request keeps its number, another one gets the next.
    forge = tack.service.tangled
    assert forge is not None
    project_id = event.repo.id
    assert await forge.pull_number(project_id, "feature", "main") == 1
    assert await forge.pull_number(project_id, "other", "main") == 2


async def test_change_without_a_build_ends_the_pipeline(tack: Tack) -> None:
    """A commit nixbot cannot fetch never becomes a build. Tack must
    still hear an end, with the reason as the log."""
    created = await tack.create("1" * 40)
    await tack.settle()

    assert tack.built == []
    event, payload = tack.hooks[-1]
    assert (event, payload["build"]["state"]) == ("build.finished", "failed")
    (job,) = payload["build"]["jobs"]
    assert (job["name"], job["state"]) == ("nixbot", "failed")
    log = await tack.api.get(f"{BUILDS}/{created['number']}/jobs/{job['id']}/log")
    assert log.status_code == 200
    assert "1111111" in log.text


async def test_failed_webhook_is_delivered_later(tack: Tack) -> None:
    tack.hook_status = 503
    created = await tack.create(tack.head)
    await tack.settle()
    assert tack.hooks == []

    tack.hook_status = 200
    await tack.settle()
    assert [p["build"]["id"] for _, p in tack.hooks] == [created["id"]]


async def test_jobs_are_the_attributes_that_ran(tack: Tack) -> None:
    created = await tack.create(tack.head)
    await tack.settle()
    pool = tack.service.pool
    build_id = await pool.fetchval("SELECT id FROM builds")
    await pool.executemany(
        "INSERT INTO build_attributes (build_id, attr, status, cached, error) "
        "VALUES ($1, $2, $3, $4, $5)",
        [
            (build_id, "checks.broken", "failed_eval", False, "error: boom"),
            (build_id, "checks.built", "succeeded", False, None),
            (build_id, "checks.cached", "succeeded", True, None),
            (build_id, "checks.local", "skipped_local", True, None),
        ],
    )

    jobs = (await tack.get(created["number"]))["jobs"]
    assert [(j["name"], j["state"], j["type"]) for j in jobs] == [
        ("nix-eval", "passed", "script"),
        ("checks.broken", "failed", "script"),
        ("checks.built", "passed", "script"),
    ]

    async def log(job: dict) -> httpx.Response:
        return await tack.api.get(f"{BUILDS}/{created['number']}/jobs/{job['id']}/log")

    assert "4 attributes, 2 cached" in (await log(jobs[0])).text
    assert (await log(jobs[1])).text == "error: boom\n"
    # Built without output so far: tack shows an empty step.
    assert (await log(jobs[2])).status_code == 404
    assert (await log({"id": "no-such-job"})).status_code == 404


async def test_pipeline_runs_until_effects_are_done(tack: Tack) -> None:
    created = await tack.create(tack.head)
    await tack.settle()
    (event,) = tack.built
    build = await builds_q.get_build(
        tack.service.pool, id_=await tack.service.pool.fetchval("SELECT id FROM builds")
    )
    assert build is not None
    reporter = tack.service.orchestrator.reporter

    await reporter.effects_started(event, build, 1)
    await tack.settle()
    assert tack.hooks[-1][0] == "build.running"

    await reporter.effects_finished(event, build, failed=1, succeeded=0)
    await tack.settle()
    assert tack.hooks[-1][0] == "build.finished"
    assert (await tack.get(created["number"]))["state"] == "failed"


async def test_tag_push_reports_the_build_of_its_commit(tack: Tack) -> None:
    await tack.create(tack.head)
    await tack.settle()
    git(tack.upstream, "tag", "-a", "v1", "-m", "release")
    tag_object = git(tack.upstream, "rev-parse", "v1")
    assert tag_object != tack.head

    # Tack sends the pushed ref's new value, the tag object.
    created = await tack.create(tag_object, "refs/tags/v1")
    await tack.settle()

    assert len(tack.built) == 1
    fetched = await tack.get(created["number"])
    assert fetched["state"] == "passed"
    assert fetched["branch"] == "refs/tags/v1"


async def test_repositories_become_projects(tack: Tack) -> None:
    row = await tack.service.pool.fetchrow(
        "SELECT * FROM projects WHERE forge = 'tangled'"
    )
    assert row is not None
    assert row["forge_repo_id"] == "widget"
    assert row["enabled"] is True
    assert row["default_branch"] == "main"
    assert row["url"] == str(tack.upstream)


def test_owner_and_name_come_from_the_url() -> None:
    assert owner_and_name("w", "https://tangled.org/alice.example/widget") == (
        "alice.example",
        "widget",
    )
    assert owner_and_name("w", "https://tangled.org/@alice.example/widget.git") == (
        "alice.example",
        "widget",
    )
    assert owner_and_name("w", "https://example.org/widget") == ("tangled", "w")


def test_pipeline_slug_must_be_a_path_segment(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="pipeline slug"):
        TangledConfig(
            api_token_file=tmp_path / "t",
            webhook_url="http://tack.test",
            webhook_secret_file=tmp_path / "s",
            repositories={"a/b": TangledRepository(url="https://tangled.org/a/b")},
        )
