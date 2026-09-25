"""ID token file for a remote build store."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .workload_identity import EffectIdentity, IdentityIssuer


def write_token(path: Path, token: str) -> None:
    # Renaming keeps the file whole for readers.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token.")
    with os.fdopen(fd, "w") as f:
        f.write(token)
    Path(tmp).replace(path)


@contextlib.asynccontextmanager
async def store_token_file(
    issuer: IdentityIssuer, identity: EffectIdentity, audience: str
) -> AsyncIterator[Path]:
    """A token file for `audience`, rewritten before expiry."""

    async def refresh(path: Path) -> None:
        while True:
            await asyncio.sleep(max(issuer.token_ttl * 2 / 3, 1))
            write_token(path, issuer.mint(identity, audience).token)

    with tempfile.TemporaryDirectory(prefix="nixbot-token-") as token_dir:
        path = Path(token_dir) / "token"
        write_token(path, issuer.mint(identity, audience).token)
        task = asyncio.create_task(refresh(path))
        try:
            yield path
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
