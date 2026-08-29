"""Commit status / check-run reporting.

GitHub uses the Check Runs API (create + PATCH by stored id);
Gitea/GitLab keep posting commit statuses. Check-run *names* and
commit-status *contexts* share the required-checks namespace, so
branch protection rules are unaffected.

Combined per-phase contexts are `nixbot/nix-eval` (warning count
appended to the description) and `nixbot/nix-build`. The prefix is
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

import logging
import re
from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol
from urllib.parse import quote

import httpx

from .ansi import strip_ansi
from .db_gen import failed as q
from .forge import ForgeError
from .forge.retry import retry_after_seconds

if TYPE_CHECKING:
    from collections.abc import Sequence

    import asyncpg

    from .build_scheduler import AttributeResult
    from .db import BuildRecord
    from .events import BuildResult, ChangeEvent, EvalReport
    from .forge import GiteaClient, GitHubAppClient, GitlabClient

# GitHub caps output.text at 65535 chars.
CHECK_RUN_TEXT_LIMIT = 60_000

logger = logging.getLogger(__name__)

# Cap on remembered (build id -> posted generation) entries. One entry
# per build forever would be a slow leak in a long-lived process.
POSTED_GENERATIONS_MAX = 1024

FAILED_STATUS_STATES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure", "cancelled"}
)

# Statuses for which no per-attribute build log is ever written: eval
# failed before a build started, or the build was skipped because the
# output is already present locally. The log viewer falls back to the
# stored eval error for failed_eval.
NO_LOG_STATUSES = frozenset({"failed_eval", "skipped_local"})


class StatusState(StrEnum):
    pending = "pending"
    success = "success"
    failure = "failure"
    error = "error"


class StatusPostError(Exception):
    """HTTP-level status post failure. retry_after carries the forge's
    Retry-After / rate-limit-reset hint in seconds, if any."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class CheckPermissionError(ForgeError):
    """The GitHub App lacks Checks: write. Latched so we stop
    hammering the API until the operator fixes the permission."""


def _raise_for_status(response: httpx.Response, repo: str) -> None:
    if response.status_code >= httpx.codes.BAD_REQUEST:
        msg = f"status post for {repo} failed: HTTP {response.status_code}"
        raise StatusPostError(msg, retry_after=retry_after_seconds(response))


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
        force_new: bool = False,
    ) -> None: ...


class CheckRunIds(Protocol):
    async def get(self, project_id: int, sha: str, name: str) -> int | None: ...

    async def set(
        self, project_id: int, sha: str, name: str, attr: str | None, external_id: int
    ) -> None: ...


class CheckRunStore:
    """(project, sha, name) → GitHub check-run id. Lets the poster
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
    # The full context repeats the repo path. Keep the title short.
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
        force_new: bool = False,
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
        # GitHub only renders a check as re-running for a *new* run of
        # the same name, so a restart creates one instead of patching
        # (community discussion 38288).
        run_id = None if force_new else await self.store.get(project_id, sha, context)
        if run_id is not None:
            response = await self.client.http.patch(
                f"{base}/{run_id}", headers=headers, json=body
            )
            # The DB row can outlive the GitHub run. Recreate instead
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
    # GitLab has no "error" state. Both map to failed.
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
    # Attrs already contain the configured attribute path unless they
    # carry the legacy "default." job prefix; avoid doubling it.
    name = (
        attr if attr == prefix or attr.startswith(f"{prefix}.") else f"{prefix}.{attr}"
    )
    return f"{context_prefix}/nix-build {forge}:{project_name}#{name}"


def effect_status_context(
    forge: str,
    project_name: str,
    name: str,
    context_prefix: str = "nixbot",
) -> str:
    return f"{context_prefix}/effect {forge}:{project_name}#{name}"


def effects_summary_context(context_prefix: str = "nixbot") -> str:
    return f"{context_prefix}/effects"


def effects_summary_description(failed: int, succeeded: int) -> str:
    total = failed + succeeded
    if failed:
        return f"{failed} of {total} effects failed"
    return f"{succeeded} effect{'s' if succeeded != 1 else ''} succeeded"


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
        force_new: bool = False,
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
                # colors (kept for the web UI). Forges show them verbatim.
                strip_ansi(description),
                self.build_url(event, build),
                project_id=event.repo.id,
                build_id=build.id_,
                attr=attr,
                text=text,
                force_new=force_new,
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
                extra={"build_id": build.id_, "context": context},
            )

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.pending,
            "waiting for evaluation",
        )

    async def eval_finished(
        self, event: ChangeEvent, build: BuildRecord, report: EvalReport
    ) -> None:
        # Failed evals show the error tail. Successful ones the warnings.
        if not report.success and report.error:
            text: str | None = _fence(report.error)
        elif report.warnings:
            text = _fence("\n".join(report.warnings))
        else:
            text = None
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.success if report.success else StatusState.failure,
            eval_description(report.success, report.warnings),
            text=text,
        )
        if report.success:
            await self._post(
                event,
                build,
                f"{self.context_prefix}/nix-build",
                StatusState.pending,
                "building attributes",
                text=_build_plan(
                    [j.attr for j in report.jobs], self.build_url(event, build)
                )
                if report.jobs
                else None,
            )

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        """Resolve the pending eval context. See the orchestrator's
        cancel path."""
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-eval",
            StatusState.error,
            "build cancelled",
        )

    async def effect_started(
        self, event: ChangeEvent, build: BuildRecord, name: str
    ) -> None:
        await self._post(
            event,
            build,
            effect_status_context(
                event.repo.forge,
                event.repo.name,
                name,
                context_prefix=self.context_prefix,
            ),
            StatusState.pending,
            "running effect",
        )

    async def effect_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        await self._post(
            event,
            build,
            effect_status_context(
                event.repo.forge,
                event.repo.name,
                name,
                context_prefix=self.context_prefix,
            ),
            StatusState.success if success else StatusState.failure,
            "effect succeeded" if success else (error or "effect failed"),
            text=_fence(error) if error else None,
        )

    async def effects_started(
        self, event: ChangeEvent, build: BuildRecord, total: int
    ) -> None:
        await self._post(
            event,
            build,
            effects_summary_context(self.context_prefix),
            StatusState.pending,
            f"running {total} effect{'s' if total != 1 else ''}",
        )

    async def effects_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        failed: int,
        succeeded: int,
    ) -> None:
        await self._post(
            event,
            build,
            effects_summary_context(self.context_prefix),
            StatusState.failure if failed else StatusState.success,
            effects_summary_description(failed, succeeded),
        )

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        generation = result.generation
        results = result.results
        attr_statuses = result.attr_statuses
        # Monotonic generation: drop stale posts after re-aggregation.
        if generation < self._posted_generations.get(build.id_, 0):
            logger.info(
                "dropping stale status post",
                extra={"build_id": build.id_, "generation": generation},
            )
            return
        self._posted_generations[build.id_] = generation
        self._posted_generations.move_to_end(build.id_)
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

    async def build_restarted(
        self, event: ChangeEvent, build: BuildRecord, attr: str | None
    ) -> None:
        """Flip the restarted checks to pending before the async rebuild
        starts. force_new so GitHub renders them as re-running."""
        if attr is None:
            await self._post(
                event,
                build,
                f"{self.context_prefix}/nix-eval",
                StatusState.pending,
                "restarting",
                force_new=True,
            )
        else:
            context = attr_status_context(
                event.repo.forge,
                event.repo.name,
                attr,
                context_prefix=self.context_prefix,
            )
            await self._post(
                event,
                build,
                context,
                StatusState.pending,
                "rebuilding",
                attr=attr,
                force_new=True,
            )
        await self._post(
            event,
            build,
            f"{self.context_prefix}/nix-build",
            StatusState.pending,
            "rebuilding",
            force_new=True,
        )

    async def _post_attribute_statuses(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        results: list[AttributeResult],
        attr_prefix: str,
    ) -> dict[str, int]:
        """Per-attribute failure statuses and success flips. Returns
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
                headline = (
                    result.failure.headline()
                    if result.failure
                    else _error_headline(result.error or "")
                )
                description = headline or result.status.value
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
            # Attribute-level cancels aggregate like failures. Only a
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


# Flat/effect excerpts prefix lines with "name> ". Structured failures
# skip this path entirely (they carry a BuildFailure).
_DRV_PREFIX = re.compile(r"^[^\s>]+> +")


def _error_headline(excerpt: str, limit: int = 200) -> str:
    """The excerpt's last real error line, for a status blurb / check
    summary."""
    for raw in reversed(excerpt.splitlines()):
        line = _DRV_PREFIX.sub("", strip_ansi(raw).strip())
        if line:
            return line[:limit]
    return ""


_STATUS_ICONS = {
    "succeeded": "✅",
    "failed": "❌",
    "failed_eval": "❌",
    "dependency_failed": "❌",
    "cached_failure": "❌",
    "ignored_failure": "⚠️",
    "cancelled": "⚪",
    "queued": "⏳",
    "building": "🔨",
}


# Row ordering for the finished build table: failures first, then succeeded,
# then remaining states. Unknown statuses sort last.
_STATUS_ORDER = (
    "failed",
    "failed_eval",
    "dependency_failed",
    "cached_failure",
    "cancelled",
    "ignored_failure",
    "succeeded",
    "skipped_local",
    "building",
    "queued",
)


def _status_rank(status: str | None) -> int:
    try:
        return _STATUS_ORDER.index(status)  # type: ignore[arg-type]
    except ValueError:
        return len(_STATUS_ORDER)


def _status_cell(status: str) -> str:
    icon = _STATUS_ICONS.get(status)
    label = status.replace("_", " ")
    return f"{icon} {label}" if icon else label


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
        # Locally-skipped attributes were not built here. Drop them.
        attrs = [a for a in attrs if statuses.get(a) != "skipped_local"]
        # Group by status (see _STATUS_ORDER), then alphabetically within
        # each status.
        attrs = sorted(
            set(attrs),
            key=lambda a: (_status_rank(statuses.get(a)), a),
        )
        header = f"Built {len(attrs)} attribute(s):"
        head = "| attribute | status | raw |"
        sep = "| --- | --- | --- |"
        trunc = "| | [all]({0}) |"
    if not attrs:
        return None
    lines = [header, "", head, sep]
    # +1 per line for the newline join() inserts. Undercounting lets the
    # joined text exceed the budget, and _check_run_output then chops it
    # mid-row into invalid markdown.
    used = sum(len(line) + 1 for line in lines)
    for i, attr in enumerate(attrs):
        live = f"{build_url}/logs/{quote(attr)}"
        raw = f"{build_url}/logs/raw/{quote(attr)}"
        status = statuses.get(attr) if statuses is not None else None
        # failed_eval attributes have no build log, but the viewer/raw
        # routes serve their eval error, so they keep their links.
        linkable = status != "skipped_local"
        attr_cell = f"[`{attr}`]({live})" if linkable else f"`{attr}`"
        raw_cell = f"[raw]({raw})" if linkable else ""
        if statuses is None:
            line = f"| {attr_cell} | {raw_cell} |"
        else:
            cell = _status_cell(status or "unknown")
            line = f"| {attr_cell} | {cell} | {raw_cell} |"
        trunc_line = f"| … {len(attrs) - i} more {trunc.format(build_url)}"
        # Reserve room for the trailing row so it always fits.
        if used + len(line) + 1 + len(trunc_line) + 1 > CHECK_RUN_TEXT_LIMIT:
            lines.append(trunc_line)
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)
