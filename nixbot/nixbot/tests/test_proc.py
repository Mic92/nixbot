"""Direct tests for the shared ProcessGroup subprocess helper.

These cover the kill semantics (whole group, including grandchildren;
no zombie. Idempotent when the child already exited) that the
executor, post-build and effects modules rely on.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

import pytest
from nixbot_effects.proc import ProcessGroup

if TYPE_CHECKING:
    from pathlib import Path


def _assert_dead(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pid, 9)  # do not leak it past the test
    pytest.fail(f"process {pid} survived process-group kill")


async def test_reap_kills_whole_group(tmp_path: Path) -> None:
    # A grandchild (own fork) must die with the group: nix and deploy
    # tools fork helpers that a plain proc.kill() would leave running.
    pidfile = tmp_path / "pid"

    group = await ProcessGroup.start(
        ["sh", "-c", f"(echo $$ > {pidfile}; sleep 60) & wait"],
        cwd=tmp_path,
    )
    while not pidfile.exists() or not pidfile.read_text().strip():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    await group.reap()
    assert group.proc.returncode is not None

    _assert_dead(int(pidfile.read_text()))


async def test_reap_after_clean_exit_is_noop(tmp_path: Path) -> None:
    group = await ProcessGroup.start(["true"], cwd=tmp_path)
    await group.proc.wait()
    await group.reap()  # must not raise or block

    assert group.proc.returncode == 0


async def test_reap_on_task_cancel(tmp_path: Path) -> None:
    # The pattern all callers use: kill the group when the awaiting
    # task is cancelled.
    pidfile = tmp_path / "pid"

    async def body() -> None:
        group = await ProcessGroup.start(
            ["sh", "-c", f"echo $$ > {pidfile}; sleep 60"], cwd=tmp_path
        )
        try:
            await group.proc.wait()
        except asyncio.CancelledError:
            await group.reap()
            raise

    task = asyncio.create_task(body())
    while not pidfile.exists() or not pidfile.read_text().strip():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    _assert_dead(int(pidfile.read_text()))
