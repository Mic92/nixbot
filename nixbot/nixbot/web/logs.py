"""Log viewer, SSE streaming, and raw downloads.

Logs live as frame-chunked zstd files on disk. Finished logs are
decompressed for the viewer/raw text endpoints. Running attributes stream through the LogRegistry: the
executor's LogWriter fans out to any number of SSE subscribers.
"""

from __future__ import annotations

import asyncio
import functools
import html
import json
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel

from ..ansi import (  # noqa: TID252
    ANSI_PARTIAL_RE,
    ANSI_TOKEN_RE,
    CTRL_RE,
    strip_ansi,
)
from ..build_scheduler import TERMINAL_FAILURES  # noqa: TID252
from ..db_gen import maintenance as maint_gen  # noqa: TID252
from ..db_gen import scheduled as sched_gen  # noqa: TID252
from ..db_gen import web as gen  # noqa: TID252
from ..executor import container_path, log_path_for_key, read_log  # noqa: TID252
from ..logstore import LogContainerReader, is_container  # noqa: TID252
from ..sql_util import row_dict, row_dicts  # noqa: TID252
from ..status import NO_LOG_STATUSES  # noqa: TID252
from .api_routes import FailureSummary, clean_row

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable
    from pathlib import Path

    from ..executor import LogWriter, StructuredCapture  # noqa: TID252
    from .app import WebContext


class LogRegistry:
    """Live LogWriters of currently running attributes."""

    def __init__(self) -> None:
        self._writers: dict[tuple[int, str], LogWriter] = {}
        # Scheduled-effect runs have no build_id. Key them by run id in
        # a separate map so the namespaces cannot collide.
        self._scheduled: dict[int, LogWriter] = {}

    def register(self, build_id: int, attr: str, writer: LogWriter) -> None:
        self._writers[(build_id, attr)] = writer

    def unregister(self, build_id: int, attr: str) -> None:
        self._writers.pop((build_id, attr), None)

    def get(self, build_id: int, attr: str) -> LogWriter | None:
        return self._writers.get((build_id, attr))

    def register_scheduled(self, run_id: int, writer: LogWriter) -> None:
        self._scheduled[run_id] = writer

    def unregister_scheduled(self, run_id: int) -> None:
        self._scheduled.pop(run_id, None)

    def get_scheduled(self, run_id: int) -> LogWriter | None:
        return self._scheduled.get(run_id)


_COLOR_CLASSES = {}
for _i, _name in enumerate(
    ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]
):
    _COLOR_CLASSES[str(30 + _i)] = f"ansi-{_name}"
    _COLOR_CLASSES[str(90 + _i)] = f"ansi-bright-{_name}"


class _Style(NamedTuple):
    fg: str | None = None  # color class
    bold: bool = False
    href: str | None = None  # OSC 8 link target


_RESET = _Style()


def _apply_sgr(params: str, style: _Style) -> _Style:
    """SGR is stateful: codes modify the current style, they don't
    replace it. Unknown codes are ignored, which also covers colon
    syntax (38:5:185 stays one unknown code). Semicolon-separated
    extended colors consume their arguments so e.g. 38;5;31 is not
    read as red."""
    fg, bold = style.fg, style.bold
    codes = (params or "0").split(";")
    i = 0
    while i < len(codes):
        code = codes[i] or "0"
        if code == "0":
            fg, bold = None, False
        elif code == "1":
            bold = True
        elif code == "22":
            bold = False
        elif code == "39":
            fg = None
        elif code in _COLOR_CLASSES:
            fg = _COLOR_CLASSES[code]
        elif code in ("38", "48"):
            # Extended color (unsupported): 38;5;n or 38;2;r;g;b.
            is_rgb = i + 1 < len(codes) and codes[i + 1] == "2"
            i += 4 if is_rgb else 2
        i += 1
    return style._replace(fg=fg, bold=bold)


def _apply_osc(payload: str, style: _Style) -> _Style:
    """OSC 8 opens/closes a hyperlink. Every other OSC (window title
    etc.) is dropped without touching the style."""
    if not payload.startswith("8;"):
        return style
    uri = payload.split(";", 2)[-1]
    # Logs are untrusted: http(s) targets only.
    if not uri.startswith(("http://", "https://")):
        uri = ""
    return style._replace(href=uri or None)


@functools.lru_cache(maxsize=256)
def _wrap(style: _Style) -> tuple[str, str]:
    pre = post = ""
    classes = " ".join(c for c in (style.fg, "ansi-bold" if style.bold else None) if c)
    if classes:
        pre, post = f'<span class="{classes}">', "</span>"
    if style.href:
        pre = f'<a href="{html.escape(style.href, quote=True)}" rel="nofollow">{pre}'
        post += "</a>"
    return pre, post


def _render_segment(segment: str, style: _Style) -> str:
    if not segment:
        return ""
    # Stripping C0 before tokenizing would eat OSC's BEL terminator.
    out = html.escape(CTRL_RE.sub("", segment))
    pre, post = _wrap(style)
    return f"{pre}{out}{post}"


def _ansi_convert(text: str, style: _Style) -> tuple[str, _Style]:
    """Convert SGR colors to spans and OSC 8 to links. Strip every
    other sequence. `style` carries in from the previous chunk/line;
    the style left open at the end is returned for the next one."""
    if "\x1b" not in text:
        return _render_segment(text, style), style
    out: list[str] = []
    pos = 0
    for match in ANSI_TOKEN_RE.finditer(text):
        out.append(_render_segment(text[pos : match.start()], style))
        pos = match.end()
        if match.group("sgr") is not None:
            style = _apply_sgr(match.group("sgr"), style)
        elif match.group("osc") is not None:
            style = _apply_osc(match.group("osc"), style)
    out.append(_render_segment(text[pos:], style))
    return "".join(out), style


def ansi_to_html(text: str) -> str:
    return _ansi_convert(text, _RESET)[0]


# Real escape sequences are tiny. A held-back "partial" larger than
# this is an unterminated OSC that would buffer the live stream
# forever.
_TAIL_MAX = 4096


class AnsiHtmlStream:
    """Chunked variant for live streams: SGR state and escape
    sequences split across chunk boundaries survive."""

    def __init__(self) -> None:
        self._style = _RESET
        self._tail = ""

    def feed(self, text: str) -> str:
        text = self._tail + text
        self._tail = ""
        partial = ANSI_PARTIAL_RE.search(text)
        if partial:
            self._tail = text[partial.start() :]
            text = text[: partial.start()]
        rendered, self._style = _ansi_convert(text, self._style)
        if len(self._tail) > _TAIL_MAX:
            # Give up on the broken sequence: flush it as plain text
            # (minus the ESC bytes) so the stream keeps moving.
            rendered += _render_segment(self._tail.replace("\x1b", ""), self._style)
            self._tail = ""
        return rendered


def render_log_lines(text: str) -> str:
    """Lines with id anchors for permalinks. Style carries across
    lines, matching what the live stream rendered."""
    lines = []
    style = _RESET
    for i, line in enumerate(text.splitlines(), 1):
        rendered, style = _ansi_convert(line, style)
        lines.append(
            f'<span class="logline" id="L{i}">'
            f'<a class="lineno" href="#L{i}">{i}</a>'
            f'<span class="logtext">{rendered}</span></span>'
        )
    # .logline is display:block. A joining "\n" inside <pre> would
    # render as an extra blank line.
    return "".join(lines)


def _reinject_phases(lines: list[str], ph: list[list]) -> list[str]:
    """Mirrors LogContainerReader.lines_with_phases for capture lines."""
    if not ph:
        return lines
    at: dict[int, list[str]] = {}
    for name, line in ph:
        at.setdefault(line, []).append(name)
    out: list[str] = []
    for n in range(len(lines) + 1):
        out.extend(f"Running phase: {name}" for name in at.get(n, []))
        if n < len(lines):
            out.append(lines[n])
    return out


def phase_sep(name: str) -> str:
    """Inline phase divider; CSS pins it as the current-phase header."""
    safe = html.escape(name)
    return (
        f'<div class="phase-sep" data-phase="{safe}">'
        f'<span class="phase-name">{safe}</span>'
        '<span class="phase-nav">'
        '<button type="button" class="phase-prev" aria-label="Previous phase">↑</button>'
        '<button type="button" class="phase-next" aria-label="Next phase">↓</button>'
        "</span></div>"
    )


def _phase_at(ph: list) -> dict[int, str]:
    """0-based line -> phase name. Empty phases collapse into the next."""
    return {line: name for name, line in ph}


def render_rows(
    idx: int,
    start: int,
    lines: Iterable[str],
    style: _Style = _RESET,
    phases: dict[int, str] | None = None,
) -> tuple[str, _Style]:
    """Anchored `d{idx}-L{n}` rows numbered from ``start``, with phase
    dividers spliced in at their first line. Returns the trailing SGR
    style so a live stream carries color into the next batch."""
    out = []
    n = start
    for line in lines:
        if phases and (n - 1) in phases:
            out.append(phase_sep(phases[n - 1]))
        rendered, style = _ansi_convert(line, style)
        out.append(
            f'<span class="logline" id="d{idx}-L{n}">'
            f'<a class="lineno" href="#d{idx}-L{n}">{n}</a>'
            f'<span class="logtext">{rendered}</span></span>'
        )
        n += 1
    return "".join(out), style


# A single huge derivation would insert tens of thousands of DOM nodes in
# one swap and hang the tab. Render only a head+tail window. The elided
# middle auto-loads as the reader scrolls, one bounded chunk at a time.
_RENDER_HEAD = 2000
_RENDER_TAIL = 3000
_RENDER_CAP = _RENDER_HEAD + _RENDER_TAIL
_RENDER_CHUNK = 2000


def chunk_marker(idx: int, base: str, start: int, end: int) -> str:
    """A marker standing in for lines [start, end) (0-based). Scrolled
    into view it fetches that range; the server renders one bounded
    chunk and a fresh marker for the remainder."""
    return (
        f'<div class="log-elided" role="separator"'
        f' hx-get="{base}/drv/{idx}?start={start}&end={end}"'
        f' hx-trigger="intersect once" hx-target="this" hx-swap="outerHTML">'
        f"loading {end - start:,} hidden lines…</div>"
    )


class _DrvLines(Protocol):
    """Finished container reader or live capture writer."""

    def __len__(self) -> int: ...
    def entry(self, i: int) -> dict: ...
    def lines(self, i: int) -> list[str]: ...


def render_drv_window(
    reader: _DrvLines,
    idx: int,
    base: str,
    start: int | None = None,
    end: int | None = None,
) -> str:
    """A derivation's log as anchored rows. The initial view (no range)
    is capped to a head+tail window with a marker spanning the gap. A
    range request renders the first chunk of [start, end) plus a marker
    for what remains of it."""
    lines = reader.lines(idx)
    ph = _phase_at(reader.entry(idx)["ph"])
    n = len(lines)
    if start is None:
        if n <= _RENDER_CAP:
            return render_rows(idx, 1, lines, phases=ph)[0]
        gap_end = n - _RENDER_TAIL
        head = render_rows(idx, 1, lines[:_RENDER_HEAD], phases=ph)[0]
        tail = render_rows(idx, gap_end + 1, lines[gap_end:], phases=ph)[0]
        return head + chunk_marker(idx, base, _RENDER_HEAD, gap_end) + tail
    start = max(0, start)
    end = min(end if end is not None else n, n)
    chunk_end = min(start + _RENDER_CHUNK, end)
    rows = render_rows(idx, start + 1, lines[start:chunk_end], phases=ph)[0]
    if chunk_end < end:
        rows += chunk_marker(idx, base, chunk_end, end)
    return rows


def _toc_entries(reader: LogContainerReader) -> list[dict]:
    fields = ("name", "status", "n", "ph", "t0", "t1")
    return [
        # .get("drv"): containers written before the drv path was recorded.
        {
            "idx": i,
            "drv": reader.entry(i).get("drv"),
            **{k: reader.entry(i)[k] for k in fields},
        }
        for i in range(len(reader))
    ]


async def _load_container(path: Path | None) -> LogContainerReader | None:
    """The `.nbl1` sidecar if a finished build wrote one, else None."""
    if path is None:
        return None
    cpath = container_path(path)
    if not await asyncio.to_thread(cpath.exists):
        return None
    blob = await asyncio.to_thread(cpath.read_bytes)
    return LogContainerReader(blob) if is_container(blob) else None


async def _log_text(
    registry: LogRegistry, build: dict, attr: str, path: Path | None
) -> str | None:
    writer = registry.get(build["id"], attr)
    if writer is not None:
        # Running attribute: part of the log is still buffered in
        # the writer, not yet on disk.
        data = await writer.snapshot()
    elif path is None or not await asyncio.to_thread(path.exists):
        return None
    else:
        # Decompression off the event loop: logs are up to 64 MB.
        data = await asyncio.to_thread(read_log, path)
    return data.decode(errors="replace")


def _strip_tail(text: str, tail: int) -> str:
    """ANSI-stripped last `tail` lines; CPU-bound on multi-MB logs, so
    callers run it via asyncio.to_thread."""
    return "\n".join(strip_ansi(text).splitlines()[-tail:])


def _plain(text: str, tail: int | None, *, ansi: bool) -> str:
    """Tail/ANSI post-processing for the plain-text API responses.
    CPU-bound on multi-MB logs, so callers run it via asyncio.to_thread."""
    if not ansi:
        text = strip_ansi(text)
    if tail:
        text = "\n".join(text.splitlines()[-tail:])
    return text if not text or text.endswith("\n") else text + "\n"


class EventStreamResponse(StreamingResponse):
    """Documents SSE routes as text/event-stream in the OpenAPI spec."""

    media_type = "text/event-stream"


class LogDerivation(BaseModel):
    """One derivation inside an attribute's structured log."""

    idx: int
    drv: str | None  # full .drv store path; None for setup/legacy entries
    name: str
    status: str
    n: int  # line count
    ph: list[list]  # [phase name, first 0-based line]
    t0: int | None  # ms epoch
    t1: int | None


class LogToc(BaseModel):
    """Table of contents of one attribute's structured log."""

    attr: str
    status: str | None
    derivations: list[LogDerivation]


def _api_derivations(entries: list[dict]) -> list[dict]:
    """Normalize capture/container entries for LogDerivation: only real
    store paths count as drv (not the synthetic setup key)."""
    return [
        {**e, "drv": d if (d := e.get("drv")) and d.endswith(".drv") else None}
        for e in entries
    ]


def _match_drv(entries: list[dict], selector: str) -> dict:
    """Entry whose drv path equals or name contains `selector`.
    404 on no match, 400 listing candidates when ambiguous."""
    matches = [e for e in entries if e.get("drv") == selector or selector in e["name"]]
    if not matches:
        raise HTTPException(status_code=404, detail="unknown derivation")
    if len(matches) > 1:
        names = ", ".join(e["name"] for e in matches)
        raise HTTPException(status_code=400, detail=f"ambiguous derivation: {names}")
    return matches[0]


async def _failure_summary(
    ctx: WebContext, registry: LogRegistry, build: dict, tail: int
) -> dict:
    failures = []
    for a in await ctx.queries.attributes(build["id"]):
        if a["status"] not in _FAILURE_STATUSES:
            continue
        # The file may not exist yet (never ran, or pending after a
        # reset); _log_text handles that.
        path = log_path_for_key(ctx.state_dir, build["id"], a["attr"])
        text = await _log_text(registry, build, a["attr"], path)
        failures.append(
            {
                "attr": a["attr"],
                "status": a["status"],
                "error": strip_ansi(a["error"]) if a["error"] else None,
                # Off the event loop: logs are up to 64 MB per attribute.
                "log_tail": await asyncio.to_thread(_strip_tail, text, tail)
                if text
                else None,
            }
        )
    return {
        "status": build["status"],
        "error": build["error"],
        # clean_row decodes the JSONB column to a list.
        "eval_warnings": clean_row(build)["eval_warnings"],
        "failures": failures,
    }


# SSE history replay bound (lines). Full logs stay available as raw
# downloads and in the static viewer.
HISTORY_MAX_LINES = 2000

_HISTORY_PAGE = 50

_FAILURE_STATUSES = {s.value for s in TERMINAL_FAILURES} | {"cancelled"}


class _LogRoutes:
    def __init__(self, ctx: WebContext, registry: LogRegistry) -> None:
        self.ctx = ctx
        self.registry = registry

    async def _build_or_404(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> tuple[dict, dict]:
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        build = await self.ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        return project, build

    async def _resolve(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> tuple[dict, dict, Path | None]:
        project, build = await self._build_or_404(request, forge, owner, name, number)
        path = log_path_for_key(self.ctx.state_dir, build["id"], attr)
        return project, build, path

    async def log_raw_text(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        tail: int | None = Query(None, ge=1),
    ) -> PlainTextResponse:
        """Full log as plain text; ?tail=N returns only the last N lines.
        Falls back to the stored eval error (e.g. failed_eval) so raw
        links work for every failure status."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        text = await self._attr_text(build, attr, path)
        if text is None:
            raise HTTPException(status_code=404)
        return PlainTextResponse(
            await asyncio.to_thread(_plain, text, tail, ansi=False)
        )

    async def log_raw_text_legacy(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        tail: int | None = Query(None, ge=1),
    ) -> Response:
        """Legacy /logs/{attr}.txt suffix route. It shadows the HTML
        viewer of an attribute literally named "{attr}.txt": when such
        an attribute exists its viewer wins (raw logs are always
        reachable under /logs/raw/), otherwise serve raw as before."""
        _, build = await self._build_or_404(request, forge, owner, name, number)
        shadowed = f"{attr}.txt"
        if await maint_gen.attribute_known(
            self.ctx.pool, build_id=build["id"], attr=shadowed
        ):
            return await self.log_viewer(request, forge, owner, name, number, shadowed)
        return await self.log_raw_text(request, forge, owner, name, number, attr, tail)

    def _scheduled_log_path(self, run_id: int) -> Path:
        return self.ctx.state_dir / "logs" / "scheduled" / f"{run_id}.zst"

    async def _scheduled_run_text(self, run_id: int) -> str | None:
        """Decoded log of a scheduled run: live writer snapshot while it
        runs, otherwise the on-disk file. None when no log exists."""
        writer = self.registry.get_scheduled(run_id)
        if writer is not None:
            data = await writer.snapshot()
        else:
            path = self._scheduled_log_path(run_id)
            if not await asyncio.to_thread(path.exists):
                return None
            data = await asyncio.to_thread(read_log, path)
        return data.decode(errors="replace")

    async def scheduled_run_log(
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        run_id: int,
    ) -> PlainTextResponse:
        """Log of one scheduled-effect run as plain text."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        row = await sched_gen.scheduled_run_exists(
            self.ctx.pool, id_=run_id, project_id=project["id"]
        )
        if row is None:
            raise HTTPException(status_code=404)
        text = await self._scheduled_run_text(run_id)
        if text is None:
            raise HTTPException(status_code=404)
        return PlainTextResponse(await asyncio.to_thread(strip_ansi, text))

    async def scheduled_run_viewer(
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        run_id: int,
    ) -> HTMLResponse:
        """ANSI-rendered HTML log of one scheduled-effect run."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        run = await sched_gen.scheduled_run_detail(
            self.ctx.pool, id_=run_id, project_id=project["id"]
        )
        if run is None:
            raise HTTPException(status_code=404)
        # Live page renders no snapshot: the stream replays full history
        # on connect (mirrors log_viewer).
        live = self.registry.get_scheduled(run_id) is not None
        content = ""
        waiting = False
        unavailable = False
        if not live:
            text = await self._scheduled_run_text(run_id)
            if text is not None:
                content = await asyncio.to_thread(render_log_lines, text)
            elif run.status == "running":
                # Started but the writer is not registered yet (fetch /
                # checkout runs before the first log byte). Poll until it
                # appears instead of claiming the log is gone.
                waiting = True
            else:
                # Terminal run whose log was pruned: placeholder, not a
                # 404, so the history link still resolves.
                unavailable = True
        return await self.ctx.render(
            "scheduled_log.html",
            request=request,
            project=project,
            run=row_dict(run),
            content=content,
            live=live,
            waiting=waiting,
            unavailable=unavailable,
        )

    async def scheduled_run_stream(
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        run_id: int,
    ) -> StreamingResponse:
        """SSE: history from disk, then live chunks until completion."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        row = await sched_gen.scheduled_run_exists(
            self.ctx.pool, id_=run_id, project_id=project["id"]
        )
        if row is None:
            raise HTTPException(status_code=404)
        writer = self.registry.get_scheduled(run_id)
        path = self._scheduled_log_path(run_id)
        return StreamingResponse(
            _stream_events(writer, path), media_type="text/event-stream"
        )

    async def scheduled_runs_history(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        schedule: str,
        effect: str,
        before: int | None = Query(None, ge=1),
    ) -> HTMLResponse:
        """Paginated run history for one (schedule, effect). schedule and
        effect arrive as query params, never path segments, so the
        repo-controlled names cannot affect routing or filesystem paths;
        runs are looked up by id only."""
        project = await self.ctx.repo_or_404(forge, owner, name, request)
        rows = await sched_gen.scheduled_runs_for_effect(
            self.ctx.pool,
            project_id=project["id"],
            schedule_name=schedule,
            effect=effect,
            before=before,
            limit_=_HISTORY_PAGE + 1,
        )
        runs = row_dicts(rows)
        has_more = len(runs) > _HISTORY_PAGE
        runs = runs[:_HISTORY_PAGE]
        template = "_schedule_run_rows.html" if before else "schedule_runs.html"
        return await self.ctx.render(
            template,
            request=request,
            project=project,
            schedule=schedule,
            effect=effect,
            runs=runs,
            has_more=has_more,
        )

    async def build_failures(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        # ge=1: splitlines()[-0:] would be the whole list, dumping the
        # full log of every failed attribute.
        tail: int = Query(50, ge=1),
    ) -> dict:
        """One-shot failure summary: failed attributes with log tails.

        Saves API consumers (CI scripts, LLM agents) a request per
        attribute when answering "why did this build fail?".
        """
        _, build = await self._build_or_404(request, forge, owner, name, number)
        return await _failure_summary(self.ctx, self.registry, build, tail)

    def _capture(self, build: dict, attr: str) -> StructuredCapture | None:
        writer = self.registry.get(build["id"], attr)
        return writer.capture if writer else None

    async def _attr_text(self, build: dict, attr: str, path: Path | None) -> str | None:
        """The attribute's log text, falling back to the stored eval error
        (e.g. failed_eval) so every failure status has readable output."""
        text = await _log_text(self.registry, build, attr, path)
        if text is None:
            text = await gen.attribute_error(
                self.ctx.pool, build_id=build["id"], attr=attr
            )
        return text

    async def api_log_toc(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> dict:
        """Table of contents of one attribute's log: its derivations with
        status, phases, line counts and timings. Running attributes are
        served from the live capture; logs without per-derivation
        structure appear as a single synthetic entry."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        attr_status = await gen.attribute_status(
            self.ctx.pool, build_id=build["id"], attr=attr
        )
        if attr_status is None:
            raise HTTPException(status_code=404, detail="unknown attribute")
        capture = self._capture(build, attr)
        if capture is not None:
            entries = capture.state(with_lines=False)
        elif (reader := await _load_container(path)) is not None:
            entries = _toc_entries(reader)
        else:
            # Flat log or eval error only: one synthetic derivation.
            text = await self._attr_text(build, attr, path)
            entries = (
                []
                if text is None
                else [
                    {
                        "idx": 0,
                        "drv": None,
                        "name": attr,
                        "status": attr_status,
                        "n": len(text.splitlines()),
                        "ph": [],
                        "t0": None,
                        "t1": None,
                    }
                ]
            )
        return {
            "attr": attr,
            "status": attr_status,
            "derivations": _api_derivations(entries),
        }

    async def api_log_text(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        tail: int | None = Query(None, ge=1),
        drv: str | None = None,
        ansi: bool = Query(default=False),
    ) -> PlainTextResponse:
        """The attribute's log as plain text. ?tail=N keeps the last N
        lines, ?drv=<store path or name substring> selects one derivation
        of a structured log, ?ansi=1 keeps SGR color sequences."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        text: str | None
        if drv:
            capture = self._capture(build, attr)
            if capture is not None:
                entry = _match_drv(capture.state(), drv)
                text = "\n".join(entry["lines"])
            elif (reader := await _load_container(path)) is not None:
                entry = _match_drv(_toc_entries(reader), drv)
                text = "\n".join(
                    await asyncio.to_thread(reader.lines_with_phases, entry["idx"])
                )
            else:
                raise HTTPException(
                    status_code=404, detail="log has no per-derivation structure"
                )
        else:
            text = await self._attr_text(build, attr, path)
            if text is None:
                raise HTTPException(status_code=404)
        return PlainTextResponse(await asyncio.to_thread(_plain, text, tail, ansi=ansi))

    async def api_log_stream(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> StreamingResponse:
        """SSE stream of a running attribute: a `state` snapshot (the TOC,
        no line history), then `drv`/`line`/`phase`/`drv-done` deltas with
        raw text, and `done` when the attribute finishes. History comes
        from the text endpoint."""
        _, build, _ = await self._resolve(request, forge, owner, name, number, attr)
        return EventStreamResponse(_structured_events_json(self._capture(build, attr)))

    async def log_drv_lines(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        idx: int,
    ) -> HTMLResponse:
        """One derivation's log as anchored HTML rows. ?start&end pulls a
        bounded slice of the elided middle for the load-more marker."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        capture = self._capture(build, attr)
        reader: _DrvLines | None = (
            capture.container if capture is not None else await _load_container(path)
        )
        if reader is None or not (0 <= idx < len(reader)):
            raise HTTPException(status_code=404)
        base = request.url.path.rsplit("/drv/", 1)[0]
        qp = request.query_params
        start = int(qp["start"]) if "start" in qp else None
        end = int(qp["end"]) if "end" in qp else None
        html = await asyncio.to_thread(render_drv_window, reader, idx, base, start, end)
        return HTMLResponse(html)

    async def log_drv_viewer(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        idx: int,
    ) -> HTMLResponse:
        """Standalone, shareable page for one derivation's log. The
        embedded card view caps its height. This page shows the same
        rows full-width with working line permalinks. While the attribute
        is still running the rows come from the live capture (the client
        follows the structured stream). Once finished, from the container.
        Capture and container assign indices in the same registration
        order, so a link shared during the build stays valid after it."""
        project, build, path = await self._resolve(
            request, forge, owner, name, number, attr
        )
        base = request.url.path.removesuffix("/view").rsplit("/drv/", 1)[0]
        writer = self.registry.get(build["id"], attr)
        capture = writer.capture if writer else None
        if capture is not None:
            entry = next((e for e in capture.state() if e["idx"] == idx), None)
            if entry is None:
                raise HTTPException(status_code=404)
            content = render_rows(
                idx, 1, entry["lines"], phases=_phase_at(entry["ph"])
            )[0]
            live = True
        else:
            reader = await _load_container(path)
            if reader is None or not (0 <= idx < len(reader)):
                raise HTTPException(status_code=404)
            entry = reader.entry(idx)
            content = await asyncio.to_thread(render_drv_window, reader, idx, base)
            live = False
        return await self.ctx.render(
            "drv.html",
            request=request,
            project=project,
            build=build,
            attr=attr,
            idx=idx,
            live=live,
            drv={k: entry[k] for k in ("name", "status", "n", "t0", "t1")},
            content=content,
        )

    async def log_drv_raw(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
        idx: int,
    ) -> PlainTextResponse:
        """One derivation's log as plain text (ANSI stripped). Served from the live capture until the container exists."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        capture = self._capture(build, attr)
        if capture is not None:
            entry = next((e for e in capture.state() if e["idx"] == idx), None)
            if entry is None:
                raise HTTPException(status_code=404)
            lines = _reinject_phases(entry["lines"], entry["ph"])
        else:
            reader = await _load_container(path)
            if reader is None or not (0 <= idx < len(reader)):
                raise HTTPException(status_code=404)
            lines = await asyncio.to_thread(reader.lines_with_phases, idx)
        return PlainTextResponse(strip_ansi("\n".join(lines)))

    async def build_search(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        q: str = "",
    ) -> HTMLResponse:
        """Search every attribute's container. Per-derivation hit groups
        (attr, name, line numbers), failures first. No container -> skipped.
        Returns rendered HTML the client swaps into #search-results."""
        _, build = await self._build_or_404(request, forge, owner, name, number)
        groups: list[dict] = []
        if len(q.strip()) >= 2:  # noqa: PLR2004
            for a in await self.ctx.queries.attributes(build["id"]):
                path = log_path_for_key(self.ctx.state_dir, build["id"], a["attr"])
                reader = await _load_container(path)
                if reader is None:
                    continue
                for hit in await asyncio.to_thread(reader.search, q):
                    hit["attr"] = a["attr"]
                    hit["status"] = reader.entry(hit["idx"])["status"]
                    groups.append(hit)
            groups.sort(key=lambda h: (h["status"] != "failed", -len(h["lines"])))
        return await self.ctx.render("_search_results.html", groups=groups)

    async def log_stream(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> StreamingResponse:
        """SSE: a per-derivation state burst then structured deltas from
        the live capture until the build finishes."""
        _, build, _ = await self._resolve(request, forge, owner, name, number, attr)
        writer = self.registry.get(build["id"], attr)
        capture = writer.capture if writer else None
        base = request.url.path.removesuffix("/stream")
        macros: Any = self.ctx.env.get_template("_macros.html").module
        return StreamingResponse(
            _structured_events(capture, macros.drv_card, base),
            media_type="text/event-stream",
        )

    async def log_viewer(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> HTMLResponse:
        project, build, path = await self._resolve(
            request, forge, owner, name, number, attr
        )
        # Effect statuses live in build_effects, not attributes.
        is_effect = attr.startswith("effect:")
        if is_effect:
            attr_status = await maint_gen.effect_status(
                self.ctx.pool,
                build_id=build["id"],
                name=attr.removeprefix("effect:"),
            )
        else:
            attr_status = await gen.attribute_status(
                self.ctx.pool, build_id=build["id"], attr=attr
            )
        # Live pages render no snapshot: the stream replays full
        # history on connect, the client would throw it away.
        writer = self.registry.get(build["id"], attr)
        live = writer is not None
        # A running attribute with a capture streams structured deltas;
        # the client builds cards from the stream, so no server toc.
        live_structured = writer is not None and writer.capture is not None
        content = ""
        toc: list[dict] | None = None
        waiting = False
        unavailable = False
        if not live:
            reader = await _load_container(path)
            if reader is not None:
                # Structured viewer: per-derivation cards render lazily
                # from /drv/{idx}. No flat body needed.
                toc = _toc_entries(reader)
            elif path is not None and path.exists():
                data = await asyncio.to_thread(read_log, path)
                content = await asyncio.to_thread(
                    render_log_lines, data.decode(errors="replace")
                )
            elif attr_status in ("pending", "building", "running"):
                # The build page links queued attributes before any
                # log exists. Show a waiting page instead of a 404.
                waiting = True
            elif attr_status in NO_LOG_STATUSES:
                # No build log, but eval failures carry a trace: show it
                # in the same viewer (line anchors, raw link) as build logs.
                error = await gen.attribute_error(
                    self.ctx.pool, build_id=build["id"], attr=attr
                )
                if error:
                    content = await asyncio.to_thread(render_log_lines, error)
                else:
                    unavailable = True
            else:
                raise HTTPException(status_code=404)
        prev_number, next_number = await self.ctx.queries.attribute_neighbors(
            project["id"], attr, number
        )
        return await self.ctx.render(
            "log.html",
            request=request,
            project=project,
            build=build,
            attr_status=attr_status,
            is_effect=is_effect,
            attr=attr,
            content=content,
            toc=toc,
            live=live,
            live_structured=live_structured,
            live_tail=LIVE_TAIL,
            waiting=waiting,
            unavailable=unavailable,
            prev_number=prev_number,
            next_number=next_number,
            can_control=await self.ctx.can_control(request, build),
        )


_BASE = "/repos/{forge}/{owner:owner}/{name:segment}/builds/{number}"


def create_log_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:
    router = APIRouter()
    routes = _LogRoutes(ctx, registry)
    # :path converters: attribute names may contain slashes. Route
    # order matters — raw/stream/.txt are matched before the greedy
    # viewer catch-all. /logs/raw/{attr} is the unambiguous raw route;
    # the .txt suffix stays as a fallback for existing consumers.
    router.get(f"{_BASE}/search")(routes.build_search)
    router.get(f"{_BASE}/logs/raw/{{attr:path}}")(routes.log_raw_text)
    router.get(f"{_BASE}/logs/{{attr}}.txt")(routes.log_raw_text_legacy)
    router.get(f"{_BASE}/logs/{{attr:path}}/stream")(routes.log_stream)
    router.get(
        f"{_BASE}/logs/{{attr:path}}/drv/{{idx}}/view", response_class=HTMLResponse
    )(routes.log_drv_viewer)
    router.get(f"{_BASE}/logs/{{attr:path}}/drv/{{idx}}/raw")(routes.log_drv_raw)
    router.get(f"{_BASE}/logs/{{attr:path}}/drv/{{idx}}", response_class=HTMLResponse)(
        routes.log_drv_lines
    )
    router.get(f"{_BASE}/logs/{{attr:path}}", response_class=HTMLResponse)(
        routes.log_viewer
    )
    # Same ordering discipline as the attribute routes above: the .txt
    # and /stream suffixes and the runs list precede the int {run_id}
    # viewer so it cannot swallow them.
    sched_base = "/repos/{forge}/{owner:owner}/{name:segment}/schedules"
    router.get(f"{sched_base}/runs", response_class=HTMLResponse)(
        routes.scheduled_runs_history
    )
    router.get(f"{sched_base}/runs/{{run_id:int}}.txt")(routes.scheduled_run_log)
    router.get(f"{sched_base}/runs/{{run_id:int}}/stream")(routes.scheduled_run_stream)
    router.get(f"{sched_base}/runs/{{run_id:int}}", response_class=HTMLResponse)(
        routes.scheduled_run_viewer
    )
    return router


def create_log_api_router(ctx: WebContext, registry: LogRegistry) -> APIRouter:
    """JSON/plain-text log endpoints of the documented /api surface."""
    router = APIRouter(tags=["api"])
    routes = _LogRoutes(ctx, registry)
    base = f"/api{_BASE}"
    router.get(f"{base}/failures", response_model=FailureSummary)(routes.build_failures)
    # :path — attribute names may contain slashes; the /text and /stream
    # suffixes are matched before the TOC catch-all.
    router.get(f"{base}/logs/{{attr:path}}/text", response_class=PlainTextResponse)(
        routes.api_log_text
    )
    router.get(f"{base}/logs/{{attr:path}}/stream", response_class=EventStreamResponse)(
        routes.api_log_stream
    )
    router.get(f"{base}/logs/{{attr:path}}", response_model=LogToc)(routes.api_log_toc)
    return router


async def _structured_events_json(
    capture: StructuredCapture | None,
) -> AsyncGenerator[str, None]:
    """JSON twin of _structured_events for the /api stream: no HTML, no
    line history (the state snapshot is TOC-shaped), raw text in line
    deltas. A finished/absent capture just signals done."""
    if capture is None:
        yield "event: done\ndata: {}\n\n"
        return
    queue = capture.subscribe()
    # Atomic with subscribe (no await between): no delta lost or duplicated.
    state = _api_derivations(capture.state(with_lines=False))
    yield f"event: state\ndata: {json.dumps(state, separators=(',', ':'))}\n\n"
    try:
        while True:
            try:
                delta = await asyncio.wait_for(queue.get(), timeout=30)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if delta is None:
                yield "event: done\ndata: {}\n\n"
                return
            # The capture's "status" delta is the derivation finishing.
            event = "drv-done" if delta["t"] == "status" else delta["t"]
            # json escapes any CR/LF in log text, so payloads cannot
            # forge SSE fields.
            payload = json.dumps(delta, separators=(",", ":"))
            yield f"event: {event}\ndata: {payload}\n\n"
    finally:
        capture.unsubscribe(queue)


async def _stream_events(
    writer: LogWriter | None, path: Path | None
) -> AsyncGenerator[str, None]:
    """History first, then live chunks (if a writer is running);
    everything rendered to HTML server-side so the client just
    appends. The replayed history is tail-limited so a late
    subscriber to a huge log does not push megabytes through the
    ANSI renderer."""
    ansi = AnsiHtmlStream()
    if writer is not None:
        history_bytes, queue = await writer.subscribe_with_history()
        history = history_bytes.decode(errors="replace")
    else:
        queue = None
        history = ""
        if path is not None and await asyncio.to_thread(path.exists):
            data = await asyncio.to_thread(read_log, path)
            history = data.decode(errors="replace")
    if history:
        lines = history.splitlines(keepends=True)
        if len(lines) > HISTORY_MAX_LINES:
            history = (
                "… earlier output truncated; use the raw log for full history …\n"
                + "".join(lines[-HISTORY_MAX_LINES:])
            )
        yield _sse(await asyncio.to_thread(ansi.feed, history))
    if queue is None:
        yield "event: done\ndata: \n\n"
        return
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=30)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if chunk is None:
                yield "event: done\ndata: \n\n"
                return
            yield _sse(ansi.feed(chunk.decode(errors="replace")))
    finally:
        if writer is not None:
            writer.unsubscribe(queue)


async def _drain(queue: asyncio.Queue[dict | None]) -> list[dict | None]:
    """Everything already queued (at least one item). One SSE event per
    burst instead of one per line keeps chatty builds cheap on both ends."""
    batch = [await asyncio.wait_for(queue.get(), timeout=30)]
    while batch[-1] is not None and not queue.empty():
        batch.append(queue.get_nowait())
    return batch


def _coalesce_lines(deltas: Iterable[dict]) -> list[dict]:
    """Merge runs of rendered line deltas of one derivation."""
    out: list[dict] = []
    for delta in deltas:
        prev = out[-1] if out else None
        if prev and delta["t"] == "line" == prev["t"] and prev["idx"] == delta["idx"]:
            prev["html"] += delta["html"]
        else:
            out.append(delta)
    return out


# The live view follows the tail. The finished page has the full log.
LIVE_TAIL = 5000


class _LiveRenderer:
    """Renders card shells (drv_card macro) and rows for the live stream,
    carrying each derivation's trailing SGR style into the next batch."""

    def __init__(self, drv_card: Callable[..., str], base: str) -> None:
        self._drv_card = drv_card
        self._base = base
        self._styles: dict[int, _Style] = {}

    def card(self, d: dict) -> str:
        # live=True: rows arrive over the SSE, so no htmx fetch attrs and
        # no raw link (the container behind it does not exist yet).
        live = d["status"] in ("running", "failed")
        return str(self._drv_card(d, self._base, open=live, live=True))

    def state(self, state: list[dict]) -> list[dict]:
        for e in state:
            lines = e.pop("lines")
            skipped = max(0, len(lines) - LIVE_TAIL)
            e["html"], self._styles[e["idx"]] = render_rows(
                e["idx"], skipped + 1, lines[skipped:], phases=_phase_at(e["ph"])
            )
            if skipped:
                e["html"] = chunk_marker(e["idx"], self._base, 0, skipped) + e["html"]
            e["card"] = self.card(e)
        return state

    def delta(self, delta: dict) -> dict:
        if delta["t"] == "drv":
            delta["status"] = "running"
            delta["card"] = self.card({**delta, "n": 0, "ph": [], "t0": None})
        elif delta["t"] == "line":
            idx = delta["idx"]
            delta["html"], self._styles[idx] = render_rows(
                idx, delta["from"], [delta.pop("text")], self._styles.get(idx, _RESET)
            )
        elif delta["t"] == "phase":
            delta = {
                "t": "line",
                "idx": delta["idx"],
                "html": phase_sep(delta["phase"]),
            }
        return delta


async def _structured_events(
    capture: StructuredCapture | None,
    drv_card: Callable[..., str],
    base: str,
) -> AsyncGenerator[str, None]:
    """Live per-derivation stream: a full-state burst on connect, then
    JSON deltas until the build finishes. A finished/absent capture just
    signals done so the client reloads into the container page."""
    if capture is None:
        yield "event: done\ndata: \n\n"
        return
    render = _LiveRenderer(drv_card, base)
    queue = capture.subscribe()
    state = render.state(capture.state())  # atomic with subscribe: no await
    yield f"event: state\ndata: {json.dumps(state, separators=(',', ':'))}\n\n"
    try:
        while True:
            try:
                batch = await _drain(queue)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            for delta in _coalesce_lines(
                render.delta(d) for d in batch if d is not None
            ):
                yield f"event: delta\ndata: {json.dumps(delta, separators=(',', ':'))}\n\n"
            if batch[-1] is None:
                yield "event: done\ndata: \n\n"
                return
    finally:
        capture.unsubscribe(queue)


def _sse(text: str) -> str:
    # EventSource accepts \r, \r\n and \n as line terminators: a bare
    # CR inside a data: payload would end the line early and let log
    # content forge SSE fields (e.g. a premature "event: done").
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"{data}\n\n"
