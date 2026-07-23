"""Shared subprocess helper for process-group lifecycle.

Children are spawned in their own process group (session) so that on
timeout or cancellation the whole tree can be killed, not just the
direct child: nix, cachix and deploy tooling all fork helpers that
would otherwise outlive the build.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

# asyncio's StreamReader default. Callers with long-line output (nix
# build logs, deploy tooling) pass a larger limit.
DEFAULT_STREAM_LIMIT = 2**16


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
        stdout: int | None = None,
        stderr: int | None = None,
        limit: int = DEFAULT_STREAM_LIMIT,
    ) -> ProcessGroup:
        """Spawn a child as the leader of a new process group."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            env=env,
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
