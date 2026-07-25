"""Nix daemon proxy for the effect sandbox.

hercules-ci-agent gives effects a private daemon socket instead of
the host's: each connection is served by its own untrusted daemon
process, so effects cannot use trusted-user privileges and the
configured extra nix options apply. `nix-daemon --stdio` provides
exactly that per-connection behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        # Propagate EOF: the daemon only exits when its stdin closes,
        # and the client only sees the end of the reply this way.
        with contextlib.suppress(OSError):
            writer.close()


async def _handle(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    extra_options: list[tuple[str, str]],
) -> None:
    args = [arg for k, v in extra_options for arg in ("--option", k, v)]
    try:
        proc = await asyncio.create_subprocess_exec(
            "nix-daemon",
            "--stdio",
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # A hanging connection would stall the nix client in the
        # effect. A closed one fails it loudly.
        writer.close()
        raise
    assert proc.stdin is not None  # noqa: S101
    assert proc.stdout is not None  # noqa: S101
    try:
        await asyncio.gather(_pump(reader, proc.stdin), _pump(proc.stdout, writer))
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


@asynccontextmanager
async def nix_daemon_proxy(
    socket_path: Path, extra_options: list[tuple[str, str]]
) -> AsyncIterator[None]:
    """Serve a unix socket while the effect runs. Stops serving on exit."""
    # Handlers are tracked so exit can cancel them. Closing the server
    # only stops accepting new connections, while in-flight handlers
    # (and their nix-daemon children) would keep running.
    handlers: set[asyncio.Task[None]] = set()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_handle(reader, writer, extra_options))
        handlers.add(task)
        task.add_done_callback(handlers.discard)

    server = await asyncio.start_unix_server(accept, path=str(socket_path))
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        for task in handlers:
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
