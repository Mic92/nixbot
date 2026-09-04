"""forge_pr against a mocked Gitea API: PR metadata mapping and
marker-based comment replacement."""

from __future__ import annotations

import json

import httpx

from nixbot.events import RepoInfo
from nixbot.forge import GiteaClient
from nixbot.forge_pr import ForgePrClient

INFO = RepoInfo(
    id=1,
    key="gitea/acme/app",
    name="acme/app",
    owner="acme",
    repo="app",
    forge="gitea",
    clone_url="https://gitea.example.com/acme/app.git",
    default_branch="main",
)


def _client(handler: httpx.MockTransport) -> ForgePrClient:
    gitea = GiteaClient(
        "https://gitea.example.com", "tok", http=httpx.AsyncClient(transport=handler)
    )
    return ForgePrClient(None, gitea, None)


async def test_pull_request_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/repos/acme/app/pulls/7"
        return httpx.Response(
            200,
            json={
                "number": 7,
                "title": "t",
                "html_url": "u",
                "state": "open",
                "merged": False,
                "draft": True,
                "user": {"login": "dave"},
                "labels": [{"name": "preview"}],
                "head": {
                    "sha": "abc",
                    "ref": "feat",
                    "repo": {"full_name": "dave/app"},
                },
                "base": {"ref": "main", "repo": {"full_name": "acme/app"}},
            },
        )

    pr = await _client(httpx.MockTransport(handler)).pull_request(INFO, 7)
    assert (pr.author, pr.is_fork, pr.labels, pr.head_rev, pr.draft, pr.open) == (
        "gitea:dave",
        True,
        ("preview",),
        "abc",
        True,
        True,
    )


async def test_comment_replaces_marked_comment() -> None:
    calls: list[tuple[str, str, dict]] = []
    comments = [
        {"id": 1, "body": "unrelated"},
        {"id": 2, "body": "old plan\n<!-- nixbot:plan -->"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(200, json=comments)
        return httpx.Response(200, json={})

    client = _client(httpx.MockTransport(handler))
    await client.comment(INFO, 7, "new plan", "plan")
    assert calls[-1] == (
        "PATCH",
        "/api/v1/repos/acme/app/issues/comments/2",
        {"body": "new plan\n<!-- nixbot:plan -->"},
    )
    # No marker or no match: a new comment.
    calls.clear()
    await client.comment(INFO, 7, "hi", None)
    await client.comment(INFO, 7, "first", "other")
    assert [(m, p) for m, p, _ in calls if m != "GET"] == [
        ("POST", "/api/v1/repos/acme/app/issues/7/comments"),
        ("POST", "/api/v1/repos/acme/app/issues/7/comments"),
    ]


async def test_is_self_uses_token_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/user"
        return httpx.Response(200, json={"login": "nixbot"})

    client = _client(httpx.MockTransport(handler))
    assert await client.is_self("gitea:NixBot")
    assert not await client.is_self("gitea:alice")
    # GitHub filters bot comments in the webhook parser.
    assert not await client.is_self("github:nixbot")
