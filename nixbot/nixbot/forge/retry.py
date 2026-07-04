"""Transport-level retry for forge HTTP clients.

Wrapping the transport means every forge call (single GETs, paginated
listings, token minting) inherits retries without changing call sites.
Network errors and transient 5xx are retried with exponential backoff;
a Retry-After hint is always honored. Waits are capped by a cumulative
budget: longer rate-limit hints are handed back to the caller so the
durable status-poster path can pace them instead of blocking here.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from email.utils import parsedate_to_datetime

import httpx

_RETRYABLE_STATUS = frozenset(range(500, 600))


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Delay a forge asks us to wait before retrying, from Retry-After
    (seconds or HTTP-date) or a GitHub rate-limit reset epoch."""
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            seconds = float(value)
            # float() accepts nan/inf; nan would poison every later
            # min/max comparison down to asyncio.sleep.
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            with contextlib.suppress(TypeError, ValueError):
                dt = parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
    # GitHub primary rate limits: no Retry-After, only a reset epoch.
    if response.headers.get("X-RateLimit-Remaining") == "0":
        with contextlib.suppress(TypeError, ValueError):
            reset = int(response.headers["X-RateLimit-Reset"])
            return max(0.0, reset - time.time())
    return None


class RetryTransport(httpx.AsyncBaseTransport):
    """Exponential backoff capped by a cumulative delay budget, always
    honoring a forge's Retry-After hint when present."""

    def __init__(
        self,
        wrapped: httpx.AsyncBaseTransport,
        *,
        base_delay: float = 0.1,
        max_cumulative_delay: float = 30.0,
    ) -> None:
        self._wrapped = wrapped
        self._base_delay = base_delay
        self._max_cumulative_delay = max_cumulative_delay

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        slept = 0.0
        attempt = 0
        while True:
            backoff = self._base_delay * 2**attempt
            try:
                response = await self._wrapped.handle_async_request(request)
            except httpx.TransportError:
                delay = backoff
                if slept + delay > self._max_cumulative_delay:
                    raise
            else:
                # A Retry-After hint (on 429 or 503) always wins over the
                # backoff schedule; otherwise retry only transient 5xx.
                hint = retry_after_seconds(response)
                delay = max(backoff, hint or 0.0)
                retryable = (
                    hint is not None or response.status_code in _RETRYABLE_STATUS
                )
                if not retryable or slept + delay > self._max_cumulative_delay:
                    return response
                # Discard the body so the connection can be reused.
                await response.aread()
                await response.aclose()
            await asyncio.sleep(delay)
            slept += delay
            attempt += 1

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def make_client() -> httpx.AsyncClient:
    """An httpx client that retries transient forge failures."""
    return httpx.AsyncClient(transport=RetryTransport(httpx.AsyncHTTPTransport()))
