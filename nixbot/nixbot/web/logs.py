"""Log viewer, SSE streaming, and raw downloads.

Logs live as frame-chunked zstd files on disk. Finished logs are
decompressed for the viewer/raw text endpoints. Running attributes stream through the LogRegistry: the
executor's LogWriter fans out to any number of SSE subscribers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
from typing import TYPE_CHECKING, NamedTuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)

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
from ..executor import log_path_for_key, read_log  # noqa: TID252
from ..status import NO_LOG_STATUSES  # noqa: TID252
from .api_routes import FailureSummary, clean_row

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from ..executor import LogWriter  # noqa: TID252
    from .app import WebContext


class LogRegistry:
    """Live LogWriters of currently running attributes."""

    def __init__(self) -> None:
        self._writers: dict[tuple[int, str], LogWriter] = {}
        # Scheduled-effect runs have no build_id; key them by run id in
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
    syntax (38:5:185 stays one unknown code); semicolon-separated
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
    """OSC 8 opens/closes a hyperlink; every other OSC (window title
    etc.) is dropped without touching the style."""
    if not payload.startswith("8;"):
        return style
    uri = payload.split(";", 2)[-1]
    # Logs are untrusted: http(s) targets only.
    if not uri.startswith(("http://", "https://")):
        uri = ""
    return style._replace(href=uri or None)


def _render_segment(segment: str, style: _Style) -> str:
    if not segment:
        return ""
    # Stripping C0 before tokenizing would eat OSC's BEL terminator.
    out = html.escape(CTRL_RE.sub("", segment))
    classes = " ".join(c for c in (style.fg, "ansi-bold" if style.bold else None) if c)
    if classes:
        out = f'<span class="{classes}">{out}</span>'
    if style.href:
        out = (
            f'<a href="{html.escape(style.href, quote=True)}" rel="nofollow">{out}</a>'
        )
    return out


def _ansi_convert(text: str, style: _Style) -> tuple[str, _Style]:
    """Convert SGR colors to spans and OSC 8 to links; strip every
    other sequence. `style` carries in from the previous chunk/line;
    the style left open at the end is returned for the next one."""
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


# Real escape sequences are tiny; a held-back "partial" larger than
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
            f'<a class="lineno" href="#L{i}">{i}</a>{rendered}</span>'
        )
    # .logline is display:block; a joining "\n" inside <pre> would
    # render as an extra blank line.
    return "".join(lines)


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


# SSE history replay bound (lines); full logs stay available as raw
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
        """Full log as plain text; ?tail=N returns only the last N lines."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        text = await _log_text(self.registry, build, attr, path)
        if text is None:
            raise HTTPException(status_code=404)
        if tail:
            return PlainTextResponse(
                await asyncio.to_thread(_strip_tail, text, tail) + "\n"
            )
        return PlainTextResponse(await asyncio.to_thread(strip_ansi, text))

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
                # checkout runs before the first log byte); poll until it
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
            run=dataclasses.asdict(run),
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
        runs = [dataclasses.asdict(r) for r in rows]
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

    async def log_stream(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        attr: str,
    ) -> StreamingResponse:
        """SSE: history from disk, then live chunks until completion."""
        _, build, path = await self._resolve(request, forge, owner, name, number, attr)
        writer = self.registry.get(build["id"], attr)
        return StreamingResponse(
            _stream_events(writer, path), media_type="text/event-stream"
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
        attr_status = await gen.attribute_status(
            self.ctx.pool, build_id=build["id"], attr=attr
        )
        # Live pages render no snapshot: the stream replays full
        # history on connect, the client would throw it away.
        live = self.registry.get(build["id"], attr) is not None
        content = ""
        waiting = False
        unavailable = False
        if not live:
            if path is not None and path.exists():
                data = await asyncio.to_thread(read_log, path)
                content = await asyncio.to_thread(
                    render_log_lines, data.decode(errors="replace")
                )
            elif attr_status in ("pending", "building"):
                # The build page links queued attributes before any
                # log exists; show a waiting page instead of a 404.
                waiting = True
            elif attr_status in NO_LOG_STATUSES:
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
            attr=attr,
            content=content,
            live=live,
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
    router.get(f"{_BASE}/logs/raw/{{attr:path}}")(routes.log_raw_text)
    router.get(f"{_BASE}/logs/{{attr}}.txt")(routes.log_raw_text_legacy)
    router.get(f"{_BASE}/logs/{{attr:path}}/stream")(routes.log_stream)
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
    """The failure summary belongs to the documented /api surface,
    unlike the HTML/plain-text log routes."""
    router = APIRouter(tags=["api"])
    routes = _LogRoutes(ctx, registry)
    router.get(f"/api{_BASE}/failures", response_model=FailureSummary)(
        routes.build_failures
    )
    return router


async def _stream_events(
    writer: LogWriter | None, path: Path | None
) -> AsyncGenerator[str, None]:
    """History first, then live chunks (if a writer is running);
    everything rendered to HTML server-side so the client just
    appends; the replayed history is tail-limited so a late
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


def _sse(text: str) -> str:
    # EventSource accepts \r, \r\n and \n as line terminators: a bare
    # CR inside a data: payload would end the line early and let log
    # content forge SSE fields (e.g. a premature "event: done").
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"{data}\n\n"
