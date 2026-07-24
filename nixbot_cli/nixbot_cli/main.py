"""nbo: gh-style command line client for the nixbot CI service."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING, Any, NoReturn

from .api import ApiError, NixbotClient, RepoRef
from .config import Settings, config_path

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

EXIT_OK = 0
EXIT_BUILD_FAILED = 1
EXIT_USAGE = 2
EXIT_AUTH = 4

FAILED_STATUSES = {
    "failed",
    "cancelled",
    "dependency_failed",
    "cached_failure",
    "failed_eval",
}
GOOD_STATUSES = {"succeeded", "skipped_local"}
# Human wording for the raw database statuses.
STATUS_LABELS = {
    "succeeded": "built",
    "skipped_local": "cached",
    "cached_failure": "failed (cached)",
    "dependency_failed": "dependency failed",
    "failed_eval": "eval failed",
}


class UsageError(Exception):
    """Bad invocation: reported on stderr, exit 2."""


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def status_str(status: str, *, cached: bool = False) -> str:
    label = STATUS_LABELS.get(status, status)
    if status == "succeeded" and cached:
        label = "cached"
    if status in GOOD_STATUSES:
        return _color(f"✓ {label}", "32")
    if status in FAILED_STATUSES:
        return _color(f"✗ {label}", "31")
    glyph = "⏵" if status in ("building", "evaluating") else "·"
    return _color(f"{glyph} {label}", "33")


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """Aligned columns like gh. The header row only appears on a TTY."""
    cells = [
        [str(row.get(c, "") if row.get(c) is not None else "") for c in columns]
        for row in rows
    ]
    if sys.stdout.isatty():
        cells.insert(0, [c.upper() for c in columns])
    widths = [max(len(r[i]) for r in cells) for i in range(len(columns))]
    for row in cells:
        print(
            "  ".join(
                cell.ljust(w) for cell, w in zip(row, widths, strict=True)
            ).rstrip()
        )


def print_json(data: Any, fields: str | None) -> None:
    """--json output. fields is a comma-separated projection of the rows."""
    if fields:
        keys = fields.split(",")
        rows = data if isinstance(data, list) else [data]
        data = [{k: row.get(k) for k in keys} for row in rows]
    print(json.dumps(data, indent=2))


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip()


def resolve_repo(client: NixbotClient, spec: str | None) -> RepoRef:
    """-R forge/owner/name, or owner/name / the CWD's git remote matched
    against the server's repositories."""
    if spec and spec.count("/") == 2:
        return RepoRef.parse(spec)
    if spec:
        parts = spec.strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            msg = f"expected [forge/]owner/name, got {spec!r}"
            raise UsageError(msg)
        owner, name = parts
    else:
        remote = _git("remote", "get-url", "origin")
        if not remote:
            msg = "not in a git repository: pass --repo forge/owner/name"
            raise UsageError(msg)
        parts = remote.rstrip("/").removesuffix(".git").replace(":", "/").split("/")
        owner, name = parts[-2], parts[-1]
    matches = [r for r in client.repos() if r["owner"] == owner and r["name"] == name]
    if not matches:
        msg = f"repository {owner}/{name} is not known to the server"
        raise UsageError(msg)
    return RepoRef(matches[0]["forge"], owner, name)


def resolve_build(client: NixbotClient, repo: RepoRef, number: int | None) -> int:
    """Explicit build number, or the latest build of the CWD's HEAD commit."""
    if number is not None:
        return number
    commit = _git("rev-parse", "HEAD")
    if not commit:
        msg = "not in a git repository: pass a build number"
        raise UsageError(msg)
    builds = client.builds(repo, commit=commit)["items"]
    if not builds:
        msg = f"no build for commit {commit[:12]} in {repo}"
        raise UsageError(msg)
    return int(builds[0]["number"])


# --- commands ----------------------------------------------------------


def cmd_repo_list(client: NixbotClient, args: argparse.Namespace) -> int:
    repos = client.repos()
    if args.json is not None:
        print_json(repos, args.json)
        return EXIT_OK
    rows = [
        {
            **r,
            "repo": f"{r['forge']}/{r['owner']}/{r['name']}",
            "enabled": "enabled" if r["enabled"] else "disabled",
        }
        for r in repos
    ]
    print_table(rows, ["repo", "enabled", "default_branch", "url"])
    return EXIT_OK


def cmd_repo_set_enabled(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repository)
    result = client.set_enabled(repo, enabled=args.action == "enable")
    state = "enabled" if result["enabled"] else "disabled"
    print(f"{repo} {state}")
    return EXIT_OK


def cmd_build_list(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    page = client.builds(
        repo,
        status=args.status,
        branch=args.branch,
        pr_number=args.pr,
        commit=args.commit,
    )
    if args.json is not None:
        print_json(page["items"], args.json)
        return EXIT_OK
    rows = [
        {
            **b,
            "status": status_str(b["status"]),
            "commit": b["commit_sha"][:12],
            "pr": b["pr_number"],
        }
        for b in page["items"]
    ]
    print_table(rows, ["number", "status", "branch", "pr", "commit", "created_at"])
    return EXIT_OK


def cmd_build_view(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.web:
        webbrowser.open(f"{client.http.base_url}/repos/{repo}/builds/{number}")
        return EXIT_OK
    detail = client.build(repo, number)
    if args.json is not None:
        print_json(detail, args.json)
        return EXIT_OK
    build, attrs = detail["build"], detail["attributes"]
    print(f"build #{number} {repo} {build['branch']} @ {build['commit_sha'][:12]}")
    print(f"status: {status_str(build['status'])}")
    if build.get("error"):
        print(f"error: {build['error']}")
    counts: dict[str, int] = {}
    for a in attrs:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    summary = " · ".join(
        f"{n} {STATUS_LABELS.get(s, s)}" for s, n in sorted(counts.items())
    )
    print(f"attributes: {len(attrs)}" + (f" ({summary})" if attrs else ""))
    failed = [a for a in attrs if a["status"] in FAILED_STATUSES]
    for a in failed:
        print(f"  {status_str(a['status'])}  {a['attr']}")
    if failed:
        print(f"logs: nbo log {number} <attr> [-R {repo}]")
    return EXIT_OK


RUNNING_STATUSES = {"pending", "evaluating", "building"}


def cmd_build_watch(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    return watch_build(client, repo, number)


def watch_build(client: NixbotClient, repo: RepoRef, number: int) -> int:
    """Append-only progress: one verdict line per finished attribute,
    then the failure summary. Refetches on every /api/events hint."""
    reported: set[str] = set()
    events: Iterator[dict] | None = None
    while True:
        detail = client.build(repo, number)
        build = detail["build"]
        for a in detail["attributes"]:
            if a["attr"] in reported or a["status"] in RUNNING_STATUSES:
                continue
            reported.add(a["attr"])
            verdict = status_str(a["status"], cached=bool(a.get("cached")))
            print(f"{verdict} {a['attr']}", flush=True)
        if build["status"] not in RUNNING_STATUSES:
            break
        if events is None:
            events = client.events(build=build["id"])
        # Any change hint or keepalive triggers a refetch. A closed
        # stream reconnects on the next round.
        if next(events, None) is None:
            events = None

    summary = client.failures(repo, number, tail=20)
    counts = f"{len(reported)} finished, {len(summary['failures'])} failed"
    print(f"{status_str(build['status'])} build #{number}: {counts}")
    if summary.get("error"):
        print(summary["error"])
    for failure in summary["failures"]:
        print(f"\n── {failure['attr']} ── {status_str(failure['status'])}")
        if failure.get("error"):
            print(failure["error"])
        if failure.get("log_tail"):
            print(failure["log_tail"])
    return EXIT_BUILD_FAILED if build["status"] in FAILED_STATUSES else EXIT_OK


def cmd_build_restart(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.effects:
        client.restart_effects(repo, number)
        print(f"restarting effects of build #{number}")
    elif args.attr:
        client.restart_attr(repo, number, args.attr)
        print(f"restarting {args.attr} of build #{number}")
    else:
        client.restart_build(repo, number)
        print(f"restarting build #{number}")
    return EXIT_OK


def cmd_build_cancel(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.attr:
        client.cancel_attr(repo, number, args.attr)
        print(f"cancelling {args.attr} of build #{number}")
    else:
        client.cancel_build(repo, number)
        print(f"cancelling build #{number}")
    return EXIT_OK


def _match_attr(attrs: list[dict], selector: str) -> dict:
    """Attribute by exact name, unambiguous substring, or drv store path."""
    if selector.endswith(".drv"):
        matches = [a for a in attrs if a.get("drv_path") == selector]
        if matches:
            return matches[0]
    exact = [a for a in attrs if a["attr"] == selector]
    if exact:
        return exact[0]
    matches = [a for a in attrs if selector in a["attr"]]
    if not matches:
        msg = f"no attribute matches {selector!r}"
        raise UsageError(msg)
    if len(matches) > 1:
        names = ", ".join(a["attr"] for a in matches)
        msg = f"{selector!r} is ambiguous: {names}"
        raise UsageError(msg)
    return matches[0]


def cmd_log(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    detail = client.build(repo, number)
    build_failed = detail["build"]["status"] in FAILED_STATUSES

    if args.follow and args.attr is None:
        msg = "--follow needs an attribute"
        raise UsageError(msg)
    if args.attr is None:  # whole build: print the failure summary
        summary = client.failures(repo, number, tail=args.tail)
        if args.json is not None:
            print_json(summary, args.json)
        else:
            print(f"build #{number}: {status_str(summary['status'])}")
            if summary.get("error"):
                print(summary["error"])
            for failure in summary["failures"]:
                print(f"\n── {failure['attr']} ── {status_str(failure['status'])}")
                if failure.get("error"):
                    print(failure["error"])
                if failure.get("log_tail"):
                    print(failure["log_tail"])
        return EXIT_BUILD_FAILED if build_failed else EXIT_OK

    attr = _match_attr(detail["attributes"], args.attr)
    drv = args.attr if args.attr.endswith(".drv") else None
    if args.follow:
        follow_attr(client, repo, number, attr["attr"], tail=args.tail)
        attr = _match_attr(client.build(repo, number)["attributes"], attr["attr"])
    elif args.json is not None:
        toc = client.log_toc(repo, number, attr["attr"])
        print_json(toc, args.json)
    else:
        print(
            client.log_text(repo, number, attr["attr"], tail=args.tail, drv=drv), end=""
        )
    failed = attr["status"] in FAILED_STATUSES
    return EXIT_BUILD_FAILED if failed else EXIT_OK


def follow_attr(
    client: NixbotClient, repo: RepoRef, number: int, attr: str, *, tail: int | None
) -> None:
    """Append-only rendering of the structured stream. A finished
    attribute has no stream, so its stored log is printed instead."""
    names: dict[int, str] = {}
    streamed = False
    for event, data in client.log_stream(repo, number, attr):
        if event == "state":
            names = {e["idx"]: e["name"] for e in data}
        elif event == "drv":
            names[data["idx"]] = data["name"]
            print(f"── {data['name']} ──")
            streamed = True
        elif event == "phase":
            print(f"── {names.get(data['idx'], attr)}: {data['phase']} ──")
        elif event == "line":
            print(data["text"])
            streamed = True
        elif event == "drv-done":
            name = names.get(data["idx"], attr)
            print(f"── {name}: {status_str(data['status'])} ──")
        elif event == "done":
            break
    if not streamed:
        print(client.log_text(repo, number, attr, tail=tail), end="")


def cmd_auth_status(client: NixbotClient, args: argparse.Namespace) -> int:  # noqa: ARG001
    settings = Settings.load()
    print(f"server: {settings.url}")
    if settings.token:
        print(f"token: {settings.token[:8]}… (NIXBOT_TOKEN or {config_path()})")
    else:
        print("token: none (anonymous). Set NIXBOT_TOKEN for control commands.")
    client.repos()  # verifies the server answers and the token is accepted
    print("server is reachable")
    return EXIT_OK


# --- argument parsing ---------------------------------------------------


def _add_repo_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-R",
        "--repo",
        metavar="[FORGE/]OWNER/NAME",
        help="repository (default: inferred from the git remote)",
    )


def _add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json",
        nargs="?",
        const="",
        metavar="FIELDS",
        help="JSON output, optionally projected to comma-separated fields",
    )


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915
    parser = argparse.ArgumentParser(prog="nbo", description="nixbot CI client")
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo", help="manage repositories").add_subparsers(
        dest="subcommand", required=True
    )
    repo_list = repo.add_parser("list", help="enabled repositories")
    _add_json_arg(repo_list)
    repo_list.set_defaults(func=cmd_repo_list)
    for action in ("enable", "disable"):
        p = repo.add_parser(action, help=f"{action} CI for a repository")
        p.add_argument("repository", metavar="[FORGE/]OWNER/NAME")
        p.set_defaults(func=cmd_repo_set_enabled, action=action)

    build = sub.add_parser("build", help="inspect and control builds").add_subparsers(
        dest="subcommand", required=True
    )
    b_list = build.add_parser("list", help="list builds")
    _add_repo_arg(b_list)
    b_list.add_argument("--branch")
    b_list.add_argument("--pr", type=int)
    b_list.add_argument("--commit")
    b_list.add_argument("--status")
    _add_json_arg(b_list)
    b_list.set_defaults(func=cmd_build_list)

    b_view = build.add_parser("view", help="show one build")
    b_view.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_view)
    _add_json_arg(b_view)
    b_view.add_argument("--web", action="store_true", help="open in the browser")
    b_view.set_defaults(func=cmd_build_view)

    b_watch = build.add_parser("watch", help="follow a build until it finishes")
    b_watch.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_watch)
    b_watch.set_defaults(func=cmd_build_watch)

    b_restart = build.add_parser(
        "restart", help="restart a build, attribute or effects"
    )
    b_restart.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_restart)
    b_restart.add_argument("--attr", help="restart only this attribute")
    b_restart.add_argument("--effects", action="store_true", help="restart the effects")
    b_restart.set_defaults(func=cmd_build_restart)

    b_cancel = build.add_parser("cancel", help="cancel a build or attribute")
    b_cancel.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_cancel)
    b_cancel.add_argument("--attr", help="cancel only this attribute")
    b_cancel.set_defaults(func=cmd_build_cancel)

    log = sub.add_parser("log", help="failure summary or an attribute's log")
    log.add_argument("number", type=int, nargs="?")
    log.add_argument(
        "attr",
        nargs="?",
        metavar="ATTR|DRV-PATH",
        help="attribute (substring) or .drv store path",
    )
    _add_repo_arg(log)
    log.add_argument("--tail", type=int, metavar="N", help="last N lines")
    log.add_argument(
        "-f", "--follow", action="store_true", help="stream a running attribute's log"
    )
    _add_json_arg(log)
    log.set_defaults(func=cmd_log)

    auth = sub.add_parser("auth", help="authentication").add_subparsers(
        dest="subcommand", required=True
    )
    auth.add_parser("status", help="configured server and token").set_defaults(
        func=cmd_auth_status
    )
    return parser


def make_client() -> NixbotClient:
    settings = Settings.load()
    return NixbotClient(settings.url, settings.token)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    args = build_parser().parse_args(argv)
    try:
        with make_client() as client:
            code = args.func(client, args)
    except (UsageError, ValueError) as err:
        print(f"nbo: {err}", file=sys.stderr)
        code = EXIT_USAGE
    except ApiError as err:
        print(f"nbo: {err}", file=sys.stderr)
        code = EXIT_AUTH if err.status in (401, 403) else EXIT_USAGE
    sys.exit(code)


if __name__ == "__main__":
    main()
