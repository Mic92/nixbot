"""The part of the Buildkite REST API that tack uses, see tangled.py.

Tack is pointed here with TACK_BUILDKITE_API_URL=<nixbot url>/buildkite.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..db_gen import projects as projects_q  # noqa: TID252
from ..db_gen import tangled as q  # noqa: TID252
from ..executor import attribute_log_path  # noqa: TID252
from ..sql_util import expect  # noqa: TID252
from ..tangled import CANCELED, FAILED, FORGE  # noqa: TID252
from ..webhooks import ChangeRequest  # noqa: TID252
from .logs import attribute_log_text, effect_run_text, plain_log_text

if TYPE_CHECKING:
    from pathlib import Path

    from ..db_gen.models import Project, TangledBuild  # noqa: TID252
    from ..service import CIService  # noqa: TID252
    from ..tangled import Job, TangledForge  # noqa: TID252
    from .logs import LogRegistry


_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class CreateBuild(BaseModel):
    commit: str
    branch: str = Field(min_length=1)
    message: str = ""
    env: dict[str, str] = {}
    meta_data: dict[str, str] = {}
    pull_request_base_branch: str = ""


@dataclass
class _Routes:
    forge: TangledForge
    service: CIService
    registry: LogRegistry
    state_dir: Path

    def authorize(self, request: Request) -> None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            token.encode(), self.forge.config.api_token.encode()
        ):
            raise HTTPException(status_code=401, detail="invalid API token")

    async def _project(self, org: str, slug: str) -> Project:
        config = self.forge.config
        project = None
        if org == config.organization and slug in config.repositories:
            project = await projects_q.project_by_forge_id(
                self.forge.pool, forge=FORGE, forge_repo_id=slug
            )
        if project is None or not project.enabled:
            raise HTTPException(status_code=404, detail="no such pipeline")
        return project

    async def _build(
        self, org: str, slug: str, number: int
    ) -> tuple[Project, TangledBuild]:
        project = await self._project(org, slug)
        row = await q.tangled_build_by_number(
            self.forge.pool, project_id=project.id_, number=number
        )
        if row is None:
            raise HTTPException(status_code=404, detail="no such build")
        return project, row

    async def create_build(
        self, org: str, slug: str, body: CreateBuild
    ) -> JSONResponse:
        project = await self._project(org, slug)
        # An abbreviated commit could name another object later.
        if not _FULL_SHA.fullmatch(body.commit):
            raise HTTPException(status_code=422, detail="commit must be a full SHA")
        pool = self.forge.pool
        base = body.pull_request_base_branch or None
        tag = None
        if base is None and body.branch.startswith("refs/tags/"):
            tag = body.branch.removeprefix("refs/tags/")
        pr_number = None
        if base is not None:
            pr_number = await self.forge.pull_number(project.id_, body.branch, base)
        # The branch of the nixbot event: PRs are keyed by their target.
        branch = tag or base or body.branch
        # Tack retries a pipeline by creating its builds again. A new
        # change would only replay the old verdict, so the failed build
        # runs once more instead.
        previous = await q.last_linked_tangled_build(
            pool,
            project_id=project.id_,
            commit_sha=body.commit,
            branch=branch,
            tag=tag,
            pr_number=pr_number,
        )
        retried = None
        if previous is not None and previous.state in (FAILED, CANCELED):
            retried = previous.build_id
        row = expect(
            await q.create_tangled_build(
                pool,
                project_id=project.id_,
                commit_sha=body.commit,
                branch=body.branch,
                base_branch=base,
                tag=tag,
                pr_number=pr_number,
                message=body.message,
                meta_data=json.dumps(body.meta_data),
                build_id=retried,
            )
        )
        if retried is not None:
            await self.service.restart_build(retried)
        else:
            actor = body.env.get("TACK_ACTOR")
            actor = f"{FORGE}:{actor}" if actor else None
            await self.service.submit(
                ChangeRequest(
                    forge=FORGE,
                    forge_repo_id=slug,
                    branch=branch,
                    commit_sha=body.commit,
                    pr_number=pr_number,
                    pr_author=actor if base else None,
                    # The tip of the target branch, fetched with the head.
                    base_sha=f"refs/heads/{base}" if base else None,
                    actor=actor,
                    head_in_base_repo=True,
                    head_ref=body.branch if base else None,
                    tag=tag,
                )
            )
        return JSONResponse(await self.forge.build_json(project, row), status_code=201)

    async def get_build(self, org: str, slug: str, number: int) -> dict[str, Any]:
        project, row = await self._build(org, slug, number)
        return await self.forge.build_json(project, row)

    async def _job_log(self, row: TangledBuild, job: Job) -> str | None:
        if job.kind in ("eval", "error"):
            return job.ref
        if job.kind == "effect":
            return await effect_run_text(self.registry, self.state_dir, int(job.ref))
        if row.build_id is None:
            return None
        return await attribute_log_text(
            self.forge.pool,
            self.registry,
            row.build_id,
            job.ref,
            attribute_log_path(self.state_dir, row.build_id, job.ref),
        )

    async def get_job_log(
        self, org: str, slug: str, number: int, job_id: str
    ) -> PlainTextResponse:
        _, row = await self._build(org, slug, number)
        job = next((j for j in await self.forge.jobs(row) if j.id == job_id), None)
        text = await self._job_log(row, job) if job is not None else None
        if text is None:
            # Tack shows an empty step for a job that has not logged yet.
            raise HTTPException(status_code=404, detail="no log")
        return PlainTextResponse(
            await asyncio.to_thread(plain_log_text, text, None, ansi=False)
        )


def create_tangled_router(
    forge: TangledForge, service: CIService, registry: LogRegistry, state_dir: Path
) -> APIRouter:
    routes = _Routes(forge, service, registry, state_dir)
    router = APIRouter(
        prefix="/buildkite/v2/organizations/{org}/pipelines/{slug}/builds",
        dependencies=[Depends(routes.authorize)],
    )
    router.post("", status_code=201)(routes.create_build)
    router.get("/{number}")(routes.get_build)
    router.get("/{number}/jobs/{job_id}/log")(routes.get_job_log)
    return router
