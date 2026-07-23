"""Shared webhook registration skeleton for token-auth forges (Gitea,
GitLab): list hooks (403 -> warn), re-sync an existing hook in place or
create a new one. Per-forge API shapes come in as callables."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .forge import ForgeError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx

logger = logging.getLogger(__name__)


async def register_hook(  # noqa: PLR0913
    *,
    repo_name: str,
    target_url: str,
    list_hooks: Callable[[], Awaitable[list[dict[str, Any]]]],
    hook_url_of: Callable[[dict[str, Any]], str],
    update_hook: Callable[[int], Awaitable[httpx.Response]],
    create_hook: Callable[[], Awaitable[httpx.Response]],
    permission_warning: str,
    cleanup: Callable[[list[dict[str, Any]]], Awaitable[None]] | None = None,
) -> None:
    """Idempotently register a webhook pointing at `target_url`."""
    try:
        hooks = await list_hooks()
    except ForgeError as e:
        if e.status_code != 403:  # noqa: PLR2004
            raise
        # Hook management needs elevated repo permission. The project
        # still works if the webhook is created manually.
        logger.warning(permission_warning, extra={"repo": repo_name, "url": target_url})
        return
    if cleanup is not None:
        await cleanup(hooks)
    existing_id = next(
        (hook["id"] for hook in hooks if hook_url_of(hook) == target_url), None
    )
    if existing_id is not None:
        # The existing hook may carry a stale secret (e.g. after a
        # database reset) and the forge never exposes it. Re-sync in place.
        response = await update_hook(existing_id)
        if response.status_code >= 400:  # noqa: PLR2004
            logger.error(
                "failed to update webhook",
                extra={"repo": repo_name, "status": response.status_code},
            )
        return
    logger.info("registering webhook", extra={"repo": repo_name})
    response = await create_hook()
    if response.status_code >= 400:  # noqa: PLR2004
        logger.error(
            "failed to register webhook",
            extra={"repo": repo_name, "status": response.status_code},
        )
