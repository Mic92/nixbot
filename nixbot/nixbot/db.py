"""Shared build persistence (sqlc-generated queries over asyncpg).

Only multi-caller operations with real invariants live here. Modules
with a single call site use db_gen directly. The SQL lives in
queries/builds.sql; `sqlc generate` produces db_gen/builds.py.

Key invariants:

- build identity is the post-merge tree hash: a second change event
  producing the same tree for the same project reuses the existing
  build instead of creating a new one,
- attribute completion is one transactional write (status + log
  metadata together),
- re-aggregation of a build's result is serialized per build via a row
  lock (SELECT ... FOR UPDATE) and bumps a monotonic status generation
  so stale forge status posts can be dropped.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .db_gen import builds as q
from .models import CacheStatus, NixEvalJobError, NixEvalJobSuccess
from .sql_util import expect

if TYPE_CHECKING:
    import asyncpg

    from .build_scheduler import AttributeResult
    from .db_gen.models import Build as BuildRecord


class BuildStatus:
    PENDING = "pending"
    EVALUATING = "evaluating"
    BUILDING = "building"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})


# Attribute statuses that count as failures when aggregating.
FAILED_ATTRIBUTE_STATUSES = frozenset(
    {"failed", "failed_eval", "dependency_failed", "cached_failure"}
)
TERMINAL_ATTRIBUTE_STATUSES = FAILED_ATTRIBUTE_STATUSES | frozenset(
    {"succeeded", "cancelled", "skipped_local", "ignored_failure"}
)


async def get_or_create_build(  # noqa: PLR0913
    pool: asyncpg.Pool,
    project_id: int,
    tree_hash: str,
    commit_sha: str,
    branch: str,
    pr_number: int | None = None,
    pr_author: str | None = None,
) -> tuple[BuildRecord, bool]:
    """Reuse keyed on post-merge tree hash across contexts."""
    async with pool.acquire() as conn, conn.transaction():
        await q.lock_build_identity(conn, key=f"{project_id}:{tree_hash}")
        row = await q.find_reusable_build(
            conn, project_id=project_id, tree_hash=tree_hash
        )
        if row is not None:
            if pr_number != row.pr_number:
                if row.pr_number is not None or row.pr_author is not None:
                    row = await q.detach_build_from_pr(
                        conn, id_=row.id_, pr_number=pr_number, branch=branch
                    )
                elif pr_number is not None and branch == row.branch:
                    # Backfill PR identity for the pr_author authz
                    # rule when a push to the PR's own head branch
                    # created the build first. A PR must not capture
                    # authz over a build for another branch (e.g. a
                    # default-branch push sharing the tree hash).
                    row = await q.attach_build_to_pr(
                        conn,
                        id_=row.id_,
                        pr_number=pr_number,
                        pr_author=pr_author,
                    )
            elif pr_author is not None and row.pr_author is None:
                # Same PR: fill in the author when a previous event
                # for this PR lacked it.
                row = await q.backfill_pr_author(conn, id_=row.id_, pr_author=pr_author)
            return expect(row), False
        row = await q.create_build(
            conn,
            project_id=project_id,
            tree_hash=tree_hash,
            commit_sha=commit_sha,
            branch=branch,
            pr_number=pr_number,
            pr_author=pr_author,
        )
        return expect(row), True


async def create_failed_build(  # noqa: PLR0913
    pool: asyncpg.Pool,
    project_id: int,
    commit_sha: str,
    branch: str,
    error: str,
    pr_number: int | None = None,
    pr_author: str | None = None,
) -> BuildRecord:
    """A build that failed before evaluation (e.g. merge conflict);
    no tree hash exists, the status is reported on the head SHA."""
    row = await q.create_failed_build(
        pool,
        project_id=project_id,
        commit_sha=commit_sha,
        branch=branch,
        pr_number=pr_number,
        pr_author=pr_author,
        error=error,
    )
    return expect(row)


async def set_build_status(
    pool: asyncpg.Pool,
    build_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    await q.set_build_status(
        pool,
        id_=build_id,
        status=status,
        error=error,
        terminal=list(BuildStatus.TERMINAL),
    )


async def complete_attribute(  # noqa: PLR0913
    pool: asyncpg.Pool,
    build_id: int,
    result: AttributeResult,
    *,
    log_size: int = 0,
    log_truncated: bool = False,
    if_unfinished: bool = False,
) -> None:
    """Single atomic write: status, outputs, error and log
    metadata together (crash-recovery invariant). With
    if_unfinished, already-terminal rows are left untouched (early
    results must not overwrite settled attributes)."""
    # failed_eval rows never went through RecordAttributes, so their
    # eval data arrives here. Successes already have it.
    job = result.job
    warnings = job.warnings if isinstance(job, NixEvalJobError) else None
    stats = job.stats if isinstance(job, NixEvalJobError) else None
    await q.complete_attribute(
        pool,
        build_id=build_id,
        attr=result.attr,
        system=result.system,
        drv_path=result.drv_path,
        outputs=json.dumps({"out": result.out_path}) if result.out_path else None,
        status=result.status.value,
        error=result.error,
        # "Came from cache": already in the local store, or
        # successfully substituted from a binary cache.
        cached=result.status.value == "skipped_local"
        or (
            result.status.value == "succeeded"
            and isinstance(job, NixEvalJobSuccess)
            and job.cache_status == CacheStatus.cached
        ),
        log_size=log_size,
        log_truncated=log_truncated,
        eval_warnings=json.dumps(warnings) if warnings else None,
        eval_wall_ms=stats.wall_ms if stats else None,
        eval_alloc_bytes=stats.alloc_bytes if stats else None,
        if_unfinished=if_unfinished,
    )


async def aggregate_build(pool: asyncpg.Pool, build_id: int) -> tuple[str, int]:
    """Recompute the build's aggregate result from its attributes.

    Serialized per build via a row lock taken before the statuses
    are read (see LockBuildRow). Bumps the monotonic status
    generation. Returns (status, generation).
    """
    async with pool.acquire() as conn, conn.transaction():
        row = await q.lock_build_row(conn, id_=build_id)
        if row is None:
            msg = f"build {build_id} not found"
            raise LookupError(msg)
        statuses = list(await q.attribute_status_list(conn, build_id=build_id))
        if any(s not in TERMINAL_ATTRIBUTE_STATUSES for s in statuses):
            # Not all attributes terminal yet: keep current status.
            return row.status, row.status_generation
        if any(s in FAILED_ATTRIBUTE_STATUSES for s in statuses):
            status = BuildStatus.FAILED
        elif any(s == "cancelled" for s in statuses):
            status = BuildStatus.CANCELLED
        else:
            status = BuildStatus.SUCCEEDED
        generation = expect(
            await q.bump_build_status(conn, id_=build_id, status=status)
        )
        return status, generation
