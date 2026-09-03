"""Tests for crash recovery and retention cleanup."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from nixbot import db
from nixbot.db_gen import builds as builds_q
from nixbot.recovery import (
    check_store_paths,
    cleanup_old_builds,
    cleanup_orphan_log_dirs,
    fail_interrupted_effects,
    find_unfinished_builds,
    settle_already_built,
)

from .support import insert_build, insert_project

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import asyncpg


async def make_build(pool: asyncpg.Pool, name: str) -> int:
    project_id = await insert_project(pool, name)
    return await insert_build(
        pool, project_id, tree_hash=f"tree-{name}", status="building"
    )


async def add_attr(
    pool: asyncpg.Pool, build_id: int, attr: str, status: str, out: str | None = None
) -> None:
    await pool.execute(
        """
        INSERT INTO build_attributes (build_id, attr, system, drv_path, outputs,
                                      status)
        VALUES ($1, $2, 'x86_64-linux', $3, $4::jsonb, $5)
        """,
        build_id,
        attr,
        f"/nix/store/{attr}.drv",
        f'{{"out": "/nix/store/{attr}-out"}}' if out is None else out,
        status,
    )


async def test_resume_skips_terminal_attributes(pool: asyncpg.Pool) -> None:
    build_id = await make_build(pool, "resume")
    await add_attr(pool, build_id, "done", "succeeded")
    await add_attr(pool, build_id, "failed", "failed")
    await add_attr(pool, build_id, "todo", "pending")

    builds = await find_unfinished_builds(pool)
    target = next(b for b in builds if b.build_id == build_id)
    # Terminal attributes never resumed.
    assert [j.attr for j in target.pending_jobs] == ["todo"]
    assert not target.effects_started


async def test_settle_already_built(pool: asyncpg.Pool) -> None:
    build_id = await make_build(pool, "settle")
    await add_attr(pool, build_id, "present", "pending")
    await add_attr(pool, build_id, "missing", "pending")
    builds = await find_unfinished_builds(pool)
    target = next(b for b in builds if b.build_id == build_id)

    async def checker(paths: list[str]) -> set[str]:
        return {p for p in paths if "present" in p}

    remaining, settled = await settle_already_built(pool, target, checker)
    assert [j.attr for j in remaining] == ["missing"]
    assert settled == [("present", "/nix/store/present-out")]
    statuses = {
        r["attr"]: r["status"]
        for r in await pool.fetch(
            "SELECT attr, status FROM build_attributes WHERE build_id = $1",
            build_id,
        )
    }
    # Store-valid path completed without rebuild.
    assert statuses["present"] == "succeeded"
    assert statuses["missing"] == "pending"


async def test_retention_cleanup(pool: asyncpg.Pool, tmp_path: Path) -> None:
    build_id = await make_build(pool, "old")
    await pool.execute(
        "UPDATE builds SET status='succeeded', "
        "finished_at = now() - interval '100 days' WHERE id = $1",
        build_id,
    )
    recent_id = await make_build(pool, "recent")
    await pool.execute(
        "UPDATE builds SET status='succeeded', finished_at = now() WHERE id = $1",
        recent_id,
    )
    log_dir = tmp_path / "logs" / str(build_id)
    log_dir.mkdir(parents=True)
    (log_dir / "a.zst").write_bytes(b"x")

    deleted = await cleanup_old_builds(pool, tmp_path, 90)
    assert deleted == 1
    assert not log_dir.exists()
    assert (
        await pool.fetchval("SELECT count(*) FROM builds WHERE id = $1", recent_id) == 1
    )


class _FakePool:
    """Returns the given build ids. Optionally runs a callback first."""

    def __init__(
        self, ids: set[int], on_fetch: Callable[[], None] | None = None
    ) -> None:
        self.ids = ids
        self.on_fetch = on_fetch

    async def fetch(self, _query: str) -> list[tuple[int]]:
        if self.on_fetch is not None:
            self.on_fetch()
        # Positional access like asyncpg.Record (the sqlc-generated
        # decode hooks index by column position).
        return [(build_id,) for build_id in self.ids]


async def test_orphan_log_dirs(tmp_path: Path) -> None:
    (tmp_path / "logs" / "42").mkdir(parents=True)
    (tmp_path / "logs" / "43").mkdir(parents=True)
    (tmp_path / "logs" / "not-a-build").mkdir(parents=True)
    await cleanup_orphan_log_dirs(_FakePool({42}), tmp_path, grace_seconds=0)
    assert (tmp_path / "logs" / "42").exists()
    assert not (tmp_path / "logs" / "43").exists()
    assert (tmp_path / "logs" / "not-a-build").exists()  # ignored


async def test_orphan_log_dirs_grace_period(tmp_path: Path) -> None:
    """A freshly created log dir is kept even without a build row: its
    build may have been inserted after the id snapshot."""
    (tmp_path / "logs" / "77").mkdir(parents=True)
    await cleanup_orphan_log_dirs(_FakePool(set()), tmp_path)
    assert (tmp_path / "logs" / "77").exists()


async def test_orphan_log_dirs_scan_before_id_snapshot(tmp_path: Path) -> None:
    """Dirs created after the directory scan must survive even when the
    id snapshot does not contain them (creation race)."""

    (tmp_path / "logs" / "1").mkdir(parents=True)  # triggers the snapshot

    def create_late_dir() -> None:
        (tmp_path / "logs" / "99").mkdir(parents=True)

    await cleanup_orphan_log_dirs(
        _FakePool(set(), on_fetch=create_late_dir), tmp_path, grace_seconds=0
    )
    assert (tmp_path / "logs" / "99").exists()
    assert not (tmp_path / "logs" / "1").exists()


async def test_resume_includes_interrupted_building_attributes(
    pool: asyncpg.Pool,
) -> None:
    # An attribute that was 'building' when the service died must
    # resume like a pending one (it is not terminal).
    build_id = await make_build(pool, "resume-building")
    await add_attr(pool, build_id, "inflight", "building")
    await add_attr(pool, build_id, "done", "succeeded")
    builds = await find_unfinished_builds(pool)
    target = next(b for b in builds if b.build_id == build_id)
    assert [j.attr for j in target.pending_jobs] == ["inflight"]


async def test_retention_skips_restarted_builds(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    # A restarted build briefly keeps its old finished_at while its
    # status is 'building' again. The hourly sweep must not delete it
    # mid-rerun.
    build_id = await make_build(pool, "restarted-old")
    await pool.execute(
        "UPDATE builds SET status = 'building', "
        "finished_at = now() - interval '100 days' WHERE id = $1",
        build_id,
    )
    await cleanup_old_builds(pool, tmp_path, 90)
    assert (
        await pool.fetchval("SELECT count(*) FROM builds WHERE id = $1", build_id) == 1
    )


async def test_retention_ages_out_failed_caches(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    old_ts = time.time() - 100 * 86400
    await pool.execute(
        "INSERT INTO failed_statuses (revision, status_name, timestamp) "
        "VALUES ('old-rev', 'ctx', $1), ('new-rev', 'ctx', $2)",
        old_ts,
        time.time(),
    )
    cache_project = await insert_project(pool, "cache-age", forge_repo_id="cache-age")
    await pool.execute(
        "INSERT INTO failed_builds (project_id, derivation, timestamp, url) "
        "VALUES ($3, '/nix/store/old.drv', $1, 'u'), "
        "($3, '/nix/store/new.drv', $2, 'u')",
        old_ts,
        time.time(),
        cache_project,
    )
    await cleanup_old_builds(pool, tmp_path, 90)
    revisions = {
        r["revision"] for r in await pool.fetch("SELECT revision FROM failed_statuses")
    }
    assert "old-rev" not in revisions
    assert "new-rev" in revisions
    drvs = {
        r["derivation"]
        for r in await pool.fetch("SELECT derivation FROM failed_builds")
    }
    assert "/nix/store/old.drv" not in drvs
    assert "/nix/store/new.drv" in drvs


async def test_check_store_paths_reads_the_nix_db(tmp_path: Path) -> None:
    """Validity comes from the store database, not from subprocesses."""
    db = tmp_path / "db.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE ValidPaths (path TEXT)")
    con.execute("INSERT INTO ValidPaths (path) VALUES ('/nix/store/aaa-built')")
    con.commit()
    con.close()

    result = await check_store_paths(
        ["/nix/store/aaa-built", "/nix/store/bbb-missing", ""], nix_db=db
    )
    assert result == {"/nix/store/aaa-built"}

    # Unreadable database: fail open as "nothing built yet".
    missing = await check_store_paths(
        ["/nix/store/aaa-built"], nix_db=tmp_path / "nope"
    )
    assert missing == set()


async def test_failed_rebuild_settles_pending_effects(pool: asyncpg.Pool) -> None:
    """A failed rebuild must settle its pending effect rows."""

    build_id = await make_build(pool, "fx-failed-rebuild")
    await pool.execute(
        "INSERT INTO effect_runs (project_id, kind, build_id, name, status) "
        "VALUES ((SELECT project_id FROM builds WHERE id = $1), 'push', $1, 'deploy', 'pending')",
        build_id,
    )
    await db.set_build_status(pool, build_id, "failed")
    row = await pool.fetchrow(
        "SELECT status, error FROM effect_runs WHERE build_id = $1",
        build_id,
    )
    assert row["status"] == "failed"
    assert "did not succeed" in row["error"]


async def test_rerun_clears_stale_eval_warnings(pool: asyncpg.Pool) -> None:
    """A re-run build must not show the previous attempt's warnings."""

    build_id = await make_build(pool, "lw-rerun")
    await builds_q.set_eval_warnings(
        pool,
        id_=build_id,
        warnings='[{"level": "warning", "message": "old", "count": 3}]',
    )
    await db.set_build_status(pool, build_id, "failed")
    assert await pool.fetchval(
        "SELECT eval_warnings FROM builds WHERE id = $1", build_id
    )
    await db.set_build_status(pool, build_id, "pending")
    assert (
        await pool.fetchval("SELECT eval_warnings FROM builds WHERE id = $1", build_id)
        is None
    )


async def test_interrupted_effects_fail_on_recovery(pool: asyncpg.Pool) -> None:
    """Effects never auto-re-run, so rows left running by a crash
    would spin forever without the startup sweep."""

    build_id = await make_build(pool, "fx-sweep")
    await pool.execute(
        "INSERT INTO effect_runs (project_id, kind, build_id, name) VALUES ((SELECT project_id FROM builds WHERE id = $1), 'push', $1, 'deploy')",
        build_id,
    )
    project_id = await pool.fetchval(
        "SELECT project_id FROM builds WHERE id = $1", build_id
    )
    await pool.execute(
        "INSERT INTO effect_runs (project_id, kind, schedule_name, name) "
        "VALUES ($1, 'schedule', 's', 'beat')",
        project_id,
    )
    await fail_interrupted_effects(pool, datetime.now(UTC) + timedelta(minutes=1))
    rows = await pool.fetch(
        "SELECT status, error, finished_at FROM effect_runs WHERE project_id = $1",
        project_id,
    )
    assert len(rows) == 2  # noqa: PLR2004
    for row in rows:
        assert row["status"] == "failed"
        assert "interrupted" in row["error"]
        assert row["finished_at"] is not None


async def test_interrupted_effects_sweep_spares_live_effects(
    pool: asyncpg.Pool,
) -> None:
    """The startup sweep runs concurrently with the work loop: effect
    rows created after process start are live deploys and must not be
    flipped to failed."""

    build_id = await make_build(pool, "fx-live")
    process_start = datetime.now(UTC)
    await pool.execute(
        "INSERT INTO effect_runs (project_id, kind, build_id, name) VALUES ((SELECT project_id FROM builds WHERE id = $1), 'push', $1, 'deploy')",
        build_id,
    )
    await fail_interrupted_effects(pool, process_start)
    status = await pool.fetchval(
        "SELECT status FROM effect_runs WHERE build_id = $1", build_id
    )
    assert status == "running"
