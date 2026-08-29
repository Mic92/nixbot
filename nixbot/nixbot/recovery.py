"""Crash recovery, DB-outage policy, and retention cleanup.

Recovery resumes rather than restarts: attributes already terminal in
the database are skipped (attribute completion is one transactional
write, so the database is trustworthy). Pending attributes are first
re-checked against the nix store by reading its sqlite database
directly (see check_store_paths) — already-valid out paths complete
without a rebuild — and only the rest re-run.
Effects are guarded by the build's started-flag and never re-run
automatically.

DB outage policy: when the database is unreachable the affected
operation raises and the service restarts via systemd. Webhook
fail-fast lives in ingestion. The health check backs the
HTTP health endpoint.

Retention: builds older than the configured horizon are deleted along
with their log files. Stale workdirs are swept via RepoManager.cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import asyncpg

from . import db
from .build_scheduler import AttributeResult, AttributeStatus
from .db_gen import maintenance as q
from .models import CacheStatus, NixEvalJobSuccess

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from datetime import datetime


logger = logging.getLogger(__name__)

UNFINISHED_STATUSES = ("pending", "evaluating", "building")


async def check_db_health(pool: asyncpg.Pool) -> bool:
    """Backs the HTTP health endpoint."""
    try:
        await pool.fetchval("SELECT 1")
    except (TimeoutError, asyncpg.PostgresError, OSError):
        return False
    return True


# --- crash recovery ----------------------------------------------------------


@dataclass(frozen=True)
class ResumableBuild:
    build_id: int
    project_id: int
    branch: str
    commit_sha: str
    pr_number: int | None
    status: str
    effects_started: bool
    # False when the build died during evaluation (no attribute rows).
    has_attributes: bool
    # Jobs reconstructed from non-terminal attribute rows.
    pending_jobs: list[NixEvalJobSuccess]


async def find_unfinished_builds(
    pool: asyncpg.Pool, build_id: int | None = None
) -> list[ResumableBuild]:
    rows = await q.find_unfinished_builds(
        pool, statuses=list(UNFINISHED_STATUSES), build_id=build_id
    )
    if not rows:
        return []
    # One round-trip for all attribute rows, not one per build.
    attr_rows = await q.attributes_for_builds(pool, build_ids=[row.id_ for row in rows])
    attrs_by_build: dict[int, list[q.AttributesForBuildsRow]] = {}
    for attr in attr_rows:
        attrs_by_build.setdefault(attr.build_id, []).append(attr)
    builds = []
    for row in rows:
        attrs = attrs_by_build.get(row.id_, [])
        pending_jobs = []
        for attr in attrs:
            # 'building' rows are attributes that were running when the
            # service died. They must resume like pending ones.
            if attr.status not in ("pending", "building"):
                continue  # terminal in DB: never redone
            if not attr.drv_path:
                continue
            outputs = json.loads(attr.outputs) if attr.outputs else {}
            pending_jobs.append(
                NixEvalJobSuccess(
                    attr=attr.attr,
                    attr_path=attr.attr.split("."),
                    cache_status=CacheStatus.not_built,
                    needed_builds=[],
                    needed_substitutes=[],
                    drv_path=attr.drv_path,
                    name=attr.attr,
                    outputs=outputs or {"out": None},
                    system=attr.system or "",
                )
            )
        builds.append(
            ResumableBuild(
                build_id=row.id_,
                project_id=row.project_id,
                branch=row.branch,
                commit_sha=row.commit_sha,
                pr_number=row.pr_number,
                status=row.status,
                effects_started=row.effects_started,
                has_attributes=len(attrs) > 0,
                pending_jobs=pending_jobs,
            )
        )
    return builds


NIX_DB = Path("/nix/var/nix/db/db.sqlite")


def _valid_paths_from_db(paths: list[str], nix_db: Path) -> set[str]:
    con = sqlite3.connect(f"file:{nix_db}?mode=ro", uri=True)
    try:
        valid: set[str] = set()
        batch_size = 500  # sqlite parameter limit
        for i in range(0, len(paths), batch_size):
            batch = paths[i : i + batch_size]
            placeholders = ",".join("?" * len(batch))
            valid.update(
                row[0]
                for row in con.execute(
                    f"SELECT path FROM ValidPaths WHERE path IN ({placeholders})",  # noqa: S608
                    batch,
                )
            )
        return valid
    finally:
        con.close()


async def fail_interrupted_effects(
    pool: asyncpg.Pool, started_before: datetime
) -> Sequence[q.FailInterruptedEffectsRow]:
    """Settle effect rows left running by a crash: started effects
    never auto-re-run. Pending rows keep their queued items and run
    after the requeue. Only rows started before `started_before`
    (process start) are swept: the sweep runs concurrently with the
    work loop, and newer rows are live effects. Returns the settled
    rows for the caller to report."""
    settled = await q.fail_interrupted_effects(pool, started_before=started_before)
    await q.fail_interrupted_scheduled_runs(pool, started_before=started_before)
    return settled


async def check_store_paths(paths: list[str], nix_db: Path = NIX_DB) -> set[str]:
    """Valid store paths among `paths`, read straight from the nix
    store database: no subprocess startup, no daemon round-trips."""
    paths = [p for p in paths if p]
    if not paths:
        return set()
    try:
        return await asyncio.to_thread(_valid_paths_from_db, paths, nix_db)
    except sqlite3.Error:
        logger.exception("reading the nix database failed; assuming unbuilt")
        return set()


async def settle_already_built(
    pool: asyncpg.Pool,
    build: ResumableBuild,
    path_checker: Callable[[list[str]], Awaitable[set[str]]] = check_store_paths,
) -> tuple[list[NixEvalJobSuccess], list[tuple[str, str]]]:
    """Complete pending attributes whose out paths already exist in the
    store. Return (jobs that still need building, settled (attr,
    out_path) pairs for gcroots/outputs post-processing)."""
    valid = await path_checker(
        [p for job in build.pending_jobs if (p := job.outputs.get("out"))]
    )
    remaining = []
    settled: list[tuple[str, str]] = []
    for job in build.pending_jobs:
        out_path = job.outputs.get("out")
        if out_path and out_path in valid:
            await db.complete_attribute(
                pool,
                build.build_id,
                AttributeResult(
                    attr=job.attr,
                    status=AttributeStatus.succeeded,
                    job=job,
                    out_path=out_path,
                    drv_path=job.drv_path,
                    system=job.system,
                ),
            )
            settled.append((job.attr, out_path))
        else:
            remaining.append(job)
    return remaining, settled


# --- retention cleanup ---------------------------------------------------------


async def cleanup_old_builds(
    pool: asyncpg.Pool, state_dir: Path, retention_days: int
) -> int:
    """Delete builds (cascading to attributes/log rows) and their log
    directories past the retention horizon. Returns deleted count."""
    rows = await q.cleanup_old_rows(pool, retention_days=retention_days)
    deleted_ids = [r.id_ for r in rows if r.kind == "build"]
    old_run_ids = [r.id_ for r in rows if r.kind == "scheduled_run"]
    for build_id in deleted_ids:
        log_dir = state_dir / "logs" / str(build_id)
        with contextlib.suppress(OSError):
            shutil.rmtree(log_dir)
    for run_id in old_run_ids:
        with contextlib.suppress(OSError):
            (state_dir / "logs" / "scheduled" / f"{run_id}.zst").unlink()
    if deleted_ids:
        logger.info("retention cleanup", extra={"deleted_builds": len(deleted_ids)})
    return len(deleted_ids)


ORPHAN_LOG_GRACE_SECONDS = 3600


async def cleanup_orphan_log_dirs(
    pool: asyncpg.Pool,
    state_dir: Path,
    grace_seconds: int = ORPHAN_LOG_GRACE_SECONDS,
) -> None:
    """Remove log directories whose build row no longer exists.

    The directory scan happens BEFORE the build-id snapshot and young
    directories are skipped: a build created concurrently with the
    sweep must not get its live log directory deleted."""
    logs_root = state_dir / "logs"
    if not logs_root.is_dir():
        return
    cutoff = time.time() - grace_seconds
    candidates: list[tuple[int, str]] = []
    for entry in os.scandir(logs_root):
        try:
            build_id = int(entry.name)
        except ValueError:
            continue
        with contextlib.suppress(OSError):
            if entry.stat().st_mtime <= cutoff:
                candidates.append((build_id, entry.path))
    if not candidates:
        return
    build_ids = set(await q.all_build_ids(pool))
    for build_id, path in candidates:
        if build_id not in build_ids:
            with contextlib.suppress(OSError):
                shutil.rmtree(path)
