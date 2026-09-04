"""Private-project visibility.

Public projects are readable without login. Private projects are
visible only to users whose forge account can access the repository.
Access is checked with the service's own forge credentials: for each
tracked repo the forge is asked what the requesting user may do, so
any authenticated identity works (web session or personal API token).
Results are cached per user (default 1h, negatives too) and dropped on
logout.
Admins see everything.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal, Protocol

import httpx

from .auth import can_view_private, is_admin
from .db_gen import projects as q
from .forge import ForgeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    import asyncpg

    from .auth import AuthzConfig, User
    from .forge import GiteaClient, GitHubAppClient, GitlabClient

logger = logging.getLogger(__name__)

DEFAULT_TTL = 60 * 60


@dataclass(frozen=True)
class RepoAccess:
    """Repo keys ("forge:forge_repo_id") a user can see, administer
    (enable/disable the project), and write to (restart/cancel builds)."""

    accessible: frozenset[str] = frozenset()
    admin: frozenset[str] = frozenset()
    writable: frozenset[str] = frozenset()


@dataclass
class _CacheEntry:
    access: RepoAccess
    expires: float


class AccessCache:
    """Per-user repo-access cache (negatives cached)."""

    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._entries: dict[str, _CacheEntry] = {}

    def get(self, user_key: str) -> RepoAccess | None:
        # Opportunistic pruning keeps the cache from growing without
        # bound as users come and go.
        now = time.monotonic()
        for key in [k for k, e in self._entries.items() if e.expires <= now]:
            del self._entries[key]
        entry = self._entries.get(user_key)
        return entry.access if entry is not None else None

    def set(self, user_key: str, access: RepoAccess) -> None:
        self._entries[user_key] = _CacheEntry(
            access=access, expires=time.monotonic() + self.ttl
        )

    def invalidate(self, user_key: str) -> None:
        self._entries.pop(user_key, None)


@dataclass(frozen=True)
class RepoRef:
    """Identity of a tracked project on its forge."""

    forge: str
    forge_repo_id: str
    owner: str
    name: str

    @property
    def key(self) -> str:
        return f"{self.forge}:{self.forge_repo_id}"


class RepoAccessFetcher(Protocol):
    async def repo_access(self, user: User, repos: Sequence[RepoRef]) -> RepoAccess: ...


# GitLab Maintainer: the lowest level allowed to manage CI settings.
_GITLAB_MAINTAINER = 40
# GitLab Developer: the lowest level that can push.
_GITLAB_DEVELOPER = 30

_Level = Literal["read", "write", "admin"]

# GitHub's permission endpoint collapses roles into these buckets;
# Gitea additionally reports "owner" for the repo owner.
_LEVELS: dict[str, _Level] = {
    "read": "read",
    "write": "write",
    "admin": "admin",
    "owner": "admin",
}

if TYPE_CHECKING:
    _Checker = Callable[[str, RepoRef], Awaitable[_Level | None]]


class BotRepoAccessFetcher:
    """Per-user, per-repo permission checks with the bot's own forge
    credentials, so no user OAuth token is needed."""

    def __init__(
        self,
        github: GitHubAppClient | None = None,
        gitea: GiteaClient | None = None,
        gitlab: GitlabClient | None = None,
        concurrency: int = 10,
    ) -> None:
        self.concurrency = concurrency
        # GitLab needs a username -> id lookup before the membership
        # query; ids are stable, so remember them across fetches.
        self._gitlab_user_ids: dict[str, int | None] = {}
        self._checkers: dict[str, _Checker] = {}
        if github is not None:
            self._checkers["github"] = partial(self._github, github)
        if gitea is not None:
            self._checkers["gitea"] = partial(self._gitea, gitea)
        if gitlab is not None:
            self._checkers["gitlab"] = partial(self._gitlab, gitlab)

    async def permission(self, forge: str, login: str, repo: RepoRef) -> _Level | None:
        """One user on one repo. Raises ForgeError on API failure."""
        checker = self._checkers.get(forge)
        if checker is None:
            return None
        return await checker(login, repo)

    async def repo_access(self, user: User, repos: Sequence[RepoRef]) -> RepoAccess:
        checker = self._checkers.get(user.provider)
        if checker is None:
            return RepoAccess()
        semaphore = asyncio.Semaphore(self.concurrency)

        async def check(repo: RepoRef) -> tuple[RepoRef, _Level | None]:
            async with semaphore:
                return repo, await checker(user.username, repo)

        results = await asyncio.gather(
            *(check(repo) for repo in repos if repo.forge == user.provider)
        )
        return RepoAccess(
            accessible=frozenset(r.key for r, level in results if level),
            admin=frozenset(r.key for r, level in results if level == "admin"),
            writable=frozenset(
                r.key for r, level in results if level in ("write", "admin")
            ),
        )

    async def _github(
        self, client: GitHubAppClient, username: str, repo: RepoRef
    ) -> _Level | None:
        installation = await client.installation_for_repo(f"{repo.owner}/{repo.name}")
        if installation is None:
            return None
        token = await client.installation_token(installation, (repo.name,))
        response = await client.http.get(
            f"{client.api_url}/repos/{repo.owner}/{repo.name}"
            f"/collaborators/{username}/permission",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
        )
        return _permission_from(response, "GitHub")

    async def _gitea(
        self, client: GiteaClient, username: str, repo: RepoRef
    ) -> _Level | None:
        # Requires the bot account to be a repo admin or site admin
        # (docs/GITEA.md sets up the latter).
        response = await client.http.get(
            f"{client.instance_url}/api/v1/repos/{repo.owner}/{repo.name}"
            f"/collaborators/{username}/permission",
            headers=client.auth_headers(),
        )
        return _permission_from(response, "Gitea")

    async def _gitlab(
        self, client: GitlabClient, username: str, repo: RepoRef
    ) -> _Level | None:
        user_id = await self._gitlab_user_id(client, username)
        if user_id is None:
            return None
        response = await client.http.get(
            f"{client.instance_url}/api/v4/projects/{repo.forge_repo_id}"
            f"/members/all/{user_id}",
            headers=client.auth_headers(),
        )
        if response.status_code in (403, 404):
            return None
        _raise_for_status(response, "GitLab")
        level = response.json().get("access_level", 0)
        if level >= _GITLAB_MAINTAINER:
            return "admin"
        if level >= _GITLAB_DEVELOPER:
            return "write"
        return "read" if level > 0 else None

    async def _gitlab_user_id(self, client: GitlabClient, username: str) -> int | None:
        if username in self._gitlab_user_ids:
            return self._gitlab_user_ids[username]
        response = await client.http.get(
            f"{client.instance_url}/api/v4/users",
            params={"username": username},
            headers=client.auth_headers(),
        )
        _raise_for_status(response, "GitLab")
        users = response.json()
        user_id = int(users[0]["id"]) if users else None
        self._gitlab_user_ids[username] = user_id
        return user_id


def _permission_from(response: httpx.Response, forge: str) -> _Level | None:
    """GitHub and Gitea share the collaborator-permission endpoint
    shape. 403/404 = not a collaborator."""
    if response.status_code in (403, 404):
        return None
    _raise_for_status(response, forge)
    return _LEVELS.get(response.json().get("permission", "none"))


def _raise_for_status(response: httpx.Response, forge: str) -> None:
    if response.status_code >= 400:  # noqa: PLR2004
        msg = f"{forge} permission check failed: {response.status_code}"
        raise ForgeError(msg, status_code=response.status_code)


class VisibilityService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        authz: AuthzConfig,
        fetcher: RepoAccessFetcher | None = None,
        cache: AccessCache | None = None,
    ) -> None:
        self.pool = pool
        self.authz = authz
        self.fetcher = fetcher
        self.cache = cache or AccessCache()

    def invalidate_user(self, user: User) -> None:
        self.cache.invalidate(user.qualified)

    async def visible_repo_ids(self, user: User | None) -> list[int] | None:
        """None = everything visible (admins). Otherwise the project ids
        the requester may see (public + accessible private)."""
        if is_admin(user, self.authz):
            return None
        rows = await q.project_visibility_rows(self.pool)
        visible = [row.id_ for row in rows if not row.private]
        # Configured viewer rules (e.g. OIDC users without forge accounts).
        if user is not None:
            visible.extend(
                row.id_
                for row in rows
                if row.private
                and can_view_private(
                    user,
                    self.authz.private_repo_viewers,
                    row.forge,
                    row.owner,
                    row.name,
                )
            )
        access = await self._repo_access(user)
        if access is None:
            return visible
        seen = set(visible)
        visible.extend(
            row.id_
            for row in rows
            if row.private
            and row.id_ not in seen
            and f"{row.forge}:{row.forge_repo_id}" in access.accessible
        )
        return visible

    async def toggleable_repo_ids(self, user: User | None) -> list[int] | None:
        """Projects the requester may enable/disable. None = all
        (instance admins). Forge-side repo admins get their own."""
        return await self._repo_ids_for(user, lambda a: a.admin)

    async def controllable_repo_ids(self, user: User | None) -> list[int] | None:
        """Projects whose builds the requester may restart/cancel. None =
        all (instance admins). Forge-side repo writers get their own."""
        return await self._repo_ids_for(user, lambda a: a.writable)

    async def _repo_ids_for(
        self,
        user: User | None,
        select: Callable[[RepoAccess], frozenset[str]],
    ) -> list[int] | None:
        if is_admin(user, self.authz):
            return None
        access = await self._repo_access(user)
        if access is None:
            return []
        granted = select(access)
        rows = await q.project_forge_ids(self.pool)
        return [
            row.id_ for row in rows if f"{row.forge}:{row.forge_repo_id}" in granted
        ]

    async def _repo_access(self, user: User | None) -> RepoAccess | None:
        """None: no usable access info (anonymous, no fetcher, or the
        forge failed) — callers fall back to their public behavior."""
        if user is None or self.fetcher is None:
            return None
        access = self.cache.get(user.qualified)
        if access is not None:
            return access
        rows = await q.project_visibility_rows(self.pool)
        repos = [
            RepoRef(row.forge, row.forge_repo_id, row.owner, row.name) for row in rows
        ]
        try:
            access = await self.fetcher.repo_access(user, repos)
        except (httpx.HTTPError, ForgeError):
            # Uncached so the next request retries.
            logger.warning(
                "failed to fetch accessible repos", extra={"user": user.qualified}
            )
            return None
        self.cache.set(user.qualified, access)
        return access
