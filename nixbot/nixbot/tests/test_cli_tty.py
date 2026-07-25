"""nbo build watch TTY renderer against a pyte terminal emulator."""

# ruff: noqa: PLR2004 (literal values in test assertions are fine)
from __future__ import annotations

import io
import time

import httpx
import pyte
from nixbot_cli.api import NixbotClient, RepoRef
from nixbot_cli.watch_tty import Display, TtyWatch

WIDTH, HEIGHT = 60, 12


def emulate(chunks: str) -> pyte.HistoryScreen:
    screen = pyte.HistoryScreen(WIDTH, HEIGHT, history=1000)
    pyte.Stream(screen).feed(chunks)
    return screen


def buffer_lines(screen: pyte.HistoryScreen) -> list[str]:
    """Scrollback plus visible rows, as plain text."""
    history = [
        "".join(line[x].data for x in range(WIDTH)).rstrip()
        for line in screen.history.top
    ]
    return history + [row.rstrip() for row in screen.display]


def make_display(out: io.StringIO) -> Display:
    return Display(out, size=lambda: (WIDTH, HEIGHT))


def test_verdicts_scroll_and_region_stays_at_bottom() -> None:
    """Verdicts land in scrollback exactly once and in order, while the
    live region only ever exists as the last rows of the screen."""
    out = io.StringIO()
    display = make_display(out)
    verdicts = []
    for i in range(40):
        batch = [f"verdict-{i}-a", f"verdict-{i}-b"]
        verdicts += batch
        region = ["HEADER", f"running-{i}", f"gist-{i}"]
        display.frame(batch, region)
    display.close()

    lines = buffer_lines(emulate(out.getvalue()))
    kept = [line for line in lines if line.startswith("verdict-")]
    assert kept == verdicts
    assert sum("HEADER" in line for line in lines) == 0  # cleared on close


def test_region_visible_while_running() -> None:
    out = io.StringIO()
    display = make_display(out)
    for i in range(20):
        display.frame([f"verdict-{i}"], ["HEADER", f"running-{i}"])

    screen = emulate(out.getvalue())
    lines = buffer_lines(screen)
    # The region exists exactly once, at the bottom of the visible screen.
    assert sum("HEADER" in line for line in lines) == 1
    visible = [row.rstrip() for row in screen.display]
    assert "HEADER" in visible[-2]
    assert visible[-1] == "running-19"
    # No verdict was lost or duplicated by the region redraws.
    kept = [line for line in lines if line.startswith("verdict-")]
    assert kept == [f"verdict-{i}" for i in range(20)]


def test_region_growth_does_not_eat_verdicts() -> None:
    """Reserving more bottom rows must not overwrite existing verdicts."""
    out = io.StringIO()
    display = make_display(out)
    display.frame(["verdict-0"], ["HEADER"])
    display.frame(["verdict-1"], ["HEADER", "run-a", "run-b", "run-c"])
    display.frame([], ["HEADER"])
    display.close()

    lines = buffer_lines(emulate(out.getvalue()))
    kept = [line for line in lines if line.startswith("verdict-")]
    assert kept == ["verdict-0", "verdict-1"]
    assert sum("run-a" in line for line in lines) == 0


def test_region_starts_at_the_prompt_row_without_a_gap() -> None:
    """Output continues right below the shell prompt instead of jumping
    to the bottom of the screen."""
    out = io.StringIO()
    display = Display(out, size=lambda: (WIDTH, HEIGHT), origin=4)
    display.frame(["verdict-0", "verdict-1"], ["HEADER", "running"])

    visible = [row.rstrip() for row in emulate(out.getvalue()).display]
    assert visible[3:7] == ["verdict-0", "verdict-1", "HEADER", "running"]
    assert all(not row for row in visible[7:])


def test_resize_keeps_single_region() -> None:
    """A SIGWINCH shows up as a changed size() result on the next frame.
    The renderer must re-anchor the region without duplicating it."""
    size = {"cols": WIDTH, "rows": HEIGHT}
    out = io.StringIO()
    display = Display(out, size=lambda: (size["cols"], size["rows"]))
    screen = pyte.HistoryScreen(WIDTH, HEIGHT, history=1000)
    stream = pyte.Stream(screen)

    def frame(batch: list[str], region: list[str]) -> None:
        mark = out.tell()
        display.frame(batch, region)
        stream.feed(out.getvalue()[mark:])

    for i in range(10):
        frame([f"verdict-{i}"], ["HEADER", f"running-{i}"])
    # The terminal shrinks between two frames.
    size.update(cols=40, rows=8)
    screen.resize(8, 40)
    for i in range(10, 16):
        frame([f"verdict-{i}"], ["HEADER", f"running-{i}"])

    lines = buffer_lines(screen)
    assert sum("HEADER" in line for line in lines) == 1
    visible = [row.rstrip() for row in screen.display]
    assert "HEADER" in visible[-2]
    assert visible[-1] == "running-15"
    # Verdicts printed after the resize are neither lost nor duplicated.
    post = [
        line for line in lines if line.startswith("verdict-1") and line != "verdict-1"
    ]
    assert post == [f"verdict-{i}" for i in range(10, 16)]


def test_tty_watch_shows_finished_and_running_attrs() -> None:
    """End to end against a mocked API: verdicts of already-finished
    attributes appear, running attributes show up in the live region and
    the run ends with the build's final status."""
    build = {"id": 9, "number": 5, "status": "building"}
    finished = [
        {
            "id": 1,
            "attr": "good",
            "status": "succeeded",
            "cached": True,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
        },
    ]
    detail_attrs = [
        *finished,
        {
            "id": 2,
            "attr": "slowone",
            "status": "building",
            "cached": False,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
        },
    ]
    hint = 'data: {"build_id":9,"attr":"slowone","status":"succeeded"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/builds/5"):
            return httpx.Response(
                200, json={"build": dict(build), "attributes": detail_attrs}
            )
        if path.endswith("/builds/5/attrs"):
            after = request.url.params.get("finished_after")
            items = [a for a in finished if not after or a["finished_at"] > after]
            return httpx.Response(200, json={"build": dict(build), "items": items})
        if path == "/api/events":
            # Give the renderer time to draw a frame with the running attr.
            time.sleep(0.6)
            finished.append(
                {
                    "id": 2,
                    "attr": "slowone",
                    "status": "succeeded",
                    "cached": False,
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:02Z",
                }
            )
            build["status"] = "succeeded"
            return httpx.Response(
                200, content=hint, headers={"content-type": "text/event-stream"}
            )
        if path.endswith("/logs/slowone/stream"):
            return httpx.Response(
                200,
                content='event: line\ndata: {"idx":0,"text":"compiling"}\n\n'
                "event: done\ndata: {}\n\n",
                headers={"content-type": "text/event-stream"},
            )
        raise AssertionError(path)

    client = NixbotClient(
        http=httpx.Client(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
    )
    out = io.StringIO()
    watcher = TtyWatch(
        client,
        RepoRef("github", "acme", "widget"),
        5,
        out,
        size=lambda: (WIDTH, HEIGHT),
    )
    status = watcher.run()

    assert status == "succeeded"
    assert watcher.finished == 2
    lines = buffer_lines(emulate(out.getvalue()))
    assert sum(line.startswith("✓ cached good") for line in lines) == 1
    assert sum(line.startswith("✓ built slowone") for line in lines) == 1
    # The live region showed the running attribute and its header at least once.
    assert "slowone" in out.getvalue()
    assert "build #5" in out.getvalue()
    # The region is gone after the run.
    assert not any("build #5" in line for line in lines)


def test_long_verdicts_wrap_without_corrupting_region() -> None:
    out = io.StringIO()
    display = make_display(out)
    long_line = "verdict-long " + "x" * (2 * WIDTH)
    for _ in range(5):
        display.frame([long_line], ["HEADER", "running"])

    lines = buffer_lines(emulate(out.getvalue()))
    assert sum("HEADER" in line for line in lines) == 1
    assert sum(line.startswith("verdict-long") for line in lines) == 5
