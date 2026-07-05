"""Tests for the attribute build executor: fair queue, log capture,
build subprocess handling (with a fake `nix` on PATH)."""

# ruff: noqa: PLR2004, ARG001 (test literals; fixtures used for side effects)

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import threading
import time
from typing import TYPE_CHECKING

import pytest

import nixbot.executor as executor_mod
from nixbot.ansi import strip_ansi
from nixbot.build_scheduler import BuildOutcome
from nixbot.executor import (
    FRAME_FLUSH_THRESHOLD,
    STRUCTURED_QUEUE_MAXSIZE,
    SUBSCRIBER_QUEUE_MAXSIZE,
    BuildSettings,
    FairScheduler,
    LogWriter,
    NixBuildExecutor,
    StructuredCapture,
    build_nix_command,
    failure_excerpt,
    is_transient_error,
    iter_lines,
    read_log,
    render_log_event,
)
from nixbot.logstore import LogContainerReader

from .support import mk_job

if TYPE_CHECKING:
    from pathlib import Path


# --- FairScheduler ---------------------------------------------------------


async def test_fair_round_robin_across_builds() -> None:
    queue = FairScheduler(1)
    order: list[str] = []
    await queue.acquire("seed")  # occupy the only slot

    async def worker(name: str, key: str) -> None:
        await queue.acquire(key)
        order.append(name)
        queue.release()

    # Build A enqueues three attrs first, build B two: fairness must
    # interleave instead of draining A first; FIFO within a build.
    tasks = [
        asyncio.create_task(worker("a1", "A")),
        asyncio.create_task(worker("a2", "A")),
        asyncio.create_task(worker("a3", "A")),
        asyncio.create_task(worker("b1", "B")),
        asyncio.create_task(worker("b2", "B")),
    ]
    await asyncio.sleep(0)  # let all enqueue
    queue.release()  # free the seed slot
    await asyncio.gather(*tasks)
    assert order == ["a1", "b1", "a2", "b2", "a3"]


async def test_fair_scheduler_capacity() -> None:
    queue = FairScheduler(2)
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        await queue.acquire("k")
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        queue.release()

    await asyncio.gather(*[worker() for _ in range(6)])

    assert peak == 2


async def test_fair_scheduler_cancelled_waiter() -> None:
    queue = FairScheduler(1)
    await queue.acquire("A")
    waiter = asyncio.create_task(queue.acquire("B"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    queue.release()
    # Slot must be available again.
    await asyncio.wait_for(queue.acquire("C"), timeout=1)


# --- LogWriter --------------------------------------------------------------


async def test_log_writer_roundtrip(tmp_path: Path) -> None:
    writer = LogWriter(path=tmp_path / "log.zst")
    await writer.write(b"hello ")
    await writer.write(b"world\n")
    await writer.close()
    assert read_log(tmp_path / "log.zst") == b"hello world\n"


async def test_log_writer_fan_out(tmp_path: Path) -> None:
    writer = LogWriter(path=tmp_path / "log.zst")
    sub1 = writer.subscribe()
    sub2 = writer.subscribe()
    await writer.write(b"line1\n")
    await writer.close()
    chunks = []
    while (chunk := await sub1.get()) is not None:
        chunks.append(chunk)
    assert await sub2.get() == b"line1\n"
    assert await sub2.get() is None

    assert chunks == [b"line1\n"]


async def test_log_writer_subscribe_with_history(tmp_path: Path) -> None:
    writer = LogWriter(path=tmp_path / "log.zst")
    await writer.write(b"early\n")
    history, queue = await writer.subscribe_with_history()
    assert history == b"early\n"
    await writer.write(b"late\n")
    assert await queue.get() == b"late\n"
    await writer.close()
    assert await queue.get() is None
    # Subscribing after close must terminate, not hang.
    history, queue = await writer.subscribe_with_history()
    assert history == b"early\nlate\n"
    assert await queue.get() is None


async def test_log_writer_no_chunk_lost_during_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chunk written while subscribe_with_history reads the on-disk
    history must arrive exactly once (in the history or the queue)."""
    started = threading.Event()
    release = threading.Event()
    orig_read_log = executor_mod.read_log

    def slow_read(path: Path) -> bytes:
        started.set()
        release.wait(5)
        return orig_read_log(path)

    monkeypatch.setattr(executor_mod, "read_log", slow_read)

    writer = LogWriter(path=tmp_path / "log.zst")
    await writer.write(b"early\n")
    task = asyncio.create_task(writer.subscribe_with_history())
    while (  # noqa: ASYNC110 — polling a threading.Event is the point
        not started.is_set()
    ):
        await asyncio.sleep(0.01)
    await writer.write(b"during\n")
    release.set()
    history, queue = await task
    await writer.close()
    chunks = []
    while (chunk := await queue.get()) is not None:
        chunks.append(chunk)
    combined = history + b"".join(chunks)
    assert combined == b"early\nduring\n"


async def test_log_writer_concurrent_flush_keeps_frame_order(tmp_path: Path) -> None:
    """A snapshot racing a threshold flush must not write zstd frames
    out of order (or share the compressor across threads)."""

    writer = LogWriter(path=tmp_path / "log.zst")
    orig_compress = writer._compressor.compress  # noqa: SLF001

    class SlowCompressor:
        @staticmethod
        def compress(data: bytes) -> bytes:
            time.sleep(0.05)
            return orig_compress(data)

    writer._compressor = SlowCompressor()  # type: ignore[assignment] # noqa: SLF001
    big = b"A" * FRAME_FLUSH_THRESHOLD
    flush_task = asyncio.create_task(writer.write(big))
    await asyncio.sleep(0.01)  # flush is inside the slow compress
    await writer.write(b"tail\n")
    snapshot = await writer.snapshot()
    await flush_task
    await writer.close()
    big = b"A" * FRAME_FLUSH_THRESHOLD
    assert snapshot == big + b"tail\n"
    assert read_log(tmp_path / "log.zst") == big + b"tail\n"


async def test_log_writer_truncation_keeps_head_and_tail(tmp_path: Path) -> None:
    writer = LogWriter(path=tmp_path / "log.zst", size_limit=1000)
    await writer.write(b"H" * 600)  # head budget is 500
    for i in range(100):
        await writer.write(f"tail-{i:03d}\n".encode())
    await writer.close()
    content = read_log(tmp_path / "log.zst")
    assert writer.truncated
    assert content.startswith(b"H" * 500)
    assert b"log truncated" in content
    assert b"tail-099" in content  # newest tail kept
    assert b"tail-000" not in content  # oldest tail dropped
    # Stored content respects the cap (plus the marker line).
    assert len(content) < 1200


# --- command assembly & transient detection ---------------------------------


def test_build_nix_command(tmp_path: Path) -> None:
    settings = BuildSettings(log_dir=tmp_path, max_silent_time=77, show_trace=True)
    cmd = build_nix_command(mk_job(), settings, tmp_path / "result-foo")
    assert cmd[:4] == ["nix", "build", "--log-format", "internal-json"]
    assert "--show-trace" in cmd
    assert cmd[cmd.index("--max-silent-time") + 1] == "77"
    assert cmd[-1] == "/nix/store/foo.drv^*"
    assert cmd[cmd.index("--out-link") + 1] == str(tmp_path / "result-foo")
    # Hosts without flakes in nix.conf must still build.
    idx = cmd.index("extra-experimental-features")
    assert cmd[idx - 1] == "--option"
    assert cmd[idx + 1] == "nix-command flakes"


def test_is_transient_error() -> None:
    assert is_transient_error("error: unable to download 'https://x': 500")
    assert is_transient_error("Connection reset by peer")
    assert not is_transient_error("error: builder failed with exit code 1")


# --- NixBuildExecutor with fake nix -----------------------------------------


@pytest.fixture
def fake_nix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `nix` on PATH controlled by environment-ish marker files."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "nix"
    script.write_text(
        f"""#!/bin/sh
control={tmp_path}/control
echo "building $@"
case "$(cat "$control" 2>/dev/null)" in
  fail) echo "error: builder failed with exit code 1"; exit 1 ;;
  transient-once)
    echo transient > "$control"
    echo "error: unable to download 'https://cache': Connection reset by peer"
    exit 1 ;;
  transient) echo ok; exit 0 ;;
  hang) sleep 60 ;;
  hangpid) echo $$ > {tmp_path}/pid; sleep 60 ;;
  racepid) echo $$ > {tmp_path}/pid; echo ok ;;
  *) echo ok; exit 0 ;;
esac
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    return tmp_path / "control"


async def run_build(
    tmp_path: Path,
    settings: BuildSettings | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[BuildOutcome, bytes]:
    executor = NixBuildExecutor(
        FairScheduler(2), settings or BuildSettings(log_dir=tmp_path)
    )
    writer = LogWriter(path=tmp_path / "log.zst")
    outcome = await executor.build_attribute(
        "build-1", mk_job(), writer, tmp_path, cancel_event
    )
    await writer.close()
    return outcome, read_log(tmp_path / "log.zst")


async def test_executor_success(tmp_path: Path, fake_nix: Path) -> None:
    outcome, log = await run_build(tmp_path)
    assert outcome == BuildOutcome.success
    assert b"building" in log


async def test_executor_failure(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("fail")
    outcome, log = await run_build(tmp_path)
    assert outcome == BuildOutcome.failure
    assert b"builder failed" in log


async def test_executor_transient_retry_succeeds(
    tmp_path: Path, fake_nix: Path
) -> None:
    fake_nix.write_text("transient-once")
    outcome, log = await run_build(tmp_path)
    assert outcome == BuildOutcome.success
    assert b"retrying once" in log


async def test_executor_timeout(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("hang")
    outcome, log = await run_build(tmp_path, BuildSettings(log_dir=tmp_path, timeout=1))
    assert outcome == BuildOutcome.failure
    assert b"timed out" in log


async def test_executor_cancel(tmp_path: Path, fake_nix: Path) -> None:
    fake_nix.write_text("hang")

    executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
    writer = LogWriter(path=tmp_path / "log.zst")
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        executor.build_attribute("b", mk_job(), writer, tmp_path, cancel_event)
    )
    await asyncio.sleep(0.2)
    cancel_event.set()
    outcome = await asyncio.wait_for(task, timeout=5)
    await writer.close()

    assert outcome == BuildOutcome.cancelled


async def test_executor_hard_cancel_kills_process_group(
    tmp_path: Path, fake_nix: Path
) -> None:
    """Cancelling the build task itself (not via the cancel event) must
    not leak the running nix process group.

    Group-kill semantics themselves are covered by
    test_proc.test_reap_kills_whole_group; this checks the executor's
    hard-cancel wiring (the finally path that also stops the log pump).
    """
    fake_nix.write_text("hangpid")
    pidfile = tmp_path / "pid"

    executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
    writer = LogWriter(path=tmp_path / "log.zst")
    task = asyncio.create_task(
        executor.build_attribute("b", mk_job(), writer, tmp_path)
    )
    while (  # noqa: ASYNC110 — polling an external file is the point
        not pidfile.exists() or not pidfile.read_text().strip()
    ):
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    await writer.close()
    pid = int(pidfile.read_text())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(pid, 9)  # do not leak it past the test
        pytest.fail("nix process leaked after hard cancel")


async def test_executor_cancel_suppresses_retry(tmp_path: Path, fake_nix: Path) -> None:
    # Cancel before start: no retry, no build.
    fake_nix.write_text("transient-once")
    cancel_event = asyncio.Event()
    cancel_event.set()
    outcome, log = await run_build(tmp_path, cancel_event=cancel_event)
    assert outcome == BuildOutcome.cancelled
    assert b"retrying" not in log


async def test_log_writer_not_truncated_between_head_and_limit(tmp_path: Path) -> None:
    # Total output fits the cap (head budget + tail budget): nothing is
    # dropped, so the log must not be reported or marked as truncated.
    writer = LogWriter(path=tmp_path / "log.zst", size_limit=1000)
    await writer.write(b"H" * 600)  # head budget is 500
    await writer.close()
    content = read_log(tmp_path / "log.zst")
    assert not writer.truncated
    assert b"log truncated" not in content
    assert content == b"H" * 600


async def test_log_writer_subscriber_queue_bounded(tmp_path: Path) -> None:
    writer = LogWriter(path=tmp_path / "log.zst")
    queue = writer.subscribe()
    # A stalled client: nothing consumes while the build streams.
    for i in range(SUBSCRIBER_QUEUE_MAXSIZE + 100):
        await writer.write(f"chunk-{i}\n".encode())
    size = queue.qsize()
    await writer.close()
    # Drain: the newest data and the close sentinel must be present.
    last = None
    while (chunk := await queue.get()) is not None:
        last = chunk
    assert size <= SUBSCRIBER_QUEUE_MAXSIZE
    assert last == f"chunk-{SUBSCRIBER_QUEUE_MAXSIZE + 99}\n".encode()


async def test_log_writer_batches_frames(tmp_path: Path) -> None:
    # Many tiny writes must not produce one zstd frame each (frame
    # overhead would make the "compressed" log larger than plaintext).
    writer = LogWriter(path=tmp_path / "log.zst")
    for i in range(10000):
        await writer.write(f"line {i}\n".encode())
    await writer.close()
    raw = (tmp_path / "log.zst").stat().st_size
    plain = read_log(tmp_path / "log.zst")
    assert plain == b"".join(f"line {i}\n".encode() for i in range(10000))
    assert raw < len(plain)


async def test_executor_sanitizes_out_link(tmp_path: Path, fake_nix: Path) -> None:
    # Repository-controlled attribute names must not traverse out of
    # the worktree via --out-link.
    executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
    writer = LogWriter(path=tmp_path / "log.zst")
    outcome = await executor.build_attribute(
        "b", mk_job('checks."../../evil"'), writer, tmp_path
    )
    assert outcome == BuildOutcome.success
    await writer.close()
    log = read_log(tmp_path / "log.zst")
    assert b"result-.." not in log
    assert b"--out-link " + str(tmp_path).encode() + b"/result-checks." in log


async def test_executor_cancel_while_queued(tmp_path: Path, fake_nix: Path) -> None:
    # A queued build whose cancel event fires must not wait for a slot.
    fake_nix.write_text("hang")

    queue = FairScheduler(1)
    executor = NixBuildExecutor(queue, BuildSettings(log_dir=tmp_path))
    blocker_writer = LogWriter(path=tmp_path / "blocker.zst")
    blocker = asyncio.create_task(
        executor.build_attribute("a", mk_job("blocker"), blocker_writer, tmp_path)
    )
    await asyncio.sleep(0.2)  # blocker holds the only slot
    cancel_event = asyncio.Event()
    writer = LogWriter(path=tmp_path / "log.zst")
    queued = asyncio.create_task(
        executor.build_attribute("b", mk_job(), writer, tmp_path, cancel_event)
    )
    await asyncio.sleep(0.1)
    cancel_event.set()
    outcome = await asyncio.wait_for(queued, timeout=2)
    blocker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await blocker

    assert outcome == BuildOutcome.cancelled


async def test_executor_finished_build_wins_cancel_race(
    tmp_path: Path, fake_nix: Path
) -> None:
    # The process exits successfully and the cancel event fires before
    # the executor observes the exit: the completed build must be
    # recorded as success, not cancelled.
    fake_nix.write_text("racepid")
    pidfile = tmp_path / "pid"

    executor = NixBuildExecutor(FairScheduler(2), BuildSettings(log_dir=tmp_path))
    writer = LogWriter(path=tmp_path / "log.zst")
    cancel_event = asyncio.Event()

    async def cancel_after_exit() -> None:
        while (  # noqa: ASYNC110 — polling an external file is the point
            not pidfile.exists() or not pidfile.read_text().strip()
        ):
            await asyncio.sleep(0.005)
        pid = int(pidfile.read_text())
        # Wait until the child has been reaped (exit observed by the
        # event loop), then request cancellation. Polling is the only
        # way to observe another process's exit from outside.
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.005)
        cancel_event.set()

    canceller = asyncio.create_task(cancel_after_exit())
    outcome = await executor.build_attribute(
        "b", mk_job(), writer, tmp_path, cancel_event
    )
    canceller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await canceller
    await writer.close()

    assert outcome == BuildOutcome.success


async def test_executor_task_cancelled_while_queued_releases_waiter(
    tmp_path: Path, fake_nix: Path
) -> None:
    # Cancelling the coroutine itself while it waits for a slot must
    # withdraw the waiter: a later grant must not leak a slot.
    fake_nix.write_text("hang")

    queue = FairScheduler(1)
    executor = NixBuildExecutor(queue, BuildSettings(log_dir=tmp_path))
    await queue.acquire("seed")  # occupy the only slot
    writer = LogWriter(path=tmp_path / "log.zst")
    queued = asyncio.create_task(
        executor.build_attribute("b", mk_job(), writer, tmp_path)
    )
    await asyncio.sleep(0.05)  # let it enqueue
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    queue.release()
    # The slot must be available again, not granted to the dead waiter.
    await asyncio.wait_for(queue.acquire("c"), timeout=1)


def test_render_log_event_attributes_and_colors() -> None:
    activities: dict[int, str] = {}
    start = (
        b'@nix {"action":"start","id":7,"type":105,'
        b'"text":"building \'/nix/store/abc123-hello-2.12.drv\'",'
        b'"fields":["/nix/store/abc123-hello-2.12.drv"]}'
    )
    assert render_log_event(start, activities) == (
        b"building '/nix/store/abc123-hello-2.12.drv'\n"
    )
    log_line = (
        b'@nix {"action":"result","id":7,"type":101,'
        b'"fields":["checking for gcc... yes"]}'
    )
    assert render_log_event(log_line, activities) == (
        b"hello-2.12> checking for gcc... yes\n"
    )
    # nix's own messages keep their ANSI colors.
    msg = b'@nix {"action":"msg","level":0,"msg":"\\u001b[31;1merror:\\u001b[0m boom"}'
    assert render_log_event(msg, activities) == b"\x1b[31;1merror:\x1b[0m boom\n"
    # Progress events produce no log output.
    assert render_log_event(b'@nix {"action":"stop","id":7}', activities) is None
    # Non-event output passes through.
    assert render_log_event(b"plain line\n", activities) == b"plain line\n"


def test_structured_capture_demux() -> None:
    clock = iter(range(1000, 2000))
    cap = StructuredCapture(clock=lambda: next(clock))
    activities: dict[int, str] = {}

    def feed(line: bytes) -> None:
        render_log_event(line, activities, cap)

    feed(
        b'@nix {"action":"start","id":1,"type":105,'
        b'"fields":["/nix/store/aaa-qtbase-5.0.drv"]}'
    )
    feed(
        b'@nix {"action":"start","id":2,"type":105,'
        b'"fields":["/nix/store/bbb-zlib-1.3.drv"]}'
    )
    feed(b'@nix {"action":"result","id":1,"type":104,"fields":["unpack"]}')
    feed(b'@nix {"action":"result","id":1,"type":101,"fields":["unpacking qt"]}')
    feed(b'@nix {"action":"result","id":2,"type":101,"fields":["building zlib"]}')
    feed(b'@nix {"action":"result","id":1,"type":104,"fields":["build"]}')
    feed(b'@nix {"action":"result","id":1,"type":101,"fields":["CC main.o"]}')
    feed(b'@nix {"action":"msg","level":0,"msg":"note: keeping going"}')
    feed(b'@nix {"action":"stop","id":1}')
    cap.set_status("/nix/store/aaa-qtbase-5.0.drv", "failed")

    r = LogContainerReader(cap.finalize())
    by_name = {r.entry(i)["name"]: i for i in range(len(r))}
    qt = by_name["qtbase-5.0"]
    assert r.lines(qt) == ["unpacking qt", "CC main.o"]
    assert r.entry(qt)["ph"] == [["unpack", 0], ["build", 1]]
    assert r.entry(qt)["status"] == "failed"
    assert r.lines(by_name["zlib-1.3"]) == ["building zlib"]
    # nix's own message lands in the synthetic setup bucket.
    assert r.lines(by_name["setup"]) == ["note: keeping going"]


async def test_structured_capture_live_stream() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    q = cap.subscribe()  # subscribe before mutating: no delta lost
    cap.start_build(1, "/nix/store/aaa-qtbase-5.0.drv")
    cap.phase(1, "build")
    cap.log_line(1, "CC main.o")
    cap.stop(1)
    cap.set_status("/nix/store/aaa-qtbase-5.0.drv", "failed")
    cap.close()

    deltas = []
    while not q.empty():
        deltas.append(q.get_nowait())

    assert deltas[0] == {"t": "drv", "idx": 1, "name": "qtbase-5.0"}
    assert {"t": "phase", "idx": 1, "phase": "build", "line": 0} in deltas
    assert {"t": "line", "idx": 1, "from": 1, "text": "CC main.o"} in deltas
    # stop marks built, finalize status overrides to failed; both stream.
    assert {"t": "status", "idx": 1, "status": "built"} in deltas
    assert {"t": "status", "idx": 1, "status": "failed"} in deltas
    assert deltas[-1] is None  # close signals done


async def test_structured_capture_stalled_subscriber_bounded() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    q = cap.subscribe()  # never drained: a stalled client
    cap.start_build(1, "/nix/store/aaa-x.drv")
    for i in range(STRUCTURED_QUEUE_MAXSIZE + 50):
        cap.log_line(1, f"line {i}")
    assert q.qsize() <= STRUCTURED_QUEUE_MAXSIZE
    cap.close()
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items[-1] is None  # done sentinel delivered even to a full queue


async def test_structured_capture_monotonic_line_from_past_cap() -> None:
    # The container caps retained lines, so its line count plateaus; live
    # delta "from" must stay monotonic anyway or row ids would collide.
    cap = StructuredCapture(clock=lambda: 1.0, max_lines=4)
    q = cap.subscribe()
    cap.start_build(1, "/nix/store/aaa-qtbase-5.0.drv")
    for _ in range(8):
        cap.log_line(1, "x")
    deltas = [q.get_nowait() for _ in range(q.qsize())]
    lines = [d["from"] for d in deltas if d and d["t"] == "line"]
    assert lines == [1, 2, 3, 4, 5, 6, 7, 8]


async def test_structured_capture_status_without_start_build() -> None:
    # Finalized without build activity: still emits a card, keeps its name.
    cap = StructuredCapture(clock=lambda: 1.0)
    q = cap.subscribe()
    cap.set_status("/nix/store/aaa-qtbase-5.0.drv", "built")
    deltas = [q.get_nowait() for _ in range(q.qsize())]
    assert deltas[0] == {"t": "drv", "idx": 1, "name": "qtbase-5.0"}
    assert deltas[1] == {"t": "status", "idx": 1, "status": "built"}
    entry = next(e for e in cap.state() if e["idx"] == 1)
    assert entry["name"] == "qtbase-5.0"


def test_structured_failure_excerpt_uses_failed_derivation() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-ghidra-cli-test.drv")
    for line in ("configuring", "Starting Ghidra bridge...", "Error: exit status 1"):
        cap.log_line(1, line)
    cap.set_status("/nix/store/aaa-ghidra-cli-test.drv", "failed")
    excerpt = cap.failure_excerpt()
    # The failing derivation's own tail under a single name header; no
    # per-line prefix, no nix re-quote.
    assert excerpt.splitlines()[0] == "ghidra-cli-test:"
    assert excerpt.splitlines()[-1] == "Error: exit status 1"
    assert "configuring" in excerpt


def test_structured_capture_marks_failures_from_nix_prose() -> None:
    # nix names each failing derivation only in prose (never as a
    # per-activity status), so --keep-going failures are recovered by
    # scraping those messages. Covers remote ("build of") and local
    # ("builder for") wording, ANSI included.
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-alpha.drv")
    cap.log_line(1, "boom alpha")
    cap.start_build(2, "/nix/store/bbb-beta.drv")
    cap.log_line(2, "boom beta")
    cap.setup_line(
        "\x1b[31;1merror:\x1b[0m build of '/nix/store/aaa-alpha.drv' "
        "on 'ssh-ng://h' failed: builder failed with exit code 1"
    )
    cap.setup_line(
        "error: builder for '/nix/store/bbb-beta.drv' failed with exit code 1"
    )
    excerpt = cap.failure_excerpt()
    assert "alpha:\nboom alpha" in excerpt
    assert "beta:\nboom beta" in excerpt


def test_structured_failure_excerpt_skips_phase_markers() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-pkg.drv")
    for line in ("Running phase: configurePhase", "Running phase: buildPhase", "boom"):
        cap.log_line(1, line)
    cap.set_status("/nix/store/aaa-pkg.drv", "failed")
    excerpt = cap.failure_excerpt()
    # Phase markers are stdenv chrome (shown in the phase bar), not output.
    assert "Running phase" not in excerpt
    assert excerpt.splitlines()[-1] == "boom"


def test_phase_echo_suppressed_and_marker_is_zero_based() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    q = cap.subscribe()
    cap.start_build(1, "/nix/store/aaa-pkg.drv")
    # stdenv order: echo line, then the structured phase event.
    cap.log_line(1, "Running phase: buildPhase")
    cap.phase(1, "buildPhase")
    cap.log_line(1, "boom")
    cap.close()

    reader = LogContainerReader(cap.finalize())
    # idx 0 is the setup bucket; the build is idx 1. The echo is not
    # stored; the marker points at the first real line (0-based).
    assert reader.lines(1) == ["boom"]
    assert reader.entry(1)["ph"] == [["buildPhase", 0]]

    deltas = []
    while not q.empty():
        deltas.append(q.get_nowait())
    assert {"t": "phase", "idx": 1, "phase": "buildPhase", "line": 0} in deltas
    # No line delta was emitted for the suppressed echo.
    assert not any(d and d.get("text", "").startswith("Running phase") for d in deltas)


def test_build_failure_returns_structured_drvs() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-pkg.drv")
    cap.log_line(1, "Running phase: buildPhase")
    cap.log_line(1, "boom")
    cap.set_status("/nix/store/aaa-pkg.drv", "failed")
    failure = cap.build_failure()
    assert failure is not None
    assert failure.total == 1
    assert failure.drvs[0].tail == ["boom"]
    assert failure.headline() == "boom"


def test_build_failure_none_when_all_built() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-pkg.drv")
    cap.log_line(1, "ok")
    cap.set_status("/nix/store/aaa-pkg.drv", "built")
    assert cap.build_failure() is None


def test_structured_failure_excerpt_none_when_all_built() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-qtbase-5.0.drv")
    cap.log_line(1, "CC main.o")
    cap.set_status("/nix/store/aaa-qtbase-5.0.drv", "built")
    assert cap.failure_excerpt() == ""


def test_structured_failure_excerpt_caps_many_failures() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    for i in range(5):
        drv = f"/nix/store/aaa-pkg-{i}.drv"
        cap.start_build(i + 1, drv)
        cap.log_line(i + 1, f"boom {i}")
        cap.set_status(drv, "failed")
    excerpt = cap.failure_excerpt(max_drvs=2)
    # Only the last two failures are shown, each under its name; the
    # rest are counted.
    assert "pkg-4:\nboom 4" in excerpt
    assert "pkg-3:\nboom 3" in excerpt
    assert "pkg-0" not in excerpt
    assert "3 more failed derivations" in excerpt


async def test_structured_capture_state_snapshot() -> None:
    cap = StructuredCapture(clock=lambda: 1.0)
    cap.start_build(1, "/nix/store/aaa-qtbase-5.0.drv")
    cap.log_line(1, "CC main.o")
    state = cap.state()
    qt = next(e for e in state if e["name"] == "qtbase-5.0")
    assert qt["status"] == "running"  # in-flight, not yet stopped
    assert qt["lines"] == ["CC main.o"]
    assert qt["n"] == 1
    # A subscriber after close gets an immediate done, not a hang.
    cap.close()
    assert cap.subscribe().get_nowait() is None


# As written by render_log_event: the builder's own lines arrive as
# internal-json events and get a name> prefix; nix's prose error then
# re-quotes the same lines with a bare "> ".
NIX_FAILURE_TAIL = """\
building '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'
fail-1> this build is supposed to fail
error: build of '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv' failed: Cannot build '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'.
       Reason: builder failed with exit code 1.
       Output paths:
         /nix/store/wxpxl5abpxrr4ljnwj100nhijnbn2riz-fail-1
       Last 1 log lines:
       > this build is supposed to fail
       For full logs, run:
         nix log /nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv
error: Cannot build '/nix/store/mxpvhwgqn3q6sl1lykymcj77z7a1iifi-fail-1.drv'.
       Reason: builder failed with exit code 1.
       Output paths:
         /nix/store/wxpxl5abpxrr4ljnwj100nhijnbn2riz-fail-1
"""


def test_failure_excerpt_extracts_log_and_reason() -> None:
    excerpt = failure_excerpt(NIX_FAILURE_TAIL)
    lines = excerpt.splitlines()
    # The structured (name-prefixed) line wins over nix's prose
    # re-quote of the same text; the failure reason follows.
    assert lines[0] == "fail-1> this build is supposed to fail"
    assert lines[1] == "Reason: builder failed with exit code 1."
    assert len([x for x in lines if "supposed to fail" in x]) == 1
    # nix boilerplate is gone.
    assert "Output paths" not in excerpt
    assert "For full logs" not in excerpt
    assert "Last 1 log lines" not in excerpt


def test_failure_excerpt_without_log_lines_keeps_filtered_tail() -> None:
    tail = (
        "error: a 'aarch64-linux' with features {} is required to build x.drv\n"
        "required (system, features): (aarch64-linux, [])\n"
        "3 available machines:\n"
    )
    excerpt = failure_excerpt(tail)
    assert "required to build" in excerpt
    assert "3 available machines" in excerpt


def test_failure_excerpt_truncates_overlong_lines() -> None:
    long = "x" * 2000
    excerpt = failure_excerpt(f"fail-1> {long}\nReason: nope.\n")
    log_line = excerpt.splitlines()[0]
    # Visible length capped; ellipsis marks the cut.
    assert log_line.endswith("…")
    assert len(strip_ansi(log_line)) <= 650


def test_failure_excerpt_truncates_overlong_reason() -> None:
    excerpt = failure_excerpt(f"Reason: {'z' * 2000}\n")
    assert len(strip_ansi(excerpt)) <= 650
    assert excerpt.endswith("…")


def test_failure_excerpt_keeps_ansi_when_truncating() -> None:
    # ANSI codes must not count toward the length budget and must
    # survive truncation intact.
    body = "\x1b[31m" + "y" * 2000 + "\x1b[0m"
    excerpt = failure_excerpt(f"fail-1> {body}\n")
    log_line = excerpt.splitlines()[0]
    assert "\x1b[31m" in log_line
    assert len(strip_ansi(log_line)) <= 650
    # Truncation drops the source line's own reset; a reset is
    # re-appended so red does not bleed into the following line.
    assert log_line.endswith("\x1b[0m")


async def test_iter_lines_survives_line_over_stream_limit() -> None:
    # A single output line larger than the StreamReader limit used to
    # raise LimitOverrunError in the pump, leaving nix blocked on the
    # full pipe until the build timeout.
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"A" * 1000 + b"\nnext\n")
    reader.feed_eof()
    chunks = [chunk async for chunk in iter_lines(reader)]
    assert b"".join(chunks) == b"A" * 1000 + b"\nnext\n"
    # Line-oriented behavior preserved for lines within bounds.
    assert chunks[-1] == b"next\n"


async def test_iter_lines_caps_buffered_line_length() -> None:
    # An endless line must not buffer unboundedly: the buffer is
    # flushed whenever it exceeds max_line, so memory stays bounded by
    # max_line plus one read chunk.
    reader = asyncio.StreamReader(limit=64)
    reader.feed_data(b"B" * 300)
    reader.feed_eof()
    chunks = [chunk async for chunk in iter_lines(reader, max_line=100)]
    assert b"".join(chunks) == b"B" * 300
    assert all(len(c) <= 100 + 64 * 1024 for c in chunks)
