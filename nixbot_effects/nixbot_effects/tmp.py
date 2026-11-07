"""Private temporary directories for short-lived secrets."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextlib.contextmanager
def secret_tmpdir(prefix: str) -> Iterator[Path]:
    """A 0700 directory removed on exit. Lives in systemd's
    RuntimeDirectory when set: tmpfs, so secrets never hit disk.
    Only for small files."""
    parent = os.environ.get("RUNTIME_DIRECTORY", "").split(":")[0] or None
    with tempfile.TemporaryDirectory(prefix=prefix, dir=parent) as directory:
        yield Path(directory)
