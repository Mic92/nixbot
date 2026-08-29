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


@dataclass(frozen=True)
class WorkItem:
    id: int
    kind: str
    dedup_key: str
    payload: dict[str, Any]


class WorkQueue:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def enqueue(
        self, kind: str, dedup_key: str, payload: dict[str, Any] | None = None
    ) -> bool:
        """Returns False when an identical pending item already exists."""
        return (
            await q.enqueue_work_item(
                self.pool,
                kind=kind,
                dedup_key=dedup_key,
                payload=json.dumps(payload or {}),
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
