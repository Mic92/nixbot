"""Hercules-style effects execution, driving the nixbot_effects library.

Policy lives here, mechanics in nixbot_effects:

- effects run per the DEFAULT BRANCH's repo config: default branch
  always; PRs when `effects_on_pull_requests`. Branches matching
  `effects_branches` globs,
- per-repo secret resolution supports exact `forge:owner/repo` and org
  wildcard `forge:owner/*` entries,
- deploy secrets and tokens are redacted from everything the effect
  writes to its log,
- every run is capped by a timeout and killed as a whole process group.

The orchestrator sets the build's effects started-flag
before invoking run_effect and never auto-re-runs effects on crash
recovery (deploys are not idempotent).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import nixbot_effects
from nixbot_effects import EffectError, EffectMeta, EffectsOptions

from .redact import Redactor, secret_literals

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from .config import Config
    from .events import RepoInfo
    from .repo_config import BranchConfig

    LogWrite = Callable[[bytes], Awaitable[None]]

logger = logging.getLogger(__name__)

# Deploys can hang on the network. Same cap as attribute builds.
DEFAULT_TIMEOUT = 60 * 60 * 3


def effect_push_url(forge: str, clone_url: str, token: str) -> str | None:
    """Token-authenticated https URL for the effect checkout's origin.
    None for non-http clone URLs (nothing sensible to push to)."""
    parts = urlsplit(clone_url)
    if parts.scheme not in ("http", "https") or parts.hostname is None:
        return None
    user = "oauth2" if forge == "gitlab" else "x-access-token"
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{user}:{token}@{host}{parts.path}"


def resolve_effects_secret(
    per_repo_effects_secrets: dict[str, str],
    forge_type: str,
    owner: str,
    repo: str,
) -> str | None:
    """Resolve effects secret, either repo-specific or org wildcard."""
    secret_name = per_repo_effects_secrets.get(f"{forge_type}:{owner}/{repo}")
    if secret_name is None:
        secret_name = per_repo_effects_secrets.get(f"{forge_type}:{owner}/*")
    return secret_name


def should_run_effects(
    default_branch_config: BranchConfig,
    default_branch: str,
    branch: str,
    *,
    is_pull_request: bool,
) -> bool:
    """Effects scope follows the default branch's repo config.

    PRs first: webhooks store the PR's BASE ref in `branch`, so a PR
    targeting the default branch must not match the default-branch rule.
    """
    if is_pull_request:
        return default_branch_config.effects_on_pull_requests
    if branch == default_branch:
        return True
    return any(
        fnmatch(branch, pattern) for pattern in default_branch_config.effects_branches
    )


@dataclass
class EffectsContext(EffectsOptions):
    """The library options plus the daemon-side secret reference."""

    # systemd credential holding this repo's deploy secrets JSON. Read
    # per run, so a misconfigured credential fails that effect's log
    # instead of effects discovery.
    secret_name: str | None = None


def effects_context(  # noqa: PLR0913
    config: Config,
    info: RepoInfo,
    *,
    worktree_path: Path,
    rev: str,
    branch: str,
    git_token: str | None,
    task_token: str | None,
) -> EffectsContext:
    """Context with the service-level configuration filled in. Shared
    by push and scheduled effect runs."""
    return EffectsContext(
        path=worktree_path,
        rev=rev,
        branch=branch,
        repo=info.name,
        project_path=info.name,
        secret_name=resolve_effects_secret(
            config.effects_per_repo_secrets, info.forge, info.owner, info.repo
        ),
        extra_sandbox_paths=config.effects_extra_sandbox_paths,
        default_branch=info.default_branch,
        git_token=git_token,
        task_token=task_token,
        api_base_url=config.url,
        project_id=str(info.id),
        mountables_file=config.effects_mountables_file,
        extra_nix_options=list(config.effects_extra_nix_options.items()),
    )


def _read_secret_file(secret_name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory is None:
        msg = (
            f"effects secret {secret_name!r} requested but "
            "$CREDENTIALS_DIRECTORY is not set"
        )
        raise EffectError(msg)
    return (Path(directory) / secret_name).read_text()


async def _with_timeout[T](coro: Coroutine[None, None, T], what: str) -> T:
    try:
        async with asyncio.timeout(DEFAULT_TIMEOUT):
            return await coro
    except TimeoutError as e:
        msg = f"{what} timed out after {DEFAULT_TIMEOUT}s"
        raise EffectError(msg) from e


async def list_effects(ctx: EffectsContext) -> dict[str, EffectMeta]:
    """The effects defined by the flake, with their after/lock metadata."""
    return await _with_timeout(nixbot_effects.list_effects(ctx), "listing effects")


async def list_all_event_effects(
    ctx: EffectsContext,
) -> dict[str, dict[str, nixbot_effects.EventEffectMeta]]:
    """onEvent definitions on the default branch, by kind."""
    return await _with_timeout(
        nixbot_effects.list_all_event_effects(ctx), "listing event effects"
    )


async def list_scheduled_effects(ctx: EffectsContext) -> dict:
    """The onSchedule definitions on the default branch."""
    return await _with_timeout(
        nixbot_effects.list_scheduled_effects(ctx), "listing scheduled effects"
    )


def _prepare_run(ctx: EffectsContext, log_write: LogWrite | None) -> None:
    """Load the deploy secrets and wrap the log sink with redaction of
    the secrets and the git and task tokens."""
    secret_content: str | None = None
    if ctx.secret_name is not None:
        secret_content = _read_secret_file(ctx.secret_name)
        ctx.secrets = json.loads(secret_content)
    sink = log_write
    literals = secret_literals(secret_content, ctx.git_token, ctx.task_token)
    if literals and sink is not None:
        redactor = Redactor(literals)
        raw_sink = sink

        async def sink(data: bytes) -> None:
            await raw_sink(redactor(data))

    ctx.log = sink


async def _run_wrapped(
    ctx: EffectsContext,
    log_write: LogWrite | None,
    run: Callable[[], Coroutine[None, None, None]],
) -> bool:
    """Run one (push or scheduled) effect with redaction and timeout.
    Returns success. Failures are written to the log."""
    _prepare_run(ctx, log_write)
    try:
        await _with_timeout(run(), "effect")
    except EffectError as e:
        if ctx.log is not None:
            await ctx.log(f"error: {e}\n".encode())
        return False
    return True


async def run_effect(
    ctx: EffectsContext,
    effect: str,
    log_write: LogWrite | None = None,
) -> bool:
    return await _run_wrapped(
        ctx, log_write, lambda: nixbot_effects.run_effect(ctx, effect)
    )


async def run_event_effect(
    ctx: EffectsContext,
    kind: str,
    effect: str,
    payload: dict,
    log_write: LogWrite | None = None,
) -> bool:
    return await _run_wrapped(
        ctx,
        log_write,
        lambda: nixbot_effects.run_event_effect(ctx, kind, effect, payload),
    )


async def run_scheduled_effect(
    ctx: EffectsContext,
    schedule_name: str,
    effect: str,
    log_write: LogWrite | None = None,
) -> bool:
    return await _run_wrapped(
        ctx,
        log_write,
        lambda: nixbot_effects.run_scheduled_effect(ctx, schedule_name, effect),
    )


class EffectsBackend(Protocol):
    """How the orchestrator evaluates and runs effects. Tests fake it."""

    async def list_effects(self, ctx: EffectsContext) -> dict[str, EffectMeta]: ...
    async def list_all_event_effects(
        self, ctx: EffectsContext
    ) -> dict[str, dict[str, nixbot_effects.EventEffectMeta]]: ...
    async def list_scheduled_effects(self, ctx: EffectsContext) -> dict: ...
    async def run_effect(
        self, ctx: EffectsContext, effect: str, log_write: LogWrite | None = None
    ) -> bool: ...
    async def run_event_effect(
        self,
        ctx: EffectsContext,
        kind: str,
        effect: str,
        payload: dict,
        log_write: LogWrite | None = None,
    ) -> bool: ...
    async def run_scheduled_effect(
        self,
        ctx: EffectsContext,
        schedule_name: str,
        effect: str,
        log_write: LogWrite | None = None,
    ) -> bool: ...


class NixEffects:
    list_effects = staticmethod(list_effects)
    list_all_event_effects = staticmethod(list_all_event_effects)
    list_scheduled_effects = staticmethod(list_scheduled_effects)
    run_effect = staticmethod(run_effect)
    run_event_effect = staticmethod(run_event_effect)
    run_scheduled_effect = staticmethod(run_scheduled_effect)
