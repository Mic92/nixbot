"""The throwaway Postgres must start under deep temp directories."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING, cast

import pytest

from .support import ephemeral_postgres

if TYPE_CHECKING:
    from pathlib import Path


class DeepTempPaths:
    """Temp dirs nested far beyond the unix socket path limit (103 bytes
    on darwin, 107 on Linux), like pytest under a long build directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.joinpath(*["d" * 40] * 3)

    def mktemp(self, basename: str) -> Path:
        path = self.root / basename
        path.mkdir(parents=True)
        return path


@pytest.mark.skipif(shutil.which("initdb") is None, reason="postgresql not available")
def test_starts_under_deep_temp_dir(tmp_path: Path) -> None:
    factory = cast("pytest.TempPathFactory", DeepTempPaths(tmp_path))
    with ephemeral_postgres(factory, "deep") as dsn:
        assert dsn.startswith("postgresql://")
