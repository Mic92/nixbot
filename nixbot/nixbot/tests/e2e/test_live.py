"""Live behavior: SSE-driven attribute refresh and log streaming.

These exercise the JavaScript in base.html/log.html against a real
browser. The httpx web tests can only assert the markers exist.
"""

from __future__ import annotations

import asyncio
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
    # Succeeded attributes are collapsed. Expand to lazy-load the rows.
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
    # The status event refreshes the page. The failed attribute moves
    # into the inline failure table.
    row.locator(".status-icon.failed").wait_for(timeout=15_000)
    assert "flipped by e2e test" in page.content()

    # The morph keeps the user-opened group open (morphIgnore: open)
    # and the morphed-in lazy placeholder refetches its rows.
    group = page.locator("details.attr-group[data-group=succeeded]")
    assert group.get_attribute("open") is not None
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

        # close() ends the SSE stream. Streamed content stays visible.
        server.run(finish())
        card = page.locator(".log-card", has_text="hello-2.12")
        assert "hello from the build" in card.inner_text()
    finally:
        server.registry.unregister(build_id, ATTR)


def test_log_page_keeps_up_with_a_line_burst(page: Page, server: TestServer) -> None:
    """Mic92/nixbot#98: thousands of lines must not cost a layout each."""
    build_id = server.run(_build_id(server, 3))
    writer = LogWriter(path=server.state_dir / "live" / f"{ATTR}-burst.zst")
    cap = StructuredCapture()
    writer.capture = cap
    server.registry.register(build_id, ATTR, writer)
    lines = 8000
    max_live_rows = 5000  # structured-logs.js MAX_LIVE_ROWS
    # Unbatched: one forced layout per line. Batched: one per frame.
    max_layouts = lines // 20
    cdp = page.context.new_cdp_session(page)
    cdp.send("Performance.enable")

    def layout_count() -> int:
        metrics = cdp.send("Performance.getMetrics")["metrics"]
        return next(int(m["value"]) for m in metrics if m["name"] == "LayoutCount")

    async def burst() -> None:
        cap.start_build(1, "/nix/store/aaa-linux-6.6.drv")
        for i in range(lines):
            cap.log_line(1, f"  CC      drivers/gpu/drm/obj_{i}.o")
            if i % 200 == 0:
                await asyncio.sleep(0)
        cap.log_line(1, "burst done")

    try:
        page.goto(f"/repos/github/acme/widget/builds/3/logs/{ATTR}")
        deadline = time.monotonic() + 15
        while not cap._subs:  # noqa: SLF001
            if time.monotonic() > deadline:
                msg = "browser never connected to the SSE stream"
                raise TimeoutError(msg)
            time.sleep(0.1)
        before = layout_count()
        server.run(burst())
        page.locator(".logline", has_text="burst done").wait_for(timeout=60_000)
        assert layout_count() - before < max_layouts
        rows = page.locator(".log-card .log-lines > .logline")
        assert rows.count() == max_live_rows
        assert "burst done" in rows.last.inner_text()
        # Scrolling up to the marker loads the trimmed lines back.
        marker = page.locator(".log-card .log-lines > .log-elided").first
        assert "obj_0.o" not in page.content()
        marker.scroll_into_view_if_needed()
        page.locator(".logline", has_text="obj_0.o").wait_for(timeout=15_000)
    finally:
        cdp.detach()
        cap.close()
        server.registry.unregister(build_id, ATTR)


def test_repeated_activity_keeps_one_card(page: Page, server: TestServer) -> None:
    """nix starts several activities for one derivation. Mic92/nixbot#168
    had a card per activity with the rows in only one of them."""
    build_id = server.run(_build_id(server, 3))
    writer = LogWriter(path=server.state_dir / "live" / f"{ATTR}-dup.zst")
    cap = StructuredCapture()
    writer.capture = cap
    server.registry.register(build_id, ATTR, writer)
    drv = "/nix/store/aaa-linux-config-7.1.10.drv"

    async def second_activity() -> None:
        cap.start_build(2, drv)
        cap.log_line(2, "from the second activity")

    try:
        cap.start_build(1, drv)
        cap.log_line(1, "from the first activity")
        page.goto(f"/repos/github/acme/widget/builds/3/logs/{ATTR}")
        cards = page.locator(".log-card", has_text="linux-config-7.1.10")
        cards.get_by_text("from the first activity").wait_for(timeout=15_000)
        server.run(second_activity())
        cards.get_by_text("from the second activity").wait_for(timeout=15_000)
        assert cards.count() == 1
    finally:
        cap.close()
        server.registry.unregister(build_id, ATTR)
