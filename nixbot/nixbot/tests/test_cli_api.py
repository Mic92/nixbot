"""nixbot_cli.api against the real web app via the ASGI transport."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from nixbot_cli.api import ApiError, NixbotClient, RepoRef

from nixbot.api_tokens import ApiTokenStore
from nixbot.auth import AuthzConfig, User
from nixbot.executor import attribute_log_path, container_path
from nixbot.logstore import LogContainerWriter
from nixbot.web.control_routes import create_control_api_router

from .support import WebHarness, db_pool, insert_build, insert_project, web_harness
from .test_control import FakeBackend

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi import FastAPI

REPO = RepoRef("github", "acme", "widget")
ROOT = User(provider="github", username="root")
AUTHZ = AuthzConfig(admins=["github:root"])


BACKEND = FakeBackend()


@pytest.fixture(scope="module")
async def postgres_dsn(postgres_dsn: str) -> str:
    async with db_pool(postgres_dsn) as pool:
        project_id = await insert_project(pool, forge_repo_id="cli-1")
        failed = await insert_build(pool, project_id, number=1, status="failed")
        await insert_build(pool, project_id, number=2, status="building", started=True)
        await pool.execute(
            """
            INSERT INTO build_attributes
              (build_id, attr, system, status, error, finished_at) VALUES
              ($1, 'good', 'x86_64-linux', 'succeeded', NULL, '2026-01-01T00:00:01Z'),
              ($1, 'bad1', 'x86_64-linux', 'failed', 'builder failed',
               '2026-01-01T00:00:02Z')
            """,
            failed,
        )
    return postgres_dsn


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[WebHarness]:
    def configure(app: FastAPI) -> None:
        ctx = app.state.web_context
        ctx.authz = AUTHZ
        ctx.token_store = ApiTokenStore(ctx.pool)
        app.include_router(
            create_control_api_router(ctx, BACKEND, AUTHZ, "http://test")
        )

    with web_harness(postgres_dsn, configure=configure) as h:
        yield h


class _SyncASGITransport(httpx.BaseTransport):
    """Bridges the CLI's sync httpx.Client onto the in-process app."""

    def __init__(self, harness: WebHarness) -> None:
        self.harness = harness
        self.asgi = httpx.ASGITransport(app=harness.app)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def send() -> httpx.Response:
            response = await self.asgi.handle_async_request(request)
            await response.aread()
            return response

        response = self.harness.run(send())
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )


def make_client(harness: WebHarness, token: str | None = None) -> NixbotClient:
    http = httpx.Client(transport=_SyncASGITransport(harness), base_url="http://test")
    return NixbotClient(token=token, http=http)


@pytest.fixture(scope="module")
def api(harness: WebHarness) -> NixbotClient:
    """Client authenticated with an admin's personal API token."""
    store = harness.ctx.token_store
    assert store is not None
    return make_client(harness, token=harness.run(store.create(ROOT, "cli")))


def test_repo_ref_parse() -> None:
    assert RepoRef.parse("github/acme/widget") == REPO
    assert str(REPO) == "github/acme/widget"
    with pytest.raises(ValueError, match="forge/owner/name"):
        RepoRef.parse("acme/widget")


def test_repos_and_builds(api: NixbotClient) -> None:
    assert [(r["owner"], r["name"]) for r in api.repos()] == [("acme", "widget")]
    assert api.repo(REPO)["enabled"] is True
    with pytest.raises(ApiError) as err:
        api.repo(RepoRef("github", "acme", "nope"))
    assert err.value.status == 404

    page = api.builds(REPO)
    assert {b["number"] for b in page["items"]} == {1, 2}
    assert [b["number"] for b in api.builds(REPO, status="failed")["items"]] == [1]

    detail = api.build(REPO, 1)
    assert detail["build"]["status"] == "failed"
    assert {a["attr"] for a in detail["attributes"]} == {"good", "bad1"}

    failures = api.failures(REPO, 1)
    assert [f["attr"] for f in failures["failures"]] == ["bad1"]
    assert [e["build_number"] for e in api.attr_history(REPO, "bad1")] == [1]
    assert [b["number"] for b in api.queue()] == [2]


def test_finished_attrs_cursor(api: NixbotClient) -> None:
    delta = api.finished_attrs(REPO, 1)
    assert delta["build"]["status"] == "failed"
    assert [a["attr"] for a in delta["items"]] == ["good", "bad1"]

    good = delta["items"][0]
    rest = api.finished_attrs(
        REPO, 1, finished_after=good["finished_at"], after_id=good["id"]
    )
    assert [a["attr"] for a in rest["items"]] == ["bad1"]


def test_control_requires_token(harness: WebHarness, api: NixbotClient) -> None:
    with pytest.raises(ApiError) as err:
        make_client(harness).restart_build(REPO, 1)
    assert err.value.status == 403

    assert api.restart_build(REPO, 1)["action"] == "restart"
    assert api.cancel_build(REPO, 1)["action"] == "cancel"
    assert api.restart_attr(REPO, 1, "bad1")["attr"] == "bad1"
    assert api.cancel_attr(REPO, 1, "bad1")["attr"] == "bad1"
    assert api.restart_effects(REPO, 1)["action"] == "restart-effects"
    assert len(BACKEND.restarted) == 1
    assert [a for _, a in BACKEND.attr_restarts] == ["bad1"]
    assert [a for _, a in BACKEND.attr_cancels] == ["bad1"]

    with pytest.raises(ApiError) as err:
        api.restart_attr(REPO, 1, "nope")
    assert err.value.status == 404


def test_enable_disable(api: NixbotClient) -> None:
    assert api.set_enabled(REPO, enabled=False)["enabled"] is False
    assert api.repo(REPO)["enabled"] is False
    assert api.set_enabled(REPO, enabled=True)["enabled"] is True


def test_log_toc_and_text(
    harness: WebHarness, api: NixbotClient, tmp_path: Path
) -> None:
    async def seed() -> None:
        harness.ctx.state_dir = tmp_path
        build_id = await harness.ctx.pool.fetchval(
            "SELECT id FROM builds WHERE number = 1"
        )
        log_file = attribute_log_path(tmp_path, build_id, "bad1")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        w = LogContainerWriter()
        w.register("/nix/store/aaa-hello-1.0.drv", "hello-1.0")
        w.line("/nix/store/aaa-hello-1.0.drv", "CC main.o")
        w.line("/nix/store/aaa-hello-1.0.drv", "error: boom")
        w.status("/nix/store/aaa-hello-1.0.drv", "failed")
        container_path(log_file).write_bytes(w.finalize())

    harness.run(seed())
    toc = api.log_toc(REPO, 1, "bad1")
    assert [d["name"] for d in toc["derivations"]] == ["hello-1.0"]

    text = api.log_text(REPO, 1, "bad1", drv="hello", tail=1)
    assert text == "error: boom\n"
    with pytest.raises(ApiError) as err:
        api.log_text(REPO, 1, "bad1", drv="nope")
    assert err.value.status == 404
