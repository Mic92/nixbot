"""Interactive TTY renderer for nbo build watch.

Verdicts of finished attributes go to normal scrollback, while a region
at the bottom is redrawn in place with the running attributes and a live
last log line for the longest-running ones.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import shutil
import sys
import termios
import threading
import time
import tty
from collections import deque
from datetime import UTC, datetime
from typing import IO, TYPE_CHECKING

from .term import (
    FAILED_STATUSES,
    RUNNING_STATUSES,
    fmt_duration,
    sanitize_line,
    status_str,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from .api import NixbotClient, RepoRef

CSI = "\x1b["
DIM = f"{CSI}2m"
RED = f"{CSI}31m"
GREEN = f"{CSI}32m"
YELLOW = f"{CSI}33m"
BOLD = f"{CSI}1m"
RESET = f"{CSI}0m"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

LIVE_LOGS = 5  # log streams followed for the longest-running attributes


class Display:
    """Scrollback for verdicts plus a live region redrawn in place.

    The region floats directly below the last verdict and only becomes
    pinned to the bottom once the output reaches it. From then on the
    verdicts scroll inside a DECSTBM margin above the region, so wrapped
    or bursty output cannot corrupt or duplicate the region by
    construction.
    """

    def __init__(
        self,
        out: IO[str],
        size: Callable[[], tuple[int, int]] = shutil.get_terminal_size,
        origin: int = 1,
    ) -> None:
        self.out = out
        self.size = size  # () -> (columns, rows)
        self.row = origin  # row the next verdict goes to
        self.top = origin  # first row of the drawn region
        self.margin = 0  # bottom row of the DECSTBM margin, 0 = none
        self.cols = 0
        self.rows = 0
        self.resizing = False  # size changed within the last frame
        # Recent verdicts, enough to rebuild the visible screen after a
        # resize rewrapped the old rows underneath us.
        self.history: deque[str] = deque(maxlen=1000)

    def _emit(self, buf: str) -> None:
        # DEC 2026 synchronized output: capable terminals paint atomically.
        self.out.write(f"{CSI}?2026h{buf}{CSI}?2026l")
        self.out.flush()

    def frame(self, permanent: list[str], region: list[str]) -> None:
        """One atomic update: append verdicts, redraw the live region."""
        cols, rows = self.size()
        region = region[: max(rows - 2, 1)]
        limit = rows - len(region)  # last row verdicts may occupy
        buf = f"{CSI}?25l"
        self.history.extend(permanent)
        if (cols, rows) != (self.cols, self.rows):
            # A resize rewraps the scrollback under us, so absolute rows
            # are unreliable until the size settles. Never erase anything:
            # release the margin, keep appending verdicts and skip the
            # region until the next frame sees a stable size.
            buf += f"{CSI}r"
            self.margin = 0
            self.resizing = self.rows != 0
            self.cols, self.rows = cols, rows
            self.row = min(self.row, rows)
        elif self.resizing:
            # The size settled: rebuild the visible screen from the model.
            # The cleared rows are repainted from history, older lines are
            # ordinary scrollback and rewrap on their own.
            self.resizing = False
            buf += f"{CSI}2J{CSI}1;1H"
            tail = list(self.history)[-(limit - 1) :] if limit > 1 else []
            buf += "".join(
                f"{CSI}{i + 1};1H{line}{CSI}K" for i, line in enumerate(tail)
            )
            self.row = len(tail) + 1
            permanent = []
        if not self.resizing and self.row > limit + 1:
            # The region grew past the space left below the output:
            # scroll the flow area up to make room.
            buf += self._set_margin(limit)
            buf += f"{CSI}{limit};1H" + "\n" * (self.row - limit - 1)
            self.row = limit + 1
        for line in permanent:
            if self.row <= limit:
                buf += f"{CSI}{self.row};1H{line}{CSI}K"
                self.row += 1
            else:
                buf += self._set_margin(limit)
                buf += f"{CSI}{limit};1H\n\r{line}{CSI}K"
        self.top = min(self.row, limit + 1)
        if not self.resizing:
            for i, line in enumerate(region):
                buf += f"{CSI}{self.top + i};1H{_clip(line, cols)}{RESET}{CSI}K"
            # Clear stale rows under the region (it may have moved or shrunk).
            buf += f"{CSI}J"
        buf += f"{CSI}{self.top};1H"
        self._emit(buf)

    def _set_margin(self, limit: int) -> str:
        if self.margin == limit:
            return ""
        self.margin = limit
        return f"{CSI}r{CSI}1;{limit}r"

    def close(self) -> None:
        """Release the margin and clear the region."""
        self._emit(f"{CSI}r{CSI}{self.top};1H{CSI}J{CSI}?25h")
        self.margin = 0


def _clip(line: str, width: int) -> str:
    """Crude ANSI-aware clip: drop the tail once the visible width is hit."""
    visible = 0
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\x1b":  # copy an escape sequence, it occupies no cells
            j = i + 2
            while j < len(line) and not ("@" <= line[j] <= "~"):
                j += 1
            out.append(line[i : j + 1])
            i = j + 1
            continue
        if visible >= width - 1:
            break
        out.append(ch)
        visible += 1
        i += 1
    return "".join(out)


def _epoch(iso: str | None) -> float:
    if not iso:
        return time.time()
    return datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp()


def cursor_row(out: IO[str], rows: int) -> int:
    """Current cursor row via a CPR query, so the live region can start
    right below the shell prompt instead of at the bottom of the screen."""
    if not (sys.stdin.isatty() and out.isatty()):
        return rows
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        out.write(f"{CSI}6n")
        out.flush()
        reply = ""
        while not reply.endswith("R"):
            if not select.select([fd], [], [], 0.2)[0]:
                return rows
            reply += os.read(fd, 32).decode(errors="replace")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    match = re.search(r"\[(\d+);\d+R", reply)
    return int(match.group(1)) if match else rows


class TtyWatch:
    """Runs the watch loop in a worker thread and renders in the caller."""

    def __init__(
        self,
        client: NixbotClient,
        repo: RepoRef,
        number: int,
        out: IO[str],
        size: Callable[[], tuple[int, int]] = shutil.get_terminal_size,
    ) -> None:
        self.client = client
        self.repo = repo
        self.number = number
        self.display = Display(out, size=size, origin=cursor_row(out, size()[1]))
        self.error: BaseException | None = None
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.running: dict[str, float] = {}  # attr -> started (epoch)
        self.gist: dict[str, str] = {}  # attr -> last live log line
        self.followers: set[str] = set()
        self.pending: list[str] = []  # verdict lines waiting for the next tick
        self.finished = 0
        self.failed = 0
        self.build_status = "pending"
        self.started_at = time.time()  # replaced by the build's start time
        self.spin = 0

    # --- worker: events + finished-attribute cursor ---------------------

    def _seed_running(self) -> None:
        detail = self.client.build(self.repo, self.number)
        with self.lock:
            self.build_status = detail["build"]["status"]
            self.started_at = _epoch(
                detail["build"].get("started_at") or detail["build"].get("created_at")
            )
            for a in detail["attributes"]:
                if a["status"] == "building":
                    self.running[a["attr"]] = _epoch(a.get("started_at"))

    def _record_finished(self, attrs: list[dict]) -> None:
        for a in attrs:
            verdict = status_str(a["status"], cached=bool(a.get("cached")))
            duration = ""
            if a.get("started_at") and a.get("finished_at"):
                took = _epoch(a["finished_at"]) - _epoch(a["started_at"])
                duration = f"  {DIM}{fmt_duration(took)}{RESET}"
            lines = [f"{verdict} {a['attr']}{duration}"]
            if a["status"] in FAILED_STATUSES:
                url = self.client.log_url(self.repo, self.number, a["attr"])
                lines.append(f"  {DIM}log: {url}{RESET}")
            with self.lock:
                self.running.pop(a["attr"], None)
                self.gist.pop(a["attr"], None)
                self.finished += 1
                self.failed += a["status"] in FAILED_STATUSES
                self.pending.extend(lines)

    def _watch(self) -> None:
        try:
            self._watch_loop()
        except BaseException as err:  # noqa: BLE001 (re-raised by run())
            self.error = err

    def _watch_loop(self) -> None:
        cursor: tuple[str, int] | None = None
        events = None
        while not self.done.is_set():
            delta = self.client.finished_attrs(
                self.repo,
                self.number,
                finished_after=cursor[0] if cursor else None,
                after_id=cursor[1] if cursor else 0,
            )
            build, attrs = delta["build"], delta["items"]
            with self.lock:
                self.build_status = build["status"]
            self._record_finished(attrs)
            if attrs:
                cursor = (attrs[-1]["finished_at"], attrs[-1]["id"])
            if build["status"] not in RUNNING_STATUSES:
                return
            if events is None:
                events = self.client.events(build=build["id"])
            hint = next(events, None)
            if hint is None:
                events = None
            elif hint.get("attr") and hint.get("status") == "building":
                with self.lock:
                    self.running.setdefault(hint["attr"], time.time())

    def _follow_log(self, attr: str) -> None:
        with contextlib.suppress(Exception):
            for event, data in self.client.log_stream(self.repo, self.number, attr):
                if self.done.is_set():
                    return
                if event == "line":
                    with self.lock:
                        self.gist[attr] = sanitize_line(data["text"])
                elif event == "done":
                    return

    # --- renderer --------------------------------------------------------

    def _rows(self) -> list[str]:
        self.spin += 1
        spin = SPINNER[self.spin % len(SPINNER)]
        with self.lock:
            running = sorted(self.running.items(), key=lambda kv: kv[1])
            finished, failed = self.finished, self.failed
            status = self.build_status
            gist = dict(self.gist)
        elapsed = fmt_duration(time.time() - self.started_at)
        _, term_rows = self.display.size()
        lines = [
            (
                f" {BOLD}build #{self.number}{RESET} {status} · "
                f"{GREEN}✓{finished - failed}{RESET} {RED}✗{failed}{RESET} "
                f"⏵{len(running)} · {elapsed}"
            )
        ]
        budget = max(2, term_rows - 3)
        now = time.time()
        for shown, (attr, started) in enumerate(running):
            live = gist.get(attr)
            rows = 2 if live else 1
            if budget - rows < (1 if shown < len(running) - 1 else 0):
                lines.append(f"   {DIM}… +{len(running) - shown} more{RESET}")
                break
            budget -= rows
            lines.append(
                f" {YELLOW}{spin}{RESET} {attr:<50} {fmt_duration(now - started):>7}"
            )
            if live:
                lines.append(f"   {DIM}{live}{RESET}")
        return lines

    def _spawn_followers(self) -> None:
        with self.lock:
            top = sorted(self.running.items(), key=lambda kv: kv[1])[:LIVE_LOGS]
            new = [attr for attr, _ in top if attr not in self.followers]
            self.followers.update(new)
        for attr in new:
            threading.Thread(target=self._follow_log, args=(attr,), daemon=True).start()

    def run(self) -> str:
        """Render until the build is terminal. Returns the final status."""
        self._seed_running()
        worker = threading.Thread(target=self._watch, daemon=True)
        worker.start()
        try:
            while True:
                alive = worker.is_alive()
                self._spawn_followers()
                # Only this loop writes to the terminal.
                with self.lock:
                    verdicts, self.pending = self.pending, []
                self.display.frame(verdicts, self._rows())
                if not alive:
                    break
                worker.join(timeout=0.25)
        finally:
            self.done.set()
            self.display.close()
        if self.error is not None:
            raise self.error
        return self.build_status
