"""Random concurrent SQL on shared rows must not deadlock or hang.

Arguments come from the sqlc signatures, so new queries are covered."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

import asyncpg
import pytest

from nixbot import db, db_gen
from nixbot.config import ScheduledEffectConfig, ScheduleWhen
from nixbot.schedules import ScheduledEffectsStore

from .support import insert_project

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.fuzz

WORKERS = 12
STEPS = 120
TIMEOUT = 120

# Few names, so concurrent multi-row writes overlap.
WORDS = list("abcde")
VOCAB = {
    "status": ["pending", "running", "succeeded", "failed", "skipped", "cancelled"],
    "kind": ["push", "check", "event"],
    "forge": ["github", "gitea"],
    "branch": ["main", "dev"],
    "deps": ["[]"],
    "changed": [""],
    "payload": ["{}"],
    "outputs": ["{}"],
    "warnings": ["[]"],
    "eval_warnings": ["[]"],
    "changed_inputs": ["{}"],
    "when_specs": ['{"hour": 3}'],
}


class Fixtures(NamedTuple):
    projects: list[int]
    builds: list[int]


def _load_queries() -> dict[str, Callable[..., Any]]:
    queries: dict[str, Callable[..., Any]] = {}
    for info in pkgutil.iter_modules(db_gen.__path__):
        if info.name != "models":
            module = importlib.import_module(f"nixbot.db_gen.{info.name}")
            for name, fn in inspect.getmembers(module, inspect.isfunction):
                if fn.__module__ == module.__name__ and "conn" in fn.__annotations__:
                    queries[f"{info.name}.{name}"] = fn
    return queries


QUERIES = _load_queries()


def _value(rng: random.Random, fx: Fixtures, name: str, annotation: str, n: int) -> Any:
    base = annotation.removesuffix(" | None")
    if base != annotation and rng.randrange(4) == 0:
        return None
    scalar = {
        "int": lambda: _int(rng, fx, name),
        "bool": lambda: bool(rng.getrandbits(1)),
        "float": lambda: rng.random() * 100,
        "uuid.UUID": uuid.uuid4,
        "datetime.datetime": lambda: (
            datetime.now(UTC) + timedelta(minutes=rng.randint(-90, 90))
        ),
        "str": lambda: rng.choice(VOCAB.get(name.removesuffix("_"), WORDS)),
    }
    if base in scalar:
        return scalar[base]()
    element = base.removeprefix("collections.abc.Sequence[").removesuffix("]")
    if element in scalar:
        return [scalar[element]() for _ in range(n)]
    msg = f"no generator for {name}: {annotation}"
    raise AssertionError(msg)


def _int(rng: random.Random, fx: Fixtures, name: str) -> int:
    if "project" in name:
        return rng.choice(fx.projects)
    if "build" in name or name in {"id_", "ids"}:
        return rng.choice([*fx.builds, *range(6)])
    return rng.choice(range(6))


async def _run(conn: Any, name: str, rng: random.Random, fx: Fixtures) -> bool:
    """False if the schema rejected the random arguments."""
    fn = QUERIES[name]
    n = rng.randint(1, 5)
    kwargs = {
        p: _value(rng, fx, p, str(a.annotation), n)
        for p, a in inspect.signature(fn).parameters.items()
        if p != "conn"
    }
    try:
        await fn(conn, **kwargs)
    except asyncpg.PostgresError as e:
        # Constraint errors are expected; class 42 and XX are bugs.
        if isinstance(e, asyncpg.DeadlockDetectedError) or (e.sqlstate or "")[:2] in {
            "42",
            "XX",
        }:
            pytest.fail(f"{name}: {e}")
        return False
    except (asyncpg.DataError, TypeError, ValueError):
        return False
    return True


async def _fixtures(pool: asyncpg.Pool, tag: str) -> Fixtures:
    projects = [await insert_project(pool, f"fuzz-{tag}-{i}") for i in range(2)]
    builds = [
        (await db.get_or_create_build(pool, p, f"tree-{tag}-{i}", "sha", "main"))[0].id_
        for p in projects
        for i in range(2)
    ]
    return Fixtures(projects, builds)


async def test_every_query_runs(pool: asyncpg.Pool) -> None:
    """Each query must succeed once, or the fuzzer silently skips it."""
    fx = await _fixtures(pool, "reach")
    rng = random.Random(0)  # noqa: S311
    unreached = []
    for name in sorted(QUERIES):
        for _ in range(60):
            if await _run(pool, name, rng, fx):
                break
        else:
            unreached.append(name)
    assert not unreached


def _queries_by_table(tables: set[str]) -> dict[str, list[str]]:
    """Lock cycles need statements on the same tables."""
    groups: dict[str, list[str]] = {}
    for name, fn in QUERIES.items():
        sql = getattr(inspect.getmodule(fn), fn.__name__.upper(), None)
        sql = sql or inspect.getsource(fn)
        for table in set(
            re.findall(r"(?:FROM|INTO|UPDATE|JOIN)\s+(\w+)", sql, re.IGNORECASE)
        ):
            if table in tables:
                groups.setdefault(table, []).append(name)
    return {t: qs for t, qs in groups.items() if len(qs) > 1}


async def _transaction(pool: asyncpg.Pool, rng: random.Random, fx: Fixtures) -> None:
    """The service's multi-statement transactions."""
    project = rng.choice(fx.projects)
    which = rng.randrange(3)
    effects = [rng.choice(WORDS) for _ in range(rng.randint(1, 4))]
    schedule = ScheduledEffectConfig(
        name="nightly", when=ScheduleWhen(hour=3), effects=effects
    )
    try:
        if which == 0:
            await db.get_or_create_build(
                pool,
                project,
                f"tree-{rng.randint(0, 3)}",
                "sha",
                "main",
                pr_number=rng.choice([None, 1, 2]),
            )
        elif which == 1:
            await db.aggregate_build(pool, rng.choice(fx.builds))
        else:
            await ScheduledEffectsStore(pool).replace_schedules(
                project, {"nightly": schedule}
            )
    except asyncpg.DeadlockDetectedError as e:
        pytest.fail(f"deadlock in transaction #{which}: {e}")
    except (asyncpg.PostgresError, LookupError):
        return


@pytest.mark.parametrize("seed", range(6))
async def test_concurrent_queries_do_not_deadlock(
    pool: asyncpg.Pool, seed: int
) -> None:
    fx = await _fixtures(pool, f"dl{seed}")
    rows = await pool.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    by_table = _queries_by_table({r["table_name"] for r in rows})

    async def worker(n: int) -> None:
        rng = random.Random(seed * 1000 + n)  # noqa: S311
        for _ in range(STEPS):
            if rng.randrange(8) == 0:
                await _transaction(pool, rng, fx)
            else:
                name = rng.choice(by_table[rng.choice(sorted(by_table))])
                await _run(pool, name, rng, fx)

    async with asyncio.timeout(TIMEOUT):
        await asyncio.gather(*(worker(n) for n in range(WORKERS)))
