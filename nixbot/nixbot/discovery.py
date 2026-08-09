"""Repository discovery and synchronization: forge repo listing,
project-table sync, webhook auto-registration, and the startup
reconciliation of heads missed while the service was down.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .forge import filter_repos
from .gitea_hooks import register_repo_hook
from .gitlab_hooks import register_repo_hook as register_gitlab_repo_hook
from .hook_secrets import WebhookSecrets
from .reconcile import (
    gitea_heads,
    github_heads,
    gitlab_heads,
    max_pr_updated,
    reconcile_repo,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .config import RepoFilters
    from .forge import DiscoveredRepo, GitHubAppClient
    from .repos import RepoRecord
    from .service import CIService

logger = logging.getLogger(__name__)


async def _reconcile_project(s: CIService, project: RepoRecord) -> None:
    try:
        watermark = await s.repo_store.reconcile_watermark(project.id)
        if project.forge == "github" and s.github is not None:
            heads = await github_heads(s.github, project, watermark)
        elif project.forge == "gitea" and s.gitea is not None:
            heads = await gitea_heads(s.gitea, project, watermark)
        elif project.forge == "gitlab" and s.gitlab is not None:
            heads = await gitlab_heads(s.gitlab, project, watermark)
        else:
            return
        await reconcile_repo(s.pool, project, heads, s)
        # Only after all submits succeeded: a crash mid-reconcile
        # must retry the same window on the next startup.
        new_watermark = max_pr_updated(heads)
        if new_watermark is not None:
            await s.repo_store.set_reconcile_watermark(project.id, new_watermark)
    except Exception:
        logger.exception(
            "reconciliation failed",
            extra={"project": f"{project.owner}/{project.name}"},
        )


async def reconcile_once(s: CIService) -> None:
    """Build default-branch and open-PR heads that got no build
    record while the service was down (missed webhooks). The
    per-project watermark bounds the PR listing to PRs updated
    since the last successful reconcile."""
    for project in await s.repo_store.enabled_repos():
        await _reconcile_project(s, project)


async def _warn_github_webhook_misconfig(s: CIService, github: GitHubAppClient) -> None:
    try:
        base = s.config.webhook_base_url or s.config.url
        for problem in await github.check_app_webhook(base):
            logger.warning("github app misconfigured: %s", problem)
    except Exception:
        logger.exception("github app webhook check failed")


async def discover_once(s: CIService) -> None:
    if s.config.pull_based is not None:
        await s.repo_store.sync_pull_based(
            [
                (repo.name, repo.url, repo.default_branch)
                for repo in s.config.pull_based.repositories.values()
            ]
        )
        await s.repo_store.prune_missing_disabled(
            "pull_based", list(s.config.pull_based.repositories)
        )
    # None marks a failed forge: its rows are neither synced nor pruned.
    by_forge: dict[str, list[DiscoveredRepo] | None] = {}
    # The topic is only a legacy import aid (one-shot enablement in
    # sync_discovered). It must not hard-filter discovery, otherwise
    # untagged repos never appear in the admin UI.
    if s.github is not None and s.config.github is not None:
        await _warn_github_webhook_misconfig(s, s.github)
        by_forge["github"] = await _discover_forge(
            "github", s.github.discover_repos(), s.config.github.filters
        )
    if s.gitea is not None and s.config.gitea is not None:
        # Only the one-shot legacy import needs topics.
        fetch_topics = (
            s.config.gitea.filters.topic is not None and await s.repo_store.is_empty()
        )
        by_forge["gitea"] = await _discover_forge(
            "gitea",
            s.gitea.discover_repos(fetch_topics=fetch_topics),
            s.config.gitea.filters,
        )
    if s.gitlab is not None and s.config.gitlab is not None:
        by_forge["gitlab"] = await _discover_forge(
            "gitlab", s.gitlab.discover_repos(), s.config.gitlab.filters
        )
    repos = [r for found in by_forge.values() if found is not None for r in found]
    topics = {
        forge: forge_config.filters.topic
        for forge, forge_config in (
            ("github", s.config.github),
            ("gitea", s.config.gitea),
            ("gitlab", s.config.gitlab),
        )
        if forge_config is not None and forge_config.filters.topic is not None
    }
    await s.repo_store.sync_discovered(repos, legacy_import_topics=topics)
    # Drop disabled repos a successful discovery no longer returned so
    # the admin toggle list stays clean. A failed forge is skipped to
    # avoid mass-deleting rows on a transient API error.
    for forge, found in by_forge.items():
        if found is not None:
            await s.repo_store.prune_missing_disabled(
                forge, [r.forge_repo_id for r in found]
            )
    # Auto-register Gitea/GitLab webhooks for enabled projects.
    await register_hooks(s)


async def _discover_forge(
    forge: str,
    discovery: Awaitable[list[DiscoveredRepo]],
    filters: RepoFilters,
) -> list[DiscoveredRepo] | None:
    """One forge failing must not abort discovery for the others;
    returns None on failure so that forge is not pruned."""
    try:
        return filter_repos(replace(filters, topic=None), await discovery)
    except Exception:
        logger.exception("%s repo discovery failed", forge)
        return None


def _hook_registrars(
    s: CIService,
) -> dict[str, tuple[Any, Callable[..., Awaitable[None]]]]:
    registrars: dict[str, tuple[Any, Callable[..., Awaitable[None]]]] = {}
    if s.gitea is not None:
        registrars["gitea"] = (s.gitea, register_repo_hook)
    if s.gitlab is not None:
        registrars["gitlab"] = (s.gitlab, register_gitlab_repo_hook)
    return registrars


async def _register_project_hook(
    s: CIService,
    project: RepoRecord,
    registrars: dict[str, tuple[Any, Callable[..., Awaitable[None]]]],
) -> None:
    if project.forge not in registrars:
        return
    client, register = registrars[project.forge]
    base = s.config.webhook_base_url or s.config.url
    try:
        await register(
            client,
            WebhookSecrets(s.pool, project.forge),
            project.id,
            project.owner,
            project.name,
            base,
        )
    except Exception:
        logger.exception(
            "%s hook registration failed",
            project.forge,
            extra={"project": f"{project.owner}/{project.name}"},
        )


async def register_hooks(s: CIService) -> None:
    registrars = _hook_registrars(s)
    for project in await s.repo_store.enabled_repos():
        await _register_project_hook(s, project, registrars)


async def activate_project(s: CIService, project_id: int) -> None:
    """Register the webhook and reconcile a single, freshly enabled
    project so it starts building without waiting for the next
    discovery cycle or a service restart (issue #82)."""
    project = await s.repo_store.by_id(project_id)
    if project is None or not project.enabled:
        return
    await _register_project_hook(s, project, _hook_registrars(s))
    await _reconcile_project(s, project)
