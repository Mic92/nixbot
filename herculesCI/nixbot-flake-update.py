"""Update flake.lock on a branch and open a pull request for it.

The effect behind effects-lib's `flakeUpdate`, configured by the JSON
file it is given (see flake-update.nix). It runs in nixbot's checkout,
fetches and pushes with the effect's `token` secret, and opens pull
requests on GitHub, Gitea and GitLab, as hercules-ci-effects'
flakeUpdate does. With `perInput`, each input gets its own branch and
pull request, as update-flake-inputs does.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class Failed(Exception):
    """The effect fails with this message."""


ERRORS = (Failed, subprocess.CalledProcessError, urllib.error.URLError)


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise Failed(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def has_ref(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


def token() -> str:
    with open(os.environ["HERCULES_CI_SECRETS_JSON"]) as f:
        return json.load(f)["token"]["data"]["token"]


class Forge:
    """The forge behind origin: its git remote and its pull request API."""

    def __init__(self, config: dict) -> None:
        self.kind = config["forgeType"]
        remote = config["gitRemote"] or git("config", "--get", "remote.origin.url")
        parts = urllib.parse.urlsplit(remote)
        self.host = parts.hostname or ""
        if parts.port:
            self.host += f":{parts.port}"
        self.url = f"{parts.scheme}://{self.host}"
        self.remote = parts._replace(netloc=self.host).geturl()
        self.path = parts.path.strip("/").removesuffix(".git")
        self.user = config["user"] or (
            "oauth2" if self.kind == "gitlab" else "x-access-token"
        )
        self.token = token()

    def setup_git(self) -> None:
        """Fetch and push with the token, not the checkout's. Its
        insteadOf entries would put that one in every forge URL."""
        keys = git(
            "config",
            "--local",
            "--name-only",
            "--get-regexp",
            r"^url\..*\.insteadof$",
            check=False,
        )
        # A key with several values is listed once per value.
        for key in dict.fromkeys(keys.splitlines()):
            git("config", "--unset-all", key)
        git("remote", "set-url", "origin", self.remote)
        # A credential store rather than a URL, so git cannot echo it.
        scheme = self.url.split("://", 1)[0]
        credentials = Path.home() / ".git-credentials"
        with credentials.open("a") as f:
            print(f"{scheme}://{self.user}:{self.token}@{self.host}", file=f)
        git("config", "--global", "credential.helper", "store")
        if self.kind == "github":
            conf = Path.home() / ".config/nix/nix.conf"
            conf.parent.mkdir(parents=True, exist_ok=True)
            with conf.open("a") as f:
                print(f"access-tokens = {self.host}={self.token}", file=f)

    def _send(self, url: str, body: dict | None, method: str = "POST") -> Any:
        scheme = "token" if self.kind == "gitea" else "Bearer"
        req = urllib.request.Request(
            url,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={
                "Authorization": f"{scheme} {self.token}",
                "Content-Type": "application/json",
                # Cloudflare rejects urllib's default user agent.
                "User-Agent": "nixbot-flake-update",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read() or b"{}")

    def _github_api(self) -> str:
        if self.url == "https://github.com":
            return "https://api.github.com"
        return f"{self.url}/api/v3"

    def _create_request(
        self, head: str, base: str, title: str, body: str
    ) -> tuple[str, dict]:
        if self.kind == "gitlab":
            project = urllib.parse.quote(self.path, safe="")
            return (
                f"{self.url}/api/v4/projects/{project}/merge_requests",
                {
                    "title": title,
                    "source_branch": head,
                    "target_branch": base,
                    "description": body,
                },
            )
        api = self._github_api() if self.kind == "github" else f"{self.url}/api/v1"
        return (
            f"{api}/repos/{self.path}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )

    def open_pull_request(
        self, head: str, base: str, title: str, body: str
    ) -> dict | None:
        """The new pull request, or None if one is open already or there
        is nothing to merge."""
        url, request = self._create_request(head, base, title, body)
        try:
            pr = self._send(url, request)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            exists = (
                e.code == 422 and ("already exists" in detail or "No commits" in detail)
                if self.kind == "github"
                else e.code == 409
            )
            if not exists:
                raise Failed(f"opening a pull request: {e}: {detail}") from e
            print(f"No new pull request needed: {detail}")
            return None
        print(f"Opened {pr.get('html_url') or pr.get('web_url') or pr['number']}")
        return pr

    def find_open_pull_request(self, head: str) -> dict | None:
        api = self._github_api() if self.kind == "github" else f"{self.url}/api/v1"
        query = "state=open"
        if self.kind == "github":
            owner = self.path.split("/", 1)[0]
            query += f"&head={urllib.parse.quote(f'{owner}:{head}')}"
        pulls = self._send(f"{api}/repos/{self.path}/pulls?{query}", None, "GET")
        return next((p for p in pulls if p["head"]["ref"] == head), None)

    def auto_merge(self, pr: dict, method: str) -> None:
        if self.kind == "gitea":
            self._gitea_auto_merge(pr, method)
        else:
            self._github_auto_merge(pr, method)

    def _gitea_auto_merge(self, pr: dict, method: str) -> None:
        url = f"{self.url}/api/v1/repos/{self.path}/pulls/{pr['number']}/merge"
        request = {
            "Do": method,
            "merge_when_checks_succeed": True,
            "delete_branch_after_merge": True,
        }
        # Gitea answers "Please try again later" while it computes the
        # pull request's mergeability.
        for _ in range(5):
            try:
                reply = self._send(url, request)
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")
                if e.code == 409:
                    print(f"#{pr['number']}: {detail}")
                    return
                raise Failed(f"enabling auto-merge: {e}: {detail}") from e
            if reply.get("message") != "Please try again later":
                print(f"Scheduled #{pr['number']} to merge ({method})")
                return
            time.sleep(2)
        raise Failed(f"Gitea kept #{pr['number']} from merging")

    def _github_auto_merge(self, pr: dict, method: str) -> None:
        """Enable auto-merge, or merge when GitHub says the pull request
        is in clean status, as hercules-ci-effects does."""
        if self.url == "https://github.com":
            graphql = "https://api.github.com/graphql"
        else:
            graphql = f"{self.url}/api/graphql"
        query = (
            "mutation ($id: ID!, $method: PullRequestMergeMethod!) {"
            " enablePullRequestAutoMerge(input: {pullRequestId: $id,"
            " mergeMethod: $method}) { clientMutationId } }"
        )
        variables = {"id": pr["node_id"], "method": method.upper()}
        reply = self._send(graphql, {"query": query, "variables": variables})
        errors = reply.get("errors") or []
        if not errors:
            print(f"Enabled auto-merge ({method}) on #{pr['number']}")
        elif any("clean status" in e.get("message", "") for e in errors):
            print(f"#{pr['number']} is already in clean status. Merging.")
            url = f"{self._github_api()}/repos/{self.path}/pulls/{pr['number']}/merge"
            self._send(url, {"merge_method": method}, method="PUT")
        else:
            raise Failed(
                f"enabling auto-merge failed: {errors}\nAuto-merge must "
                "be allowed in the repository settings, and the base "
                "branch needs a protection rule with required checks."
            )


def nix_flake_update(nix: str, path: str, inputs: list[str], summary: str) -> None:
    command = [
        nix,
        "--extra-experimental-features",
        "nix-command flakes",
        "flake",
        "update",
        *inputs,
        "--commit-lock-file",
    ]
    if summary:
        command += ["--commit-lockfile-summary", summary]
    # Answer "n" to accepting nixConfig, as hercules-ci-effects does.
    subprocess.run(command, cwd=path, input="n\n" * 100, text=True, check=True)


def post_update(config: dict) -> None:
    if config["postUpdate"]:
        print("Running postUpdate...", file=sys.stderr)
        subprocess.run([config["postUpdate"]], check=True)


def flake_inputs(rev: str, path: str, flake: dict) -> list[str]:
    """The inputs to update: those named, or the lock file's direct
    inputs, less the excluded ones."""
    inputs = flake.get("inputs") or []
    excluded = flake.get("excludeInputs") or []
    if not inputs and (excluded or rev != "HEAD"):
        lock = Path(path, "flake.lock").as_posix()
        if not has_ref(f"{rev}:{lock}"):
            print(f"Skipping {path}: it has no flake.lock", file=sys.stderr)
            return []
        locks = json.loads(git("show", f"{rev}:{lock}"))
        inputs = sorted(locks["nodes"][locks["root"]].get("inputs", {}))
    return [i for i in inputs if i not in excluded]


def identity(rev: str) -> str:
    """A commit, less its dates and people."""
    return git("log", "-1", "--format=%T%n%P%n%B", rev)


def merge_base(update_branch: str, base: str, method: str | None) -> None:
    """Check out the update branch: brought up to date with the base by
    `method`, or as it stands without one. A new branch, or one that is
    reset, starts from the base."""
    update = f"refs/remotes/origin/{update_branch}"
    if not has_ref(update) or method == "reset":
        git("checkout", "--quiet", "-B", update_branch, base)
        return
    git("checkout", "--quiet", "-B", update_branch, update)
    if method is None:
        return
    command = {
        "merge": ["merge", "--no-edit", base],
        "rebase": ["rebase", base],
        "fast-forward": ["merge", "--ff-only", base],
    }[method]
    if subprocess.run(["git", *command], check=False).returncode == 0:
        return
    if method == "fast-forward":
        raise Failed(
            f"{update_branch} has commits the base lacks, so it cannot "
            f"fast-forward. Delete it (git push origin :{update_branch})"
            " or merge it, or use another baseMergeMethod."
        )
    conflicts = git("diff", "--name-only", "--diff-filter=U")
    raise Failed(
        f"Resolve the conflicts by hand and push to {update_branch}:\n{conflicts}"
    )


def push(update_branch: str, before: str, force: str | None) -> bool:
    """Push the update branch if the update committed anything."""
    # Untracked files too: a file postUpdate generates but does not add.
    if changes := git("status", "--porcelain"):
        raise Failed(f"the update left uncommitted changes:\n{changes}")
    if git("rev-parse", "HEAD") == before:
        print(f"No updates to push to {update_branch}.")
        return False
    # A rebuilt branch differs from the pushed one only in its dates
    # unless an input moved: pushing it would rerun CI for nothing.
    remote = f"refs/remotes/origin/{update_branch}"
    if has_ref(remote) and identity("HEAD") == identity(remote):
        print(f"{update_branch} is up to date.")
        return True
    flags = [f"--{force}"] if force else []
    git("push", "origin", f"HEAD:refs/heads/{update_branch}", *flags)
    return True


def commit_bodies(before: str) -> str:
    """The bodies of the commits since `before`, oldest first: the lock
    file's, and those postUpdate made."""
    bodies = git("log", "--reverse", "--format=%b%x00", f"{before}..HEAD")
    return "\n\n".join(b.strip() for b in bodies.split("\0") if b.strip())


def open_pull_request(
    forge: Forge,
    config: dict,
    update_branch: str,
    base: str,
    title: str,
    body: str,
) -> None:
    update = f"refs/remotes/origin/{update_branch}"
    # Too many pull requests beats too few: make sure one is open.
    if not config["createPullRequest"] or not has_ref(update):
        return
    if config["pullRequestBody"] is not None:
        body = config["pullRequestBody"]
    elif not body:
        body = git("log", "-1", "--format=%b", update)
    pr = forge.open_pull_request(update_branch, base, title, body)
    method = config["autoMergeMethod"]
    if method is None:
        return
    # A push by someone else turns GitHub's auto-merge off again.
    pr = pr or forge.find_open_pull_request(update_branch)
    if pr is not None:
        forge.auto_merge(pr, method)


def title_for(paths: list[str]) -> str:
    """As hercules-ci-effects names it."""
    names = ["`flake.lock`" if p == "." else f"`{p}/flake.lock`" for p in paths]
    return "`flake.lock`" if len(names) > 3 else ", ".join(names)


def update_together(forge: Forge, config: dict, default_branch: str, nix: str) -> None:
    """One branch and pull request for every flake, as upstream."""
    branch = config["updateBranch"]
    base_branch = config["baseMergeBranch"]
    # Upstream's baseMerge.enable: a method applies only with a branch.
    method = config["baseMergeMethod"] if base_branch else None
    merge_base(branch, f"refs/remotes/origin/{base_branch or default_branch}", method)
    before = git("rev-parse", "HEAD")
    flakes = config["flakes"]
    for path, flake in flakes.items():
        inputs = flake_inputs("HEAD", path, flake)
        if not inputs and (flake.get("inputs") or flake.get("excludeInputs")):
            continue
        where = "" if list(flakes) == ["."] else f" in '{path}'"
        print(f"Running nix flake update{where}...", file=sys.stderr)
        nix_flake_update(nix, path, inputs, flake.get("commitSummary", ""))
    post_update(config)
    # Rebase rewrites the branch, and reset starts it over.
    force = {"rebase": "force-with-lease", "reset": "force"}.get(method or "")
    push(branch, before, force)
    title = config["pullRequestTitle"] or f"{title_for(list(flakes))}: Update"
    body = commit_bodies(before)
    open_pull_request(forge, config, branch, default_branch, title, body)


def update_each(forge: Forge, config: dict, default_branch: str, nix: str) -> None:
    """A branch and pull request per input, each started afresh from the
    base. One failing input does not stop the others."""
    base = f"refs/remotes/origin/{config['baseMergeBranch'] or default_branch}"
    failed = []
    for path, flake in config["flakes"].items():
        inputs = flake_inputs(base, path, flake)
        where = "" if path == "." else f" in {path}"
        for name in inputs:
            suffix = "" if path == "." else f"-{path}"
            branch = f"{config['updateBranch']}{suffix}-{name}".replace("/", "-")
            summary = f"Update {name}{where}"
            try:
                print(f"{summary}...", file=sys.stderr)
                git("checkout", "--quiet", "-B", branch, base)
                before = git("rev-parse", "HEAD")
                nix_flake_update(nix, path, [name], summary)
                post_update(config)
                if not push(branch, before, "force") and has_ref(
                    f"refs/remotes/origin/{branch}"
                ):
                    # The base has the update already.
                    git("push", "origin", f":refs/heads/{branch}")
                    continue
                open_pull_request(
                    forge,
                    config,
                    branch,
                    default_branch,
                    config["pullRequestTitle"] or summary,
                    commit_bodies(before),
                )
            except ERRORS as e:
                print(f"{summary} failed: {e}", file=sys.stderr)
                failed.append(summary)
    if failed:
        raise Failed("failed: " + ", ".join(failed))


def run(config: dict) -> None:
    for who in ("AUTHOR", "COMMITTER"):
        os.environ.setdefault(f"GIT_{who}_NAME", "nixbot")
        os.environ.setdefault(f"GIT_{who}_EMAIL", "nixbot@localhost")
    forge = Forge(config)
    forge.setup_git()
    head = git("ls-remote", "--symref", "origin", "HEAD")
    default_branch = head.split("\t", 1)[0].removeprefix("ref: refs/heads/")
    # The checkout comes from nixbot's mirror. The forge has the
    # branches as they are now.
    git("fetch", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*")
    if config["perInput"]:
        update_each(forge, config, default_branch, config["nix"])
    else:
        update_together(forge, config, default_branch, config["nix"])


def main() -> None:
    with open(sys.argv[1]) as f:
        config = json.load(f)
    try:
        run(config)
    except ERRORS as e:
        sys.exit(f"flakeUpdate: {e}")


main()
