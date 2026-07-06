"""Attribute build executor.

Runs `nix build` per derivation with:

- a global concurrency cap, dequeued fairly round-robin across builds
  (FIFO within one build) so huge matrices cannot head-of-line block
  other projects,
- per-attribute timeout (default 3h) plus nix's --max-silent-time
  (default 20min),
- one automatic retry on transient infrastructure errors, suppressed
  once cancellation is requested,
- process-group kill on cancel/timeout,
- frame-chunked zstd log capture: one zstd frame per flush so live
  tailing only decompresses new frames; a single reader fans out to all
  subscribers; compression runs off the event loop; logs are capped
  (default 64 MB) keeping head and tail.

The eval gc-roots directory is owned by the orchestrator and held for
the duration of the build, so derivations cannot be garbage-collected
while queued here.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import zstandard

from .ansi import ANSI_TOKEN_RE, strip_ansi
from .build_scheduler import BuildFailure, BuildOutcome, DrvFailure
from .gcroots import safe_attr_filename
from .logstore import LogContainerWriter
from .proc import ProcessGroup

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from .models import NixEvalJobSuccess

logger = logging.getLogger(__name__)

# StreamReader buffer limit: asyncio's 64 KiB default makes readline()
# raise on longer lines, which nix build logs routinely contain.
STREAM_LIMIT = 16 * 1024 * 1024

# Cap per-subscriber backlog so a stalled SSE client cannot buffer the
# whole build output in memory; the oldest chunks are dropped.
SUBSCRIBER_QUEUE_MAXSIZE = 256
STRUCTURED_QUEUE_MAXSIZE = 4096  # per-line deltas, chattier than byte chunks
RECENT_BUFFER_SIZE = 4096

# Batch log output into zstd frames of at least this size; one frame
# per output line would make "compressed" logs larger than plaintext.
FRAME_FLUSH_THRESHOLD = 64 * 1024


# Log path is derived from (build_id, name), not stored. Names come from
# untrusted flakes: percent-encode so a log cannot escape its build
# directory, and keep effects in a subdirectory so an attribute named
# "effect-X" cannot collide with an effect log.


def build_log_dir(state_dir: Path, build_id: int) -> Path:
    return state_dir / "logs" / str(build_id)


def attribute_log_path(state_dir: Path, build_id: int, attr: str) -> Path:
    return build_log_dir(state_dir, build_id) / f"{quote(attr, safe='')}.zst"


def effect_log_path(state_dir: Path, build_id: int, name: str) -> Path:
    return (
        build_log_dir(state_dir, build_id) / "effects" / f"{quote(name, safe='')}.zst"
    )


def log_path_for_key(state_dir: Path, build_id: int, key: str) -> Path:
    """Path for a log-registry key: "effect:<name>" for effects, the
    attribute name otherwise."""
    if key.startswith("effect:"):
        return effect_log_path(state_dir, build_id, key.removeprefix("effect:"))
    return attribute_log_path(state_dir, build_id, key)


async def iter_lines(
    stream: asyncio.StreamReader, max_line: int = STREAM_LIMIT
) -> AsyncIterator[bytes]:
    """Line-split a stream via read() chunks.

    readline() raises LimitOverrunError on lines over the StreamReader
    limit, killing the pump while nix blocks on the full pipe. Reading
    chunks never raises; lines beyond max_line are flushed in pieces so
    one pathological line cannot buffer unboundedly.
    """
    buffer = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        buffer += chunk
        while (newline := buffer.find(b"\n")) != -1:
            yield bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
        if len(buffer) > max_line:
            yield bytes(buffer)
            buffer.clear()
    if buffer:
        yield bytes(buffer)


TRANSIENT_ERROR_MARKERS = (
    "unexpected end-of-file",
    "error: unable to download",
    "Connection reset by peer",
    "Connection refused",
    "Connection timed out",
    "Temporary failure in name resolution",
    "SSL connection",
    "writing to file: Broken pipe",
    "substituter",
)


class FairScheduler:
    """Global slot pool with round-robin dequeue across keys (builds)
    and FIFO order within a key."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            msg = f"capacity must be >= 1, got {capacity}"
            raise ValueError(msg)
        self.capacity = capacity
        self._active = 0
        # key -> FIFO of waiter futures; OrderedDict gives stable rotation.
        self._waiters: OrderedDict[object, deque[asyncio.Future[None]]] = OrderedDict()
        self._rotation: deque[object] = deque()

    async def acquire(self, key: object) -> None:
        if self._active < self.capacity and not self._waiters:
            self._active += 1
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        if key not in self._waiters:
            self._waiters[key] = deque()
            self._rotation.append(key)
        self._waiters[key].append(future)
        try:
            await future
        except asyncio.CancelledError:
            queue = self._waiters.get(key)
            if queue is not None and future in queue:
                queue.remove(future)
                if not queue:
                    del self._waiters[key]
                    self._rotation.remove(key)
            if future.cancelled() or not future.done():
                raise
            # Granted concurrently with cancellation: give the slot back.
            self.release()
            raise

    def release(self) -> None:
        self._active -= 1
        self._dispatch()

    def _dispatch(self) -> None:
        while self._active < self.capacity and self._rotation:
            key = self._rotation[0]
            queue = self._waiters.get(key)
            if not queue:
                self._rotation.popleft()
                self._waiters.pop(key, None)
                continue
            future = queue.popleft()
            # Rotate: next grant goes to the next build.
            self._rotation.rotate(-1)
            if not queue:
                del self._waiters[key]
                self._rotation.remove(key)
            if not future.done():
                self._active += 1
                future.set_result(None)


@dataclass
class LogWriter:
    """Frame-chunked zstd log writer with subscriber fan-out and a size
    cap keeping head and tail."""

    path: Path
    size_limit: int = 64 * 1024 * 1024
    bytes_seen: int = 0
    truncated: bool = False
    closed: bool = False
    # Live capture, so the web layer can stream per-derivation deltas.
    capture: StructuredCapture | None = None
    _head_budget: int = field(init=False)
    _tail: deque[bytes] = field(default_factory=deque)
    _tail_size: int = 0
    _recent: deque[bytes] = field(default_factory=deque)
    _recent_size: int = 0
    _dropped: int = 0
    _subscribers: list[asyncio.Queue[bytes | None]] = field(default_factory=list)
    _frame_buffer: bytearray = field(default_factory=bytearray)
    _compressor: zstandard.ZstdCompressor = field(
        default_factory=zstandard.ZstdCompressor
    )
    # Serializes frame flushes: a snapshot racing a threshold flush
    # must not write frames out of order or share the (stateful)
    # compressor across to_thread workers.
    _flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._head_budget = self.size_limit // 2
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_MAXSIZE
        )
        self._subscribers.append(queue)
        return queue

    @staticmethod
    def _offer(queue: asyncio.Queue[bytes | None], item: bytes | None) -> None:
        """Enqueue without blocking; drop the oldest chunk when the
        subscriber has stalled (no backpressure toward the writer)."""
        while True:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            else:
                return

    def unsubscribe(self, queue: asyncio.Queue[bytes | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subscribers.remove(queue)

    async def snapshot(self) -> bytes:
        """Full log content so far: flushed frames plus the unflushed
        frame buffer and the in-memory tail. The lock excludes a
        concurrent flush moving bytes from buffer to file mid-read."""
        async with self._flush_lock:
            history = await asyncio.to_thread(read_log, self.path)
            # Captured after the file read: a write landing during the
            # read appends here and is still included.
            pending = bytes(self._frame_buffer)
        return history + pending + b"".join(self._tail)

    async def subscribe_with_history(self) -> tuple[bytes, asyncio.Queue[bytes | None]]:
        """Snapshot of the log plus a live subscription: no chunk is
        lost or duplicated between the two.

        Subscribe first, then snapshot: a chunk written during the
        snapshot's await points would otherwise be in neither. Chunks
        written during the snapshot land in both; the overlap is
        dropped from the queue by byte count."""
        queue = self.subscribe()
        start_offset = self.bytes_seen
        history = await self.snapshot()
        overlap = self.bytes_seen - start_offset
        while overlap > 0:
            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                # A stalled-queue drop during the snapshot: fewer bytes
                # queued than written; nothing left to dedupe.
                break
            if chunk is None:
                break  # close() ran during the snapshot; re-signalled below
            overlap -= len(chunk)
        if self.closed:
            # Closed but not yet unregistered: terminate immediately
            # instead of leaving the subscriber waiting forever.
            self._offer(queue, None)
        return history, queue

    async def write(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_seen += len(data)
        self._recent.append(data)
        self._recent_size += len(data)
        while self._recent_size > RECENT_BUFFER_SIZE and len(self._recent) > 1:
            self._recent_size -= len(self._recent.popleft())
        for queue in self._subscribers:
            self._offer(queue, data)
        if self._head_budget > 0:
            chunk = data[: self._head_budget]
            self._head_budget -= len(chunk)
            await self._append_frame(chunk)
            data = data[len(chunk) :]
        if data:
            # Past the head budget: keep only the tail in memory.
            self._tail.append(data)
            self._tail_size += len(data)
            tail_limit = self.size_limit - self.size_limit // 2
            while self._tail_size > tail_limit and self._tail:
                dropped = self._tail.popleft()
                self._tail_size -= len(dropped)
                self._dropped += len(dropped)
                # Only an actual drop truncates: anything still in the
                # tail buffer reaches the disk in full.
                self.truncated = True

    async def _append_frame(self, data: bytes) -> None:
        # Batch into frames so tiny writes (one per output line) don't
        # blow up storage with per-frame overhead; compression runs off
        # the event loop.
        self._frame_buffer += data
        if len(self._frame_buffer) >= FRAME_FLUSH_THRESHOLD:
            await self._flush_frame()

    async def _flush_frame(self) -> None:
        async with self._flush_lock:
            if not self._frame_buffer:
                return
            chunk = bytes(self._frame_buffer)
            self._frame_buffer.clear()
            frame = await asyncio.to_thread(self._compressor.compress, chunk)
            with self.path.open("ab") as f:
                f.write(frame)

    def tail_lines(self, max_lines: int = 30, max_chars: int = 4000) -> str:
        """The last lines of output, for failure excerpts."""
        text = b"".join(self._recent).decode("utf-8", errors="replace")
        lines = text.splitlines()[-max_lines:]
        return "\n".join(lines)[-max_chars:]

    async def close(self) -> None:
        self.closed = True
        if self._dropped:
            marker = (
                f"\n\n... log truncated ({self._dropped} bytes omitted) ...\n\n"
            ).encode()
            await self._append_frame(marker)
        while self._tail:
            await self._append_frame(self._tail.popleft())
        self._tail_size = 0
        await self._flush_frame()
        for queue in self._subscribers:
            self._offer(queue, None)
        self._subscribers.clear()


@dataclass
class BuildSettings:
    log_dir: Path
    timeout: int = 60 * 60 * 3
    max_silent_time: int = 60 * 20
    show_trace: bool = False
    log_size_limit: int = 64 * 1024 * 1024
    # Extra `nix build` arguments, e.g. --option overrides.
    extra_args: list[str] = field(default_factory=list)


def build_nix_command(
    job: NixEvalJobSuccess, settings: BuildSettings, out_link: Path
) -> list[str]:
    return [
        "nix",
        "build",
        # internal-json keeps the ANSI colors that the terminal loggers
        # strip from non-tty output and tags every build-log line with
        # its derivation; render_log_event turns that back into
        # attributed, colored text.
        "--log-format",
        "internal-json",
        *(["--show-trace"] if settings.show_trace else []),
        "--option",
        "keep-going",
        "true",
        "--max-silent-time",
        str(settings.max_silent_time),
        # Hosts without flakes enabled in nix.conf (single-user
        # installs, containers) must still build; the eval command
        # carries the same override.
        "--option",
        "extra-experimental-features",
        "nix-command flakes",
        "--accept-flake-config",
        *settings.extra_args,
        "--out-link",
        str(out_link),
        f"{job.drv_path}^*",
    ]


# nix names failures only in prose: "build of" (remote) / "builder for"
# (local). Newer nix also uses a two-line "Cannot build '<drv>'." /
# "Reason:" form (see setup_line).
_BUILD_FAILED = re.compile(r"(?:build of|builder for) '([^']+\.drv)'.*?failed")
_CANNOT_BUILD = re.compile(r"Cannot build '([^']+\.drv)'")
_PHASE_ECHO = "Running phase: "


def _clean_tail(lines: list[str], max_lines: int) -> list[str]:
    kept = [_limit_line(line) for line in lines if line.strip()]
    return kept[-max_lines:]


_QUOTED_LINE = re.compile(r"[^\s>]*> +(.*\S)")
_EXCERPT_NOISE = re.compile(
    r"Output paths:|/nix/store/\S+$|Last \d+ log lines:"
    r"|For full logs, run:|nix log /nix/store/\S+$|building '/nix/store/"
)


_LINE_CHAR_LIMIT = 650


def _limit_line(line: str, limit: int = _LINE_CHAR_LIMIT) -> str:
    """Cap a line at `limit` visible characters, escapes not counted and
    never severed; a dropped tail's SGR reset is re-appended so color
    does not bleed into later lines."""
    visible = 0
    pos = 0
    # ANSI_TOKEN_RE spans are escapes; the gaps between them are text.
    for start, end in [(m.start(), m.end()) for m in ANSI_TOKEN_RE.finditer(line)] + [
        (len(line), len(line))
    ]:
        room = limit - visible
        if start - pos >= room:
            # Reserve the last slot for the ellipsis.
            kept = line[: pos + room - 1]
            reset = "\x1b[0m" if ANSI_TOKEN_RE.search(kept) else ""
            return kept + "…" + reset
        visible += start - pos
        pos = end
    return line


def failure_excerpt(tail: str, max_lines: int = 8) -> str:
    """The interesting part of a failed build's output: the builder's
    log lines (name>-prefixed from internal-json, deduped against
    nix's bare-'>' prose re-quote) plus one Reason line; without any,
    the tail minus nix's boilerplate. Lines are matched ANSI-stripped
    but emitted raw, so colors survive."""
    quoted: dict[str, str] = {}
    other: dict[str, str] = {}
    reason = None
    for line in tail.splitlines():
        raw, plain = line.strip(), strip_ansi(line).strip()
        if m := _QUOTED_LINE.match(plain):
            quoted.setdefault(m.group(1), raw)
        elif plain.startswith("Reason:"):
            reason = raw
        elif plain and not _EXCERPT_NOISE.match(plain):
            other.setdefault(plain, raw)
    lines = [_limit_line(x) for x in list((quoted or other).values())[-max_lines:]]
    if reason:
        lines.append(_limit_line(reason))
    return "\n".join(lines)


# nix activity/result types (nix/util/logging.hh).
ACT_BUILD = 105
RES_BUILD_LOG_LINE = 101
RES_SET_PHASE = 104


def _drv_display_name(drv_path: str) -> str:
    name = drv_path.rsplit("/", 1)[-1].removesuffix(".drv")
    _, _, name = name.partition("-")  # drop the store hash
    return name or drv_path


class StructuredCapture:
    """Demux the internal-json stream into a per-derivation container.

    Activity ids map to full drv paths; log lines, phases and stop
    timestamps attach to the owning derivation. Unattributed output (nix's
    own evaluation/scheduling/copy messages) collects under a synthetic
    ``setup`` entry.
    """

    SETUP = "<setup>"

    def __init__(
        self, clock: Callable[[], float] = time.time, max_lines: int | None = None
    ) -> None:
        self._w = (
            LogContainerWriter(max_lines=max_lines)
            if max_lines is not None
            else LogContainerWriter()
        )
        self._clock = clock
        self._act: dict[int, str] = {}
        self._idx: dict[str, int] = {self.SETUP: 0}
        # Monotonic per-derivation line counter for live delta numbering:
        # the container's line count stops growing once a drv hits its
        # retention cap, which would repeat delta "from" and collide row
        # ids. Live ids are ephemeral (the finish reload renumbers).
        self._seen: dict[str, int] = {}
        self._running: set[str] = set()
        self._pending_cannot: str | None = None
        self._subs: list[asyncio.Queue[dict | None]] = []
        self._closed = False
        self._w.register(self.SETUP, "setup")

    def _ts(self) -> int:
        return int(self._clock() * 1000)

    @staticmethod
    def _put(q: asyncio.Queue[dict | None], item: dict | None) -> None:
        # Drop the oldest delta for a stalled subscriber rather than grow
        # unbounded; the missing line is a cosmetic gap the finish reload
        # heals. Matches LogWriter's stalled-subscriber policy.
        while True:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            else:
                return

    def _emit(self, delta: dict) -> None:
        for q in self._subs:
            self._put(q, delta)

    def subscribe(self) -> asyncio.Queue[dict | None]:
        """Live deltas for a viewer; a full snapshot comes from state().
        Subscribe then snapshot with no await between so no delta is lost
        or duplicated (single event loop, atomic)."""
        q: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=STRUCTURED_QUEUE_MAXSIZE)
        if self._closed:
            q.put_nowait(None)
        else:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict | None]) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(q)

    def state(self) -> list[dict]:
        st = self._w.state()
        for e in st:
            if e["idx"] in self._running_idx:
                e["status"] = "running"
        return st

    @property
    def _running_idx(self) -> set[int]:
        return {self._idx[d] for d in self._running if d in self._idx}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for q in self._subs:
            self._put(q, None)
        self._subs.clear()

    def start_build(self, act_id: int, drv_path: str) -> None:
        self._act[act_id] = drv_path
        name = _drv_display_name(drv_path)
        self._w.register(drv_path, name)
        if drv_path not in self._idx:
            self._idx[drv_path] = len(self._idx)
        self._running.add(drv_path)
        self._emit({"t": "drv", "idx": self._idx[drv_path], "name": name})

    def _bump(self, drv: str) -> int:
        self._seen[drv] = self._seen.get(drv, 0) + 1
        return self._seen[drv]

    def log_line(self, act_id: int, text: str) -> None:
        drv = self._act.get(act_id, self.SETUP)
        if strip_ansi(text).startswith(_PHASE_ECHO):
            # RES_SET_PHASE already records this; drop stdenv's echo.
            return
        self._w.line(drv, text, ts=self._ts())
        self._emit(
            {
                "t": "line",
                "idx": self._idx.get(drv, 0),
                "from": self._bump(drv),
                "text": text,
            }
        )

    def phase(self, act_id: int, name: str) -> None:
        drv = self._act.get(act_id, self.SETUP)
        self._w.phase(drv, name, ts=self._ts())
        self._emit(
            {
                "t": "phase",
                "idx": self._idx.get(drv, 0),
                "phase": name,
                "line": self._seen.get(drv, 0),
            }
        )

    def stop(self, act_id: int) -> None:
        drv = self._act.get(act_id)
        if drv is not None:
            self._w.stop(drv, self._ts())
            self._running.discard(drv)
            self._emit({"t": "status", "idx": self._idx.get(drv, 0), "status": "built"})

    def setup_line(self, text: str) -> None:
        self._w.line(self.SETUP, text, ts=self._ts())
        self._emit(
            {"t": "line", "idx": 0, "from": self._bump(self.SETUP), "text": text}
        )
        stripped = strip_ansi(text)
        # --keep-going: mark every failed drv, not just the top-level one.
        for m in _BUILD_FAILED.finditer(stripped):
            drv = m.group(1)
            if drv in self._idx:
                self.set_status(drv, "failed")
        # Two-line form: flag the drv only once its "Reason:" says the
        # builder failed, not a dependency (a cascade parent).
        if cannot := _CANNOT_BUILD.search(stripped):
            self._pending_cannot = cannot.group(1)
        elif self._pending_cannot and "Reason:" in stripped:
            drv, self._pending_cannot = self._pending_cannot, None
            if "builder failed" in stripped and drv in self._idx:
                self.set_status(drv, "failed")

    def set_status(self, drv_path: str, status: str) -> None:
        if drv_path not in self._idx:
            # Finalized with no build activity (e.g. substituted): register
            # now so the live delta lands on a card. Appended last, so its
            # index equals its container position.
            name = _drv_display_name(drv_path)
            self._w.register(drv_path, name)
            self._idx[drv_path] = len(self._idx)
            self._emit({"t": "drv", "idx": self._idx[drv_path], "name": name})
        self._w.status(drv_path, status)
        self._running.discard(drv_path)
        self._emit({"t": "status", "idx": self._idx[drv_path], "status": status})

    def mark_failed(self, drv_path: str) -> None:
        if drv_path in self._idx:
            self.set_status(drv_path, "failed")
        elif not self._w.failing():
            # Top-level never ran and no leaf failed: the failure was in
            # setup (eval, scheduling, remote-builder). Attribute it there,
            # else the setup bucket keeps its default "built" (succeeded).
            self._w.status(self.SETUP, "failed")
            self._emit({"t": "status", "idx": 0, "status": "failed"})
        # else: a leaf drv already carries the failure; adding the
        # top-level as a phantom "no output" card only duplicates it.

    def build_failure(
        self, max_lines: int = 8, max_drvs: int = 3
    ) -> BuildFailure | None:
        failing = self._w.failing()
        if not failing:
            return None
        drvs = [
            DrvFailure(name, tail)
            for name, lines in failing[-max_drvs:]
            if (tail := _clean_tail(lines, max_lines))
        ]
        return BuildFailure(drvs=drvs, total=len(failing))

    def failure_excerpt(self, max_lines: int = 8, max_drvs: int = 3) -> str:
        failure = self.build_failure(max_lines, max_drvs)
        return failure.as_text() if failure else ""

    def finalize(self) -> bytes:
        return self._w.finalize()


def _feed_capture(event: Any, capture: StructuredCapture) -> None:
    action, etype = event.get("action"), event.get("type")
    fields = event.get("fields") or []
    if action == "start" and etype == ACT_BUILD and fields:
        capture.start_build(event["id"], str(fields[0]))
    elif action == "result" and etype == RES_BUILD_LOG_LINE:
        capture.log_line(event.get("id"), (fields or [""])[0])
    elif action == "result" and etype == RES_SET_PHASE and fields and fields[0]:
        capture.phase(event.get("id"), str(fields[0]))
    elif action == "stop":
        capture.stop(event.get("id"))
    elif action == "msg" and event.get("msg"):
        capture.setup_line(event["msg"])


def _render_event(event: Any, activities: dict[int, str]) -> bytes | None:
    action = event.get("action")
    if action == "start" and event.get("type") == ACT_BUILD:
        fields = event.get("fields") or []
        if fields:
            activities[event["id"]] = _drv_display_name(str(fields[0]))
        text = event.get("text", "")
        return f"{text}\n".encode() if text else None
    if action == "result" and event.get("type") == RES_BUILD_LOG_LINE:
        fields = event.get("fields") or [""]
        name = activities.get(event.get("id"), "")
        prefix = f"{name}> " if name else ""
        return f"{prefix}{fields[0]}\n".encode()
    if action == "msg":
        msg = event.get("msg", "")
        return f"{msg}\n".encode() if msg else None
    return None


def render_log_event(
    line: bytes,
    activities: dict[int, str],
    capture: StructuredCapture | None = None,
) -> bytes | None:
    """One line of `nix build --log-format internal-json` to log text.

    Build-log lines get a `name> ` prefix from their build activity;
    nix's own messages pass through with their ANSI colors. Returns
    None for events with no log output (progress, stops, ...). When
    `capture` is given, the same parsed event also feeds the structured
    per-derivation container (phases and stop timestamps included).
    """
    if not line.startswith(b"@nix "):
        if capture is not None and line.strip():
            capture.setup_line(line.decode(errors="replace").rstrip("\n"))
        return line  # not an event: pass through (e.g. daemon chatter)
    try:
        event = json.loads(line[len(b"@nix ") :])
    except ValueError:
        return line
    if capture is not None:
        _feed_capture(event, capture)
    return _render_event(event, activities)


def is_transient_error(output_tail: str) -> bool:
    return any(marker in output_tail for marker in TRANSIENT_ERROR_MARKERS)


async def _pump_output(
    stream: asyncio.StreamReader,
    output_tail: deque[bytes],
    log_writer: LogWriter,
    capture: StructuredCapture | None = None,
) -> None:
    activities: dict[int, str] = {}
    async for raw in iter_lines(stream):
        line = render_log_event(raw, activities, capture)
        if line is None:
            continue
        output_tail.append(line)
        await log_writer.write(line)


class NixBuildExecutor:
    """Builds attributes through the fair global queue."""

    def __init__(self, queue: FairScheduler, settings: BuildSettings) -> None:
        self.queue = queue
        self.settings = settings

    async def build_attribute(
        self,
        build_key: object,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event | None = None,
    ) -> BuildOutcome:
        """Run `nix build` for one attribute, with one automatic retry
        on transient errors (suppressed when cancellation is requested)."""
        cancel_event = cancel_event or asyncio.Event()
        if cancel_event.is_set():
            return BuildOutcome.cancelled
        # A superseded build must not sit "building" until an unrelated
        # build frees a slot: race the acquisition against the cancel.
        acquire_task = asyncio.create_task(self.queue.acquire(build_key))
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            await asyncio.wait(
                {acquire_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            # Cancelled in the same turn the slot was granted: give it
            # back, nobody will run the build.
            if acquire_task.done() and not acquire_task.cancelled():
                self.queue.release()
            raise
        finally:
            # Also runs on cancellation of this coroutine: a pending
            # waiter (and its eventual slot) must not leak.
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            if not acquire_task.done():
                acquire_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await acquire_task
        if acquire_task.cancelled():
            return BuildOutcome.cancelled
        await acquire_task
        try:
            outcome, transient = await self._run_once(
                job, log_writer, cwd, cancel_event
            )
            if outcome == BuildOutcome.failure and transient:
                if cancel_event.is_set():
                    # Cancel arrived while the retry was pending: the
                    # retry must not fire and the attribute ends
                    # cancelled (never cached as a failure).
                    return BuildOutcome.cancelled
                await log_writer.write(
                    b"\n\nnixbot: transient error detected, retrying once\n\n"
                )
                outcome, _ = await self._run_once(job, log_writer, cwd, cancel_event)
            return outcome
        finally:
            self.queue.release()

    async def _run_once(
        self,
        job: NixEvalJobSuccess,
        log_writer: LogWriter,
        cwd: Path,
        cancel_event: asyncio.Event,
    ) -> tuple[BuildOutcome, bool]:
        if cancel_event.is_set():
            return BuildOutcome.cancelled, False
        out_link = cwd / f"result-{safe_attr_filename(job.attr)}"
        cmd = build_nix_command(job, self.settings, out_link)
        group = await ProcessGroup.start(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=STREAM_LIMIT,
        )
        proc = group.proc
        assert proc.stdout is not None  # noqa: S101

        output_tail: deque[bytes] = deque(maxlen=100)
        capture = StructuredCapture()
        log_writer.capture = capture
        pump_task = asyncio.create_task(
            _pump_output(proc.stdout, output_tail, log_writer, capture)
        )
        cancel_task = asyncio.create_task(cancel_event.wait())
        try:
            wait_task = asyncio.ensure_future(proc.wait())
            done, _ = await asyncio.wait(
                {wait_task, cancel_task},
                timeout=self.settings.timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            timed_out = not done
            if wait_task not in done:  # cancel requested or timeout
                await group.reap()
            await pump_task
            if timed_out:
                await log_writer.write(
                    f"\n\nnixbot: build timed out after "
                    f"{self.settings.timeout}s\n\n".encode()
                )
                await _finalize_container(
                    capture, log_writer, job.drv_path, failed=True
                )
                # Timeouts are genuine failures (cached when enabled).
                return BuildOutcome.failure, False
            if proc.returncode == 0:
                # Even when the kill raced a clean exit: the build
                # finished, the result is real.
                await _finalize_container(
                    capture, log_writer, job.drv_path, failed=False
                )
                return BuildOutcome.success, False
            if cancel_event.is_set():
                return BuildOutcome.cancelled, False
            await _finalize_container(capture, log_writer, job.drv_path, failed=True)
            tail_text = b"".join(output_tail).decode(errors="replace")
            return BuildOutcome.failure, is_transient_error(tail_text)
        finally:
            # Terminate live viewers even on the cancel/timeout paths;
            # idempotent with the close in _finalize_container.
            capture.close()
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            if proc.returncode is None:
                # Hard cancel of the surrounding task: the nix process
                # group must not outlive the build (cancel_event covers
                # only the cooperative path).
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task
                await group.reap()


def container_path(log_path: Path) -> Path:
    return log_path.with_name(log_path.name + ".nbl1")


async def _finalize_container(
    capture: StructuredCapture,
    log_writer: LogWriter,
    drv_path: str,
    *,
    failed: bool,
) -> None:
    if failed:
        capture.mark_failed(drv_path)
    else:
        capture.set_status(drv_path, "built")
    capture.close()
    blob = capture.finalize()
    await asyncio.to_thread(container_path(log_writer.path).write_bytes, blob)


def read_log(path: Path) -> bytes:
    """Decompress a frame-chunked zstd log file (one frame per flush)."""
    data = path.read_bytes()
    dctx = zstandard.ZstdDecompressor()
    # One streaming pass; re-slicing unused_data per frame is quadratic.
    with dctx.stream_reader(io.BytesIO(data), read_across_frames=True) as reader:
        return reader.read()
