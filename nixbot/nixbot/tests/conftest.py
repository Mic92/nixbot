"""Shared fixtures: one ephemeral Postgres per test module, a fresh
upstream git repo, and work-queue isolation."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from .support import (
    FakeEffects,
    db_pool,
    ephemeral_postgres,
    init_upstream,
    truncate_work_queue,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    import asyncpg


@pytest.fixture(scope="session")
def scratch_nix(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """A daemon-less Nix store, config and HOME per xdist worker. Workers
    sharing one store and cache crashed nix-eval-jobs with SIGBUS."""
    if shutil.which("nix-store") is None:
        pytest.skip("nix not available")
    root = tmp_path_factory.mktemp("nix")
    conf = root / "etc"
    conf.mkdir()
    (root / "home").mkdir()
    # The sandboxed build has no network, so no substituters either.
    (conf / "nix.conf").write_text(
        "experimental-features = nix-command flakes\nsandbox = false\nsubstituters =\n"
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("NIX_REMOTE", raising=False)
        mp.setenv("HOME", str(root / "home"))
        mp.setenv("NIX_STORE_DIR", str(root / "store"))
        mp.setenv("NIX_STATE_DIR", str(root / "var"))
        # Compiled in separately from NIX_STATE_DIR.
        mp.setenv("NIX_LOG_DIR", str(root / "var/log/nix"))
        mp.setenv("NIX_CONF_DIR", str(conf))
        subprocess.run(["nix-store", "--init"], check=True)
        yield


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
def _fake_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real discovery would evaluate the flake with nix."""
    monkeypatch.setattr("nixbot.orchestrator.default_effects", FakeEffects)
