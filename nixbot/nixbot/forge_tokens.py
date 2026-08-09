"""Server-side session revocation.

The session cookie carries only an opaque session id: the cookie is
signed but not encrypted, and server-side state lets logout invalidate
the session immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .db_gen import tokens as q

if TYPE_CHECKING:
    import asyncpg


class SessionRevocations(Protocol):
    async def revoke(self, session_id: str, lifetime: int) -> None: ...

    async def is_revoked(self, session_id: str) -> bool: ...


class RevokedSessionStore:
    """Logout denylist for the stateless session cookies: the cookie
    stays validly signed until expiry, so revocation must be recorded
    server-side and checked on every authenticated request."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def revoke(self, session_id: str, lifetime: int) -> None:
        # Lazy pruning: rows are only needed until the cookie itself
        # would have expired.
        await q.revoke_session(
            self.pool, session_id=session_id, lifetime=float(lifetime)
        )

    async def is_revoked(self, session_id: str) -> bool:
        return bool(await q.session_revoked(self.pool, session_id=session_id))
