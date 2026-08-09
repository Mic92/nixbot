"""Private-project visibility tests: anonymous and
unauthorized access on HTML, fragment, log, and SSE endpoints."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import httpx
import pytest

from nixbot.api_tokens import ApiTokenStore
from nixbot.auth import AuthzConfig, User
from nixbot.forge import GiteaClient, GitHubAppClient, GitlabClient
from nixbot.visibility import (
    AccessCache,
    BotRepoAccessFetcher,
    RepoAccess,
    RepoRef,
    VisibilityService,
)
from nixbot.web.auth_routes import SESSION_COOKIE, create_auth_router

from .support import (
    WebHarness,
    cookie_header,
    db_pool,
    insert_build,
    insert_project,
    web_harness,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from fastapi import FastAPI


class FakeFetcher:
    """Grants per-user repo access keyed by qualified username."""

    def __init__(
        self,
        grants: dict[str, frozenset[str]],
        admin_grants: dict[str, frozenset[str]] | None = None,
        writable_grants: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.grants = grants
        self.admin_grants = admin_grants or {}
        self.writable_grants = writable_grants or {}
        self.calls = 0

    async def repo_access(
        self,
        user: User,
        repos: Sequence[RepoRef],  # noqa: ARG002
    ) -> RepoAccess:
        self.calls += 1
        key = user.qualified
        return RepoAccess(
            accessible=self.grants.get(key, frozenset()),
            admin=self.admin_grants.get(key, frozenset()),
            writable=self.writable_grants.get(key, frozenset()),
        )


@pytest.fixture(scope="module")
async def postgres_dsn(postgres_dsn: str) -> str:
    await seed(postgres_dsn)
    return postgres_dsn


async def seed(dsn: str) -> None:
    async with db_pool(dsn) as pool:
        for repo_id, name, private in [
            ("pub-1", "public", False),
            ("priv-1", "secret", True),
            ("pending-1", "pending", True),
        ]:
            project_id = await insert_project(
                pool, name, forge_repo_id=repo_id, private=private
            )
            build_id = await insert_build(pool, project_id, status="succeeded")
            await pool.execute(
                "INSERT INTO build_attributes (build_id, attr, system, status) "
                "VALUES ($1, 'a.x', 'x86_64-linux', 'succeeded')",
                build_id,
            )
        # A discovered-but-disabled repo: only admins should see it, and
        # without needing a search query.
        await pool.execute(
            "UPDATE projects SET enabled = FALSE WHERE forge_repo_id = 'pending-1'"
        )


FETCHER = FakeFetcher({"github:carol": frozenset({"github:priv-1"})})


@pytest.fixture(scope="module")
def harness(postgres_dsn: str) -> Iterator[WebHarness]:
    def configure(app: FastAPI) -> None:
        ctx = app.state.web_context
        ctx.visibility = VisibilityService(
            ctx.pool,
            AuthzConfig(admins=["github:root"]),
            fetcher=FETCHER,
            cache=AccessCache(ttl=3600),
        )

    with web_harness(postgres_dsn, configure=configure) as h:
        yield h


CAROL = User(provider="github", username="carol")
MALLORY = User(provider="github", username="mallory")
ROOT = User(provider="github", username="root")


def test_anonymous_sees_public_only(harness: WebHarness) -> None:
    home = harness.get("/")
    assert "acme/public" in home.text
    assert "secret" not in home.text  # name leak check
    assert harness.get("/repos/github/acme/public").status_code == 200
    assert harness.get("/repos/github/acme/secret").status_code == 404
    assert harness.get("/repos/github/acme/secret/builds/1").status_code == 404
    # Log + SSE endpoints hidden too.
    assert harness.get("/repos/github/acme/secret/builds/1/logs/a.x").status_code == 404
    assert (
        harness.get("/repos/github/acme/secret/builds/1/logs/a.x/stream").status_code
        == 404
    )
    assert (
        harness.get("/repos/github/acme/secret/builds/1/attributes").status_code == 404
    )


def test_unauthorized_user_sees_public_only(harness: WebHarness) -> None:
    assert harness.get("/repos/github/acme/secret", MALLORY).status_code == 404
    home = harness.get("/", MALLORY)
    assert "secret" not in home.text


def test_authorized_user_sees_private(harness: WebHarness) -> None:
    assert harness.get("/repos/github/acme/secret", CAROL).status_code == 200
    home = harness.get("/", CAROL)
    assert "acme/secret" in home.text


def test_admin_sees_everything(harness: WebHarness) -> None:
    assert harness.get("/repos/github/acme/secret", ROOT).status_code == 200


def test_admin_sees_disabled_repos_without_search(harness: WebHarness) -> None:
    home = harness.get("/", ROOT).text
    assert "Disabled" in home
    assert "acme/pending" in home


def test_non_admin_does_not_see_disabled_repos(harness: WebHarness) -> None:
    # Anonymous and unauthorized users have an empty toggleable set, so
    # the disabled list stays hidden.
    assert "acme/pending" not in harness.get("/").text
    assert "acme/pending" not in harness.get("/", MALLORY).text


def test_api_token_sees_private(harness: WebHarness) -> None:
    """Personal API tokens carry no forge OAuth token; access is
    checked with the bot's credentials instead (issue #109)."""
    ctx = harness.ctx
    ctx.token_store = ApiTokenStore(ctx.pool)
    token = harness.run(ctx.token_store.create(CAROL, "cli"))
    response = harness.get(
        "/repos/github/acme/secret", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    # A token of a user without forge access stays out.
    mallory_token = harness.run(ctx.token_store.create(MALLORY, "cli"))
    response = harness.get(
        "/repos/github/acme/secret",
        headers={"Authorization": f"Bearer {mallory_token}"},
    )
    assert response.status_code == 404


def test_admin_api_token_sees_private(harness: WebHarness) -> None:
    # Bearer tokens carry the owner's identity: an admin token may
    # read private projects.
    ctx = harness.ctx
    ctx.token_store = ApiTokenStore(ctx.pool)
    token = harness.run(ctx.token_store.create(ROOT, "admin-script"))
    response = harness.get(
        "/repos/github/acme/secret", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


class FailingFetcher:
    def __init__(self) -> None:
        self.fail = True
        self.calls = 0

    async def repo_access(
        self,
        user: User,  # noqa: ARG002
        repos: Sequence[RepoRef],  # noqa: ARG002
    ) -> RepoAccess:
        self.calls += 1
        if self.fail:
            msg = "forge down"
            raise httpx.ConnectError(msg)
        return RepoAccess(frozenset({"github:priv-1"}))


def test_forge_repo_admins_can_toggle_their_repos(harness: WebHarness) -> None:
    ctx = harness.ctx
    fetcher = FakeFetcher(
        grants={"github:carol": frozenset({"github:priv-1"})},
        admin_grants={"github:carol": frozenset({"github:priv-1"})},
    )
    service = VisibilityService(
        ctx.pool,
        AuthzConfig(admins=["github:root"]),
        fetcher=fetcher,
        cache=AccessCache(ttl=3600),
    )

    async def run() -> None:
        # Instance admin: everything (None).
        assert await service.toggleable_repo_ids(ROOT) is None
        # Repo admin: exactly their repo.
        ids = await service.toggleable_repo_ids(CAROL)
        assert ids is not None
        assert len(ids) == 1
        # Access without forge-admin permission: nothing.
        assert await service.toggleable_repo_ids(MALLORY) == []
        # Anonymous: nothing.
        assert await service.toggleable_repo_ids(None) == []

    harness.run(run())


def test_forge_repo_writers_can_control_their_repos(harness: WebHarness) -> None:
    """Repo write access (push/maintain/admin) grants restart/cancel even
    without instance-admin or PR-author rights (issue #52)."""
    ctx = harness.ctx
    fetcher = FakeFetcher(
        grants={"github:carol": frozenset({"github:priv-1"})},
        writable_grants={"github:carol": frozenset({"github:priv-1"})},
    )
    service = VisibilityService(
        ctx.pool,
        AuthzConfig(admins=["github:root"]),
        fetcher=fetcher,
        cache=AccessCache(ttl=3600),
    )

    async def run() -> None:
        # Instance admin: everything (None).
        assert await service.controllable_repo_ids(ROOT) is None
        # Repo writer: exactly their repo.
        ids = await service.controllable_repo_ids(CAROL)
        assert ids is not None
        assert len(ids) == 1
        # Read-only access (no write grant): nothing.
        assert await service.controllable_repo_ids(MALLORY) == []
        # Anonymous: nothing.
        assert await service.controllable_repo_ids(None) == []

    harness.run(run())


def test_fetch_errors_are_not_cached(harness: WebHarness) -> None:
    """A transient forge failure must not poison the access cache:
    the next request retries and sees the private project again."""
    ctx = harness.ctx
    fetcher = FailingFetcher()
    service = VisibilityService(
        ctx.pool,
        AuthzConfig(admins=[]),
        fetcher=fetcher,
        cache=AccessCache(ttl=3600),
    )

    async def run() -> None:
        # While the forge errors: public-only, nothing cached.
        first = await service.visible_repo_ids(CAROL)
        assert first is not None
        assert len(first) == 1
        fetcher.fail = False
        second = await service.visible_repo_ids(CAROL)
        assert second is not None
        assert len(second) == 2
        assert fetcher.calls == 2

    harness.run(run())


def test_access_cache_used(harness: WebHarness) -> None:
    calls_before = FETCHER.calls
    harness.get("/repos/github/acme/secret", CAROL)
    harness.get("/repos/github/acme/secret", CAROL)
    # TTL cache: at most one fetch for repeated requests.
    assert FETCHER.calls <= calls_before + 1


def test_cache_negative_results() -> None:
    cache = AccessCache(ttl=60)
    empty = RepoAccess()
    assert cache.get("u") is None
    cache.set("u", empty)
    assert cache.get("u") == empty
    cache.invalidate("u")
    assert cache.get("u") is None


def test_metrics_unauthenticated_no_private_names(harness: WebHarness) -> None:
    response = harness.get("/metrics")
    assert response.status_code == 200
    assert "nixbot_builds" in response.text
    assert "nixbot_queue_depth" in response.text
    assert "nixbot_projects" in response.text
    # No private repo names leak into metrics.
    assert "secret" not in response.text


def test_configured_viewers_see_private(harness: WebHarness) -> None:
    """privateRepoViewers grants visibility to users without forge
    accounts (e.g. OIDC logins)."""
    ctx = harness.ctx
    assert ctx.visibility is not None
    saved = ctx.visibility.authz
    ctx.visibility.authz = AuthzConfig(
        admins=["github:root"],
        private_repo_viewers={
            "github:acme/secret": [
                "oidc:idp:*",
                "oidc:idp:group:auditors",
            ]
        },
    )
    try:
        idp_user = User(provider="oidc:idp", username="dora")
        assert harness.get("/repos/github/acme/secret", idp_user).status_code == 200
        auditor = User(provider="oidc:other", username="erik", groups=("auditors",))
        # Different provider: neither rule matches.
        assert harness.get("/repos/github/acme/secret", auditor).status_code == 404
        # Anonymous stays out.
        assert harness.get("/repos/github/acme/secret").status_code == 404
    finally:
        ctx.visibility.authz = saved


def test_api_token_inherits_login_groups(harness: WebHarness) -> None:
    """Tokens snapshot the creator's groups, so group-granted viewers
    keep their visibility over the API."""
    ctx = harness.ctx
    assert ctx.visibility is not None
    ctx.token_store = ApiTokenStore(ctx.pool)
    saved = ctx.visibility.authz
    ctx.visibility.authz = AuthzConfig(
        admins=["github:root"],
        private_repo_viewers={"*": ["oidc:idp:group:auditors"]},
    )
    try:
        creator = User(provider="oidc:idp", username="erika", groups=("auditors",))
        token = harness.run(ctx.token_store.create(creator, "t1"))
        restored = harness.run(ctx.token_store.authenticate(token))
        assert restored is not None
        assert restored.groups == ("auditors",)

        url = "/api/repos/github/acme/secret/builds"
        assert harness.get(url).status_code == 404  # anonymous API access
        response = harness.get(url, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
    finally:
        ctx.visibility.authz = saved


def test_access_cache_prunes_expired_on_get() -> None:
    """Expired entries must not accumulate forever. get() prunes them."""
    cache = AccessCache(ttl=0)
    cache.set("github:alice", RepoAccess())
    cache.set("github:bob", RepoAccess())
    assert cache.get("github:alice") is None
    assert cache._entries == {}  # noqa: SLF001


def test_logout_invalidates_access_cache(postgres_dsn: str) -> None:
    """The cache docstring promises entries are dropped on logout."""

    def configure(app: FastAPI) -> None:
        ctx = app.state.web_context
        ctx.visibility = VisibilityService(
            ctx.pool,
            AuthzConfig(admins=[]),
            fetcher=FETCHER,
            cache=AccessCache(ttl=3600),
        )
        app.include_router(
            create_auth_router(
                {},
                ctx.signer,
                "http://test",
                revoked_sessions=ctx.revoked_sessions,
                on_logout=ctx.visibility.invalidate_user,
            )
        )

    with web_harness(postgres_dsn, configure=configure) as h:
        visibility = h.ctx.visibility
        assert visibility is not None
        visibility.cache.set(CAROL.qualified, RepoAccess(frozenset({"github:priv-1"})))
        cookie = h.signer.session_for(CAROL, "sid-vis-logout")
        headers = cookie_header({SESSION_COOKIE: cookie}) | {"Origin": "http://test"}
        assert h.run(h.http.post("/logout", headers=headers)).status_code == 303
        assert visibility.cache.get(CAROL.qualified) is None


REPOS = [
    RepoRef("gitea", "1", "acme", "secret"),
    RepoRef("gitea", "2", "acme", "other"),
    RepoRef("github", "5", "acme", "widget"),
]


async def test_bot_fetcher_gitea() -> None:
    """Per-user access via the collaborator-permission endpoint with
    the bot's token; foreign-forge repos are skipped."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        assert request.headers["Authorization"] == "token bot-tok"
        if (
            request.url.path
            == "/api/v1/repos/acme/secret/collaborators/carol/permission"
        ):
            return httpx.Response(200, json={"permission": "write"})
        return httpx.Response(404)

    client = GiteaClient(
        "https://gitea.example.com",
        "bot-tok",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    fetcher = BotRepoAccessFetcher(gitea=client)
    access = await fetcher.repo_access(User(provider="gitea", username="carol"), REPOS)
    assert access.accessible == frozenset({"gitea:1"})
    assert access.writable == frozenset({"gitea:1"})
    assert access.admin == frozenset()
    # Only gitea repos were queried.
    assert all(path.startswith("/api/v1/repos/acme/") for path in seen)


async def test_bot_fetcher_gitea_owner_is_admin() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"permission": "owner"})

    client = GiteaClient(
        "https://gitea.example.com",
        "bot-tok",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    fetcher = BotRepoAccessFetcher(gitea=client)
    access = await fetcher.repo_access(
        User(provider="gitea", username="carol"), REPOS[:1]
    )
    assert access.admin == frozenset({"gitea:1"})


async def test_bot_fetcher_gitlab() -> None:
    """Username resolves to an id once, then membership access levels
    map to read/write/admin."""
    user_lookups = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal user_lookups
        assert request.headers["PRIVATE-TOKEN"] == "bot-tok"
        if request.url.path == "/api/v4/users":
            user_lookups += 1
            assert request.url.params["username"] == "dora"
            return httpx.Response(200, json=[{"id": 7}])
        if request.url.path == "/api/v4/projects/31/members/all/7":
            return httpx.Response(200, json={"access_level": 40})
        if request.url.path == "/api/v4/projects/32/members/all/7":
            return httpx.Response(200, json={"access_level": 30})
        return httpx.Response(404)

    client = GitlabClient(
        "https://gitlab.example.com",
        "bot-tok",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    fetcher = BotRepoAccessFetcher(gitlab=client)
    repos = [
        RepoRef("gitlab", "31", "grp", "a"),
        RepoRef("gitlab", "32", "grp", "b"),
        RepoRef("gitlab", "33", "grp", "c"),
    ]
    access = await fetcher.repo_access(User(provider="gitlab", username="dora"), repos)
    assert access.accessible == frozenset({"gitlab:31", "gitlab:32"})
    assert access.admin == frozenset({"gitlab:31"})
    assert access.writable == frozenset({"gitlab:31", "gitlab:32"})
    assert user_lookups == 1  # id cached across repos


async def test_bot_fetcher_unknown_provider() -> None:
    """OIDC users have no forge account to check: empty access, the
    configured viewer rules still apply on top."""
    fetcher = BotRepoAccessFetcher()
    access = await fetcher.repo_access(User(provider="oidc:idp", username="x"), REPOS)
    assert access == RepoAccess()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_bot_fetcher_github(tmp_path: Path) -> None:
    """Repo-scoped installation token, then the collaborator-permission
    endpoint decides the level."""
    key = tmp_path / "app-key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/acme/widget/installation":
            return httpx.Response(200, json={"id": 11})
        if path == "/app/installations/11/access_tokens":
            assert request.read() == b'{"repositories":["widget"]}'
            return httpx.Response(201, json={"token": "ghs_tok"})
        if path == "/repos/acme/widget/collaborators/erik/permission":
            assert request.headers["Authorization"] == "Bearer ghs_tok"
            return httpx.Response(200, json={"permission": "admin"})
        return httpx.Response(404)

    client = GitHubAppClient(
        app_id=42,
        private_key_file=key,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    fetcher = BotRepoAccessFetcher(github=client)
    access = await fetcher.repo_access(User(provider="github", username="erik"), REPOS)
    assert access.accessible == frozenset({"github:5"})
    assert access.admin == frozenset({"github:5"})
