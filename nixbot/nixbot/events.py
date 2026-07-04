"""Shared build-event value types and the status-reporting protocol.

Kept separate from the orchestrator so forge integration, webhooks,
and the web frontend can depend on these without importing the whole
build pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .build_scheduler import AttributeResult
    from .db import BuildRecord
    from .models import NixEvalJobSuccess


@dataclass(frozen=True)
class RepoInfo:
    """The service-side view of an enabled project."""

    id: int  # database id
    key: str  # e.g. "github/owner/repo" (clone directory key)
    name: str  # "owner/repo"
    owner: str
    repo: str
    forge: str  # "github" | "gitea" | "gitlab" | "pull_based"
    clone_url: str
    default_branch: str


@dataclass(frozen=True)
class ChangeEvent:
    """A push or pull-request event from a forge or poller."""

    repo: RepoInfo
    branch: str
    commit_sha: str
    # PR-only fields; base_sha is the base branch head to merge into.
    pr_number: int | None = None
    pr_author: str | None = None
    base_sha: str | None = None
    commit_message: str = ""


def event_for_build(repo: RepoInfo, build: BuildRecord) -> ChangeEvent:
    """The triggering event of a build, reconstructed from its stored
    fields; used by rerun and out-of-band reporting paths."""
    return ChangeEvent(
        repo=repo,
        branch=build.branch,
        commit_sha=build.commit_sha,
        pr_number=build.pr_number,
    )


def effects_event_for_build(repo: RepoInfo, build: BuildRecord) -> ChangeEvent:
    """Effects report on the ref that triggered them (e.g. a
    default-branch merge that reused a PR build), recorded at
    mark_effects_started; pre-0018 builds fall back to the build ref."""
    if build.effects_commit_sha is None:
        return event_for_build(repo, build)
    return ChangeEvent(
        repo=repo,
        branch=build.effects_branch or build.branch,
        commit_sha=build.effects_commit_sha,
        pr_number=build.effects_pr_number,
    )


@dataclass(frozen=True)
class EvalReport:
    """Evaluation-phase outcome reported to a forge. warnings and jobs
    are populated on success; error carries the evaluator's log tail on
    failure."""

    success: bool
    warnings: list[str] = field(default_factory=list)
    jobs: Sequence[NixEvalJobSuccess] | None = None
    error: str | None = None


@dataclass(frozen=True)
class BuildResult:
    """The final outcome of a build, as reported to a forge."""

    status: str
    generation: int
    results: list[AttributeResult]
    attr_statuses: dict[str, str] | None = None
    attr_prefix: str = "checks"


class StatusReporter(Protocol):
    """Receives lifecycle events; forge integration implements this."""

    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None: ...

    async def eval_finished(
        self, event: ChangeEvent, build: BuildRecord, report: EvalReport
    ) -> None: ...

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None: ...

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None: ...

    async def effect_started(
        self, event: ChangeEvent, build: BuildRecord, name: str
    ) -> None: ...

    async def effect_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None: ...

    async def effects_started(
        self, event: ChangeEvent, build: BuildRecord, total: int
    ) -> None: ...

    async def effects_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        failed: int,
        succeeded: int,
    ) -> None: ...


class NullStatusReporter:
    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def eval_finished(
        self, event: ChangeEvent, build: BuildRecord, report: EvalReport
    ) -> None:
        pass

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        pass

    async def effect_started(
        self, event: ChangeEvent, build: BuildRecord, name: str
    ) -> None:
        pass

    async def effect_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        name: str,
        *,
        success: bool,
        error: str | None = None,
    ) -> None:
        pass

    async def effects_started(
        self, event: ChangeEvent, build: BuildRecord, total: int
    ) -> None:
        pass

    async def effects_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        failed: int,
        succeeded: int,
    ) -> None:
        pass
