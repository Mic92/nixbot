"""Gitea webhook auto-registration.

When a project is enabled, the service stores a per-repository secret
(hook_secrets.py) and registers a webhook pointing at
`<webhook_base_url>/webhooks/gitea`.
The webhook base URL may differ from the UI URL (`webhookBaseUrl`).

Leftover buildbot-era webhooks are removed only when their URL matches
this instance's own configured webhook base URL — never "anything that
looks like buildbot".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .hook_registration import register_hook

if TYPE_CHECKING:
    import httpx

    from .forge import GiteaClient
    from .hook_secrets import WebhookSecrets

logger = logging.getLogger(__name__)

LEGACY_HOOK_PATH = "/change_hook/gitea"
HOOK_PATH = "/webhooks/gitea"


def hook_url(webhook_base_url: str) -> str:
    return webhook_base_url.rstrip("/") + HOOK_PATH


def legacy_hook_urls(webhook_base_url: str) -> set[str]:
    base = webhook_base_url.rstrip("/")
    # Old buildbot deployments registered with or without trailing slash.
    return {base + LEGACY_HOOK_PATH, base + LEGACY_HOOK_PATH + "/"}


async def register_repo_hook(  # noqa: PLR0913
    client: GiteaClient,
    secrets_store: WebhookSecrets,
    project_id: int,
    owner: str,
    repo: str,
    webhook_base_url: str,
) -> None:
    """Idempotently register our webhook. Remove legacy buildbot hooks
    only when they point at our own webhook base URL."""
    secret = await secrets_store.get_or_create(project_id)
    target_url = hook_url(webhook_base_url)
    legacy_urls = legacy_hook_urls(webhook_base_url)
    api = f"{client.instance_url}/api/v1/repos/{owner}/{repo}/hooks"
    repo_name = f"{owner}/{repo}"

    hook_body = {
        "name": "web",
        "active": True,
        # "pull_request" alone does not cover pushes to an open
        # PR; Gitea delivers those as "pull_request_sync".
        "events": ["push", "pull_request", "pull_request_sync"],
        "type": "gitea",
        "config": {
            "url": target_url,
            "content_type": "json",
            "secret": secret,
        },
    }
    json_headers = {**client.auth_headers(), "Content-Type": "application/json"}

    async def remove_legacy_hooks(hooks: list[dict[str, Any]]) -> None:
        for hook in hooks:
            url = (hook.get("config") or {}).get("url", "")
            if url not in legacy_urls:
                continue
            logger.info(
                "removing legacy buildbot webhook",
                extra={"repo": repo_name, "url": url},
            )
            response = await client.http.delete(
                f"{api}/{hook['id']}", headers=client.auth_headers()
            )
            if response.status_code >= 400:  # noqa: PLR2004
                # The old hook keeps delivering until removed manually.
                logger.warning(
                    "failed to remove legacy buildbot webhook",
                    extra={
                        "repo": repo_name,
                        "url": url,
                        "status": response.status_code,
                    },
                )

    async def update_hook(existing_id: int) -> httpx.Response:
        return await client.http.patch(
            f"{api}/{existing_id}", headers=json_headers, json=hook_body
        )

    async def create_hook() -> httpx.Response:
        return await client.http.post(api, headers=json_headers, json=hook_body)

    await register_hook(
        repo_name=repo_name,
        target_url=target_url,
        list_hooks=lambda: client.paginated(f"{api}?limit=100"),
        hook_url_of=lambda hook: (hook.get("config") or {}).get("url", ""),
        update_hook=update_hook,
        create_hook=create_hook,
        permission_warning=(
            "no admin permission to manage webhooks; create one manually "
            "for push, pull_request and pull_request_sync events"
        ),
        cleanup=remove_legacy_hooks,
    )
