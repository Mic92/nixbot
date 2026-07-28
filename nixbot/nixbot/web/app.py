"""FastAPI web frontend.

Server-rendered Jinja2, classless CSS with system-preference dark
mode. Live updates are pushed: Postgres triggers NOTIFY on status
changes, /events fans them out over SSE, and a few lines of inline
JS refetch fragments or patch rows. Per-project sequential build
numbers in URLs. Prev/next navigation between builds and
per-attribute history. Attributes grouped by system with
failed-first ordering and inline error excerpts.

Visibility filtering hooks (`visible_repo_ids`) are wired by task
5.3b; None means everything is visible.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

from http import HTTPStatus

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from nixbot.effects_state import TaskTokens

from ..auth import User, can_control_build, is_admin  # noqa: TID252
from ..forge_tokens import RevokedSessionStore  # noqa: TID252
from ..recovery import check_db_health  # noqa: TID252
from ..schedules import ScheduledEffectsStore, schedule_overview  # noqa: TID252
from . import badge
from .api_routes import create_api_router
from .auth_routes import SESSION_COOKIE
from .badge import message_for
from .events import EventBroker, create_events_router
from .logs import LogRegistry, create_log_api_router, create_log_router
from .metrics import create_metrics_router
from .queries import PAGE_SIZE, BuildFilters, WebQueries
from .routing import register_owner_convertor
from .state_routes import create_state_router
from .templating import STATIC_DIR, CachedStaticFiles, make_env

if TYPE_CHECKING:
    from ..api_tokens import ApiTokenStore  # noqa: TID252
    from ..auth import AuthzConfig, SessionSigner  # noqa: TID252
    from ..forge_tokens import (  # noqa: TID252
        ForgeTokenRefresher,
        SessionRevocations,
        TokenVault,
    )
    from ..visibility import VisibilityService  # noqa: TID252

if TYPE_CHECKING:
    import asyncpg

# Routes below use {owner:owner} to match nested GitLab group owners.
register_owner_convertor()

# Upper bound for live row refreshes of deep-scrolled tables.
MAX_ROWS = 500

# Builds can have thousands of attributes: failures and running work
# render inline, the rest collapses to counts and loads in pages.
ATTR_PAGE = 200
ATTR_GROUPS: dict[str, tuple[str, ...]] = {
    "failed": ("failed", "failed_eval", "dependency_failed", "cached_failure"),
    "building": ("building",),
    "pending": ("pending",),
    "cancelled": ("cancelled",),
    "succeeded": ("succeeded",),
    "skipped": ("skipped_local",),
    "ignored": ("ignored_failure",),
}
INLINE_GROUPS = ("failed", "building")


class WebContext:
    """Shared state for the routes. Auth (5.3) extends this."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        state_dir: Path | None = None,
        signer: SessionSigner | None = None,
        visibility: VisibilityService | None = None,
    ) -> None:
        self.pool = pool
        self.state_dir = state_dir or Path("/var/lib/nixbot")
        self.queries = WebQueries(pool)
        self.env = make_env()
        self.signer = signer
        self.visibility = visibility
        # Wired by bootstrap; None hides control buttons (authz unknown).
        self.authz: AuthzConfig | None = None
        # Wired by bootstrap. Admins see Gitea webhook setup with it.
        self.webhook_base_url: str | None = None
        self.token_store: ApiTokenStore | None = None
        self.forge_tokens: TokenVault | None = None
        # Wired by bootstrap. Renews expired forge access tokens.
        self.token_refresher: ForgeTokenRefresher | None = None
        # Logout denylist: stateless cookies stay verifiable until
        # expiry, so revoked session ids are checked server-side.
        self.revoked_sessions: SessionRevocations | None = RevokedSessionStore(pool)
        # Reverse-proxy auth header for the authenticated username.
        # Wired by bootstrap.
        self.proxy_auth_header: str | None = None

    async def can_control(self, request: Request, build: dict[str, Any]) -> bool:
        """Whether the requester may restart/cancel this build (drives
        both the buttons and the control routes): instance admin, PR
        author, or forge write access to the repo."""
        if self.authz is None:
            return False
        if can_control_build(
            await self.request_user(request),
            self.authz,
            build_pr_author=build.get("pr_author"),
        ):
            return True
        controllable = await self.controllable_repo_ids(request)
        return controllable is None or build["project_id"] in controllable

    async def current_user(self, request: Request) -> User | None:
        if self.signer is None:
            return None
        token = request.cookies.get(SESSION_COOKIE)
        user = self.signer.user_from(token)
        if user is None:
            return None
        # A validly signed cookie may belong to a logged-out session;
        # captured copies must not stay usable after logout.
        session_id = self.signer.session_id_from(token)
        if (
            session_id is not None
            and self.revoked_sessions is not None
            and await self.revoked_sessions.is_revoked(session_id)
        ):
            return None
        return user

    async def request_user(self, request: Request) -> User | None:
        """Session cookie, proxy auth header, or personal API token
        (Authorization: ***). Cached per request: routes ask several
        times (authz checks, visibility, toggleability) and token auth
        hits the database."""
        if not hasattr(request.state, "bn_user"):
            request.state.bn_user = await self._request_user(request)
        return request.state.bn_user

    async def _request_user(self, request: Request) -> User | None:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer ") and self.token_store is not None:
            return await self.token_store.authenticate(
                auth_header.removeprefix("Bearer ")
            )
        if self.proxy_auth_header is not None:
            remote_user = request.headers.get(self.proxy_auth_header)
            if remote_user:
                return User(
                    provider="proxy",
                    username=remote_user,
                )
        return await self.current_user(request)

    async def render(
        self, template: str, request: Request | None = None, **context: Any
    ) -> HTMLResponse:
        if request is not None and "user" not in context:
            context["user"] = await self.request_user(request)
        if request is not None and "forge_token_expired" not in context:
            # The forge token can expire long before the session does;
            # private-repo visibility then silently degrades to public,
            # so tell the user a re-login restores it.
            context["forge_token_expired"] = (
                self.visibility is not None
                and self.forge_tokens is not None
                and context.get("user") is not None
                and request.cookies.get(SESSION_COOKIE) is not None
                and await self._forge_token(request) is None
            )
        return HTMLResponse(self.env.get_template(template).render(**context))

    async def visible_repo_ids(self, request: Request) -> list[int] | None:
        """None = all projects visible."""
        if self.visibility is None:
            return None
        return await self.visibility.visible_repo_ids(
            await self.request_user(request), await self._forge_token(request)
        )

    async def toggleable_repo_ids(self, request: Request) -> list[int] | None:
        """Projects the requester may enable/disable; None = all."""
        if self.visibility is None:
            return []
        return await self.visibility.toggleable_repo_ids(
            await self.request_user(request), await self._forge_token(request)
        )

    async def controllable_repo_ids(self, request: Request) -> list[int] | None:
        """Projects whose builds the requester may restart/cancel; None =
        all. No visibility service means no forge integration, so no
        repo-based control."""
        if self.visibility is None:
            return []
        return await self.visibility.controllable_repo_ids(
            await self.request_user(request), await self._forge_token(request)
        )

    async def _forge_token(self, request: Request) -> str | None:
        if not hasattr(request.state, "bn_forge_token"):
            request.state.bn_forge_token = await self._fetch_forge_token(request)
        return request.state.bn_forge_token

    async def _fetch_forge_token(self, request: Request) -> str | None:
        if self.signer is None or self.forge_tokens is None:
            return None
        session_id = self.signer.session_id_from(request.cookies.get(SESSION_COOKIE))
        if session_id is None:
            return None
        if self.token_refresher is not None:
            return await self.token_refresher.access_token(session_id)
        return await self.forge_tokens.get(session_id)

    async def repo_or_404(
        self, forge: str, owner: str, name: str, request: Request | None = None
    ) -> dict[str, Any]:
        """404 for unknown projects AND for private projects the
        requester cannot see (their existence stays hidden)."""
        project = await self.queries.repo_by_name(forge, owner, name)
        if project is None:
            raise HTTPException(status_code=404)
        if request is not None:
            visible = await self.visible_repo_ids(request)
            if visible is not None and project["id"] not in visible:
                raise HTTPException(status_code=404)
        return project


def create_legacy_router(ctx: WebContext) -> APIRouter:
    """Transitional redirects: v1 URLs said /projects/ without a
    forge segment. Resolve the forge by lookup and 307
    (method-preserving) to the canonical URL. Remove once the rename
    has been out for a while."""
    router = APIRouter()

    @router.api_route(
        "/projects/{owner:owner}/{name:segment}{rest:path}", methods=["GET", "POST"]
    )
    @router.api_route(
        "/api/projects/{owner:owner}/{name:segment}{rest:path}", methods=["GET", "POST"]
    )
    async def legacy_repo_urls(
        request: Request, owner: str, name: str, rest: str
    ) -> RedirectResponse:
        candidates = await ctx.queries.repo_candidates(owner, name)
        # Same visibility rule as the canonical routes: redirect-vs-404
        # must not let outsiders probe private repo existence.
        visible = await ctx.visible_repo_ids(request)
        if visible is not None:
            candidates = [c for c in candidates if c["id"] in visible]
        # Ambiguous owner/name across forges: 404 instead of silently
        # picking one — the caller must use the forge-qualified URL.
        if len({c["forge"] for c in candidates}) != 1:
            raise HTTPException(status_code=404)
        forge = candidates[0]["forge"]
        prefix = "/api/repos" if request.url.path.startswith("/api/") else "/repos"
        url = f"{prefix}/{forge}/{owner}/{name}{rest}"
        if request.url.query:
            url += f"?{request.url.query}"
        return RedirectResponse(url, status_code=307)

    return router


class _PageRoutes:
    def __init__(self, ctx: WebContext) -> None:
        self.ctx = ctx

    async def index(self, request: Request, q: str = "") -> HTMLResponse:
        ctx = self.ctx
        visible = await ctx.visible_repo_ids(request)
        # Discovery inserts repos disabled. Admins see the disabled repos
        # they may toggle so a fresh instance is not a blank page. The
        # search box filters the list. Non-admins have an empty
        # toggleable set and see nothing here.
        toggleable = await ctx.toggleable_repo_ids(request)
        disabled = [
            p
            for p in await ctx.queries.projects(enabled=False, q=q)
            if toggleable is None or p["id"] in toggleable
        ]
        return await ctx.render(
            "index.html",
            request=request,
            q=q,
            toggleable=toggleable,
            projects=await ctx.queries.repo_overview(project_ids=visible, q=q or None),
            disabled_projects=disabled,
        )

    async def activity(self, request: Request) -> HTMLResponse:
        ctx = self.ctx
        visible = await ctx.visible_repo_ids(request)
        # limit+1 probe (like activity_rows): a full page is not proof
        # of more rows when the total is an exact multiple of PAGE_SIZE.
        builds = await ctx.queries.recent_builds(
            limit=PAGE_SIZE + 1, project_ids=visible
        )
        return await ctx.render(
            "activity.html",
            request=request,
            queue=await ctx.queries.queue(visible),
            can_cancel_queue=await self._can_cancel_queue(request),
            builds=builds[:PAGE_SIZE],
            has_more=len(builds) > PAGE_SIZE,
            more_url="/builds/rows",
        )

    async def activity_rows(
        self, request: Request, before: int | None = None, limit: int = PAGE_SIZE
    ) -> HTMLResponse:
        """Row fragments: infinite scroll (before=) and live refresh of
        the loaded rows (limit=)."""
        ctx = self.ctx
        limit = min(max(limit, 1), MAX_ROWS)
        visible = await ctx.visible_repo_ids(request)
        builds = await ctx.queries.recent_builds(
            limit=limit + 1, project_ids=visible, before=before
        )
        return await ctx.render(
            "_build_rows.html",
            request=request,
            builds=builds[:limit],
            has_more=len(builds) > limit,
            more_url="/builds/rows",
        )

    async def queue_fragment(self, request: Request) -> HTMLResponse:
        ctx = self.ctx
        visible = await ctx.visible_repo_ids(request)
        return await ctx.render(
            "_queue.html",
            request=request,
            queue=await ctx.queries.queue(visible),
            can_cancel_queue=await self._can_cancel_queue(request),
        )

    async def _can_cancel_queue(self, request: Request) -> bool:
        """UX only. The cancel-all route re-checks server-side."""
        ctx = self.ctx
        return ctx.authz is not None and is_admin(
            await ctx.request_user(request), ctx.authz
        )

    async def repo_page(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        page: int = 1,
        status: str | None = None,
        ref: str | None = None,
    ) -> HTMLResponse:
        ctx = self.ctx
        project = await ctx.repo_or_404(forge, owner, name, request)
        builds = await ctx.queries.builds_for_repo(
            project["id"],
            page=page,
            filters=BuildFilters.for_ref(ref, status=status),
        )
        store = ScheduledEffectsStore(ctx.pool)
        return await ctx.render(
            "repo.html",
            request=request,
            project=project,
            builds=builds,
            status=status or "",
            ref=ref or "",
            webhook_url=await self._webhook_url(request, project),
            schedules=schedule_overview(
                await store.schedules_for_project(project["id"]),
                await store.latest_runs_for_project(project["id"]),
            ),
            can_run_schedules=await self._can_run_schedules(request, project["id"]),
        )

    async def _can_run_schedules(self, request: Request, project_id: int) -> bool:
        """UX only. The run-schedule route re-checks server-side."""
        ctx = self.ctx
        if ctx.authz is None:
            return False
        if is_admin(await ctx.request_user(request), ctx.authz):
            return True
        toggleable = await ctx.toggleable_repo_ids(request) or []
        return project_id in toggleable

    async def _webhook_url(
        self, request: Request, project: dict[str, Any]
    ) -> str | None:
        """Gitea webhook target for admins: manual setup when the
        buildbot user cannot manage hooks itself. The secret is not
        included. It is rotated and shown once via the control route."""
        ctx = self.ctx
        if (
            project["forge"] not in ("gitea", "gitlab")
            or ctx.authz is None
            or ctx.webhook_base_url is None
            or not is_admin(await ctx.request_user(request), ctx.authz)
        ):
            return None
        return f"{ctx.webhook_base_url.rstrip('/')}/webhooks/{project['forge']}"

    async def repo_rows(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        before: int | None = None,
        limit: int = PAGE_SIZE,
        status: str | None = None,
        ref: str | None = None,
    ) -> HTMLResponse:
        """Row fragments: infinite scroll (before=) and live refresh of
        the loaded rows (limit=)."""
        ctx = self.ctx
        limit = min(max(limit, 1), MAX_ROWS)
        project = await ctx.repo_or_404(forge, owner, name, request)
        builds = await ctx.queries.builds_for_repo(
            project["id"],
            limit=limit,
            filters=BuildFilters.for_ref(ref, status=status or None, before=before),
        )
        return await ctx.render(
            "_build_rows.html",
            request=request,
            builds=builds.items,
            has_more=builds.has_next,
            more_url=f"/repos/{forge}/{owner}/{name}/rows?status={status or ''}"
            f"&ref={quote(ref or '')}",
            project=project,
        )

    async def build_page(
        self, request: Request, forge: str, owner: str, name: str, number: int
    ) -> HTMLResponse:
        ctx = self.ctx
        project = await ctx.repo_or_404(forge, owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        if isinstance(build.get("eval_warnings"), str):
            # asyncpg returns jsonb as a string.
            build["eval_warnings"] = json.loads(build["eval_warnings"])
        prev_number, next_number = await ctx.queries.neighbor_numbers(
            project["id"], number
        )
        group_counts, inline = await self._grouped_attributes(build["id"], None)
        total = sum(group_counts.values())
        return await ctx.render(
            "build.html",
            request=request,
            project=project,
            build=build,
            attrs_total=total,
            attrs_done=total - group_counts["pending"] - group_counts["building"],
            group_counts=group_counts,
            inline=inline,
            effects=await ctx.queries.effects(build["id"]),
            prev_number=prev_number,
            next_number=next_number,
            can_control=await ctx.can_control(request, build),
        )

    async def attribute_rows(  # noqa: PLR0913
        self,
        request: Request,
        forge: str,
        owner: str,
        name: str,
        number: int,
        group: str | None = None,
        q: str | None = None,
        page: int = 1,
        limit: int = ATTR_PAGE,
    ) -> HTMLResponse:
        """One group's infinite-scroll rows, or the full grouped
        fragment filtered by a search query."""
        ctx = self.ctx
        statuses = ATTR_GROUPS.get(group) if group else None
        if statuses is None and q is None:
            raise HTTPException(status_code=404)
        project = await ctx.repo_or_404(forge, owner, name, request)
        build = await ctx.queries.build_by_number(project["id"], number)
        if build is None:
            raise HTTPException(status_code=404)
        limit = min(max(limit, 1), MAX_ROWS)
        if statuses is None:
            return await self._attribute_groups(request, project, build, q or None)
        rows = await ctx.queries.attribute_page(
            build["id"], statuses, limit, max(page, 1), q or None
        )
        return await ctx.render(
            "_attr_rows.html",
            request=request,
            project=project,
            build=build,
            attributes=rows.items,
            group=group,
            q=q or None,
            page=rows.page,
            limit=limit if limit != ATTR_PAGE else None,
            has_next=rows.has_next,
        )

    async def _grouped_attributes(
        self, build_id: int, q: str | None
    ) -> tuple[dict[str, int], dict[str, Any]]:
        """Per-group counts plus eagerly loaded first pages: the inline
        groups normally, every matching group under a search query."""
        counts = await self.ctx.queries.attribute_counts(build_id, q)
        group_counts = {
            group: sum(counts.get(s, 0) for s in statuses)
            for group, statuses in ATTR_GROUPS.items()
        }
        eager = tuple(ATTR_GROUPS) if q else INLINE_GROUPS
        inline = {
            group: await self.ctx.queries.attribute_page(
                build_id, ATTR_GROUPS[group], ATTR_PAGE, 1, q
            )
            for group in eager
            if group_counts[group]
        }
        return group_counts, inline

    async def _attribute_groups(
        self,
        request: Request,
        project: dict[str, Any],
        build: dict[str, Any],
        q: str | None,
    ) -> HTMLResponse:
        """The grouped attribute fragment. A query filters every group
        and renders all of them eagerly opened."""
        group_counts, inline = await self._grouped_attributes(build["id"], q)
        return await self.ctx.render(
            "_attributes.html",
            request=request,
            project=project,
            build=build,
            group_counts=group_counts,
            inline=inline,
            q=q,
        )

    async def attribute_history(
        self, request: Request, forge: str, owner: str, name: str, attr: str
    ) -> HTMLResponse:
        ctx = self.ctx
        project = await ctx.repo_or_404(forge, owner, name, request)
        return await ctx.render(
            "attribute_history.html",
            request=request,
            project=project,
            attr=attr,
            history=await ctx.queries.attribute_history(project["id"], attr),
        )

    async def badge(
        self, request: Request, forge: str, owner: str, name: str, branch: str = ""
    ) -> Response:
        """SVG status badge for the latest build on a branch (default:
        the repo's default branch). Private repos 404 for anonymous
        callers like every other route, so status never leaks."""
        ctx = self.ctx
        project = await ctx.repo_or_404(forge, owner, name, request)
        builds = await ctx.queries.builds_for_repo(
            project["id"],
            limit=1,
            filters=BuildFilters(branch=branch or project["default_branch"]),
        )
        status = builds.items[0]["status"] if builds.items else None
        message, color = message_for(status)
        svg = badge.render(ctx.env, "nixbot", message, color)
        return Response(
            svg,
            media_type="image/svg+xml",
            # Short cache: reflect new builds quickly without refetching
            # on every README load.
            headers={"Cache-Control": "max-age=60"},
        )

    async def health(self) -> PlainTextResponse:
        if not await check_db_health(self.ctx.pool):
            return PlainTextResponse("database unavailable", status_code=503)
        return PlainTextResponse("ok")

    async def llms_txt(self) -> PlainTextResponse:
        return PlainTextResponse(LLMS_TXT)


def create_router(ctx: WebContext) -> APIRouter:
    router = APIRouter()
    pages = _PageRoutes(ctx)
    html_pages: list[tuple[str, Callable[..., Any]]] = [
        ("/", pages.index),
        ("/builds", pages.activity),
        ("/builds/rows", pages.activity_rows),
        ("/builds/queue", pages.queue_fragment),
        ("/repos/{forge}/{owner:owner}/{name:segment}", pages.repo_page),
        ("/repos/{forge}/{owner:owner}/{name:segment}/rows", pages.repo_rows),
        (
            "/repos/{forge}/{owner:owner}/{name:segment}/builds/{number}",
            pages.build_page,
        ),
        (
            "/repos/{forge}/{owner:owner}/{name:segment}/builds/{number}/attrs",
            pages.attribute_rows,
        ),
        # :path — attribute names may contain slashes.
        (
            "/repos/{forge}/{owner:owner}/{name:segment}/attrs/{attr:path}",
            pages.attribute_history,
        ),
    ]
    for path, handler in html_pages:
        router.get(path, response_class=HTMLResponse)(handler)
    router.get("/repos/{forge}/{owner:owner}/{name:segment}/badge.svg")(pages.badge)
    router.get("/health", response_class=PlainTextResponse)(pages.health)
    router.get("/llms.txt", response_class=PlainTextResponse)(pages.llms_txt)
    return router


LLMS_TXT = """\
# nixbot

Nix CI. Every build evaluates a flake and builds each derivation
("attribute") of `.#checks`. JSON API under /api (OpenAPI spec:
/api/openapi.json). All endpoints support unauthenticated GET unless
the instance restricts project visibility.

## Answering "why did build X / commit Y fail?"

- GET /api/repos -> [{owner, name, ...}]
- GET /api/repos/{forge}/{owner}/{name}/builds?commit={sha-prefix} -> find the build number
  (other filters: status, branch, pr_number, page)
- GET /api/repos/{forge}/{owner}/{name}/builds/{number}/failures?tail=N
  -> {status, error, eval_warnings, failures: [{attr, status, error, log_tail}]}
  (tail defaults to 50 lines per attribute)

## Other endpoints

- GET /api/repos/{forge}/{owner}/{name} -> single project
- GET /api/repos/{forge}/{owner}/{name}/builds/{number} -> build + all attributes
- GET /api/repos/{forge}/{owner}/{name}/attrs/{attr} -> per-attribute history
- GET /api/queue -> global build queue
- GET /api/repos/{forge}/{owner}/{name}/builds/{number}/logs/{attr}
  -> log table of contents: derivations with drv path, status, phases,
     line counts and timings
- GET /api/repos/{forge}/{owner}/{name}/builds/{number}/logs/{attr}/text?tail=N&drv=<path|name>&ansi=1
  -> plain-text log (whole attribute, or one derivation via drv=)
- GET /api/repos/{forge}/{owner}/{name}/builds/{number}/logs/{attr}/stream
  -> SSE while the attribute runs: `state` snapshot (no history), then
     drv/line/phase/drv-done deltas with raw text, `done` at the end
- GET /api/events?build=N -> SSE build/attribute status-change cues
- GET /repos/{forge}/{owner}/{name}/badge.svg?branch=B
  -> SVG build-status badge for a branch (default branch when branch is omitted)

## Control (Authorization: Bearer <token>; create tokens at /settings)

- POST /api/repos/{forge}/{owner}/{name}/builds/{number}/restart
- POST /api/repos/{forge}/{owner}/{name}/builds/{number}/cancel
- POST /api/repos/{forge}/{owner}/{name}/builds/{number}/attrs/{attr}/restart
- POST /api/repos/{forge}/{owner}/{name}/builds/{number}/attrs/{attr}/cancel
- POST /api/repos/{forge}/{owner}/{name}/builds/{number}/effects/restart
- POST /api/repos/{forge}/{owner}/{name}/enable (admin)
- POST /api/repos/{forge}/{owner}/{name}/disable (admin)

## Command line

The `nbo` CLI (package `nixbot-cli`) wraps this API: repo/build
listing, restart/cancel, failure summaries, log tails and live
following. See docs/CLI.md in the nixbot repository.
"""


def create_app(
    pool: asyncpg.Pool,
    state_dir: Path | None = None,
    log_registry: LogRegistry | None = None,
    task_tokens: TaskTokens | None = None,
) -> FastAPI:
    broker = EventBroker(pool)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await broker.start()
        try:
            yield
        finally:
            await broker.stop()

    app = FastAPI(title="nixbot", openapi_url="/api/openapi.json", lifespan=lifespan)
    ctx = WebContext(pool, state_dir)
    app.state.web_context = ctx
    # Exposed so tests can inject NOTIFY payloads.
    app.state.event_broker = broker

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        wants_html = "text/html" in request.headers.get(
            "accept", ""
        ) and not request.url.path.startswith("/api/")
        if not wants_html:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        detail = exc.detail or HTTPStatus(exc.status_code).phrase
        page = await ctx.render(
            "error.html", request=request, status_code=exc.status_code, detail=detail
        )
        page.status_code = exc.status_code
        return page

    # Only /api belongs in the OpenAPI spec; HTML pages, SSE, logs and
    # metrics are excluded so /docs documents just the JSON API.
    app.include_router(create_events_router(ctx, broker), include_in_schema=False)
    app.include_router(create_router(ctx), include_in_schema=False)
    registry = log_registry or LogRegistry()
    app.state.log_registry = registry
    app.include_router(create_log_router(ctx, registry), include_in_schema=False)
    app.include_router(create_log_api_router(ctx, registry))
    app.include_router(create_metrics_router(pool), include_in_schema=False)
    if state_dir is not None:
        app.include_router(
            create_state_router(state_dir, task_tokens or TaskTokens()),
            include_in_schema=False,
        )
    app.include_router(create_api_router(ctx))
    # Last: the legacy catch-alls must not shadow real routes.
    app.include_router(create_legacy_router(ctx), include_in_schema=False)
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")
    return app
