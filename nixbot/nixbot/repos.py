"""Project store: DB-backed enablement keyed by stable forge repo ID.

Discovery (forge/) feeds repos in. Rows are upserted on
(forge, forge_repo_id) so renames/transfers keep history and the
enablement flag. Enablement is toggled by admins in the web UI. The
legacy topic filter is imported once, on the first startup with an
empty projects table.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .db_gen import projects as q
from .events import RepoInfo

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    import asyncpg

    from .db_gen.models import Project as RepoRecord
    from .forge import DiscoveredRepo

logger = logging.getLogger(__name__)


def repo_info(record: RepoRecord) -> RepoInfo:
    return RepoInfo(
        id=record.id_,
        key=f"{record.forge}/{record.owner}/{record.name}",
        name=f"{record.owner}/{record.name}",
        owner=record.owner,
        repo=record.name,
        forge=record.forge,
        clone_url=record.url,
        default_branch=record.default_branch,
    )


class RepoStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def is_empty(self) -> bool:
        """No forge-discovered projects yet. Pull-based rows are ignored:
        they come from static config and may be synced before discovery
        runs, which must not suppress the one-shot legacy import."""
        return await q.count_discovered_projects(self.pool) == 0

    async def sync_discovered(
        self,
        repos: list[DiscoveredRepo],
        *,
        legacy_import_topics: dict[str, str] | None = None,
    ) -> None:
        """Upsert discovered repos. When `legacy_import_topics` (one
        topic per forge) is given (only on first startup with an empty
        table), repos carrying their forge's topic are enabled — a
        one-shot import of the old topic-based project selection."""
        topics = legacy_import_topics or {}
        do_import = bool(topics) and await self.is_empty()
        # Last entry wins per key: a duplicate inside one batch would
        # make ON CONFLICT DO UPDATE fail with "cannot affect row a
        # second time".
        repos = list({(r.forge, r.forge_repo_id): r for r in repos}.values())
        if repos:
            await q.upsert_discovered_projects(
                self.pool,
                forges=[r.forge for r in repos],
                forge_repo_ids=[r.forge_repo_id for r in repos],
                owners=[r.owner for r in repos],
                names=[r.repo for r in repos],
                default_branches=[r.default_branch for r in repos],
                urls=[r.clone_url for r in repos],
                privates=[r.private for r in repos],
                enableds=[
                    bool(do_import and topics.get(r.forge) in r.topics) for r in repos
                ],
            )
        if do_import:
            count = await q.count_enabled_projects(self.pool)
            logger.info(
                "legacy topic import complete",
                extra={"topics": topics, "enabled": count},
            )

    async def sync_pull_based(self, repos: list[tuple[str, str, str]]) -> None:
        """Upsert pull-based repositories as (name, url, default_branch).

        Listing a repository in the static config is the enablement
        decision, so new rows start enabled. The admin toggle is
        preserved on conflict."""
        if not repos:
            return
        # Same in-batch dedup as sync_discovered (forge_repo_id = name).
        repos = list({name: (name, url, db) for name, url, db in repos}.values())
        split = [(name.rpartition("/"), name, url, db) for name, url, db in repos]
        await q.upsert_pull_based_projects(
            self.pool,
            forge_repo_ids=[name for _, name, _, _ in split],
            owners=[parts[0] or "pull_based" for parts, _, _, _ in split],
            names=[parts[2] for parts, _, _, _ in split],
            default_branches=[db for _, _, _, db in split],
            urls=[url for _, _, url, _ in split],
        )

    async def prune_missing_disabled(
        self, forge: str, forge_repo_ids: Sequence[str]
    ) -> None:
        """Delete disabled repos of `forge` that discovery no longer
        returned. Enabled projects keep their build history."""
        await q.prune_missing_disabled_projects(
            self.pool, forge=forge, forge_repo_ids=list(forge_repo_ids)
        )

    async def set_enabled(self, project_id: int, *, enabled: bool) -> None:
        await q.set_project_enabled(self.pool, id_=project_id, enabled=enabled)

    async def enabled_repos(self) -> Sequence[RepoRecord]:
        return await q.enabled_projects(self.pool)

    async def by_id(self, project_id: int) -> RepoRecord | None:
        return await q.project_by_id(self.pool, id_=project_id)

    async def reconcile_watermark(self, project_id: int) -> datetime | None:
        return await q.reconcile_watermark(self.pool, id_=project_id)

    async def set_reconcile_watermark(
        self, project_id: int, watermark: datetime
    ) -> None:
        """Advance (never rewind) the reconcile watermark."""
        await q.advance_reconcile_watermark(
            self.pool, id_=project_id, reconcile_watermark=watermark
        )

    async def by_forge_id(self, forge: str, forge_repo_id: str) -> RepoRecord | None:
        return await q.project_by_forge_id(
            self.pool, forge=forge, forge_repo_id=forge_repo_id
        )
