"""Pull-request metadata and per-user permission lookups for onEvent
deliveries. One code path per forge, called with the bot's own
credentials at delivery time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .forge import ForgeError
from .visibility import BotRepoAccessFetcher, RepoRef

if TYPE_CHECKING:
    import httpx

    from .db_gen.models import Project
    from .events import RepoInfo
    from .forge import GiteaClient, GitHubAppClient, GitlabClient


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    title: str
    url: str
    draft: bool
    is_fork: bool
    head_rev: str
    head_ref: str
    base_ref: str
    labels: tuple[str, ...]
    # Forge qualified ("github:alice"). None when the forge hides it.
    author: str | None
    merged: bool
    open: bool

    def payload(self, author_permission: str | None) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "url": self.url,
            "draft": self.draft,
            "isFork": self.is_fork,
            "headRev": self.head_rev,
            "headRef": self.head_ref,
            "baseRef": self.base_ref,
            "labels": list(self.labels),
            "author": {"name": self.author, "permission": author_permission}
            if self.author
            else None,
            "merged": self.merged,
        }


def _raise(response: Any, forge: str, what: str) -> None:
    if response.status_code >= 400:  # noqa: PLR2004
        msg = f"{forge} {what} failed: {response.status_code}"
        raise ForgeError(msg, status_code=response.status_code)


def _github_like(forge: str, pr: dict[str, Any]) -> PullRequestInfo:
    """GitHub and Gitea share the pulls/:n response shape."""
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    base_repo = (base.get("repo") or {}).get("full_name")
    login = (pr.get("user") or {}).get("login")
    return PullRequestInfo(
        number=pr["number"],
        title=pr.get("title", ""),
        url=pr.get("html_url", ""),
        draft=bool(pr.get("draft", False)),
        is_fork=head_repo is not None and head_repo != base_repo,
        head_rev=head.get("sha", ""),
        head_ref=head.get("ref", ""),
        base_ref=base.get("ref", ""),
        labels=tuple(lbl["name"] for lbl in pr.get("labels") or []),
        author=f"{forge}:{login}" if login else None,
        merged=bool(pr.get("merged", False)),
        open=pr.get("state") == "open",
    )


@dataclass(frozen=True)
class _CommentApi:
    http: httpx.AsyncClient
    headers: dict[str, str]
    list_url: str  # GET lists, POST creates
    edit_url: str  # with {id}
    edit_method: str


# Token user per forge, process-wide (tokens do not change at runtime).
_BOT_LOGINS: dict[str, str] = {}


class ForgePrClient:
    """Bound to the configured forge clients. Methods raise ForgeError."""

    def __init__(
        self,
        github: GitHubAppClient | None,
        gitea: GiteaClient | None,
        gitlab: GitlabClient | None,
    ) -> None:
        self.github = github
        self.gitea = gitea
        self.gitlab = gitlab
        self._access = BotRepoAccessFetcher(github=github, gitea=gitea, gitlab=gitlab)

    async def pull_request(self, info: RepoInfo, number: int) -> PullRequestInfo:
        if info.forge == "github" and self.github is not None:
            c = self.github
            installation = await c.installation_for_repo(info.name)
            if installation is None:
                msg = f"no GitHub installation for {info.name}"
                raise ForgeError(msg)
            token = await c.installation_token(installation, (info.repo,))
            response = await c.http.get(
                f"{c.api_url}/repos/{info.name}/pulls/{number}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
            )
            _raise(response, "GitHub", "pull request lookup")
            return _github_like("github", response.json())
        if info.forge == "gitea" and self.gitea is not None:
            g = self.gitea
            response = await g.http.get(
                f"{g.instance_url}/api/v1/repos/{info.name}/pulls/{number}",
                headers=g.auth_headers(),
            )
            _raise(response, "Gitea", "pull request lookup")
            return _github_like("gitea", response.json())
        if info.forge == "gitlab" and self.gitlab is not None:
            gl = self.gitlab
            response = await gl.http.get(
                f"{gl.project_api_url(info.owner, info.repo)}/merge_requests/{number}",
                headers=gl.auth_headers(),
            )
            _raise(response, "GitLab", "merge request lookup")
            mr = response.json()
            username = (mr.get("author") or {}).get("username")
            return PullRequestInfo(
                number=mr["iid"],
                title=mr.get("title", ""),
                url=mr.get("web_url", ""),
                draft=bool(mr.get("draft", False)),
                is_fork=mr.get("source_project_id") != mr.get("target_project_id"),
                head_rev=mr.get("sha", ""),
                head_ref=mr.get("source_branch", ""),
                base_ref=mr.get("target_branch", ""),
                labels=tuple(mr.get("labels") or []),
                author=f"gitlab:{username}" if username else None,
                merged=mr.get("state") == "merged",
                open=mr.get("state") == "opened",
            )
        msg = f"no {info.forge} client configured"
        raise ForgeError(msg)

    async def comment(
        self, info: RepoInfo, number: int, body: str, marker: str | None
    ) -> None:
        """Post a PR comment. With a marker, the newest existing comment
        carrying it (first 100) is edited instead so repeated runs update
        one comment. Editing someone else's comment fails on the forge
        and falls back to posting."""
        if marker:
            body = f"{body}\n<!-- nixbot:{marker} -->"
        api = await self._comment_api(info, number)
        if marker:
            response = await api.http.get(
                f"{api.list_url}?per_page=100", headers=api.headers
            )
            _raise(response, info.forge, "comment listing")
            for c in reversed(response.json()):
                if f"<!-- nixbot:{marker} -->" in (c.get("body") or ""):
                    response = await api.http.request(
                        api.edit_method,
                        api.edit_url.format(id=c["id"]),
                        json={"body": body},
                        headers=api.headers,
                    )
                    if response.status_code < 400:  # noqa: PLR2004
                        return
                    break
        response = await api.http.post(
            api.list_url, json={"body": body}, headers=api.headers
        )
        _raise(response, info.forge, "comment")

    async def _comment_api(self, info: RepoInfo, number: int) -> _CommentApi:
        if info.forge == "github" and self.github is not None:
            c = self.github
            installation = await c.installation_for_repo(info.name)
            if installation is None:
                msg = f"no GitHub installation for {info.name}"
                raise ForgeError(msg)
            token = await c.installation_token(installation, (info.repo,))
            base = f"{c.api_url}/repos/{info.name}/issues"
            return _CommentApi(
                c.http,
                {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                },
                f"{base}/{number}/comments",
                f"{base}/comments/{{id}}",
                "PATCH",
            )
        if info.forge == "gitea" and self.gitea is not None:
            g = self.gitea
            base = f"{g.instance_url}/api/v1/repos/{info.name}/issues"
            return _CommentApi(
                g.http,
                g.auth_headers(),
                f"{base}/{number}/comments",
                f"{base}/comments/{{id}}",
                "PATCH",
            )
        if info.forge == "gitlab" and self.gitlab is not None:
            gl = self.gitlab
            base = f"{gl.project_api_url(info.owner, info.repo)}/merge_requests/{number}/notes"
            return _CommentApi(
                gl.http, gl.auth_headers(), base, f"{base}/{{id}}", "PUT"
            )
        msg = f"no {info.forge} client configured"
        raise ForgeError(msg)

    async def is_self(self, forge_user: str) -> bool:
        """Whether `forge_user` is nixbot's own account, so effects that
        comment "/cmd" cannot trigger themselves. GitHub marks app
        comments as Bot in the webhook already. Token forges need the
        token's user, fetched once."""
        forge, _, login = forge_user.partition(":")
        client = {"gitea": self.gitea, "gitlab": self.gitlab}.get(forge)
        if client is None:
            return False
        if forge not in _BOT_LOGINS:
            base = (
                f"{client.instance_url}/api/v1/user"
                if forge == "gitea"
                else f"{client.instance_url}/api/v4/user"
            )
            response = await client.http.get(base, headers=client.auth_headers())
            _raise(response, forge, "token user lookup")
            data = response.json()
            _BOT_LOGINS[forge] = data.get("login") or data.get("username") or ""
        return login.lower() == _BOT_LOGINS[forge].lower()

    async def permission(self, project: Project, forge_user: str | None) -> str | None:
        """read|write|admin, or None. `forge_user` is "forge:login"."""
        if not forge_user:
            return None
        forge, _, login = forge_user.partition(":")
        if forge != project.forge or not login:
            return None
        return await self._access.permission(
            forge,
            login,
            RepoRef(
                forge=project.forge,
                forge_repo_id=project.forge_repo_id,
                owner=project.owner,
                name=project.name,
            ),
        )
