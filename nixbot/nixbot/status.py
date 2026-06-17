"""Commit status / check-run reporting.

GitHub uses the Check Runs API (create + PATCH by stored id);
Gitea/GitLab keep posting commit statuses. Check-run *names* and
commit-status *contexts* share the required-checks namespace, so
branch protection rules are unaffected.

Combined per-phase contexts are `nixbot/nix-eval` (warning count
appended to the description) and `nixbot/nix-build`; the prefix is
configurable via status_context_prefix, e.g. "buildbot" to keep
branch protection rules from a buildbot-nix deployment working.
Per-attribute failure statuses (`nixbot/nix-build
<forge>:<owner>/<repo>#checks.<attr>`) cover failing/cancelled
attributes, capped by failedBuildReportLimit (default 47).

Failed per-attribute statuses are persisted per revision
(failed_statuses table, port of db/failed_status.py) so a later
rebuild flips them to success — including force-running already-built
attributes (the orchestrator feeds them to the scheduler as
force_attrs). Status posts carry the build's monotonic generation;
stale posts (lower generation than the last one sent for that build)
are dropped.

Target URLs point at the service's own URL scheme
(/repos/<forge>/<owner>/<name>/builds/<number>), independent of the frontend tasks.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol
from urllib.parse import quote

import httpx

from .ansi import strip_ansi
from .db_gen import failed as q
from .forge import ForgeError

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

    from .build_scheduler import AttributeResult
    from .db import BuildRecord
    from .events import BuildResult, ChangeEvent
    from .forge import GiteaClient, GitHubAppClient, GitlabClient
    from .models import NixEvalJobSuccess

# GitHub caps output.text at 65535 chars.
CHECK_RUN_TEXT_LIMIT = 60_000

logger = logging.getLogger(__name__)

# Cap on remembered (build id -> posted generation) entries; one entry
# per build forever would be a slow leak in a long-lived process.
POSTED_GENERATIONS_MAX = 1024

FAILED_STATUS_STATES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure", "cancelled"}
)


class StatusState(StrEnum):
    pending = "pending"
    success = "success"
    failure = "failure"
    error = "error"


class StatusPostError(Exception):
    """HTTP-level status post failure; retry_after carries the forge's
    Retry-After / rate-limit-reset hint in seconds, if any."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CheckPermissionError(ForgeError):
    """The GitHub App lacks Checks: write; latched so we stop
    hammering the API until the operator fixes the permission."""


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
            # float() accepts nan/inf; nan would poison every later
            # min/max comparison down to asyncio.sleep.
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            with contextlib.suppress(TypeError, ValueError):
                dt = parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
    # GitHub primary rate limits: no Retry-After, only a reset epoch.
    if response.headers.get("X-RateLimit-Remaining") == "0":
        with contextlib.suppress(TypeError, ValueError):
            reset = int(response.headers["X-RateLimit-Reset"])
            return max(0.0, reset - time.time())
    return None


def _raise_for_status(response: httpx.Response, repo: str) -> None:
    if response.status_code >= httpx.codes.BAD_REQUEST:
        msg = f"status post for {repo} failed: HTTP {response.status_code}"
        raise StatusPostError(msg, retry_after=_retry_after_seconds(response))


class CommitStatusPoster(Protocol):
    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
        *,
        project_id: int = 0,
        build_id: int = 0,
        attr: str | None = None,
        text: str | None = None,
    ) -> None: ...


class CheckRunIds(Protocol):
    async def get(self, project_id: int, sha: str, name: str) -> int | None: ...

    async def set(
        self, project_id: int, sha: str, name: str, attr: str | None, external_id: int
    ) -> None: ...


class CheckRunStore:
    """(project, sha, name) → GitHub check-run id; lets the poster
    PATCH the existing run instead of stacking duplicates on a SHA."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def get(self, project_id: int, sha: str, name: str) -> int | None:
        return await q.get_check_run_id(
            self.pool, project_id=project_id, sha=sha, name=name
        )

    async def set(
        self, project_id: int, sha: str, name: str, attr: str | None, external_id: int
    ) -> None:
        await q.upsert_check_run(
            self.pool,
            project_id=project_id,
            sha=sha,
            name=name,
            attr=attr,
            external_id=external_id,
            timestamp=datetime.now(tz=UTC).timestamp(),
        )


# StatusState → (status, conclusion). "error" becomes cancelled so
# dashboards separate infra problems from CI verdicts.
_CHECK_RUN_FIELDS: dict[StatusState, tuple[str, str | None]] = {
    StatusState.pending: ("in_progress", None),
    StatusState.success: ("completed", "success"),
    StatusState.failure: ("completed", "failure"),
    StatusState.error: ("completed", "cancelled"),
}


def _check_run_output(context: str, summary: str, text: str | None) -> dict[str, str]:
    # The full context repeats the repo path; keep the title short.
    output = {"title": context.split(" ", 1)[0], "summary": summary}
    if text:
        if len(text) > CHECK_RUN_TEXT_LIMIT:
            text = text[:CHECK_RUN_TEXT_LIMIT] + "\n… (truncated)"
        output["text"] = text
    return output


def _raise_for_check_run(response: httpx.Response, repo: str) -> None:
    if response.status_code == httpx.codes.FORBIDDEN:
        msg = (
            f"GitHub check-run post for {repo} returned 403; grant the "
            "GitHub App the 'Checks: read & write' permission"
        )
        raise CheckPermissionError(msg)
    _raise_for_status(response, repo)


class GitHubCheckRunPoster:
    """Upserts GitHub check runs. external_id is set to our build id
    so a check_run rerequested webhook hands it straight back."""

    def __init__(self, client: GitHubAppClient, store: CheckRunIds) -> None:
        self.client = client
        self.store = store

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
        *,
        project_id: int = 0,
        build_id: int = 0,
        attr: str | None = None,
        text: str | None = None,
    ) -> None:
        installation_id = await self.client.installation_for_repo(f"{owner}/{repo}")
        if installation_id is None:
            # installation_for_repo already logged the failed lookup.
            return
        token = await self.client.installation_token(installation_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
        status, conclusion = _CHECK_RUN_FIELDS[state]
        body: dict[str, object] = {
            "name": context,
            "status": status,
            "external_id": str(build_id),
            "details_url": target_url,
            "output": _check_run_output(context, description, text),
        }
        if conclusion is not None:
            body["conclusion"] = conclusion

        base = f"{self.client.api_url}/repos/{owner}/{repo}/check-runs"
        run_id = await self.store.get(project_id, sha, context)
        if run_id is not None:
            response = await self.client.http.patch(
                f"{base}/{run_id}", headers=headers, json=body
            )
            # The DB row can outlive the GitHub run; recreate instead
            # of retrying the terminal summary forever.
            if response.status_code not in (httpx.codes.NOT_FOUND, httpx.codes.GONE):
                _raise_for_check_run(response, f"{owner}/{repo}")
                await self.store.set(project_id, sha, context, attr, run_id)
                return
        response = await self.client.http.post(
            base, headers=headers, json=body | {"head_sha": sha}
        )
        _raise_for_check_run(response, f"{owner}/{repo}")
        await self.store.set(project_id, sha, context, attr, int(response.json()["id"]))


class GiteaStatusPoster:
    def __init__(self, client: GiteaClient) -> None:
        self.client = client

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
        **_: object,
    ) -> None:
        response = await self.client.http.post(
            f"{self.client.instance_url}/api/v1/repos/{owner}/{repo}/statuses/{sha}",
            headers=self.client.auth_headers(),
            json={
                "state": state.value,
                "context": context,
                "description": description[:255],
                "target_url": target_url,
            },
        )
        _raise_for_status(response, f"{owner}/{repo}")


class GitlabStatusPoster:
    # GitLab has no "error" state; both map to failed.
    _STATES: ClassVar[dict[StatusState, str]] = {
        StatusState.pending: "pending",
        StatusState.success: "success",
        StatusState.failure: "failed",
        StatusState.error: "failed",
    }

    def __init__(self, client: GitlabClient) -> None:
        self.client = client

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
        **_: object,
    ) -> None:
        response = await self.client.http.post(
            f"{self.client.project_api_url(owner, repo)}/statuses/{sha}",
            headers=self.client.auth_headers(),
            json={
                "state": self._STATES[state],
                "context": context,
                "description": description[:255],
                "target_url": target_url,
            },
        )
        _raise_for_status(response, f"{owner}/{repo}")


class FailedStatusStorage(Protocol):
    async def mark_failed(self, revision: str, status_name: str) -> None: ...

    async def get_failed(self, revision: str) -> set[str]: ...

    async def clear(self, revision: str, status_name: str) -> None: ...


class FailedStatusStore:
    """Port of db/failed_status.py onto the service schema."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def mark_failed(self, revision: str, status_name: str) -> None:
        await q.upsert_failed_status(
            self.pool,
            revision=revision,
            status_name=status_name,
            timestamp=datetime.now(tz=UTC).timestamp(),
        )

    async def get_failed(self, revision: str) -> set[str]:
        return set(await q.failed_status_names(self.pool, revision=revision))

    async def clear(self, revision: str, status_name: str) -> None:
        await q.clear_failed_status(
            self.pool, revision=revision, status_name=status_name
        )


def _count_key(status: str) -> str:
    """Summary bucket for one attribute status."""
    if status == "cancelled":
        return "cancelled"
    return "failed" if status in FAILED_STATUS_STATES else "succeeded"


def attr_status_context(
    forge: str,
    project_name: str,
    attr: str,
    prefix: str = "checks",
    context_prefix: str = "nixbot",
) -> str:
    return f"{context_prefix}/nix-build {forge}:{project_name}#{prefix}.{attr}"


def eval_description(success: bool, warnings: list[str]) -> str:
    base = "evaluation succeeded" if success else "evaluation failed"
    if warnings:
        count = len(warnings)
        return f"{base} ({count} warning{'s' if count != 1 else ''})"
    return base


class ForgeStatusReporter:
    """Implements the orchestrator's StatusReporter protocol."""

    def __init__(
        self,
        posters: dict[str, CommitStatusPoster],
        failed_statuses: FailedStatusStorage,
        base_url: str,
        failed_build_report_limit: int = 47,
        context_prefix: str = "nixbot",
    ) -> None:
        # Keyed by forge so mixed GitHub+Gitea deployments post to the
        # right API.
        self.posters = posters
        self.failed_statuses = failed_statuses
        self.base_url = base_url.rstrip("/")
        self.failed_build_report_limit = failed_build_report_limit
        self.context_prefix = context_prefix
        # build id -> highest generation posted (drop stale posts).
        # Bounded LRU: stale-post races only matter around a build's
        # final re-aggregation, so old entries are safe to evict.
        self._posted_generations: OrderedDict[int, int] = OrderedDict()

    def build_url(self, event: ChangeEvent, build: BuildRecord) -> str:
        return f"{self.base_url}/repos/{event.repo.forge}/{event.repo.name}/builds/{build.number}"

    async def _post(  # noqa: PLR0913
        self,
        event: ChangeEvent,
        build: BuildRecord,
        context: str,
        state: StatusState,
        description: str,
        *,
        attr: str | None = None,
        text: str | None = None,
        propagate: bool = False,
    ) -> None:
        poster = self.posters.get(event.repo.forge)
        if poster is None:
            return
        try:
            await poster.post(
                event.repo.owner,
                event.repo.repo,
                event.commit_sha,
                context,
                state,
                # Descriptions may carry failure excerpts with raw ANSI
                # colors (kept for the web UI); forges show them verbatim.
                strip_ansi(description),
                self.build_url(event, build),
                project_id=event.repo.id,
                build_id=build.id,
                attr=attr,
                text=text,
            )
        except CheckPermissionError:
            # Per-org and not transient: log the hint and move on, never
            # latch off posting for the whole forge.
            logger.exception(
                "failed to post commit status",
                extra={"forge": event.repo.forge},
            )
        except (httpx.HTTPError, ForgeError, StatusPostError):
            # Transient failures must not propagate into the
            # orchestrator task and leave builds stuck — except the
            # terminal summary, whose failure drives the queued retry.
            if propagate:
                raise
            logger.exception(
                "failed to post commit status",
                extra={"build_id": build.id, "context": context},
            )

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.pending,
            "evaluating flake",
        )

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
        jobs: Sequence[NixEvalJobSuccess] | None = None,
    ) -> None:
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.success if success else StatusState.failure,
            eval_description(success, warnings),
            text=_fence("\n".join(warnings)) if warnings else None,
        )
        if success:
            await self._post(
                event,
                build,
                f"{self.context_prefix}/nix-build",
                StatusState.pending,
                "building attributes",
                text=_build_plan([j.attr for j in jobs], self.build_url(event, build))
                if jobs
                else None,
            )

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        """Resolve the pending eval context; see the orchestrator's
        cancel path."""
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.error,
            "build cancelled",
        )

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        generation = result.generation
        results = result.results
        attr_statuses = result.attr_statuses
        # Monotonic generation: drop stale posts after re-aggregation.
        if generation < self._posted_generations.get(build.id, 0):
            logger.info(
                "dropping stale status post",
                extra={"build_id": build.id, "generation": generation},
            )
            return
        self._posted_generations[build.id] = generation
        self._posted_generations.move_to_end(build.id)
        while len(self._posted_generations) > POSTED_GENERATIONS_MAX:
            self._posted_generations.popitem(last=False)

        counts = await self._post_attribute_statuses(
            event, build, results, result.attr_prefix
        )
        if attr_statuses is not None:
            # Reruns pass only the re-run subset as `results`: the
            # summary description must still cover the whole build.
            counts = {"failed": 0, "succeeded": 0, "cancelled": 0}
            for attr_status in attr_statuses.values():
                counts[_count_key(attr_status)] += 1
        table_statuses = attr_statuses or {r.attr: r.status.value for r in results}
        await self._post_summary(event, build, result.status, counts, table_statuses)

    async def _post_attribute_statuses(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        results: list[AttributeResult],
        attr_prefix: str,
    ) -> dict[str, int]:
        """Per-attribute failure statuses and success flips; returns
        failed/succeeded counts over `results`."""
        revision = event.commit_sha
        previously_failed = await self.failed_statuses.get_failed(revision)

        counts = {"failed": 0, "succeeded": 0, "cancelled": 0}
        reported = 0
        for result in results:
            context = attr_status_context(
                event.repo.forge,
                event.repo.name,
                result.attr,
                attr_prefix,
                context_prefix=self.context_prefix,
            )
            if result.status.value in FAILED_STATUS_STATES:
                counts[_count_key(result.status.value)] += 1
                if context not in previously_failed:
                    # Only new failures consume the report budget;
                    # previously-failed contexts always re-post so they
                    # can later flip to success.
                    if reported >= self.failed_build_report_limit:
                        continue
                    reported += 1
                await self.failed_statuses.mark_failed(revision, context)
                description = result.error or result.status.value
                await self._post(
                    event,
                    build,
                    context,
                    StatusState.failure,
                    description,
                    attr=result.attr,
                    text=_fence(result.error) if result.error else None,
                )
            else:
                counts["succeeded"] += 1
                if context in previously_failed:
                    # Success-flip for a previously failed status.
                    await self.failed_statuses.clear(revision, context)
                    await self._post(
                        event,
                        build,
                        context,
                        StatusState.success,
                        "succeeded",
                        attr=result.attr,
                    )
        return counts

    async def _post_summary(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        status: str,
        counts: dict[str, int],
        statuses: dict[str, str],
    ) -> None:
        if status == "succeeded":
            state = StatusState.success
            description = f"{counts['succeeded']} attributes built"
        elif status == "cancelled":
            state = StatusState.error
            # Attribute-level cancels aggregate like failures; only a
            # build-level cancel (no attribute info at all) was
            # superseded by a newer build.
            parts = [
                f"{counts[key]} {key}"
                for key in ("cancelled", "failed", "succeeded")
                if counts[key]
            ]
            description = ", ".join(parts) if parts else "build cancelled (superseded)"
        else:
            state = StatusState.failure
            description = (
                f"{counts['failed']} of {sum(counts.values())} attributes failed"
                if counts["failed"]
                else (build.tree_hash and "build failed") or "merge conflict"
            )
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-build",
            state,
            description or "failed",
            text=_build_plan(list(statuses), self.build_url(event, build), statuses),
            propagate=True,
        )


def _fence(text: str) -> str:
    return f"```\n{strip_ansi(text)}\n```"


_STATUS_ICONS = {
    "succeeded": "✅",
    "failed": "❌",
    "failed_eval": "❌",
    "dependency_failed": "❌",
    "cached_failure": "❌",
    "cancelled": "⚪",
    "queued": "⏳",
    "building": "🔨",
}


def _status_cell(status: str) -> str:
    icon = _STATUS_ICONS.get(status)
    return f"{icon} {status}" if icon else status


def _build_plan(
    attrs: Sequence[str], build_url: str, statuses: dict[str, str] | None = None
) -> str | None:
    """Markdown table of the build's attributes, each linking to its
    (live-tailing) raw log. Posted twice: in the pending nix-build run
    as the build plan (statuses is None, no status column), then again
    at build finish with each attribute's terminal status, failures
    first. Truncated to the check-run text budget."""
    if statuses is None:
        attrs = sorted(set(attrs))
        header = f"Building {len(attrs)} attribute(s):"
        head, sep, trunc = "| attribute | raw |", "| --- | --- |", "| [all]({0}) |"
    else:
        # Failures first so the actionable rows lead, then by attr name.
        attrs = sorted(
            set(attrs),
            key=lambda a: (statuses.get(a) not in FAILED_STATUS_STATES, a),
        )
        header = f"Built {len(attrs)} attribute(s):"
        head = "| attribute | status | raw |"
        sep = "| --- | --- | --- |"
        trunc = "| | [all]({0}) |"
    if not attrs:
        return None
    lines = [header, "", head, sep]
    for i, attr in enumerate(attrs):
        live = f"{build_url}/logs/{quote(attr)}"
        raw = f"{build_url}/logs/raw/{quote(attr)}"
        if statuses is None:
            line = f"| [`{attr}`]({live}) | [raw]({raw}) |"
        else:
            cell = _status_cell(statuses.get(attr, "unknown"))
            line = f"| [`{attr}`]({live}) | {cell} | [raw]({raw}) |"
        if sum(map(len, lines)) + len(line) > CHECK_RUN_TEXT_LIMIT:
            lines.append(f"| … {len(attrs) - i} more {trunc.format(build_url)}")
            break
        lines.append(line)
    return "\n".join(lines)
