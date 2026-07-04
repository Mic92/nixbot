"""RetryTransport: transient forge failures are retried, permanent
ones and rate limits are passed straight through."""

# ruff: noqa: PLR2004, ARG001 (status codes and unused handler args are fine in tests)

from __future__ import annotations

import httpx
import pytest

from nixbot.forge.retry import RetryTransport


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    # Tiny delays keep the test fast while still exercising the budget.
    transport = RetryTransport(handler, base_delay=0.0, max_cumulative_delay=1.0)
    return httpx.AsyncClient(transport=transport)


async def test_retries_transient_5xx_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200 if calls >= 3 else 503)

    async with _client(httpx.MockTransport(handler)) as http:
        response = await http.get("https://forge/x")
    assert response.status_code == 200
    assert calls == 3


async def test_retries_network_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        msg = "boom"
        nonlocal calls
        calls += 1
        if calls < 2:
            raise httpx.ConnectError(msg)
        return httpx.Response(200)

    async with _client(httpx.MockTransport(handler)) as http:
        response = await http.get("https://forge/x")
    assert response.status_code == 200
    assert calls == 2


async def test_retries_429_when_retry_after_fits_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200)

    async with _client(httpx.MockTransport(handler)) as http:
        response = await http.get("https://forge/x")
    assert response.status_code == 200
    assert calls == 2


async def test_gives_up_immediately_when_retry_after_exceeds_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # A long rate-limit hint (over the budget) is handed straight
        # back so the durable status-poster path can honor it instead.
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with _client(httpx.MockTransport(handler)) as http:
        response = await http.get("https://forge/x")
    assert response.status_code == 429
    assert calls == 1


async def test_does_not_retry_plain_client_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # No Retry-After hint: a 4xx is permanent, not retried.
        return httpx.Response(404)

    async with _client(httpx.MockTransport(handler)) as http:
        response = await http.get("https://forge/x")
    assert response.status_code == 404
    assert calls == 1


async def test_gives_up_after_budget_and_returns_last_response() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    transport = RetryTransport(
        httpx.MockTransport(handler), base_delay=0.5, max_cumulative_delay=1.0
    )
    async with httpx.AsyncClient(transport=transport) as http:
        response = await http.get("https://forge/x")
    # 0.5 + 1.0 exceeds the 1.0 budget on the second backoff, so two tries.
    assert response.status_code == 500
    assert calls == 2


async def test_reraises_when_network_errors_exhaust_budget() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        msg = "boom"
        raise httpx.ConnectError(msg)

    transport = RetryTransport(
        httpx.MockTransport(handler), base_delay=0.5, max_cumulative_delay=1.0
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(httpx.ConnectError):
            await http.get("https://forge/x")
