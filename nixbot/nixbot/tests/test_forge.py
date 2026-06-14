"""Tests for forge clients (fake httpx transports), discovery filters,
and the project store incl. one-shot legacy topic import."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import pytest

from nixbot.config import RepoFilters
from nixbot.forge import (
    DiscoveredRepo,
    ForgeError,
    GitHubAppClient,
    GitHubFetchCredentialsProvider,
    NetrcFetchCredentialsProvider,
    filter_repos,
)
from nixbot.gitea_hooks import (
    register_repo_hook,
)
from nixbot.gitlab_hooks import register_repo_hook as gitlab_register_repo_hook
from nixbot.hook_secrets import WebhookSecrets
from nixbot.reconcile import (
    RemoteHead,
    gitea_heads,
    gitlab_heads,
    max_pr_updated,
    reconcile_repo,
)
from nixbot.repos import RepoStore
from nixbot.status import (
    GiteaStatusPoster,
    GitHubCheckRunPoster,
    StatusState,
)
from nixbot.tests.test_status import _MemoryCheckRunIds

from .support import (
    FakeGitea,
    gitea_client,
    gitlab_client,
    insert_build,
    insert_project,
)

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg


def repo(owner: str, name: str, topics: tuple[str, ...] = ()) -> DiscoveredRepo:
    return DiscoveredRepo(
        forge="github",
        forge_repo_id=f"{owner}-{name}",
        owner=owner,
        repo=name,
        default_branch="main",
        clone_url=f"https://github.com/{owner}/{name}.git",
        private=False,
        topics=topics,
    )


# --- filters ------------------------------------------------------------------


FILTER_REPOS = [
    repo("a", "x", topics=("build-with-buildbot",)),
    repo("b", "y"),
    repo("c", "z"),
]


@pytest.mark.parametrize(
    ("filters", "expected_indices"),
    [
        pytest.param(RepoFilters(), [0, 1, 2], id="no-allowlists-allow-all"),
        pytest.param(RepoFilters(user_allowlist=["a"]), [0], id="user-allowlist"),
        pytest.param(RepoFilters(repo_allowlist=["b/y"]), [1], id="repo-allowlist"),
        pytest.param(
            RepoFilters(user_allowlist=["a"], repo_allowlist=["b/y"]),
            [0, 1],
            id="union-of-allowlists",
        ),
        # The topic only drives the one-shot legacy enablement import
        # (projects.py); it must not exclude repos from discovery.
        pytest.param(
            RepoFilters(topic="build-with-buildbot"),
            [0, 1, 2],
            id="topic-does-not-filter",
        ),
    ],
)
def test_filter_repos(filters: RepoFilters, expected_indices: list[int]) -> None:
    expected = [FILTER_REPOS[i] for i in expected_indices]
    assert filter_repos(filters, FILTER_REPOS) == expected


# --- GitHub client ---------------------------------------------------------------


def github_transport(
    hook_url: str = "https://buildbot.example.com/webhooks/github",
    events: tuple[str, ...] = ("push", "pull_request", "check_run", "check_suite"),
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/app":
            return httpx.Response(200, json={"events": list(events)})
        if path == "/app/hook/config":
            return httpx.Response(200, json={"url": hook_url})
        if path == "/app/installations":
            return httpx.Response(200, json=[{"id": 11}, {"id": 22}])
        if path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            inst = path.split("/")[3]
            return httpx.Response(201, json={"token": f"ghs_token_{inst}"})
        if path == "/installation/repositories":
            token = request.headers["Authorization"].removeprefix("Bearer ")
            inst = token.removeprefix("ghs_token_")
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {
                            "id": int(inst) * 100,
                            "name": f"repo{inst}",
                            "owner": {"login": "acme"},
                            "default_branch": "main",
                            "clone_url": f"https://github.com/acme/repo{inst}.git",
                            "private": inst == "22",
                            "topics": ["build-with-buildbot"],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def github_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitHubAppClient:
    key = tmp_path / "app-key.pem"
    subprocess.run(  # noqa: S603
        ["openssl", "genrsa", "-out", str(key), "2048"],
        check=True,
        capture_output=True,
    )
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    return GitHubAppClient(
        app_id=42,
        private_key_file=key,
        http=httpx.AsyncClient(transport=github_transport()),
    )


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_webhook_check_ok(github_client: GitHubAppClient) -> None:
    problems = await github_client.check_app_webhook("https://buildbot.example.com")
    assert problems == []


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_webhook_check_misconfigured(
    github_client: GitHubAppClient,
) -> None:
    github_client.http = httpx.AsyncClient(
        transport=github_transport(hook_url="", events=("push",))
    )
    problems = await github_client.check_app_webhook("https://buildbot.example.com")
    assert any("webhook URL" in p for p in problems)
    assert any("pull_request" in p for p in problems)
    assert any("check_run" in p for p in problems)


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_discovery(github_client: GitHubAppClient) -> None:
    repos = await github_client.discover_repos()
    assert {r.name for r in repos} == {"acme/repo11", "acme/repo22"}
    assert {r.forge_repo_id for r in repos} == {"1100", "2200"}
    private = next(r for r in repos if r.name == "acme/repo22")
    assert private.private
    assert github_client.repo_installations == {
        "acme/repo11": 11,
        "acme/repo22": 22,
    }


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_fetch_credentials(github_client: GitHubAppClient) -> None:
    await github_client.discover_repos()
    provider = GitHubFetchCredentialsProvider(github_client)
    creds = await provider.get("https://github.com/acme/repo22.git")
    assert creds.netrc_file is not None
    content = creds.netrc_file.read_text()
    assert "x-access-token" in content
    assert "ghs_token_22" in content
    # Unknown repo: no credentials (public/netrc fallback).
    assert (await provider.get("https://github.com/other/x.git")).netrc_file is None


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_fetch_credentials_enterprise_host(
    github_client: GitHubAppClient,
) -> None:
    # The netrc machine entry must match the host git fetches from.
    await github_client.discover_repos()
    provider = GitHubFetchCredentialsProvider(github_client)
    creds = await provider.get("https://ghe.example.com/acme/repo22.git")
    assert creds.netrc_file is not None
    assert "machine ghe.example.com " in creds.netrc_file.read_text()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_discovery_isolates_failed_installation(
    github_client: GitHubAppClient,
) -> None:
    """One suspended installation (403 on token mint) must not abort
    discovery of the remaining installations."""
    base = github_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations/11/access_tokens":
            return httpx.Response(403, json={"message": "suspended"})
        response = base.handler(request)  # type: ignore[attr-defined]
        assert isinstance(response, httpx.Response)
        return response

    github_client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    repos = await github_client.discover_repos()
    assert {r.name for r in repos} == {"acme/repo22"}


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_fetch_credentials_repo_scoped(
    github_client: GitHubAppClient,
) -> None:
    """Tokens handed to fetch paths must be scoped to the single repo
    being fetched, not the whole installation."""
    scoped_requests: list[list[str] | None] = []

    base = github_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            body = json.loads(request.content) if request.content else {}
            scoped_requests.append(body.get("repositories"))
        response = base.handler(request)  # type: ignore[attr-defined]
        assert isinstance(response, httpx.Response)
        return response

    github_client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    await github_client.discover_repos()
    provider = GitHubFetchCredentialsProvider(github_client)
    creds = await provider.get("https://github.com/acme/repo22.git")
    assert creds.token is not None
    assert scoped_requests[-1] == ["repo22"]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_credentials_before_discovery(
    github_client: GitHubAppClient,
) -> None:
    """Webhooks are served before the initial discovery finishes; an
    unknown repo's installation must be looked up on demand instead of
    dropping credentials (private fetch and statuses would fail)."""
    base = github_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo22/installation":
            return httpx.Response(200, json={"id": 22})
        response = base.handler(request)  # type: ignore[attr-defined]
        assert isinstance(response, httpx.Response)
        return response

    github_client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    provider = GitHubFetchCredentialsProvider(github_client)
    creds = await provider.get("https://github.com/acme/repo22.git")
    assert creds.token == "ghs_token_22"  # noqa: S105
    # Cached: later lookups must not re-query the API.
    assert github_client.repo_installations["acme/repo22"] == 22
    # Unknown repo (404) degrades to no credentials.
    assert (await provider.get("https://github.com/acme/nope.git")).token is None


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_jwt_is_signed(github_client: GitHubAppClient) -> None:
    token = await github_client._app_jwt()  # noqa: SLF001
    header_b64, payload_b64, signature = token.split(".")

    def unpad(data: str) -> bytes:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    assert json.loads(unpad(header_b64)) == {"alg": "RS256", "typ": "JWT"}
    payload = json.loads(unpad(payload_b64))
    assert payload["iss"] == "42"
    assert payload["exp"] > payload["iat"]
    assert signature


# --- Gitea client ---------------------------------------------------------------


async def test_gitea_discovery() -> None:
    forge = FakeGitea(
        [
            {
                "id": 7,
                "name": "widget",
                "owner": {"login": "acme"},
                "default_branch": "main",
                "clone_url": "https://gitea.example.com/acme/widget.git",
                "private": True,
                # null permissions: allowed, must not crash.
                "permissions": None,
            },
            {
                # No admin permission: still discovered; hook
                # registration degrades to a manual-setup hint.
                "id": 8,
                "name": "readonly",
                "owner": {"login": "acme"},
                "default_branch": "main",
                "clone_url": "https://gitea.example.com/acme/readonly.git",
                "private": False,
                "permissions": {"admin": False},
            },
        ],
        topics={"widget": ["ci"], "readonly": []},
    )
    repos = await forge.client().discover_repos(fetch_topics=True)
    assert [r.forge_repo_id for r in repos] == ["7", "8"]
    assert repos[0].forge == "gitea"
    assert repos[0].topics == ("ci",)
    assert repos[0].private


async def test_gitea_discovery_skips_topics_by_default() -> None:
    """Topics are only the one-shot legacy import aid; the hourly sync
    must not pay one extra request per repo for them."""
    forge = FakeGitea(
        [
            {
                "id": 7,
                "name": "widget",
                "owner": {"login": "acme"},
                "default_branch": "main",
                "clone_url": "https://gitea.example.com/acme/widget.git",
                "private": False,
            }
        ],
        topics={"widget": ["ci"]},
    )
    repos = await forge.client().discover_repos()
    assert forge.topics_requests == 0
    assert repos[0].topics == ()


async def test_gitea_discovery_null_topics() -> None:
    """Gitea returns {"topics": null} for repos without topics; that
    must not crash discovery."""
    forge = FakeGitea(
        [
            {
                "id": 7,
                "name": "widget",
                "owner": {"login": "acme"},
                "default_branch": "main",
                "clone_url": "https://gitea.example.com/acme/widget.git",
                "private": False,
            }
        ]
    )
    repos = await forge.client().discover_repos(fetch_topics=True)
    assert repos[0].topics == ()


# --- project store -----------------------------------------------------------------


async def test_project_store_sync_and_legacy_import(pool: asyncpg.Pool) -> None:
    store = RepoStore(pool)
    repos = [
        repo("acme", "tagged", topics=("build-with-buildbot",)),
        repo("acme", "untagged"),
    ]
    # First startup with empty table: topic import enables.
    await store.sync_discovered(
        repos, legacy_import_topics={"github": "build-with-buildbot"}
    )
    enabled = await store.enabled_repos()
    assert [p.name for p in enabled] == ["tagged"]

    # Rename keeps identity and enablement (stable forge id).
    renamed = DiscoveredRepo(**{**repos[0].__dict__, "repo": "renamed", "topics": ()})
    await store.sync_discovered(
        [renamed], legacy_import_topics={"github": "build-with-buildbot"}
    )
    enabled = await store.enabled_repos()
    assert [p.name for p in enabled] == ["renamed"]

    # Non-empty table: topic import never runs again.
    newly_tagged = repo("acme", "later", topics=("build-with-buildbot",))
    await store.sync_discovered(
        [newly_tagged], legacy_import_topics={"github": "build-with-buildbot"}
    )
    assert {p.name for p in await store.enabled_repos()} == {"renamed"}

    # Admin toggle.
    later = await store.by_forge_id("github", "acme-later")
    assert later is not None
    await store.set_enabled(later.id, enabled=True)
    assert {p.name for p in await store.enabled_repos()} == {
        "renamed",
        "later",
    }


async def test_legacy_import_runs_despite_pull_based_rows(pool: asyncpg.Pool) -> None:
    """sync_pull_based fills the projects table before discovery; that
    must not suppress the one-shot legacy topic import."""

    await pool.execute("TRUNCATE projects CASCADE")
    store = RepoStore(pool)
    await store.sync_pull_based([("pull/one", "https://x/one.git", "main")])
    await store.sync_discovered(
        [repo("acme", "tagged", topics=("ci-topic",))],
        legacy_import_topics={"github": "ci-topic"},
    )
    enabled = {p.name for p in await store.enabled_repos()}
    assert "tagged" in enabled


async def test_legacy_import_scopes_topics_per_forge(pool: asyncpg.Pool) -> None:
    """Each forge's configured topic only enables that forge's repos."""

    await pool.execute("TRUNCATE projects CASCADE")
    store = RepoStore(pool)
    gitea_repo = DiscoveredRepo(
        **{
            **repo("acme", "gt", topics=("gitea-topic",)).__dict__,
            "forge": "gitea",
        }
    )
    cross = repo("acme", "cross", topics=("gitea-topic",))
    await store.sync_discovered(
        [gitea_repo, cross],
        legacy_import_topics={
            "github": "github-topic",
            "gitea": "gitea-topic",
        },
    )
    enabled = {p.name for p in await store.enabled_repos()}
    assert "gt" in enabled
    assert "cross" not in enabled


async def test_project_store_sync_skips_unchanged_rows(pool: asyncpg.Pool) -> None:
    """Re-syncing identical repo metadata must not rewrite rows:
    discovery runs every poll cycle over every repo, and unconditional
    updates churn WAL and autovacuum on otherwise idle databases."""

    store = RepoStore(pool)
    repos = [repo("acme", "stable"), repo("acme", "other")]
    await store.sync_discovered(repos)
    await store.sync_pull_based([("pull/one", "https://x/one.git", "main")])
    before = await pool.fetch(
        "SELECT name, xmin, updated_at FROM projects ORDER BY name"
    )

    await store.sync_discovered(repos)
    await store.sync_pull_based([("pull/one", "https://x/one.git", "main")])
    after = await pool.fetch(
        "SELECT name, xmin, updated_at FROM projects ORDER BY name"
    )
    assert [tuple(r) for r in before] == [tuple(r) for r in after]

    # A real change still updates the row.
    changed = DiscoveredRepo(**{**repos[0].__dict__, "default_branch": "develop"})
    await store.sync_discovered([changed])
    row = await pool.fetchrow(
        "SELECT default_branch FROM projects WHERE name = 'stable'"
    )
    assert row is not None
    assert row["default_branch"] == "develop"


# --- gitea webhook auto-registration ------------------------------


async def test_gitea_hook_registration(pool: asyncpg.Pool) -> None:
    forge = FakeGitea(
        hooks=[
            {"id": 1, "config": {"url": "https://ci.example.com/change_hook/gitea"}},
            {
                "id": 2,
                "config": {"url": "https://other-ci.example.com/change_hook/gitea"},
            },
            # Trailing-slash legacy variant must be removed too.
            {"id": 3, "config": {"url": "https://ci.example.com/change_hook/gitea/"}},
        ]
    )

    project_id = await insert_project(pool, forge="gitea", forge_repo_id="hook-1")
    secrets_store = WebhookSecrets(pool, "gitea")
    client = forge.client()
    await register_repo_hook(
        client,
        secrets_store,
        project_id,
        "acme",
        "widget",
        "https://ci.example.com",
    )
    # Hook created with the stored per-repo secret.
    assert len(forge.created) == 1
    hook = forge.created[0]
    assert hook["config"]["url"] == "https://ci.example.com/webhooks/gitea"
    # PR head pushes arrive as pull_request_sync, not pull_request.
    assert hook["events"] == ["push", "pull_request", "pull_request_sync"]
    secret = await secrets_store.secret_for_repo("hook-1")
    assert hook["config"]["secret"] == secret
    # Legacy hooks removed only when they match OUR base URL,
    # with or without trailing slash.
    assert forge.deleted == ["1", "3"]

    # Secret is stable across calls.
    assert await secrets_store.get_or_create(project_id) == secret

    # An existing hook is updated in place to re-sync the secret.
    forge.hooks[:] = [
        {"id": 9, "config": {"url": "https://ci.example.com/webhooks/gitea"}}
    ]
    forge.deleted.clear()
    await register_repo_hook(
        client,
        secrets_store,
        project_id,
        "acme",
        "widget",
        "https://ci.example.com",
    )
    assert len(forge.created) == 1  # no duplicate hook created
    assert forge.deleted == []
    assert len(forge.patched) == 1
    hook_id, patch_body = forge.patched[0]
    assert hook_id == "9"
    assert patch_body["config"]["secret"] == secret


# --- startup reconciliation ----------------------------------------


async def test_gitlab_heads_carry_target_branch_base(pool: asyncpg.Pool) -> None:
    """GitLab's MR API has no base sha; reconciliation must still merge
    MR heads into the target branch."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/projects/acme/glrecon/repository/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v4/projects/acme/glrecon/merge_requests":
            return httpx.Response(
                200,
                json=[
                    {
                        "iid": 5,
                        "sha": "head-mr5",
                        "target_branch": "main",
                        "author": {"username": "alice"},
                        "updated_at": "2026-06-09T10:00:00.000Z",
                    }
                ],
            )
        return httpx.Response(404)

    await insert_project(pool, "glrecon", forge="gitlab", forge_repo_id="glrecon-1")
    project = await RepoStore(pool).by_forge_id("gitlab", "glrecon-1")
    assert project is not None
    client = gitlab_client(handler)
    heads = await gitlab_heads(client, project)
    mr_head = next(h for h in heads if h.pr_number == 5)
    assert mr_head.base_sha == "refs/heads/main"


async def test_gitea_heads_encode_slashed_default_branch(pool: asyncpg.Pool) -> None:
    """A default branch with a slash must be URL-encoded or the
    branches endpoint 404s and the head is never reconciled."""

    def handler(request: httpx.Request) -> httpx.Response:
        # httpx keeps the encoded form in raw_path.
        if (
            request.url.raw_path.decode().split("?")[0]
            == "/api/v1/repos/acme/slashy/branches/release%2F1.0"
        ):
            return httpx.Response(200, json={"commit": {"id": "head-rel"}})
        if request.url.path == "/api/v1/repos/acme/slashy/pulls":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    await insert_project(
        pool,
        "slashy",
        forge="gitea",
        forge_repo_id="slashy-1",
        default_branch="release/1.0",
    )
    project = await RepoStore(pool).by_forge_id("gitea", "slashy-1")
    assert project is not None
    client = gitea_client(handler)
    heads = await gitea_heads(client, project)
    assert [h.commit_sha for h in heads] == ["head-rel"]


async def test_reconcile_unbuilt_heads(pool: asyncpg.Pool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/acme/recon/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v1/repos/acme/recon/pulls":
            page = int(request.url.params.get("page", "1"))
            if page > 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 5,
                        "user": {"login": "alice"},
                        "head": {"sha": "head-pr5"},
                        "base": {"ref": "main", "sha": "base-pr5"},
                        "updated_at": "2026-06-09T10:00:00Z",
                    },
                    {
                        "number": 6,
                        "user": {"login": "bob"},
                        "head": {"sha": "already-built"},
                        "base": {"ref": "main", "sha": "base-pr6"},
                        "updated_at": "2026-06-09T09:00:00Z",
                    },
                ],
            )
        return httpx.Response(404)

    project_id = await insert_project(
        pool, "recon", forge="gitea", forge_repo_id="recon-1"
    )
    # PR 6's head already has a build record.
    await insert_build(pool, project_id, commit_sha="already-built", status="succeeded")
    project = await RepoStore(pool).by_forge_id("gitea", "recon-1")
    assert project is not None

    client = gitea_client(handler)
    heads = await gitea_heads(client, project)
    assert len(heads) == 3

    events: list[object] = []

    class Sink:
        async def submit(self, event: object) -> None:
            events.append(event)

    submitted = await reconcile_repo(pool, project, heads, Sink())
    # main head + PR 5; PR 6 already built.
    assert submitted == 2
    shas = {e.commit_sha for e in events}  # type: ignore[attr-defined]
    assert shas == {"head-main", "head-pr5"}

    # First contact (no builds at all): default branch only, the
    # open-PR backlog is not built.
    await pool.execute("DELETE FROM builds WHERE project_id = $1", project_id)
    events.clear()
    submitted = await reconcile_repo(pool, project, heads, Sink())
    assert submitted == 1
    assert events[0].commit_sha == "head-main"  # type: ignore[attr-defined]

    # Cancelled-only history is still fresh: an operator who
    # cancels the initial build must not get the PR backlog on
    # the next restart.
    await insert_build(
        pool,
        project_id,
        number=2,
        commit_sha="head-main",
        status="cancelled",
    )
    events.clear()
    submitted = await reconcile_repo(pool, project, heads, Sink())
    assert submitted == 0  # main head cancelled, PRs skipped


async def test_gitea_watermark_stops_pagination(pool: asyncpg.Pool) -> None:
    """PRs are fetched newest-update-first; pagination must stop at
    the first PR older than the watermark instead of walking the
    whole open-PR backlog (nixpkgs-scale repos)."""
    requested_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/repos/acme/wm/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v1/repos/acme/wm/pulls":
            assert request.url.params["sort"] == "recentupdate"
            page = int(request.url.params.get("page", "1"))
            requested_pages.append(page)
            pages = {
                1: [
                    {
                        "number": 2,
                        "user": {"login": "alice"},
                        "head": {"sha": "head-pr2"},
                        "base": {"ref": "main", "sha": "b"},
                        "updated_at": "2026-06-09T10:00:00Z",
                    }
                ],
                2: [
                    {
                        "number": 1,
                        "user": {"login": "bob"},
                        "head": {"sha": "head-pr1"},
                        "base": {"ref": "main", "sha": "b"},
                        "updated_at": "2026-06-09T08:00:00Z",
                    }
                ],
            }
            return httpx.Response(200, json=pages.get(page, []))
        return httpx.Response(404)

    await insert_project(pool, "wm", forge="gitea", forge_repo_id="wm-1")
    project = await RepoStore(pool).by_forge_id("gitea", "wm-1")
    assert project is not None
    client = gitea_client(handler)

    watermark = datetime(2026, 6, 9, 9, 0, tzinfo=UTC)
    heads = await gitea_heads(client, project, watermark)
    # main + PR 2; PR 1 (older than watermark) excluded, page 3 never
    # requested.
    assert [h.commit_sha for h in heads] == ["head-main", "head-pr2"]
    assert requested_pages == [1, 2]

    # Watermark boundary is inclusive: a PR updated exactly at the
    # watermark is still returned (dedup happens via is_built).
    requested_pages.clear()
    heads = await gitea_heads(client, project, datetime(2026, 6, 9, 10, 0, tzinfo=UTC))
    assert [h.commit_sha for h in heads] == ["head-main", "head-pr2"]

    # No watermark (first reconcile): full fetch.
    requested_pages.clear()
    heads = await gitea_heads(client, project)
    assert [h.commit_sha for h in heads] == ["head-main", "head-pr2", "head-pr1"]
    assert requested_pages == [1, 2, 3]


async def test_gitlab_heads_filter_updated_after(pool: asyncpg.Pool) -> None:
    """GitLab filters server-side: the MR listing must carry
    updated_after, backed off one second to keep the watermark
    boundary inclusive."""
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/projects/acme/glwm/repository/branches/main":
            return httpx.Response(200, json={"commit": {"id": "head-main"}})
        if path == "/api/v4/projects/acme/glwm/merge_requests":
            seen_params.append(dict(request.url.params))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    await insert_project(pool, "glwm", forge="gitlab", forge_repo_id="glwm-1")
    project = await RepoStore(pool).by_forge_id("gitlab", "glwm-1")
    assert project is not None
    client = gitlab_client(handler)

    await gitlab_heads(client, project)
    assert "updated_after" not in seen_params[0]

    await gitlab_heads(client, project, datetime(2026, 6, 9, 9, 0, tzinfo=UTC))
    assert (
        seen_params[1]["updated_after"]
        == datetime(2026, 6, 9, 8, 59, 59, tzinfo=UTC).isoformat()
    )


async def test_reconcile_watermark_store(pool: asyncpg.Pool) -> None:
    """Watermark derives from the newest PR update time and never
    rewinds (a filtered fetch sees fewer PRs than a full one)."""
    project_id = await insert_project(
        pool, "wmstore", forge="gitea", forge_repo_id="wmstore-1"
    )
    store = RepoStore(pool)
    assert await store.reconcile_watermark(project_id) is None

    older = datetime(2026, 6, 9, 8, 0, tzinfo=UTC)
    newer = datetime(2026, 6, 9, 10, 0, tzinfo=UTC)
    heads = [
        RemoteHead(branch="main", commit_sha="m"),  # no PR: no timestamp
        RemoteHead(branch="main", commit_sha="a", pr_number=1, updated_at=older),
        RemoteHead(branch="main", commit_sha="b", pr_number=2, updated_at=newer),
    ]
    assert max_pr_updated(heads) == newer
    assert max_pr_updated([heads[0]]) is None

    await store.set_reconcile_watermark(project_id, newer)
    assert await store.reconcile_watermark(project_id) == newer
    await store.set_reconcile_watermark(project_id, older)  # must not rewind
    assert await store.reconcile_watermark(project_id) == newer


# --- status posting against fake forge APIs -------------------------


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl required")
async def test_github_check_run_post(github_client: GitHubAppClient) -> None:
    """End-to-end through the real App client (JWT, installation
    token, repo lookup); the upsert/PATCH details are covered in
    test_status."""
    posted: list[dict] = []
    fallback = github_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo11/check-runs":
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 1})
        return fallback.handler(request)  # type: ignore[attr-defined,return-value]

    github_client.http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=""
    )

    await github_client.discover_repos()
    poster = GitHubCheckRunPoster(github_client, _MemoryCheckRunIds())
    await poster.post(
        "acme",
        "repo11",
        "sha1",
        "nixbot/nix-eval",
        StatusState.success,
        "evaluation succeeded",
        "https://ci.test/repos/acme/repo11/builds/1",
        project_id=1,
        build_id=1,
    )
    assert posted == [
        {
            "name": "nixbot/nix-eval",
            "head_sha": "sha1",
            "status": "completed",
            "conclusion": "success",
            "external_id": "1",
            "details_url": "https://ci.test/repos/acme/repo11/builds/1",
            "output": {"title": "nixbot/nix-eval", "summary": "evaluation succeeded"},
        }
    ]


async def test_gitea_status_post() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/repos/acme/widget/statuses/sha9":
            assert request.headers["Authorization"] == "token tkn"
            posted.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    client = gitea_client(handler)
    await GiteaStatusPoster(client).post(
        "acme",
        "widget",
        "sha9",
        "nixbot/nix-build",
        StatusState.failure,
        "2 of 3 attributes failed",
        "https://ci.test/repos/acme/widget/builds/7",
    )
    assert posted[0]["state"] == "failure"
    assert posted[0]["context"] == "nixbot/nix-build"


async def test_register_repo_hook_without_admin_warns(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """No admin permission on the repo: degrade to a manual-setup hint
    instead of a stack trace."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(403, json={"message": "forbidden"})
        return httpx.Response(404)

    with caplog.at_level("WARNING"):
        project_id = await insert_project(
            pool, "locked", forge="gitea", forge_repo_id="hook-403"
        )
        client = gitea_client(handler)
        await register_repo_hook(
            client,
            WebhookSecrets(pool, "gitea"),
            project_id,
            "acme",
            "locked",
            "https://ci.example.com",
        )
    assert any("manually" in r.message for r in caplog.records)


async def test_gitlab_discovery() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "glpat-x"
        if request.url.path == "/api/v4/projects":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "path_with_namespace": "group/sub/tool",
                        "default_branch": "develop",
                        "http_url_to_repo": "https://gitlab.example.com/group/sub/tool.git",
                        "visibility": "private",
                        "topics": ["ci"],
                    },
                    {
                        "id": 8,
                        "path_with_namespace": "Mic92/dotfiles",
                        "default_branch": "main",
                        "http_url_to_repo": "https://gitlab.example.com/Mic92/dotfiles.git",
                        "visibility": "public",
                    },
                ],
            )
        raise AssertionError(request.url.path)

    # Trailing slash must be normalized away in generated API URLs.
    client = gitlab_client(
        handler,
        base_url="https://gitlab.example.com/",
        token="glpat-x",  # noqa: S106 (test credential)
    )
    nested, public = await client.discover_repos()
    assert nested == DiscoveredRepo(
        forge="gitlab",
        forge_repo_id="7",
        owner="group/sub",
        repo="tool",
        default_branch="develop",
        clone_url="https://gitlab.example.com/group/sub/tool.git",
        private=True,
        topics=("ci",),
    )
    assert public.name == "Mic92/dotfiles"
    assert not public.private
    assert (
        client.project_api_url("group/sub", "tool")
        == "https://gitlab.example.com/api/v4/projects/group%2Fsub%2Ftool"
    )


async def test_gitlab_hook_registration(pool: asyncpg.Pool) -> None:
    hooks: list[dict] = []
    created: list[dict] = []
    updated: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.startswith(b"/api/v4/projects/acme%2Fwidget/hooks")
        if request.method == "GET":
            return httpx.Response(200, json=hooks)
        if request.method == "POST":
            created.append(json.loads(request.content))
            return httpx.Response(201, json={})
        if request.method == "PUT":
            updated.append(
                (request.url.path.rsplit("/", 1)[-1], json.loads(request.content))
            )
            return httpx.Response(200, json={})
        return httpx.Response(404)

    project_id = await insert_project(pool, forge="gitlab", forge_repo_id="glhook-1")
    secrets_store = WebhookSecrets(pool, "gitlab")
    client = gitlab_client(handler)
    await gitlab_register_repo_hook(
        client,
        secrets_store,
        project_id,
        "acme",
        "widget",
        "https://ci.example.com",
    )
    assert len(created) == 1
    hook = created[0]
    assert hook["url"] == "https://ci.example.com/webhooks/gitlab"
    assert hook["push_events"]
    assert hook["merge_requests_events"]
    assert hook["token"] == await secrets_store.secret_for_repo("glhook-1")

    # An existing hook is updated in place to re-sync the secret.
    hooks[:] = [{"id": 9, "url": "https://ci.example.com/webhooks/gitlab"}]
    await gitlab_register_repo_hook(
        client,
        secrets_store,
        project_id,
        "acme",
        "widget",
        "https://ci.example.com",
    )
    assert len(created) == 1
    assert updated[0][0] == "9"


async def test_register_repo_hook_500_with_403_in_body_raises(
    pool: asyncpg.Pool,
) -> None:
    """A 500 whose response body merely contains "403" is not a
    permission problem: it must propagate, not degrade to a warning."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(500, json={"message": "object id 403 missing"})
        return httpx.Response(404)

    project_id = await insert_project(
        pool, "err500", forge="gitea", forge_repo_id="hook-500"
    )
    client = gitea_client(handler)
    with pytest.raises(ForgeError):
        await register_repo_hook(
            client,
            WebhookSecrets(pool, "gitea"),
            project_id,
            "acme",
            "err500",
            "https://ci.example.com",
        )


async def test_gitlab_register_repo_hook_status_classification(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """403 degrades to a manual-setup warning; a 500 whose body
    contains "403" propagates as ForgeError."""
    status = 403

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/hooks"):
            return httpx.Response(status, json={"message": "see id 403"})
        return httpx.Response(404)

    with caplog.at_level("WARNING"):
        project_id = await insert_project(
            pool, "glperm", forge="gitlab", forge_repo_id="glhook-403"
        )
        client = gitlab_client(handler)
        secrets_store = WebhookSecrets(pool, "gitlab")
        await gitlab_register_repo_hook(
            client,
            secrets_store,
            project_id,
            "acme",
            "glperm",
            "https://ci.example.com",
        )
        status = 500
        with pytest.raises(ForgeError):
            await gitlab_register_repo_hook(
                client,
                secrets_store,
                project_id,
                "acme",
                "glperm",
                "https://ci.example.com",
            )
    assert any("manually" in r.message for r in caplog.records)


# --- credential temp-dir lifecycle ---------------------------------------------


async def test_netrc_provider_cleanup_removes_tempdir() -> None:
    provider = NetrcFetchCredentialsProvider("https://gitea.example.com", "tkn")
    creds = await provider.get("https://gitea.example.com/a/b.git")
    assert creds.netrc_file is not None
    assert creds.netrc_file.exists()
    provider.cleanup()
    assert not creds.netrc_file.parent.exists()


async def test_gitea_legacy_hook_delete_failure_warns(
    pool: asyncpg.Pool, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed legacy-hook DELETE silently leaves the old hook
    delivering forever; it must at least be logged."""
    forge = FakeGitea(
        hooks=[
            {"id": 1, "config": {"url": "https://ci.example.com/change_hook/gitea"}},
        ],
        delete_status=500,
    )

    project_id = await insert_project(pool, forge="gitea", forge_repo_id="hook-del-1")
    client = forge.client()
    with caplog.at_level("WARNING", logger="nixbot.gitea_hooks"):
        await register_repo_hook(
            client,
            WebhookSecrets(pool, "gitea"),
            project_id,
            "acme",
            "widget",
            "https://ci.example.com",
        )
    assert any("failed to remove legacy" in record.message for record in caplog.records)


async def test_sync_tolerates_in_batch_duplicates(pool: asyncpg.Pool) -> None:
    """A forge listing (or static config) repeating the same repo must
    not abort the batch upsert: ON CONFLICT DO UPDATE raises "cannot
    affect row a second time" on in-batch duplicates, so the store
    dedupes with last-entry-wins."""

    await pool.execute("TRUNCATE projects CASCADE")
    store = RepoStore(pool)
    first = repo("acme", "dup")
    renamed = DiscoveredRepo(**{**first.__dict__, "repo": "dup-renamed"})
    await store.sync_discovered([first, renamed])
    row = await store.by_forge_id("github", "acme-dup")
    assert row is not None
    assert row.name == "dup-renamed"

    await store.sync_pull_based(
        [
            ("pull/two", "https://x/old.git", "main"),
            ("pull/two", "https://x/new.git", "main"),
        ]
    )
    row = await store.by_forge_id("pull_based", "pull/two")
    assert row is not None
    assert row.url == "https://x/new.git"
