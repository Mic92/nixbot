"""Approval gate for pull requests from outside contributors.

GitHub only: the webhook carries `author_association`; PRs whose head
branch is in the base repository are trusted regardless. Untrusted PRs
get an `action_required` check run with an approve button instead of a
build. The click arrives as `check_run.requested_action` (GitHub shows
the button only to users with write access). The web UI offers the
same to repo writers. Approval unlocks the PR, later pushes build.
Repositories opt out with `require_approval = false` in nixbot.toml.
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Protocol

from .db_gen import approvals as q
from .webhooks import ChangeRequest

if TYPE_CHECKING:
    import asyncpg

    from .config import PrApprovalConfig
    from .events import RepoInfo


def untrusted(config: PrApprovalConfig, change: ChangeRequest) -> bool:
    if not config.enable or change.forge != "github" or change.pr_number is None:
        return False
    # Pushing the head branch to the base repo already needed write
    # access. Covers GitHub Apps/bots, which always report NONE.
    if change.head_in_base_repo:
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
    """Posts the gate. ForgeStatusReporter, so later builds of the
    commit know to replace it."""

    async def approval_required(
        self, repo: RepoInfo, sha: str, pr_number: int
    ) -> None: ...
