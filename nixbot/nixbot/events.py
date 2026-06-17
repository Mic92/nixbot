"""Shared build-event value types and the status-reporting protocol.

Kept separate from the orchestrator so forge integration, webhooks,
and the web frontend can depend on these without importing the whole
build pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
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
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
        jobs: Sequence[NixEvalJobSuccess] | None = None,
    ) -> None: ...

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None: ...

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None: ...


class NullStatusReporter:
    async def build_started(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def eval_finished(
        self,
        event: ChangeEvent,
        build: BuildRecord,
        *,
        success: bool,
        warnings: list[str],
        jobs: Sequence[NixEvalJobSuccess] | None = None,
    ) -> None:
        pass

    async def eval_cancelled(self, event: ChangeEvent, build: BuildRecord) -> None:
        pass

    async def build_finished(
        self, event: ChangeEvent, build: BuildRecord, result: BuildResult
    ) -> None:
        pass
