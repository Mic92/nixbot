"""Endpoints effects call with their per-run task token."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from ..forge import ForgeError  # noqa: TID252
from ..repos import RepoStore, repo_info  # noqa: TID252

if TYPE_CHECKING:
    from ..effects_state import TaskClaim, TaskTokens  # noqa: TID252
    from .app import WebContext

logger = logging.getLogger(__name__)

MAX_COMMENT_SIZE = 60_000


class PrCommentRequest(BaseModel):
    body: str
    marker: str | None = None


def create_task_router(ctx: WebContext, tokens: TaskTokens) -> APIRouter:
    router = APIRouter()

    def _claim(request: Request) -> TaskClaim:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        claim = tokens.claim_for(token) if scheme.lower() == "bearer" else None
        if claim is None:
            raise HTTPException(status_code=401, detail="invalid task token")
        return claim

    @router.post("/api/v1/pr-comment")
    async def pr_comment(request: Request, body: PrCommentRequest) -> dict:
        """Comment on the pull request this effect run is about. With
        `marker`, a previous comment carrying it is edited instead."""
        claim = _claim(request)
        pr_number = claim.identity.pr_number if claim.identity else None
        if pr_number is None:
            raise HTTPException(status_code=403, detail="this run has no pull request")
        if len(body.body) > MAX_COMMENT_SIZE:
            raise HTTPException(status_code=413, detail="comment too large")
        project = await RepoStore(ctx.pool).by_id(claim.project_id)
        if project is None or ctx.forge_pr is None:
            raise HTTPException(status_code=404, detail="project not found")
        try:
            await ctx.forge_pr.comment(
                repo_info(project), pr_number, body.body, body.marker
            )
        except ForgeError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {}

    return router
