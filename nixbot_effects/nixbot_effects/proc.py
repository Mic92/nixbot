"""Shared subprocess helpers.

Children are spawned in their own process group (session) so that on
timeout or cancellation the whole tree can be killed, not just the
direct child: nix, cachix and deploy tooling all fork helpers that
would otherwise outlive the run. The nixbot daemon reuses ProcessGroup
for its own build subprocesses.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import sys
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .errors import EffectError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    LogWrite = Callable[[bytes], Awaitable[None]]

# asyncio's StreamReader default. Callers with long-line output (nix
# build logs, deploy tooling) pass a larger limit.
DEFAULT_STREAM_LIMIT = 2**16

# Deploy tooling emits arbitrarily long lines and nix logs freely on
# stderr. Keep the limit well above both.
STREAM_LIMIT = 16 * 1024 * 1024


@dataclass
class ProcessGroup:
    """A child process running as the leader of its own process group.

    Wraps the asyncio Process so callers keep direct access to its
    streams/communicate()/wait()/returncode, while kill semantics
    (whole group, idempotent, no zombie) live in one place.
    """

    proc: asyncio.subprocess.Process

    @classmethod
    async def start(  # noqa: PLR0913
        cls,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        limit: int = DEFAULT_STREAM_LIMIT,
    ) -> ProcessGroup:
        """Spawn a child as the leader of a new process group."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            limit=limit,
            start_new_session=True,
        )
        return cls(proc)

    async def reap(self) -> None:
        """SIGKILL the whole group if still running, then reap the
        child so no zombie is left behind. The group may already be
        gone when the kill races the leader's exit."""
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            await self.proc.wait()


async def _pump(
    stream: asyncio.StreamReader, sink: LogWrite | None, tail: deque[bytes]
) -> None:
    async for line in stream:
        tail.append(line)
        if sink is not None:
            await sink(line)


async def stream_command(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    log: LogWrite | None = None,
    capture_stdout: bool = False,
    debug: bool = False,
) -> str:
    """Run a command in its own process group, streaming output to `log`
    line-wise. Returns stdout when `capture_stdout` (stderr still goes
    to `log`, so nix logging cannot corrupt JSON output). Raises
    EffectError on non-zero exit. On cancellation the whole process
    group is killed."""
    if debug:
        print("$", shlex.join(cmd), file=sys.stderr)
    group = await ProcessGroup.start(
        cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    proc = group.proc
    assert proc.stderr is not None  # noqa: S101
    stdout_chunks: list[bytes] = []
    tail: deque[bytes] = deque(maxlen=100)

    async def read_stdout() -> None:
        assert proc.stdout is not None  # noqa: S101
        if capture_stdout:
            stdout_chunks.extend([line async for line in proc.stdout])
        else:
            await _pump(proc.stdout, log, tail)

    try:
        await asyncio.gather(read_stdout(), _pump(proc.stderr, log, tail), proc.wait())
    except BaseException:
        await group.reap()
        raise
    if proc.returncode != 0:
        msg = f"{cmd[0]} failed with exit code {proc.returncode}"
        if log is None:
            # Without a log sink the caller never saw the output.
            detail = b"".join(tail).decode(errors="replace")
            msg += f":\n{detail[-2000:]}"
        raise EffectError(msg)
    return b"".join(stdout_chunks).decode(errors="replace")
