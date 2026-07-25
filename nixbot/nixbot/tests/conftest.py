"""Shared fixtures: one ephemeral Postgres per test module, a fresh
upstream git repo, and work-queue isolation."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from .support import db_pool, ephemeral_postgres, init_upstream, truncate_work_queue

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    import asyncpg


@pytest.fixture(scope="module")
def postgres_dsn(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[str]:
    if shutil.which("initdb") is None:
        pytest.skip("postgresql not available")
    dbname = request.module.__name__.rsplit(".", 1)[-1].removeprefix("test_")
    with ephemeral_postgres(tmp_path_factory, dbname) as dsn:
        yield dsn


@pytest.fixture
async def pool(postgres_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    """asyncpg pool on the module's database, closed after the test."""
    async with db_pool(postgres_dsn) as pool:
        yield pool


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    return init_upstream(tmp_path / "upstream")


@pytest.fixture
def fresh_work_queue(postgres_dsn: str) -> None:
    """Per-test isolation for modules sharing one database."""
    truncate_work_queue(postgres_dsn)


@pytest.fixture(autouse=True)
def _no_effects_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub effects/schedule discovery, which would evaluate the flake
    with real nix. Tests that want effects patch these again."""

    async def no_effects(_ctx: object) -> list[str]:
        return []

    async def no_schedules(_ctx: object) -> dict:
        return {}

    monkeypatch.setattr("nixbot.effects_run.list_effects", no_effects)
    monkeypatch.setattr("nixbot.schedule_runner.discover_schedules", no_schedules)
