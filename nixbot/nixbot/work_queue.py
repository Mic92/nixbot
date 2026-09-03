"""Work queue (migration 0010): producers enqueue intent, one
dispatcher claims and executes. Pending items dedupe per (kind, key);
claims serialize per key and survive restarts via requeue. Dedup
includes the payload: same key with different payloads (e.g. report
retries with rising attempt counts) are distinct intents."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from .db_gen import work_queue as q


class TransientError(Exception):
    """Raised by a work handler when the item should be retried later,
    e.g. the forge API was down. retry_after carries a Retry-After hint."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    dedup_key: str
    payload: dict[str, Any]
    attempts: int = 0


class WorkQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def enqueue(
        self,
        kind: str,
        dedup_key: str,
        payload: dict[str, Any] | None = None,
        *,
        delay: float = 0,
    ) -> bool:
        """Returns False when an identical pending item already exists."""
        return (
            await q.enqueue_work_item(
                self.pool,
                kind=kind,
                dedup_key=dedup_key,
                payload=json.dumps(payload or {}),
                delay=delay,
            )
            is not None
        )

    async def claim_next(self) -> WorkItem | None:
        """Claim the oldest pending item whose dedup key is idle."""
        try:
            row = await self._claim_row()
        except asyncpg.UniqueViolationError:
            # Lost the running slot (see work_queue_running_uniq);
            # the item stays pending for a later pass.
            return None
        if row is None:
            return None
        return WorkItem(
            id=row.id_,
            kind=row.kind,
            dedup_key=row.dedup_key,
            payload=json.loads(row.payload),
            attempts=row.attempts,
        )

    async def _claim_row(self) -> q.ClaimNextWorkItemRow | None:
        return await q.claim_next_work_item(self.pool)

    async def finish(self, item_id: int, *, error: str | None = None) -> None:
        await q.finish_work_item(
            self.pool,
            id_=item_id,
            status="done" if error is None else "failed",
            error=error,
        )

    async def retry(self, item_id: int, *, delay: float, error: str) -> bool:
        """Put a claimed item back to pending, due after `delay` seconds.
        False when an identical item is pending anyway."""
        try:
            n = await q.retry_work_item(
                self.pool, id_=item_id, error=error, delay=delay
            )
        except asyncpg.UniqueViolationError:
            return False
        return n > 0

    async def settle_interrupted(self) -> None:
        """Startup: requeue work the previous process died holding.
        Executors are idempotent against completed state (existing
        builds are found by tree hash, effects by the started flag),
        so re-dispatching is safe. Assumes a single dispatcher process;
        with several, this would steal live work (a claimed_at lease
        would be needed instead)."""
        await q.settle_interrupted_work(self.pool)

    async def cleanup(self, retention_days: int) -> None:
        await q.cleanup_work_queue(self.pool, retention_days=retention_days)


MAX_WORK_ATTEMPTS = 6
MAX_WORK_DELAY_SECONDS = 3600


def work_retry_delay(attempt: int, retry_after: float | None, base: float) -> float:
    """Exponential backoff starting at `base` seconds, but never shorter
    than the forge's Retry-After: GitHub escalates rate limits when it
    is ignored."""
    backoff = min(base * 2 ** (attempt - 1), 900)
    hinted = retry_after or 0.0
    return min(max(backoff, hinted), MAX_WORK_DELAY_SECONDS)
