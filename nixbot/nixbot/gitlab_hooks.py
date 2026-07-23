"""GitLab webhook auto-registration.

Same flow as gitea_hooks.py: the service stores a per-repository secret
(hook_secrets.py) and registers a webhook pointing at
`<webhook_base_url>/webhooks/gitlab`. Hook management needs Maintainer
on the project. Without it the hook must be created manually.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .hook_registration import register_hook

if TYPE_CHECKING:
    import httpx

    from .forge import GitlabClient
    from .hook_secrets import WebhookSecrets

logger = logging.getLogger(__name__)

HOOK_PATH = "/webhooks/gitlab"


def hook_url(webhook_base_url: str) -> str:
    return webhook_base_url.rstrip("/") + HOOK_PATH


async def register_repo_hook(  # noqa: PLR0913
    client: GitlabClient,
    secrets_store: WebhookSecrets,
    project_id: int,
    owner: str,
    repo: str,
    webhook_base_url: str,
) -> None:
    """Idempotently register our webhook."""
    secret = await secrets_store.get_or_create(project_id)
    target_url = hook_url(webhook_base_url)
    api = client.project_api_url(owner, repo)

    hook_body = {
        "url": target_url,
        "token": secret,
        "push_events": True,
        "merge_requests_events": True,
        "enable_ssl_verification": True,
    }

    async def update_hook(existing_id: int) -> httpx.Response:
        return await client.http.put(
            f"{api}/hooks/{existing_id}",
            headers=client.auth_headers(),
            json=hook_body,
        )

    async def create_hook() -> httpx.Response:
        return await client.http.post(
            f"{api}/hooks", headers=client.auth_headers(), json=hook_body
        )

    await register_hook(
        repo_name=f"{owner}/{repo}",
        target_url=target_url,
        list_hooks=lambda: client.paginated(f"{api}/hooks"),
        hook_url_of=lambda hook: hook.get("url", ""),
        update_hook=update_hook,
        create_hook=create_hook,
        permission_warning=(
            "no maintainer permission to manage webhooks; create one "
            "manually for push and merge request events"
        ),
    )
