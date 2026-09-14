"""Approval gate for pull requests from outside contributors.

GitHub only: the webhook carries `author_association`. Untrusted PRs
get an `action_required` check run with an approve button instead of a
build. The click arrives as `check_run.requested_action` (GitHub shows
the button only to users with write access). The web UI offers the
same to repo writers. Approval unlocks the PR, later pushes build.
Repositories opt out with `require_approval = false` in nixbot.toml.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Protocol

from .db_gen import approvals as q
from .webhooks import APPROVE_ACTION, APPROVE_EXTERNAL_PREFIX, ChangeRequest

if TYPE_CHECKING:
    import asyncpg

    from .config import PrApprovalConfig
    from .forge import GitHubAppClient
    from .status import CheckRunIds

logger = logging.getLogger(__name__)


def untrusted(config: PrApprovalConfig, change: ChangeRequest) -> bool:
    if not config.enable or change.forge != "github" or change.pr_number is None:
        return False
    return (change.author_association or "NONE") not in config.trusted_associations


async def gate(
    pool: asyncpg.Pool, project_id: int, pr_number: int, change: ChangeRequest
) -> None:
    await q.gate_pr(
        pool,
        project_id=project_id,
        pr_number=pr_number,
        pending=json.dumps(dataclasses.asdict(change)),
    )


async def approve(
    pool: asyncpg.Pool, project_id: int, pr_number: int, actor: str | None
) -> ChangeRequest | None:
    """Mark approved (creating the row if the PR was never gated).
    Returns the held change, if any."""
    pending = await q.approve_pr(
        pool, project_id=project_id, pr_number=pr_number, approved_by=actor
    )
    return None if pending is None else ChangeRequest(**json.loads(pending))


class GatePoster(Protocol):
    async def post_gate(  # noqa: PLR0913
        self,
        project_id: int,
        owner: str,
        repo: str,
        sha: str,
        pr_number: int,
        details_url: str,
    ) -> None: ...


class GitHubGatePoster:
    def __init__(
        self, client: GitHubAppClient, store: CheckRunIds, context_prefix: str
    ) -> None:
        self.client = client
        self.store = store
        self.name = f"{context_prefix}/nix-eval"

    async def post_gate(  # noqa: PLR0913
        self,
        project_id: int,
        owner: str,
        repo: str,
        sha: str,
        pr_number: int,
        details_url: str,
    ) -> None:
        installation_id = await self.client.installation_for_repo(f"{owner}/{repo}")
        if installation_id is None:
            return
        token = await self.client.installation_token(installation_id)
        # Evaluation is the first thing approval unlocks. Same name as
        # the eval status and registered in the check-run store, so the
        # approved build PATCHes this run instead of leaving it stale.
        body = {
            "name": self.name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": "action_required",
            "external_id": f"{APPROVE_EXTERNAL_PREFIX}{pr_number}",
            "details_url": details_url,
            "output": {
                "title": "Waiting for maintainer approval",
                "summary": (
                    "This pull request is from an outside contributor. "
                    "A maintainer must approve CI for it."
                ),
            },
            "actions": [
                {
                    "label": "Approve CI",
                    "description": "Build this PR and later pushes to it",
                    "identifier": APPROVE_ACTION,
                }
            ],
        }
        response = await self.client.http.post(
            f"{self.client.api_url}/repos/{owner}/{repo}/check-runs",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json=body,
        )
        if response.status_code >= 400:  # noqa: PLR2004
            logger.error(
                "failed to post approval gate",
                extra={
                    "repo": f"{owner}/{repo}",
                    "status": response.status_code,
                    "body": response.text[:500],
                },
            )
            return
        await self.store.set(
            project_id, sha, self.name, None, int(response.json()["id"])
        )
