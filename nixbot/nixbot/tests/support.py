"""Shared test helpers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx

from nixbot.auth import SessionSigner
from nixbot.config import Config
from nixbot.db_gen import builds as builds_q
from nixbot.forge import GiteaClient, GitlabClient
from nixbot.migrations import apply_migrations
from nixbot.web.app import WebContext, create_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
    from pathlib import Path

    import pytest
    from fastapi import FastAPI

    from nixbot.auth import User
    from nixbot.build_scheduler import CachedFailure

from nixbot.models import CacheStatus, NixEvalJobSuccess


def make_config(dsn: str, state_dir: Path, **kwargs: Any) -> Config:
    """A minimal valid Config for service-level tests."""
    return Config(
        db_url=dsn,
        build_systems=["x86_64-linux"],
        url="http://ci.test",
        state_dir=state_dir,
        **kwargs,
    )


def mk_job(
    attr: str = "foo",
    *,
    deps: list[str] | None = None,
    cache_status: CacheStatus = CacheStatus.not_built,
    system: str = "x86_64-linux",
    out: str | None = None,
) -> NixEvalJobSuccess:
    """An eval result like nix-eval-jobs emits: needed_builds covers
    the job's own drv plus its dependencies."""
    drv = f"/nix/store/{attr}.drv"
    return NixEvalJobSuccess(
        attr=attr,
        attr_path=attr.split("."),
        cache_status=cache_status,
        needed_builds=[drv, *(f"/nix/store/{d}.drv" for d in (deps or []))],
        needed_substitutes=[],
        drv_path=drv,
        name=attr,
        outputs={"out": out or f"/nix/store/{attr}-out"},
        system=system,
    )


class FakeCache:
    """Failed-build cache fake: configurable lookups, records adds."""

    def __init__(self, entries: dict[str, CachedFailure] | None = None) -> None:
        self.entries = entries or {}
        self.added: list[tuple[str, str]] = []

    async def check(self, drv_path: str) -> CachedFailure | None:
        return self.entries.get(drv_path)

    async def add(self, drv_path: str, url: str) -> None:
        self.added.append((drv_path, url))


def gitea_client(handler: Callable[[httpx.Request], httpx.Response]) -> GiteaClient:
    """GiteaClient backed by an httpx mock transport."""
    return GiteaClient(
        "https://gitea.example.com",
        "tkn",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def gitlab_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    base_url: str = "https://gitlab.example.com",
    token: str = "tkn",  # noqa: S107 (test credential)
) -> GitlabClient:
    """GitlabClient backed by an httpx mock transport."""
    return GitlabClient(
        base_url,
        token,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class FakeGitea:
    """Minimal Gitea API fake: paged repo list, per-repo topics, and a
    mutable hooks store recording create/update/delete calls."""

    def __init__(
        self,
        repos: list[dict] | None = None,
        *,
        topics: dict[str, list[str]] | None = None,
        hooks: list[dict] | None = None,
        delete_status: int = 204,
    ) -> None:
        self.repos = repos or []
        self.topics = topics or {}
        self.hooks = hooks or []
        self.delete_status = delete_status
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.patched: list[tuple[str, dict]] = []
        self.topics_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        assert request.headers["Authorization"] == "token tkn"
        path = request.url.path
        if path == "/api/v1/user/repos":
            page = int(request.url.params["page"])
            return httpx.Response(200, json=self.repos if page == 1 else [])
        topics = re.fullmatch(r"/api/v1/repos/[^/]+/([^/]+)/topics", path)
        if topics:
            self.topics_requests += 1
            return httpx.Response(200, json={"topics": self.topics.get(topics[1])})
        if request.method == "GET" and path.endswith("/hooks"):
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(200, json=self.hooks if page == 1 else [])
        if request.method == "DELETE":
            self.deleted.append(path.rsplit("/", 1)[-1])
            return httpx.Response(self.delete_status)
        if request.method == "PATCH":
            self.patched.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            return httpx.Response(200, json={})
        if request.method == "POST" and path.endswith("/hooks"):
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    def client(self) -> GiteaClient:
        return gitea_client(self.handler)


class FakeGitlab:
    """Minimal GitLab API fake: project list and a hooks store
    recording created hooks."""

    def __init__(
        self,
        projects: list[dict] | None = None,
        *,
        token: str = "tkn",  # noqa: S107 (test credential)
    ) -> None:
        self.projects = projects or []
        self.hooks: list[dict] = []
        self.token = token
        self.created: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == self.token
        path = request.url.path
        if path == "/api/v4/projects" and request.method == "GET":
            return httpx.Response(200, json=self.projects)
        if path.endswith("/hooks") and request.method == "GET":
            return httpx.Response(200, json=self.hooks)
        if path.endswith("/hooks") and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    def client(self, *, base_url: str = "https://gitlab.example.com") -> GitlabClient:
        return gitlab_client(self.handler, base_url=base_url, token=self.token)


@contextlib.asynccontextmanager
async def db_pool(dsn: str, **kwargs: Any) -> AsyncIterator[asyncpg.Pool]:
    """asyncpg pool that is closed on exit."""
    pool = await asyncpg.create_pool(dsn, **kwargs)
    try:
        yield pool
    finally:
        await pool.close()


async def attribute_statuses(pool: asyncpg.Pool, build_id: int) -> dict[str, str]:
    """Attribute name -> status for assertions."""
    rows = await builds_q.attribute_statuses(pool, build_id=build_id)
    return {r.attr: r.status for r in rows}


def cookie_header(cookies: dict[str, str]) -> dict[str, str]:
    """Cookies as a request header (per-request cookies= is
    deprecated in httpx)."""
    return {"cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}


def run_sync[T](coro: Coroutine[None, None, T]) -> T:
    """asyncio.run in a fresh thread.

    Playwright's sync API keeps an asyncio loop running (greenlet-
    suspended) in the calling thread, so asyncio.run would fail there.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result(timeout=120)


@contextlib.contextmanager
def ephemeral_postgres(
    tmp_path_factory: pytest.TempPathFactory, dbname: str
) -> Iterator[str]:
    """Throwaway Postgres on a unix socket with migrations applied."""
    datadir = tmp_path_factory.mktemp("pgdata")
    sockdir = tmp_path_factory.mktemp("pgsock")
    subprocess.run(  # noqa: S603
        ["initdb", "-D", str(datadir), "-U", "test", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    proc = subprocess.Popen(  # noqa: S603
        ["postgres", "-D", str(datadir), "-k", str(sockdir), "-c", "listen_addresses="],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (sockdir / ".s.PGSQL.5432").exists():
            if time.monotonic() > deadline:
                msg = "postgres did not start"
                raise RuntimeError(msg)
            time.sleep(0.1)
        subprocess.run(  # noqa: S603
            ["createdb", "-h", str(sockdir), "-U", "test", dbname],
            check=True,
            capture_output=True,
        )
        dsn = f"postgresql://test@/{dbname}?host={sockdir}"
        run_sync(apply_migrations(dsn))
        yield dsn
    finally:
        proc.terminate()
        proc.wait()


def git(repo: Path, *args: str) -> str:
    """Run git with a hermetic identity/config environment."""
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
    ).stdout.strip()


def init_upstream(repo: Path, files: dict[str, str] | None = None) -> Path:
    """A fresh git repo on branch main with an initial commit."""
    repo.mkdir()
    git(repo, "init", "-b", "main")
    for name, content in (files or {"flake.nix": "{}"}).items():
        (repo / name).write_text(content)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    return repo


async def insert_project(  # noqa: PLR0913
    pool: asyncpg.Pool,
    name: str = "widget",
    *,
    forge: str = "github",
    forge_repo_id: str | None = None,
    owner: str = "acme",
    default_branch: str = "main",
    url: str = "u",
    private: bool = False,
) -> int:
    """An enabled projects row; idempotent on (forge, forge_repo_id)."""
    return await pool.fetchval(
        """
        INSERT INTO projects (forge, forge_repo_id, owner, name,
                              default_branch, url, private, enabled)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE)
        ON CONFLICT (forge, forge_repo_id) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        forge,
        forge_repo_id or name,
        owner,
        name,
        default_branch,
        url,
        private,
    )


async def insert_build(  # noqa: PLR0913
    pool: asyncpg.Pool,
    project_id: int,
    *,
    number: int = 1,
    branch: str = "main",
    commit_sha: str = "sha",
    tree_hash: str | None = None,
    status: str = "pending",
    pr_number: int | None = None,
    pr_author: str | None = None,
    effects_started: bool = False,
    error: str | None = None,
    started: bool = False,
    eval_completed: bool | None = None,
) -> int:
    """A builds row for the project.

    eval_completed defaults as in production: set once past
    pending/evaluating. Pass it explicitly to model a mid-eval crash or
    cancellation."""
    if eval_completed is None:
        eval_completed = status not in ("pending", "evaluating")
    return await pool.fetchval(
        """
        INSERT INTO builds (project_id, number, branch, commit_sha, tree_hash,
                            status, pr_number, pr_author, effects_started,
                            error, started_at, eval_completed)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                CASE WHEN $11 THEN now() END, $12)
        RETURNING id
        """,
        project_id,
        number,
        branch,
        commit_sha,
        tree_hash,
        status,
        pr_number,
        pr_author,
        effects_started,
        error,
        started,
        eval_completed,
    )


SESSION_COOKIE = "nixbot_session"


@dataclass
class WebHarness:
    """ASGI test client + event loop around the web app."""

    loop: asyncio.AbstractEventLoop
    http: httpx.AsyncClient
    app: FastAPI
    signer: SessionSigner

    @property
    def ctx(self) -> WebContext:
        return self.app.state.web_context  # type: ignore[no-any-return]

    def run[T](self, coro: Coroutine[None, None, T]) -> T:
        return self.loop.run_until_complete(coro)

    def _cookies(self, user: User | None, token: str) -> dict[str, str]:
        if user is None:
            return {}
        session_id = None
        if token:
            # Forge tokens are stored server-side, keyed by session id.
            vault = self.ctx.forge_tokens
            assert vault is not None
            session_id = secrets.token_urlsafe(8)
            self.run(vault.save(session_id, token, 3600))
        return {SESSION_COOKIE: self.signer.session_for(user, session_id)}

    def get(
        self,
        url: str,
        user: User | None = None,
        token: str = "",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = cookie_header(self._cookies(user, token)) | (headers or {})
        return self.run(self.http.get(url, headers=request_headers))

    def post(
        self,
        url: str,
        user: User | None = None,
        origin: str = "http://test",
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = (
            ({"Origin": origin} if origin else {})
            | cookie_header(self._cookies(user, ""))
            | (headers or {})
        )
        return self.run(self.http.post(url, headers=request_headers, data=data))


@contextlib.contextmanager
def web_harness(
    dsn: str,
    *,
    configure: Callable[[FastAPI, asyncpg.Pool], None] | None = None,
) -> Iterator[WebHarness]:
    """Web app harness on a dedicated event loop."""
    signer = SessionSigner([b"test-key"])
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def setup() -> tuple[asyncpg.Pool, httpx.AsyncClient, FastAPI]:
        pool = await asyncpg.create_pool(dsn)
        app = create_app(pool)
        app.state.web_context.signer = signer
        if configure is not None:
            configure(app, pool)
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        return pool, client, app

    pool, client, app = loop.run_until_complete(setup())
    try:
        yield WebHarness(loop, client, app, signer)
    finally:
        loop.run_until_complete(client.aclose())
        loop.run_until_complete(pool.close())
        asyncio.set_event_loop(None)
        loop.close()


def truncate_work_queue(dsn: str) -> None:
    """Per-test isolation for modules sharing one database: leftover
    queued work would be executed by another test's drain_work."""

    async def run() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute("TRUNCATE work_queue")
        finally:
            await conn.close()

    run_sync(run())
