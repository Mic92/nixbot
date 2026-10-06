"""Open a pull request for a pushed branch unless one is open already.

Runs in an effect with nixbot's checkout: the forge's host and the
repository come from its origin, the token from the effect's `token`
secret. Exits 0 when the pull request exists or there is nothing to
merge, as hercules-ci-effects' flake update does. With --auto-merge, a
new GitHub pull request gets auto-merge, or is merged right away when
it is already mergeable, as there.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


def origin() -> tuple[str, str]:
    """(base URL, owner/repo) of the checkout's origin. The configured
    URL, not `git remote get-url`, which would apply insteadOf."""
    url = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    path = parts.path.strip("/").removesuffix(".git")
    return f"{parts.scheme}://{host}", path


def token() -> str:
    with open(os.environ["HERCULES_CI_SECRETS_JSON"]) as f:
        return json.load(f)["token"]["data"]["token"]


def github_api(base: str) -> str:
    if base == "https://github.com":
        return "https://api.github.com"
    return f"{base}/api/v3"


def github_graphql(base: str) -> str:
    if base == "https://github.com":
        return "https://api.github.com/graphql"
    return f"{base}/api/graphql"


def send(url: str, headers: dict, body: dict, method: str = "POST") -> str:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method=method,
        headers={
            **headers,
            "Content-Type": "application/json",
            # Cloudflare rejects urllib's default user agent.
            "User-Agent": "nixbot-open-pr",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return response.read().decode(errors="replace")


def request(forge: str, base: str, path: str, args: argparse.Namespace):
    """URL, headers and body of the create request."""
    if forge == "github":
        api = github_api(base)
        body = {"title": args.title, "head": args.head, "base": args.base}
        if args.body is not None:
            body["body"] = args.body
        return (
            f"{api}/repos/{path}/pulls",
            {"Authorization": f"Bearer {token()}"},
            body,
        )
    if forge == "gitea":
        body = {"title": args.title, "head": args.head, "base": args.base}
        if args.body is not None:
            body["body"] = args.body
        return (
            f"{base}/api/v1/repos/{path}/pulls",
            {"Authorization": f"token {token()}"},
            body,
        )
    project = urllib.parse.quote(path, safe="")
    body = {
        "title": args.title,
        "source_branch": args.head,
        "target_branch": args.base,
    }
    if args.body is not None:
        body["description"] = args.body
    return (
        f"{base}/api/v4/projects/{project}/merge_requests",
        {"Authorization": f"Bearer {token()}"},
        body,
    )


def nothing_to_do(forge: str, status: int, detail: str) -> bool:
    """The pull request is open already, or there is nothing to merge."""
    if forge == "github":
        return status == 422 and (
            "already exists" in detail or "No commits between" in detail
        )
    return status == 409


def auto_merge(base: str, path: str, pr: dict, method: str) -> None:
    """Enable auto-merge on a new GitHub pull request, or merge it when
    GitHub says it is in clean status, as hercules-ci-effects does."""
    headers = {"Authorization": f"Bearer {token()}"}
    query = (
        "mutation ($id: ID!, $method: PullRequestMergeMethod!) {"
        " enablePullRequestAutoMerge(input: {pullRequestId: $id,"
        " mergeMethod: $method}) { clientMutationId } }"
    )
    variables = {"id": pr["node_id"], "method": method.upper()}
    body = {"query": query, "variables": variables}
    reply = json.loads(send(github_graphql(base), headers, body))
    errors = reply.get("errors") or []
    if not errors:
        print(f"Enabled auto-merge ({method}) on #{pr['number']}")
        return
    if any("clean status" in e.get("message", "") for e in errors):
        print(f"#{pr['number']} is already in clean status. Merging.")
        url = f"{github_api(base)}/repos/{path}/pulls/{pr['number']}/merge"
        print(send(url, headers, {"merge_method": method}, method="PUT"))
        return
    sys.exit(
        f"nixbot-open-pr: enabling auto-merge failed: {errors}\n"
        "Auto-merge must be allowed in the repository settings, and the "
        "base branch needs a protection rule with required checks."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forge", choices=["github", "gitea", "gitlab"])
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body")
    parser.add_argument("--auto-merge", choices=["merge", "rebase", "squash"])
    args = parser.parse_args()
    if args.auto_merge is not None and args.forge != "github":
        parser.error("--auto-merge needs --forge github")
    base, path = origin()
    url, headers, body = request(args.forge, base, path, args)
    try:
        reply = send(url, headers, body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if not nothing_to_do(args.forge, e.code, detail):
            sys.exit(f"nixbot-open-pr: {e}: {detail}")
        print(f"nixbot-open-pr: no new pull request needed: {detail}")
        return
    print(reply)
    if args.auto_merge is not None:
        auto_merge(base, path, json.loads(reply), args.auto_merge)


main()
