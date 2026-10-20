"""Work-queue claim semantics: dedup, per-key serialization, requeue."""

from __future__ import annotations

import dataclasses

import asyncpg
import pytest

from nixbot.db_gen.maintenance import supersede_pending_changes
from nixbot.webhooks import ChangeRequest
from nixbot.work_queue import WorkQueue

from .support import insert_project

pytestmark = pytest.mark.usefixtures("fresh_work_queue")


async def test_enqueue_dedupes_pending(pool: asyncpg.Pool) -> None:
    queue = WorkQueue(pool)
    assert await queue.enqueue("rerun", "build-1", {"build_id": 1})
    # Double click: identical pending intent collapses.
    assert not await queue.enqueue("rerun", "build-1", {"build_id": 1})
    # A different payload is a distinct intent.
    assert await queue.enqueue("report", "build-1", {"attempt": 2})
    item = await queue.claim_next()
    assert item is not None
    assert item.kind == "rerun"
    assert item.payload == {"build_id": 1}
    # Claimed (running) no longer blocks new intent.
    assert await queue.enqueue("rerun", "build-1", {"build_id": 1})
    await queue.finish(item.id)


async def test_claim_serializes_per_key(pool: asyncpg.Pool) -> None:
    queue = WorkQueue(pool)
    await queue.enqueue("rerun", "build-2")
    first = await queue.claim_next()
    assert first is not None
    await queue.enqueue("effects", "build-2")
    # Same key still running: must not run concurrently.
    assert await queue.claim_next() is None
    # work_queue_running_uniq enforces this in the database.
    with pytest.raises(asyncpg.UniqueViolationError):
        await pool.execute(
            "UPDATE work_queue SET status = 'running' "
            "WHERE kind = 'effects' AND dedup_key = 'build-2'"
        )
    # A different key is unaffected.
    await queue.enqueue("rerun", "build-3")
    other = await queue.claim_next()
    assert other is not None
    assert other.dedup_key == "build-3"
    # An exception with an empty message must still fail the item.
    await queue.finish(first.id, error=str(Exception()))
    row = await pool.fetchrow(
        "SELECT status, error FROM work_queue WHERE id = $1", first.id
    )
    assert (row["status"], row["error"]) == ("failed", "")
    blocked = await queue.claim_next()
    assert blocked is not None
    assert blocked.kind == "effects"


async def test_settle_interrupted_requeues(pool: asyncpg.Pool) -> None:
    queue = WorkQueue(pool)
    await queue.enqueue("change", "proj-1")
    assert await queue.claim_next() is not None
    # Same intent re-enqueued before the crash.
    await queue.enqueue("change", "proj-1")
    await queue.settle_interrupted()
    item = await queue.claim_next()
    assert item is not None
    assert item.dedup_key == "proj-1"
    await queue.finish(item.id)
    assert await queue.claim_next() is None
    superseded = await pool.fetchval(
        "SELECT count(*) FROM work_queue WHERE status = 'failed'"
    )
    assert superseded == 1


async def _enqueue_pr(
    queue: WorkQueue, number: int, base: str, head: str, sha: str
) -> None:
    await _enqueue_change(
        queue,
        ChangeRequest(
            forge="github",
            forge_repo_id="stack",
            branch=base,
            commit_sha=sha,
            pr_number=number,
            base_sha=f"refs/heads/{base}",
            head_in_base_repo=True,
            head_ref=head,
        ),
    )


async def _enqueue_change(queue: WorkQueue, change: ChangeRequest) -> None:
    await queue.enqueue(
        "change",
        f"change-{change.forge}-{change.forge_repo_id}-{change.commit_sha}",
        dataclasses.asdict(change),
    )


async def _claim_prs(queue: WorkQueue) -> dict[int, int]:
    """Claim everything claimable: PR number -> work item id."""
    claimed = {}
    while (item := await queue.claim_next()) is not None:
        claimed[item.payload["pr_number"]] = item.id
    return claimed


async def test_claim_builds_stacked_prs_bottom_up(pool: asyncpg.Pool) -> None:
    """A restack pushes every branch at once, top first here. Each PR
    waits for the PR whose head branch it is based on, failed or not."""
    await insert_project(pool, forge_repo_id="stack")
    queue = WorkQueue(pool)
    await _enqueue_pr(queue, 3, "b2", "b3", "c3")
    await _enqueue_pr(queue, 2, "b1", "b2", "c2")
    await _enqueue_pr(queue, 1, "main", "b1", "c1")

    bottom = await _claim_prs(queue)
    assert list(bottom) == [1]
    # A newer push to the bottom PR keeps the stack waiting for it.
    await _enqueue_pr(queue, 1, "main", "b1", "c1-new")
    await queue.finish(bottom[1], error="build failed")
    newer = await _claim_prs(queue)
    assert list(newer) == [1]
    await queue.finish(newer[1])

    middle = await _claim_prs(queue)
    assert list(middle) == [2]
    await queue.finish(middle[2])
    assert list(await _claim_prs(queue)) == [3]


async def test_claim_holds_only_on_open_pr_branches(pool: asyncpg.Pool) -> None:
    """No hold on a fork's head, on the default branch (a main to
    release PR), on a base PR closed meanwhile, or around a cycle."""
    await insert_project(pool, forge_repo_id="stack")
    queue = WorkQueue(pool)
    await _enqueue_change(
        queue,
        ChangeRequest(
            forge="github",
            forge_repo_id="stack",
            branch="main",
            commit_sha="c1",
            pr_number=1,
            head_ref="fork-main",
        ),
    )
    await _enqueue_pr(queue, 2, "fork-main", "b2", "c2")
    await _enqueue_pr(queue, 3, "release", "main", "c3")
    await _enqueue_pr(queue, 4, "main", "b4", "c4")
    await _enqueue_pr(queue, 5, "y", "x", "c5")
    await _enqueue_pr(queue, 6, "x", "y", "c6")
    await _enqueue_pr(queue, 7, "main", "b7", "c7")
    await _enqueue_pr(queue, 8, "b7", "b8", "c8")
    await supersede_pending_changes(
        pool, forge="github", forge_repo_id="stack", pr_number=7
    )
    assert list(await _claim_prs(queue)) == [1, 2, 3, 4, 5, 6, 8]
