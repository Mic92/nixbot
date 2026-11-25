"""Tangled support through tack's Buildkite provider.

Tangled has no API to post a commit status to. CI runs on a spindle,
which the Tangled web app queries for pipelines and logs. Tack is a
spindle that hands the work to an external CI through that CI's API,
and nixbot serves the part of the Buildkite API tack uses
(web/tangled_routes.py). So tack creates a build per workflow, nixbot
reports state changes to tack's Buildkite webhook, and tack fetches
jobs and logs when someone looks at the pipeline.

A tack build is a `tangled_builds` row. It follows the nixbot build
that the queued change turns into, by way of the reporter below.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from .db import BuildStatus
from .db_gen import builds as builds_q
from .db_gen import projects as projects_q
from .db_gen import tangled as q
from .forge.retry import make_client
from .sql_util import expect

if TYPE_CHECKING:
    from datetime import datetime

    import asyncpg

    from .config import TangledConfig
    from .db import BuildRecord
    from .db_gen.models import Project, TangledBuild
    from .events import BuildResult, ChangeEvent, EvalReport, StatusReporter
    from .webhooks import ChangeRequest

logger = logging.getLogger(__name__)

FORGE = "tangled"

# Tack maps exactly these Buildkite build states and ignores the rest.
SCHEDULED = "scheduled"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
CANCELED = "canceled"

_BUILD_STATES = {
    BuildStatus.SUCCEEDED: PASSED,
    BuildStatus.FAILED: FAILED,
    BuildStatus.CANCELLED: CANCELED,
}
_EVAL_STATES = {
    BuildStatus.PENDING: SCHEDULED,
    BuildStatus.EVALUATING: RUNNING,
    BuildStatus.FAILED: FAILED,
    BuildStatus.CANCELLED: CANCELED,
}
_ATTRIBUTE_STATES = {
    "pending": SCHEDULED,
    "building": RUNNING,
    "succeeded": PASSED,
    "skipped_local": PASSED,
    "ignored_failure": PASSED,
    "cancelled": CANCELED,
}
_EFFECT_STATES = {
    "pending": SCHEDULED,
    "running": RUNNING,
    "succeeded": PASSED,
    "cancelled": CANCELED,
}
_WEBHOOK_EVENTS = {SCHEDULED: "build.scheduled", RUNNING: "build.running"}

DELIVERY_INTERVAL = 30


def owner_and_name(slug: str, url: str) -> tuple[str, str]:
    """Project owner and name from the repository URL, e.g.
    https://tangled.org/alice.example/repo."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if len(parts) >= 2:  # noqa: PLR2004
        return parts[-2].removeprefix("@"), parts[-1].removesuffix(".git")
    return FORGE, slug


@dataclass(frozen=True)
class Job:
    """One step of a pipeline as the Tangled web app shows it."""

    id: str
    name: str
    state: str
    # "eval", "attr", "effect" or "error", with what `ref` means: the
    # log text itself, the attribute or the effect run id.
    kind: str
    ref: str

    def as_json(self) -> dict[str, str]:
        return {"id": self.id, "type": "script", "name": self.name, "state": self.state}


def _job(row: TangledBuild, kind: str, name: str, state: str, ref: str) -> Job:
    return Job(str(uuid.uuid5(row.id_, f"{kind}:{name}")), name, state, kind, ref)


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class TangledForge:
    def __init__(
        self,
        pool: asyncpg.Pool,
        config: TangledConfig,
        base_url: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.pool = pool
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.http = http or make_client()
        self._wake = asyncio.Event()

    async def pull_number(self, project_id: int, source: str, target: str) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            await builds_q.lock_build_identity(conn, key=f"tangled-pull:{project_id}")
            number = await q.tangled_pull_number(
                conn, project_id=project_id, source_branch=source, target_branch=target
            )
            if number is None:
                number = await q.allocate_tangled_pull(
                    conn,
                    project_id=project_id,
                    source_branch=source,
                    target_branch=target,
                )
            return expect(number)

    async def advance(self, event: ChangeEvent, build: BuildRecord, state: str) -> None:
        await q.advance_tangled_builds(
            self.pool,
            state=state,
            build_id=build.id_,
            project_id=event.repo.id,
            commit_sha=event.commit_sha,
            branch=event.branch,
            tag=event.tag,
            pr_number=event.pr_number,
        )
        self._wake.set()

    async def settle_unbuilt(self, change: ChangeRequest, error: str | None) -> None:
        """The change is done and may have produced no build: skipped
        by a marker, superseded by a newer commit, or failed before a
        build existed. Tack would wait for such a build forever."""
        project = await projects_q.project_by_forge_id(
            self.pool, forge=FORGE, forge_repo_id=change.forge_repo_id
        )
        if project is None:
            return
        await q.settle_unbuilt_tangled_builds(
            self.pool,
            state=CANCELED if error is None else FAILED,
            error=error or "nixbot did not build this commit",
            project_id=project.id_,
            commit_sha=change.commit_sha,
            branch=change.branch,
            tag=change.tag,
            pr_number=change.pr_number,
        )
        self._wake.set()

    async def jobs(self, row: TangledBuild) -> list[Job]:
        if row.build_id is None:
            if row.error is None:
                return []
            return [_job(row, "error", "nixbot", row.state, row.error)]
        build = await builds_q.get_build(self.pool, id_=row.build_id)
        if build is None:
            return []
        attributes = await q.tangled_build_attributes(self.pool, build_id=build.id_)
        # Cached attributes ran nothing and have no log. A flake has
        # hundreds of them, each a step and a request to tack.
        built = [
            a
            for a in attributes
            if a.status != "skipped_local"
            and not (a.status == "succeeded" and a.cached)
        ]
        eval_state = (
            PASSED if build.eval_completed else _EVAL_STATES.get(build.status, PASSED)
        )
        summary = build.error or (
            f"{len(attributes)} attributes, {len(attributes) - len(built)} cached\n"
        )
        jobs = [_job(row, "eval", "nix-eval", eval_state, summary)]
        jobs += [
            _job(row, "attr", a.attr, _ATTRIBUTE_STATES.get(a.status, FAILED), a.attr)
            for a in built
        ]
        jobs += [
            _job(
                row,
                "effect",
                f"effect {e.name}",
                _EFFECT_STATES.get(e.status, FAILED),
                str(e.id_),
            )
            for e in await builds_q.effects_for_build(self.pool, build_id=build.id_)
            if e.status != "skipped"
        ]
        return jobs

    async def build_json(self, project: Project, row: TangledBuild) -> dict[str, Any]:
        web_url = f"{self.base_url}/repos/{FORGE}/{project.owner}/{project.name}"
        if row.build_id is not None:
            build = await builds_q.get_build(self.pool, id_=row.build_id)
            if build is not None:
                web_url += f"/builds/{build.number}"
        return {
            "id": str(row.id_),
            "number": row.number,
            "state": row.state,
            "web_url": web_url,
            "commit": row.commit_sha,
            "branch": row.branch,
            "message": row.message,
            "meta_data": json.loads(row.meta_data),
            "pull_request": (
                {"id": str(row.pr_number), "base": row.base_branch}
                if row.pr_number is not None
                else None
            ),
            "pipeline": {"slug": project.forge_repo_id, "name": project.name},
            "jobs": [job.as_json() for job in await self.jobs(row)],
            "created_at": _timestamp(row.created_at),
            "started_at": _timestamp(row.started_at),
            "finished_at": _timestamp(row.finished_at),
        }

    async def _notify(self, row: TangledBuild) -> None:
        project = await projects_q.project_by_id(self.pool, id_=row.project_id)
        if project is None:
            return
        event = _WEBHOOK_EVENTS.get(row.state, "build.finished")
        body = json.dumps(
            {
                "event": event,
                "build": await self.build_json(project, row),
                "pipeline": {"slug": project.forge_repo_id, "name": project.name},
                "organization": {"slug": self.config.organization},
            }
        ).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(
            self.config.webhook_secret.encode(),
            timestamp.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        response = await self.http.post(
            self.config.webhook_url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Buildkite-Event": event,
                "X-Buildkite-Signature": f"timestamp={timestamp},signature={signature}",
            },
        )
        response.raise_for_status()
        await q.mark_tangled_notified(self.pool, id_=row.id_, notified_state=row.state)

    async def deliver_pending(self) -> None:
        """Tell tack the current state of every build it has not seen.
        Intermediate states a build went through meanwhile are skipped."""
        for row in await q.pending_tangled_notifications(self.pool):
            try:
                await self._notify(row)
            except httpx.HTTPError:
                # Stays pending, the loop retries it.
                logger.exception(
                    "tack webhook failed", extra={"tangled_build": row.number}
                )

    async def delivery_loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.deliver_pending()
            except Exception:
                logger.exception("tack webhook delivery failed")
            try:
                async with asyncio.timeout(DELIVERY_INTERVAL):
                    await self._wake.wait()
            except TimeoutError:
                pass


@dataclass
class TangledReporter:
    """Moves tack's builds along with the nixbot build they wait on.
    Events of other forges go to `inner`."""

    forge: TangledForge
    inner: StatusReporter

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        if event.repo.forge != FORGE:
            return await self.inner.build_started(event, build)
        return await self.forge.advance(event, build, RUNNING)

    async def eval_finished(
        self, event: ChangeEvent, build: BuildRecord, report: EvalReport
    ) -> None:
        # A failed evaluation ends in build_finished like any other.
        if event.repo.forge != FORGE:
            await self.inner.eval_finished(event, build, report)

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        if event.repo.forge != FORGE:
            await self.inner.eval_cancelled(event, build)

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        if event.repo.forge != FORGE:
            return await self.inner.build_finished(event, build, result)
        return await self.forge.advance(
            event, build, _BUILD_STATES.get(result.status, RUNNING)
        )

    async def build_restarted(
        self, event: ChangeEvent, build: BuildRecord, attr: str | None
    ) -> None:
        if event.repo.forge != FORGE:
            return await self.inner.build_restarted(event, build, attr)
        return await self.forge.advance(event, build, RUNNING)

    async def effect_started(
        self, event: ChangeEvent, build: BuildRecord, name: str
    ) -> None:
        if event.repo.forge != FORGE:
            await self.inner.effect_started(event, build, name)

    async def effect_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        if event.repo.forge != FORGE:
            await self.inner.effect_finished(
                event, build, name, success=success, error=error
            )

    async def effects_started(
        self, event: ChangeEvent, build: BuildRecord, total: int
    ) -> None:
        # A pipeline has one status: it is not done while effects run.
        if event.repo.forge != FORGE:
            return await self.inner.effects_started(event, build, total)
        return await self.forge.advance(event, build, RUNNING)

    async def effects_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        failed: int,
        succeeded: int,
    ) -> None:
        if event.repo.forge != FORGE:
            return await self.inner.effects_finished(
                event, build, failed=failed, succeeded=succeeded
            )
        return await self.forge.advance(
            event, build, FAILED if failed else _BUILD_STATES.get(build.status, PASSED)
        )
