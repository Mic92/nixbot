"""Concurrent random queries must neither deadlock nor hang.

Workers fire the build, effect and work-queue queries at a few shared
builds in random order. Postgres aborts a lock cycle with
DeadlockDetectedError, so any such error fails the test; the timeout
catches waits it cannot see, like one worker holding a lock across an
await.
"""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

import asyncpg
import pytest

from nixbot import db
from nixbot.db_gen import approvals as approvals_q
from nixbot.db_gen import builds as builds_q
from nixbot.db_gen import maintenance as maintenance_q
from nixbot.db_gen import work_queue as wq

from .support import insert_project

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

NAMES = ["a", "b", "c", "d", "e"]
WORKERS = 12
STEPS = 150
TIMEOUT = 60


async def _insert_effects(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    # Same names in a different order each call: the lock order of
    # concurrent multi-row writes is what can deadlock.
    names = rng.sample(NAMES, rng.randint(1, len(NAMES)))
    await builds_q.insert_build_effects(
        pool,
        build_id=build,
        kind="push",
        status=rng.choice(["pending", "skipped"]),
        names=names,
        deps=["[]"] * len(names),
        changed=[""] * len(names),
        force_run=False,
    )


async def _enqueue(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    names = rng.sample(NAMES, rng.randint(1, len(NAMES)))
    await wq.enqueue_effect_items(
        pool,
        build_id=build,
        kind="push",
        names=names,
        dedup_keys=[f"build-{build}-effect-{n}" for n in names],
    )


async def _claim_and_finish_work(
    pool: asyncpg.Pool, rng: random.Random, _build: int
) -> None:
    item = await wq.claim_next_work_item(pool)
    if item is not None:
        await wq.finish_work_item(
            pool, id_=item.id_, status=rng.choice(["done", "failed"]), error=None
        )


async def _finish_effect(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    name = rng.choice(NAMES)
    if await builds_q.claim_effect(
        pool, build_id=build, kind="push", name=name, status="running"
    ):
        await builds_q.finish_effect(
            pool,
            build_id=build,
            kind="push",
            name=name,
            status=rng.choice(["succeeded", "failed"]),
            error=None,
            log_size=0,
            log_truncated=False,
        )


async def _drop(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    names = None if rng.getrandbits(1) else rng.sample(NAMES, rng.randint(1, 3))
    await maintenance_q.drop_effects_for_rerun(pool, build_id=build, names=names)


async def _drop_removed(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    await maintenance_q.drop_removed_effects(
        pool, build_id=build, names=rng.sample(NAMES, rng.randint(1, len(NAMES)))
    )


async def _aggregate(pool: asyncpg.Pool, _rng: random.Random, build: int) -> None:
    await db.aggregate_build(pool, build)


async def _mark_started(pool: asyncpg.Pool, _rng: random.Random, build: int) -> None:
    await builds_q.mark_effects_started(pool, id_=build)


async def _gate_and_approve(pool: asyncpg.Pool, rng: random.Random, build: int) -> None:
    project_id = await pool.fetchval(
        "SELECT project_id FROM builds WHERE id = $1", build
    )
    pr = rng.randint(1, 3)
    if rng.getrandbits(1):
        await approvals_q.gate_pr(
            pool, project_id=project_id, pr_number=pr, pending=None
        )
    else:
        await approvals_q.approve_pr(
            pool, project_id=project_id, pr_number=pr, approved_by="fuzz"
        )


OPERATIONS: list[Callable[[asyncpg.Pool, random.Random, int], Awaitable[None]]] = [
    _insert_effects,
    _insert_effects,
    _enqueue,
    _claim_and_finish_work,
    _finish_effect,
    _drop,
    _drop_removed,
    _aggregate,
    _mark_started,
    _gate_and_approve,
]


@pytest.mark.parametrize("seed", range(8))
async def test_concurrent_queries_do_not_deadlock(
    pool: asyncpg.Pool, seed: int
) -> None:
    project_id = await insert_project(pool, f"fuzz{seed}")
    builds = [
        (
            await db.get_or_create_build(
                pool, project_id, f"tree-{seed}-{i}", "sha", "main"
            )
        )[0].id_
        for i in range(3)
    ]

    async def worker(n: int) -> None:
        rng = random.Random(seed * 1000 + n)  # noqa: S311
        for _ in range(STEPS):
            op = rng.choice(OPERATIONS)
            try:
                await op(pool, rng, rng.choice(builds))
            except asyncpg.DeadlockDetectedError as e:
                pytest.fail(f"deadlock in {op.__name__} (seed {seed}, worker {n}): {e}")

    async with asyncio.timeout(TIMEOUT):
        await asyncio.gather(*(worker(n) for n in range(WORKERS)))
