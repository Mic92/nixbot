"""Tests for the persistent batching upload queue."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from nixbot.config import UploaderConfig
from nixbot.db_gen import uploads as q
from nixbot.upload import (
    MAX_ATTEMPTS,
    BatchUploader,
    StreamUploader,
    Uploader,
    chunk_args,
    parse_path_info,
    start_uploaders,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import asyncpg


async def identity(paths: set[str]) -> set[str]:
    return paths


async def drv_to_out(paths: set[str]) -> set[str]:
    # Stand-in for nix-store: failed.drv has no valid output.
    return {p.removesuffix(".drv") for p in paths if p != "/nix/store/failed.drv"}


@pytest.fixture(autouse=True)
async def clean(pool: asyncpg.Pool) -> None:
    await pool.execute("TRUNCATE upload_queue")


@contextlib.asynccontextmanager
async def running(uploader: Uploader, pool: asyncpg.Pool) -> AsyncIterator[None]:
    tasks = await start_uploaders([uploader], pool)
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def recorder(
    tmp_path: Path, name: str = "up", *, stdin: bool = False
) -> UploaderConfig:
    script = 'cat >> "$LOG"' if stdin else 'printf "%s\\n" "$@" >> "$LOG"'
    return UploaderConfig(
        name=name,
        command=["sh", "-c", f'{script}; echo batch >> "$LOG"', "sh"],
        environment={"LOG": str(tmp_path / f"{name}.log")},
        paths_via="stdin" if stdin else "argv",
    )


def test_chunk_args_splits_on_byte_limit() -> None:
    assert chunk_args(["aa", "bb", "cc"], limit=6) == [["aa", "bb"], ["cc"]]
    assert chunk_args([], limit=6) == []


def test_parse_path_info_both_formats() -> None:
    new = '{"/nix/store/a":{"narSize":1},"/nix/store/b":null}'
    old = '[{"path":"/nix/store/a"},{"path":"/nix/store/b","valid":false}]'
    assert parse_path_info(new) == {"/nix/store/a"}
    assert parse_path_info(old) == {"/nix/store/a"}


async def test_final_upload_reports_and_clears_queue(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    up = Uploader.create(recorder(tmp_path), pool, resolve=identity)
    async with running(up, pool):
        result = await up.upload(["/nix/store/a", "/nix/store/b"])
    assert result.success
    assert (tmp_path / "up.log").read_text().split() == [
        "/nix/store/a",
        "/nix/store/b",
        "batch",
    ]
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0


async def test_intermediates_batch_with_final_and_drop_invalid(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    up = Uploader.create(recorder(tmp_path), pool, resolve=drv_to_out)
    # Queued before the worker runs: everything lands in one push.
    up.enqueue_nowait("/nix/store/dep1.drv")
    up.enqueue_nowait("/nix/store/failed.drv")
    async with running(up, pool):
        result = await up.upload(["/nix/store/top"])
    assert result.success
    lines = (tmp_path / "up.log").read_text().split()
    assert lines.count("batch") == 1
    assert set(lines) == {"/nix/store/dep1", "/nix/store/top", "batch"}


async def test_stdin_mode(pool: asyncpg.Pool, tmp_path: Path) -> None:
    up = Uploader.create(recorder(tmp_path, stdin=True), pool, resolve=identity)
    async with running(up, pool):
        assert (await up.upload(["/nix/store/a"])).success
    assert (tmp_path / "up.log").read_text().split() == ["/nix/store/a", "batch"]


async def test_pending_paths_survive_restart(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    # Left over by a previous service instance that died mid-batch.
    await q.enqueue_upload_paths(pool, uploader="up", paths=["/nix/store/old"])
    await q.enqueue_upload_paths(pool, uploader="gone", paths=["/nix/store/x"])
    up = Uploader.create(recorder(tmp_path), pool, resolve=identity)
    async with running(up, pool):
        assert (await up.upload(["/nix/store/new"])).success
    lines = (tmp_path / "up.log").read_text().split()
    assert "/nix/store/old" in lines
    assert "/nix/store/new" in lines
    # Rows of uploaders no longer configured are dropped at startup.
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0


def streamer(tmp_path: Path, body: str, name: str = "up") -> UploaderConfig:
    """Fake `niks3 push --stdin`: `body` runs per input line with `p`
    bound and may call ok(p)/err(p, msg)."""
    script = tmp_path / f"{name}.py"
    script.write_text(
        textwrap.dedent("""
        import json, os, sys
        log = open(os.environ["LOG"], "a")
        log.write("start\\n"); log.flush()
        def ok(p): print(json.dumps({"path": p, "status": "ok"}), flush=True)
        def err(p, m): print(json.dumps({"path": p, "status": "error", "message": m}), flush=True)
        for line in sys.stdin:
            p = line.strip()
            log.write(p + "\\n"); log.flush()
        """)
        + textwrap.indent(textwrap.dedent(body), "    ")
    )
    return UploaderConfig(
        name=name,
        command=[sys.executable, str(script)],
        environment={"LOG": str(tmp_path / f"{name}.log")},
        paths_via="stream",
    )


def test_uploader_factory_picks_stream(pool: asyncpg.Pool, tmp_path: Path) -> None:
    assert isinstance(Uploader.create(recorder(tmp_path), pool), BatchUploader)
    assert isinstance(
        Uploader.create(streamer(tmp_path, "ok(p)"), pool), StreamUploader
    )


async def test_stream_reports_per_path_and_clears_queue(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    up = Uploader.create(streamer(tmp_path, "ok(p)"), pool, resolve=drv_to_out)
    up.enqueue_nowait("/nix/store/dep1.drv")
    up.enqueue_nowait("/nix/store/failed.drv")
    async with running(up, pool):
        a, b = await asyncio.gather(
            up.upload(["/nix/store/a"]), up.upload(["/nix/store/b"])
        )
        assert a.success
        assert b.success
        for _ in range(200):
            if await pool.fetchval("SELECT count(*) FROM upload_queue") == 0:
                break
            await asyncio.sleep(0.01)
    lines = (tmp_path / "up.log").read_text().split()
    assert lines.count("start") == 1
    assert set(lines) == {"start", "/nix/store/dep1", "/nix/store/a", "/nix/store/b"}
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0


async def test_stream_error_is_reported_with_message(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    body = 'err(p, "cache down") if p.endswith("bad") else ok(p)'
    up = Uploader.create(streamer(tmp_path, body), pool, resolve=identity)
    async with running(up, pool):
        good, bad = await asyncio.gather(
            up.upload(["/nix/store/good"]), up.upload(["/nix/store/bad"])
        )
    assert good.success
    assert not bad.success
    assert "cache down" in bad.output
    # niks3 retries itself; nixbot does not resend.
    assert (tmp_path / "up.log").read_text().split().count("/nix/store/bad") == 1
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0


async def test_stream_child_crash_refeeds_pending(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    # First instance dies on its first path; the restart is fed the
    # queue again and the waiter gets its ack from there.
    marker = tmp_path / "crashed"
    body = f"""
        import pathlib
        m = pathlib.Path({str(marker)!r})
        if not m.exists():
            m.touch(); sys.exit(3)
        ok(p)
    """
    await q.enqueue_upload_paths(pool, uploader="up", paths=["/nix/store/old"])
    up = Uploader.create(
        streamer(tmp_path, body), pool, resolve=identity, retry_delay=0.01
    )
    async with running(up, pool):
        assert (await up.upload(["/nix/store/new"])).success
        for _ in range(300):
            if await pool.fetchval("SELECT count(*) FROM upload_queue") == 0:
                break
            await asyncio.sleep(0.01)
    runs = (tmp_path / "up.log").read_text().split("start\n")
    assert len(runs) == 1 + 2
    assert {"/nix/store/old", "/nix/store/new"} <= set(runs[2].split())
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0


async def test_failure_reports_retries_and_gives_up(
    pool: asyncpg.Pool, tmp_path: Path
) -> None:
    counter = tmp_path / "count"
    cfg = UploaderConfig(
        name="up",
        command=["sh", "-c", f"echo x >> {counter}; echo cache down; exit 1", "sh"],
    )
    up = Uploader.create(cfg, pool, resolve=identity, retry_delay=0.01)
    async with running(up, pool):
        result = await up.upload(["/nix/store/a"])
        assert not result.success
        assert "cache down" in result.output
        for _ in range(200):
            if await pool.fetchval("SELECT count(*) FROM upload_queue") == 0:
                break
            await asyncio.sleep(0.01)
    assert await pool.fetchval("SELECT count(*) FROM upload_queue") == 0
    assert counter.read_text().count("x") == MAX_ATTEMPTS
