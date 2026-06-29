"""Read-side queries for the web frontend."""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..db_gen import web as gen  # noqa: TID252

if TYPE_CHECKING:
    import asyncpg

PAGE_SIZE = 50


@dataclass
class BuildFilters:
    status: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    commit: str | None = None
    before: int | None = None  # cursor: only builds with a smaller id

    @classmethod
    def for_ref(cls, ref: str | None, **kwargs: Any) -> BuildFilters:
        """Parse a ref filter: "#123" or "pr/123" means a PR, anything
        else (including bare digits — branches may be named "2024")
        a branch name."""
        if ref and (match := re.fullmatch(r"(?:#|pr/)(\d+)", ref)):
            return cls(pr_number=int(match.group(1)), **kwargs)
        return cls(branch=ref or None, **kwargs)


def _like_escape(query: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(q: str | None) -> str | None:
    return f"%{_like_escape(q)}%" if q else None


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    page: int
    has_next: bool


def _dicts(rows: Any) -> list[dict[str, Any]]:
    """Generated row dataclasses -> template-friendly dicts."""
    return [dataclasses.asdict(r) for r in rows]


class WebQueries:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def projects(
        self, *, enabled: bool | None = True, q: str | None = None
    ) -> list[dict[str, Any]]:
        return _dicts(
            await gen.web_projects(self.pool, enabled=enabled, pattern=_like_pattern(q))
        )

    async def repo_by_name(
        self, forge: str, owner: str, name: str
    ) -> dict[str, Any] | None:
        row = await gen.web_repo(self.pool, forge=forge, owner=owner, name=name)
        return dataclasses.asdict(row) if row else None

    async def repo_candidates(self, owner: str, name: str) -> list[dict[str, Any]]:
        """All forges' rows for an unqualified owner/name; used by the
        legacy-URL redirect to pick a target."""
        return _dicts(await gen.web_repo_candidates(self.pool, owner=owner, name=name))

    async def repo_overview(
        self, project_ids: list[int] | None = None, q: str | None = None
    ) -> list[dict[str, Any]]:
        """Homepage pipeline rows: each project with its latest build,
        the last ten builds (status + duration) for the bar chart, and
        median duration/pass rate over the last thirty builds."""
        rows = await gen.web_repo_overview(
            self.pool, project_ids=project_ids, pattern=_like_pattern(q)
        )
        overview = _dicts(rows)
        for row in overview:
            row["history"] = json.loads(row["history"]) if row["history"] else []
        return overview

    async def recent_builds(
        self,
        limit: int = 50,
        project_ids: list[int] | None = None,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        """Activity feed; cursor on build id for infinite scroll."""
        return _dicts(
            await gen.web_recent_builds(
                self.pool, project_ids=project_ids, before=before, limit_=limit
            )
        )

    async def builds_for_repo(
        self,
        project_id: int,
        *,
        page: int = 1,
        limit: int = PAGE_SIZE,
        filters: BuildFilters | None = None,
    ) -> Page:
        f = filters or BuildFilters()
        page = max(page, 1)
        rows = await gen.web_builds_for_repo(
            self.pool,
            project_id=project_id,
            # `or None`: empty strings from query params mean "no filter".
            status=f.status or None,
            branch=f.branch or None,
            pr_number=f.pr_number,
            commit_prefix=f.commit or None,
            before=f.before,
            limit=limit + 1,
            offset=(page - 1) * limit,
        )
        return Page(items=_dicts(rows[:limit]), page=page, has_next=len(rows) > limit)

    async def build_by_number(
        self, project_id: int, number: int
    ) -> dict[str, Any] | None:
        row = await gen.web_build_by_number(
            self.pool, project_id=project_id, number=number
        )
        return dataclasses.asdict(row) if row else None

    async def neighbor_numbers(
        self, project_id: int, number: int
    ) -> tuple[int | None, int | None]:
        """Prev/next build numbers within the project."""
        row = await gen.web_neighbor_numbers(
            self.pool, project_id=project_id, number=number
        )
        return (row.prev, row.next) if row else (None, None)

    async def attributes(self, build_id: int) -> list[dict[str, Any]]:
        """Attributes, failed first, then by name."""
        return _dicts(await gen.web_attributes(self.pool, build_id=build_id))

    async def effects(self, build_id: int) -> list[dict[str, Any]]:
        return _dicts(await gen.web_effects(self.pool, build_id=build_id))

    async def attribute_counts(
        self, build_id: int, q: str | None = None
    ) -> dict[str, int]:
        """Attribute counts per status, optionally name-filtered."""
        rows = await gen.web_attribute_counts(
            self.pool, build_id=build_id, pattern=_like_pattern(q)
        )
        return {r.status: r.count for r in rows}

    async def attribute_page(
        self,
        build_id: int,
        statuses: tuple[str, ...],
        limit: int,
        page: int,
        q: str | None = None,
    ) -> Page:
        """One page of attributes with the given statuses, by name."""
        rows = await gen.web_attribute_page(
            self.pool,
            build_id=build_id,
            statuses=list(statuses),
            pattern=_like_pattern(q),
            limit_=limit + 1,
            offset_=(page - 1) * limit,
        )
        return Page(items=_dicts(rows[:limit]), page=page, has_next=len(rows) > limit)

    async def attribute_history(
        self, project_id: int, attr: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Results of the same attribute across a project's builds."""
        return _dicts(
            await gen.web_attribute_history(
                self.pool, project_id=project_id, attr=attr, limit_=limit
            )
        )

    async def attribute_neighbors(
        self, project_id: int, attr: str, build_number: int
    ) -> tuple[int | None, int | None]:
        """Prev/next build numbers containing the same attribute."""
        row = await gen.web_attribute_neighbor_numbers(
            self.pool, project_id=project_id, attr=attr, build_number=build_number
        )
        return (row.prev, row.next) if row else (None, None)

    async def queue(self, project_ids: list[int] | None = None) -> list[dict[str, Any]]:
        """Pending (FIFO position by id) and running builds.

        queue_position numbers the GLOBAL queue of pending builds:
        computed before any visibility filter so every viewer sees the
        same position, and NULL for already-running builds."""
        return _dicts(await gen.web_queue(self.pool, project_ids=project_ids))
