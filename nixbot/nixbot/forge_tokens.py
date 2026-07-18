"""Server-side storage for forge OAuth tokens.

The session cookie carries only an opaque session id: the cookie is
signed but not encrypted, and server-side storage lets logout
invalidate the token immediately.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx

from .auth import OAuthError
from .db_gen import tokens as q

if TYPE_CHECKING:
    import asyncpg

    from .auth import OAuthProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RefreshCredentials:
    """OAuth refresh token plus which login provider issued it."""

    refresh_token: str
    provider: str


class TokenVault(Protocol):
    async def save(
        self,
        session_id: str,
        token: str,
        lifetime: int,
        *,
        refresh: RefreshCredentials | None = None,
        refresh_lifetime: int | None = None,
    ) -> None: ...

    async def get(self, session_id: str) -> str | None: ...

    async def get_refresh(self, session_id: str) -> RefreshCredentials | None: ...

    async def delete(self, session_id: str) -> None: ...


class ForgeTokenStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def save(
        self,
        session_id: str,
        token: str,
        lifetime: int,
        *,
        refresh: RefreshCredentials | None = None,
        refresh_lifetime: int | None = None,
    ) -> None:
        await q.save_forge_token(
            self.pool,
            session_id=session_id,
            token=token,
            lifetime=float(lifetime),
            refresh_token=refresh.refresh_token if refresh else None,
            provider=refresh.provider if refresh else None,
            refresh_lifetime=float(refresh_lifetime)
            if refresh_lifetime is not None
            else None,
        )

    async def get(self, session_id: str) -> str | None:
        return await q.get_forge_token(self.pool, session_id=session_id)

    async def get_refresh(self, session_id: str) -> RefreshCredentials | None:
        row = await q.get_forge_refresh_token(self.pool, session_id=session_id)
        if row is None or row.refresh_token is None or row.provider is None:
            return None
        return RefreshCredentials(
            refresh_token=row.refresh_token, provider=row.provider
        )

    async def delete(self, session_id: str) -> None:
        await q.delete_forge_token(self.pool, session_id=session_id)


class ForgeTokenRefresher:
    """Hands out a usable forge access token for a session, renewing
    an expired one via the stored OAuth refresh token: Gitea/OIDC
    access tokens expire after ~1h while the session cookie lives for
    weeks."""

    def __init__(
        self,
        vault: TokenVault,
        providers: dict[str, OAuthProvider],
        http: httpx.AsyncClient,
        session_lifetime: int,
    ) -> None:
        self.vault = vault
        self.providers = providers
        self.http = http
        self.session_lifetime = session_lifetime
        # Serializes refreshes: providers rotate refresh tokens, so two
        # concurrent requests refreshing the same session would
        # invalidate each other's new refresh token.
        self._lock = asyncio.Lock()

    async def access_token(self, session_id: str) -> str | None:
        token = await self.vault.get(session_id)
        if token is not None:
            return token
        async with self._lock:
            # Another request may have refreshed while we waited.
            token = await self.vault.get(session_id)
            if token is not None:
                return token
            return await self._refresh(session_id)

    async def _refresh(self, session_id: str) -> str | None:
        credentials = await self.vault.get_refresh(session_id)
        if credentials is None:
            return None
        provider = self.providers.get(credentials.provider)
        if provider is None:
            return None
        try:
            token = await provider.refresh(self.http, credentials.refresh_token)
        except (OAuthError, httpx.HTTPError):
            # Revoked/expired refresh token or transient forge failure;
            # visibility falls back to public.
            logger.warning(
                "failed to refresh forge token",
                extra={"provider": credentials.provider},
            )
            return None
        lifetime = self.session_lifetime
        if token.expires_in is not None:
            lifetime = min(lifetime, token.expires_in)
        await self.vault.save(
            session_id,
            token.access_token,
            lifetime,
            # Providers may rotate the refresh token; keep the old one
            # if the response omitted a new one.
            refresh=RefreshCredentials(
                token.refresh_token or credentials.refresh_token,
                credentials.provider,
            ),
            refresh_lifetime=self.session_lifetime,
        )
        return token.access_token


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
