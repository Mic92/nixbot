"""Startup reconciliation: after downtime, build the
default-branch head and open-PR heads that have no build record yet.

Runs after crash recovery. Duplicates are resolved by the
supersede rules. "Built" means any build record exists for the head
commit in that project, regardless of result — exact merge-tree
checking happens once the orchestrator computes the tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .db_gen import maintenance as q
from .webhooks import ChangeRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .repos import RepoRecord
    from .webhooks import ChangeSink

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteHead:
    branch: str
    commit_sha: str
    pr_number: int | None = None
    pr_author: str | None = None
    base_sha: str | None = None
    # Forge-clock PR update time. Feeds the reconcile watermark.
    updated_at: datetime | None = None


def max_pr_updated(heads: list[RemoteHead]) -> datetime | None:
    """Newest PR update time seen. The next reconcile's watermark."""
    times = [h.updated_at for h in heads if h.updated_at is not None]
    return max(times) if times else None


async def _collect_until(
    pages: AsyncIterator[list[dict[str, Any]]],
    updated_since: datetime | None,
) -> list[dict[str, Any]]:
    """Collect update-sorted (desc) PRs, stopping pagination at the
    first PR not updated since the watermark (inclusive boundary:
    equal timestamps are kept, is_built dedupes them)."""
    results: list[dict[str, Any]] = []
    async for page in pages:
        for pull in page:
            if (
                updated_since is not None
                and datetime.fromisoformat(pull["updated_at"]) < updated_since
            ):
                return results
            results.append(pull)
    return results


async def github_heads(
    client: GitHubAppClient,
    project: RepoRecord,
    updated_since: datetime | None = None,
) -> list[RemoteHead]:
    installation_id = await client.installation_for_repo(
        f"{project.owner}/{project.name}"
    )
    if installation_id is None:
        return []
    token = await client.installation_token(installation_id)
    repo_url = f"{client.api_url}/repos/{project.owner}/{project.name}"
    heads = []
    response = await client.http.get(
        f"{repo_url}/branches/{quote(project.default_branch, safe='')}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["sha"],
            )
        )
    pulls = await _collect_until(
        client.paginated_pages(
            f"{repo_url}/pulls?state=open&per_page=100&sort=updated&direction=desc",
            token,
        ),
        updated_since,
    )
    heads.extend(
        RemoteHead(
            branch=pull["base"]["ref"],
            commit_sha=pull["head"]["sha"],
            pr_number=pull["number"],
            pr_author=f"github:{pull['user']['login']}",
            # base.sha is frozen at PR creation. Merge into the current
            # base branch tip instead (see webhooks._parse_pr_event).
            base_sha=f"refs/heads/{pull['base']['ref']}",
            updated_at=datetime.fromisoformat(pull["updated_at"]),
        )
        for pull in pulls
    )
    return heads


async def gitea_heads(
    client: GiteaClient,
    project: RepoRecord,
    updated_since: datetime | None = None,
) -> list[RemoteHead]:
    repo_url = f"{client.instance_url}/api/v1/repos/{project.owner}/{project.name}"
    heads = []
    response = await client.http.get(
        f"{repo_url}/branches/{quote(project.default_branch, safe='')}",
        headers=client.auth_headers(),
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["id"],
            )
        )
    pulls = await _collect_until(
        client.paginated_pages(
            f"{repo_url}/pulls?state=open&limit=50&sort=recentupdate"
        ),
        updated_since,
    )
    heads.extend(
        RemoteHead(
            branch=pull["base"]["ref"],
            commit_sha=pull["head"]["sha"],
            pr_number=pull["number"],
            pr_author=f"gitea:{pull['user']['login']}",
            base_sha=f"refs/heads/{pull['base']['ref']}",
            updated_at=datetime.fromisoformat(pull["updated_at"]),
        )
        for pull in pulls
    )
    return heads


async def gitlab_heads(
    client: GitlabClient,
    project: RepoRecord,
    updated_since: datetime | None = None,
) -> list[RemoteHead]:
    repo_url = client.project_api_url(project.owner, project.name)
    heads = []
    response = await client.http.get(
        f"{repo_url}/repository/branches/{quote(project.default_branch, safe='')}",
        headers=client.auth_headers(),
    )
    if response.status_code < 400:  # noqa: PLR2004
        heads.append(
            RemoteHead(
                branch=project.default_branch,
                commit_sha=response.json()["commit"]["id"],
            )
        )
    mr_url = f"{repo_url}/merge_requests?state=opened&per_page=100"
    if updated_since is not None:
        # updated_after is exclusive, but the watermark equals a seen
        # MR's updated_at. Back off one second to keep the boundary
        # inclusive like the other forges (is_built dedupes overlap).
        cutoff = (updated_since - timedelta(seconds=1)).isoformat()
        mr_url += f"&updated_after={quote(cutoff, safe='')}"
    pulls = await client.paginated(mr_url)
    heads.extend(
        RemoteHead(
            branch=pull["target_branch"],
            commit_sha=pull["sha"],
            pr_number=pull["iid"],
            pr_author=f"gitlab:{pull['author']['username']}",
            # The MR API exposes no base sha. Merge against the
            # target branch ref instead.
            base_sha=f"refs/heads/{pull['target_branch']}",
            updated_at=datetime.fromisoformat(pull["updated_at"]),
        )
        for pull in pulls
    )
    return heads


async def _has_builds(pool: asyncpg.Pool, project_id: int) -> bool:
    """Cancelled builds don't count: an operator cancelling the storm
    after enabling must not turn the project non-fresh."""
    return await q.project_has_builds(pool, project_id=project_id) is not None


async def is_built(pool: asyncpg.Pool, project_id: int, commit_sha: str) -> bool:
    return (
        await q.commit_built(pool, project_id=project_id, commit_sha=commit_sha)
        is not None
    )


async def reconcile_repo(
    pool: asyncpg.Pool,
    project: RepoRecord,
    heads: list[RemoteHead],
    sink: ChangeSink,
) -> int:
    """Submit change events for unbuilt heads. Returns submit count.

    A project without any non-cancelled build record is fresh, not
    recovering from downtime: build the default branch only, the
    open-PR backlog would be stale work. PRs build on their next
    push."""
    first_contact = not await _has_builds(pool, project.id_)
    submitted = 0
    for head in heads:
        if first_contact and head.pr_number is not None:
            continue
        if await is_built(pool, project.id_, head.commit_sha):
            continue
        logger.info(
            "reconciliation: building unbuilt head",
            extra={
                "project": f"{project.owner}/{project.name}",
                "branch": head.branch,
                "pr": head.pr_number,
            },
        )
        await sink.submit(
            ChangeRequest(
                forge=project.forge,
                forge_repo_id=project.forge_repo_id,
                branch=head.branch,
                commit_sha=head.commit_sha,
                pr_number=head.pr_number,
                pr_author=head.pr_author,
                base_sha=head.base_sha,
            )
        )
        submitted += 1
    return submitted
