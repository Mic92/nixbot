"""Shared test infrastructure: ephemeral Postgres, seed data, and a
real uvicorn server hosting the service web app.

Used by the browser e2e tests and the httpx-based web tests.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING

import asyncpg
import uvicorn
import zstandard

from nixbot.executor import attribute_log_path, container_path
from nixbot.logstore import LogContainerWriter
from nixbot.tests.support import insert_build, insert_project
from nixbot.web.app import create_app
from nixbot.web.logs import LogRegistry

if TYPE_CHECKING:
    import concurrent.futures
    from collections.abc import Coroutine
    from pathlib import Path


def _seed_container(*, failed: bool) -> bytes:
    """A container with one primary derivation (phased, optionally
    failed) plus a built dependency, for the structured log viewer."""
    w = LogContainerWriter()
    hello = "/nix/store/aaa-hello-2.12.drv"
    w.register(hello, "hello-2.12")
    w.phase(hello, "unpack")
    w.line(hello, "unpacking sources")
    w.phase(hello, "build")
    for i in range(30):
        w.line(hello, f"CC step {i}.o")
    if failed:
        w.line(hello, "error: build failed on the last step")
    w.status(hello, "failed" if failed else "built")
    dep = "/nix/store/bbb-zlib-1.3.drv"
    w.register(dep, "zlib-1.3")
    w.line(dep, "building zlib")
    w.status(dep, "built")
    return w.finalize()


async def seed(dsn: str, state_dir: Path | None = None) -> None:
    """One project with a succeeded, a failed, and a running build."""
    pool = await asyncpg.create_pool(dsn)
    try:
        project_id = await insert_project(
            pool, forge_repo_id="web-1", url="https://github.com/acme/widget"
        )
        for number, status, branch in [
            (1, "succeeded", "main"),
            (2, "failed", "main"),
            (3, "building", "feature"),
        ]:
            build_id = await insert_build(
                pool,
                project_id,
                number=number,
                tree_hash=f"tree-{number}",
                commit_sha=f"sha-{number}{'0' * 30}",
                branch=branch,
                status=status,
                pr_number=5 if branch == "feature" else None,
                started=True,
            )
            await pool.execute(
                """
                INSERT INTO build_attributes (build_id, attr, system, status, error)
                VALUES
                  ($1, 'x86_64-linux.ok', 'x86_64-linux', 'succeeded', NULL),
                  ($1, 'x86_64-linux.bad', 'x86_64-linux', $2, $3),
                  ($1, 'aarch64-linux.other', 'aarch64-linux', 'succeeded', NULL)
                """,
                build_id,
                "failed" if status == "failed" else "succeeded",
                "\x1b[31;1merror:\x1b[0m builder failed loudly"
                if status == "failed"
                else None,
            )
            # Finished builds get a log for the prev/next navigation.
            if state_dir is not None and status != "building":
                log_file = attribute_log_path(state_dir, build_id, "x86_64-linux.bad")
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_bytes(
                    zstandard.ZstdCompressor().compress(f"log #{number}\n".encode())
                )
                # Structured sidecar: the log page renders the
                # per-derivation viewer when this exists.
                container_path(log_file).write_bytes(
                    _seed_container(failed=status == "failed")
                )
                await pool.execute(
                    "UPDATE build_attributes SET log_size = $3"
                    " WHERE build_id = $1 AND attr = $2",
                    build_id,
                    "x86_64-linux.bad",
                    log_file.stat().st_size,
                )
    finally:
        await pool.close()


class TestServer:
    """Service web app on a real TCP port, in a background event loop.

    Playwright drives a real browser, so an ASGI test client is not
    enough. `run()` executes coroutines (DB updates, log writes) on the
    server's loop from the synchronous test thread.
    """

    def __init__(self, dsn: str, state_dir: Path) -> None:
        self.dsn = dsn
        self.state_dir = state_dir
        self.registry = LogRegistry()
        # Assigned by the server thread before the TCP port opens.
        self.pool: asyncpg.Pool
        self.loop: asyncio.AbstractEventLoop
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def main() -> None:
            async def serve() -> None:
                self.loop = asyncio.get_running_loop()
                self.pool = await asyncpg.create_pool(self.dsn)
                try:
                    app = create_app(self.pool, self.state_dir, self.registry)
                    config = uvicorn.Config(
                        app, host="127.0.0.1", port=self.port, log_level="warning"
                    )
                    self._server = uvicorn.Server(config)
                    await self._server.serve()
                finally:
                    await self.pool.close()

            asyncio.run(serve())

        self._thread = threading.Thread(target=main, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                if not self._thread.is_alive():
                    msg = "server thread died during startup"
                    raise RuntimeError(msg) from None
                if time.monotonic() > deadline:
                    msg = "uvicorn did not start"
                    raise RuntimeError(msg) from None
                time.sleep(0.1)

    def run[T](self, coro: Coroutine[None, None, T]) -> T:
        """Run a coroutine on the server's event loop."""
        future: concurrent.futures.Future[T] = asyncio.run_coroutine_threadsafe(
            coro, self.loop
        )
        return future.result(timeout=30)

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
