"""Live behavior: SSE-driven attribute refresh and log streaming.

These exercise the JavaScript in base.html/log.html against a real
browser; the httpx web tests can only assert the markers exist.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from nixbot.executor import LogWriter, StructuredCapture

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .support import TestServer

ATTR = "x86_64-linux.ok"


async def _build_id(server: TestServer, number: int) -> int:
    return await server.pool.fetchval("SELECT id FROM builds WHERE number = $1", number)


def test_attribute_table_refreshes_while_building(
    page: Page, server: TestServer
) -> None:
    page.goto("/repos/github/acme/widget/builds/3")
    # Succeeded attributes are collapsed; expand to lazy-load the rows.
    page.get_by_text("succeeded", exact=False).first.click()
    row = page.locator('tr[data-attr="aarch64-linux.other"]')
    row.locator(".status-icon.succeeded").wait_for()

    async def fail_attribute() -> None:
        build_id = await _build_id(server, 3)
        await server.pool.execute(
            """
            UPDATE build_attributes SET status = 'failed',
                   error = 'error: flipped by e2e test'
            WHERE build_id = $1 AND attr = 'aarch64-linux.other'
            """,
            build_id,
        )

    server.run(fail_attribute())
    # The status event refreshes the page; the failed attribute moves
    # into the inline failure table.
    row.locator(".status-icon.failed").wait_for(timeout=15_000)
    assert "flipped by e2e test" in page.content()

    # Groups must stay expandable after a morph: the morphed-in lazy
    # placeholder needs htmx processing or it never fetches.
    group = page.locator("details.attr-group[data-group=succeeded]")
    if group.get_attribute("open") is None:
        group.locator("summary").click()
    page.locator(
        'details[data-group=succeeded] tr[data-attr="x86_64-linux.ok"]'
    ).wait_for(timeout=15_000)


def test_log_page_streams_live_output(page: Page, server: TestServer) -> None:
    build_id = server.run(_build_id(server, 3))
    writer = LogWriter(path=server.state_dir / "live" / f"{ATTR}.zst")
    cap = StructuredCapture()
    writer.capture = cap
    server.registry.register(build_id, ATTR, writer)
    drv = "/nix/store/aaa-hello-2.12.drv"

    started = False

    async def emit(text: str) -> None:
        nonlocal started
        if not started:
            cap.start_build(1, drv)
            started = True
        cap.log_line(1, text)

    async def finish() -> None:
        cap.close()

    try:
        page.goto(f"/repos/github/acme/widget/builds/3/logs/{ATTR}")

        # Structured live has no history replay: deltas sent before the
        # EventSource subscribes are lost, so wait for the subscription.
        deadline = time.monotonic() + 15
        while not cap._subs:  # noqa: SLF001
            if time.monotonic() > deadline:
                msg = "browser never connected to the SSE stream"
                raise TimeoutError(msg)
            time.sleep(0.1)

        lines = page.locator(".log-card .log-lines .logline")
        server.run(emit("hello from the build"))
        lines.get_by_text("hello from the build").wait_for(timeout=15_000)

        server.run(emit("second line arrives later"))
        lines.get_by_text("second line arrives later").wait_for(timeout=15_000)

        # close() ends the SSE stream; streamed content stays visible.
        server.run(finish())
        card = page.locator(".log-card", has_text="hello-2.12")
        assert "hello from the build" in card.inner_text()
    finally:
        server.registry.unregister(build_id, ATTR)
