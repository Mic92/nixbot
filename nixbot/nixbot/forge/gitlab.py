"""GitLab client: personal/group/project access token (api scope),
discovery via /api/v4/projects?membership=true. No GitHub-App
equivalent exists, so auth follows the Gitea model."""

from __future__ import annotations

from urllib.parse import quote

from .base import DiscoveredRepo, TokenForgeClient


class GitlabClient(TokenForgeClient):
    forge_name = "GitLab"

    def auth_headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def project_api_url(self, owner: str, repo: str) -> str:
        # GitLab accepts the URL-encoded full path wherever it takes a
        # numeric project id. Namespaces may be nested (a/b/c).
        return (
            f"{self.instance_url}/api/v4/projects/{quote(f'{owner}/{repo}', safe='')}"
        )

    async def discover_repos(self) -> list[DiscoveredRepo]:
        repos = []
        for repo in await self.paginated(
            f"{self.instance_url}/api/v4/projects"
            "?membership=true&archived=false&per_page=100"
        ):
            owner, _, name = repo["path_with_namespace"].rpartition("/")
            repos.append(
                DiscoveredRepo(
                    forge="gitlab",
                    forge_repo_id=str(repo["id"]),
                    owner=owner,
                    repo=name,
                    default_branch=repo.get("default_branch") or "main",
                    clone_url=repo["http_url_to_repo"],
                    private=repo.get("visibility") != "public",
                    topics=tuple(repo.get("topics") or ()),
                )
            )
        return repos
