"""onEvent deliveries: match the default branch's `onEvent.<kind>`
effects against an event and queue the matching ones as effect runs
on the build the event is about. See docs/EFFECTS.md.

Runs service-side (needs forge clients for PR metadata and
permissions). The orchestrator only enqueues `deliver` work items.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from nixbot_effects import EffectError, EventEffectMeta
from nixbot_effects.match import expand_lock, skip_reason

from .db_gen import builds as builds_q
from .db_gen import events as ev_q
from .db_gen import work_queue as wq
from .effects import effects_context
from .forge import ForgeError
from .gitrepo import GitError
from .repos import repo_info
from .work_queue import TransientError

if TYPE_CHECKING:
    from .db import BuildRecord
    from .db_gen.models import Project
    from .events import RepoInfo
    from .gitrepo import FetchCredentials
    from .service import CIService

logger = logging.getLogger(__name__)


@dataclass
class Delivery:
    """An event to deliver, as stored in the work queue. Only what the
    webhook or build knew; PR metadata and permissions are looked up
    when it is delivered."""

    kind: str
    build_id: int
    actor: str | None = None
    pr_number: int | None = None
    command: str | None = None
    args: str | None = None
    previous_status: str | None = None
    failed_attrs: list[str] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class EventListing:
    code_rev: str
    effects: dict[str, dict[str, EventEffectMeta]]
    error: str | None = None


class EventListingCache:
    """The default branch's `onEvent` effects per project, evaluated once
    per default-branch rev. Deliveries in between reuse the result, so a
    project without onEvent pays one nix eval per push to the default
    branch. Evaluation errors are cached the same way."""

    def __init__(self) -> None:
        self._cache: dict[int, EventListing] = {}
        # One evaluation (and worktree) per project at a time.
        self._locks: dict[int, asyncio.Lock] = {}

    async def get(
        self,
        s: CIService,
        info: RepoInfo,
        code_rev: str,
        credentials: FetchCredentials | None,
    ) -> EventListing:
        lock = self._locks.setdefault(info.id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(info.id)
            if cached is None or cached.code_rev != code_rev:
                cached = await self._list(s, info, code_rev, credentials)
                self._cache[info.id] = cached
        return cached

    async def _list(
        self,
        s: CIService,
        info: RepoInfo,
        code_rev: str,
        credentials: FetchCredentials | None,
    ) -> EventListing:
        repos = s.orchestrator.repos
        worktree = await repos.checkout_for_build(
            info.key,
            f"event-listing-{info.id}",
            base_commit=code_rev,
            credentials=credentials,
        )
        try:
            ctx = effects_context(
                s.config,
                info,
                worktree_path=worktree.path,
                rev=code_rev,
                branch=info.default_branch,
                git_token=None,
                task_token=None,
            )
            try:
                return EventListing(
                    code_rev, await s.orchestrator.effects.list_all_event_effects(ctx)
                )
            except (EffectError, OSError) as e:
                logger.info(
                    "onEvent listing failed",
                    extra={"project": info.name, "rev": code_rev},
                )
                return EventListing(code_rev, {}, str(e))
        finally:
            await repos.remove_worktree(worktree)


def build_payload(base_url: str, info: RepoInfo, build: BuildRecord) -> dict[str, Any]:
    return {
        "id": build.id_,
        "number": build.number,
        "url": f"{base_url}/repos/{info.forge}/{info.name}/builds/{build.number}",
        "status": build.status,
        "branch": build.branch,
        "rev": build.commit_sha,
    }


def event_dedup_key(
    project_id: int, build_id: int, kind: str, name: str, lock: str | None
) -> str:
    # onPush effects use the same lock key, so a lock is shared across both.
    if lock is not None:
        return f"effect-lock-{project_id}-{lock}"
    return f"build-{build_id}-{kind}-{name}"


async def deliver(s: CIService, d: Delivery, *, last_attempt: bool = False) -> None:
    build = await builds_q.get_build(s.pool, id_=d.build_id)
    if build is None:
        return
    project = await s.repo_store.by_id(build.project_id)
    if project is None or not project.enabled:
        return
    info = repo_info(project)
    credentials = await s.credentials_provider(info.forge).get(info.clone_url)
    repos = s.orchestrator.repos
    try:
        await repos.fetch(
            info.key,
            info.clone_url,
            [f"+refs/heads/{info.default_branch}:refs/heads/{info.default_branch}"],
            credentials,
        )
    except GitError as e:
        raise TransientError(str(e)) from e
    code_rev = await repos.rev_parse(info.key, f"refs/heads/{info.default_branch}")
    cached = await s.event_listings.get(s, info, code_rev, credentials)
    listing = cached.effects.get(d.kind, {})
    await _record_listing_error(s, build, cached.error, code_rev)
    if not listing:
        return
    try:
        if d.command is not None and d.actor and await s.forge_pr.is_self(d.actor):
            return
        payload = await _payload(s, project, info, build, d)
        if "pullRequest" in payload and any(
            "modified" in m.when for m in listing.values()
        ):
            payload["modifiedFiles"] = await _modified_files(
                s, info, payload["pullRequest"], credentials
            )
    except ForgeError as e:
        if e.transient and not last_attempt:
            raise TransientError(str(e)) from e
        await _fail_listing(s, d, build, listing, code_rev, e)
        return
    await ev_q.supersede_event_effects(
        s.pool,
        project_id=project.id_,
        kind=d.kind,
        build_id=build.id_,
        pr_number=d.pr_number,
        branch=build.branch,
        command=d.command,
    )
    await _match_and_enqueue(s, d, info, build, listing, payload, code_rev)


async def _record_listing_error(
    s: CIService, build: BuildRecord, error: str | None, code_rev: str
) -> None:
    """Show a broken onEvent on the build page instead of silently
    delivering nothing."""
    if error is None:
        await builds_q.clear_effect_eval_error(
            s.pool, build_id=build.id_, source="delivery"
        )
        return
    await builds_q.record_effect_eval_error(
        s.pool,
        build_id=build.id_,
        source="delivery",
        error=error[-8000:],
        code_rev=code_rev,
    )


async def _fail_listing(  # noqa: PLR0913
    s: CIService,
    d: Delivery,
    build: BuildRecord,
    listing: dict[str, EventEffectMeta],
    code_rev: str,
    error: Exception,
) -> None:
    """The forge stayed broken. Record why nothing ran rather than
    matching with missing data, which would look like "no permission"."""
    logger.warning(
        "delivery failed", extra={"build_id": build.id_, "error": str(error)}
    )
    for name in listing:
        await ev_q.upsert_event_effect(
            s.pool,
            build_id=build.id_,
            kind=d.kind,
            name=name,
            status="failed",
            skip_reason=f"forge lookup failed: {error}",
            payload="{}",
            code_rev=code_rev,
            actor=d.actor,
            lock=None,
        )


async def _match_and_enqueue(  # noqa: PLR0913
    s: CIService,
    d: Delivery,
    info: RepoInfo,
    build: BuildRecord,
    listing: dict[str, EventEffectMeta],
    payload: dict[str, Any],
    code_rev: str,
) -> None:
    names: list[str] = []
    keys: list[str] = []
    # Effects still running from an earlier event. A /command for one
    # of them gets a reply instead of silently doing nothing.
    busy: list[str] = []
    payload_json = json.dumps(payload)
    for name, meta in listing.items():
        reason = skip_reason(meta.when, payload)
        lock = None
        try:
            lock = expand_lock(meta.lock, payload)
        except EffectError as e:
            reason = str(e)
        run_id = await ev_q.upsert_event_effect(
            s.pool,
            build_id=build.id_,
            kind=d.kind,
            name=name,
            status="skipped" if reason else "pending",
            skip_reason=reason,
            payload=payload_json,
            code_rev=code_rev,
            actor=d.actor,
            lock=lock,
        )
        if run_id is None:
            if not reason:
                busy.append(name)
            continue
        if reason:
            continue
        names.append(name)
        keys.append(event_dedup_key(build.project_id, build.id_, d.kind, name, lock))
    if names:
        await wq.enqueue_effect_items(
            s.pool, build_id=build.id_, kind=d.kind, names=names, dedup_keys=keys
        )
        s.wake_work()
    if busy and d.command is not None and d.pr_number is not None:
        try:
            await s.forge_pr.comment(
                info,
                d.pr_number,
                f"`/{d.command}`: {', '.join(busy)} still running, "
                "comment again once it finished.",
                None,
            )
        except ForgeError:
            logger.warning("busy notice failed", exc_info=True)
    logger.info(
        "event delivered",
        extra={
            "kind": d.kind,
            "build_id": build.id_,
            "queued": names,
            "skipped": len(listing) - len(names),
        },
    )


async def _modified_files(
    s: CIService,
    info: RepoInfo,
    pr: dict[str, Any],
    credentials: FetchCredentials | None,
) -> list[str]:
    """The PR's changed files for `when.modified`, from the mirror."""
    base = f"refs/heads/{pr['baseRef']}"
    try:
        await s.orchestrator.repos.fetch(
            info.key, info.clone_url, [f"+{base}:{base}"], credentials
        )
        return await s.orchestrator.repos.changed_files(info.key, base, pr["headRev"])
    except GitError as e:
        raise TransientError(str(e)) from e


async def _payload(
    s: CIService, project: Project, info: RepoInfo, build: BuildRecord, d: Delivery
) -> dict[str, Any]:
    payload: dict[str, Any] = {"build": build_payload(s.config.url, info, build)}
    if d.previous_status is not None:
        payload["build"]["previousStatus"] = d.previous_status
    if d.failed_attrs is not None:
        payload["build"]["failedAttrs"] = d.failed_attrs
    if d.actor:
        payload["actor"] = {
            "name": d.actor,
            "permission": await s.forge_pr.permission(project, d.actor),
        }
    if d.pr_number is not None:
        pr = await s.forge_pr.pull_request(info, d.pr_number)
        payload["pullRequest"] = pr.payload(
            await s.forge_pr.permission(project, pr.author)
        )
    if d.command is not None:
        payload["command"] = d.command
        payload["args"] = d.args or ""
    return payload
