"""nbo: gh-style command line client for the nixbot CI service."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import webbrowser
from typing import TYPE_CHECKING, Any, NoReturn

from nixbot_effects import EffectError

from . import effects
from .api import ApiError, NixbotClient, RepoRef
from .config import Settings, config_path
from .term import (
    FAILED_STATUSES,
    RUNNING_STATUSES,
    STATUS_LABELS,
    bold,
    cyan,
    dim,
    green,
    link,
    sanitize_block,
    sanitize_line,
    status_str,
    strip_ansi,
)
from .watch_tty import TtyWatch

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

EXIT_OK = 0
EXIT_BUILD_FAILED = 1
EXIT_USAGE = 2
EXIT_AUTH = 4


class UsageError(Exception):
    """Bad invocation: reported on stderr, exit 2."""


def visible_len(text: str) -> int:
    """Column width of a cell: ANSI color codes take no space."""
    return len(strip_ansi(text))


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    """Aligned columns like gh. The header row only appears on a TTY."""
    cells = [
        [str(row.get(c, "") if row.get(c) is not None else "") for c in columns]
        for row in rows
    ]
    if sys.stdout.isatty():
        cells.insert(0, [bold(c.upper()) for c in columns])
    if not cells:
        return
    widths = [max(visible_len(r[i]) for r in cells) for i in range(len(columns))]
    for row in cells:
        print(
            "  ".join(
                cell + " " * (w - visible_len(cell))
                for cell, w in zip(row, widths, strict=True)
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
    # Forge names are case-insensitive.
    matches = [
        r
        for r in client.repos()
        if r["owner"].lower() == owner.lower() and r["name"].lower() == name.lower()
    ]
    if not matches:
        server = str(client.http.base_url) or "the server"
        msg = (
            f"repository {owner}/{name} is not known to {server} "
            f"(is it hosted on another nixbot instance? see nbo auth status)"
        )
        raise UsageError(msg)
    # Canonical spelling: the API paths are case-sensitive.
    return RepoRef(matches[0]["forge"], matches[0]["owner"], matches[0]["name"])


def resolve_build(client: NixbotClient, repo: RepoRef, number: int | None) -> int:
    """Explicit build number, the latest build of the CWD's HEAD commit,
    or of its branch when that commit was never built."""
    if number is not None:
        return number
    commit = _git("rev-parse", "HEAD")
    if not commit:
        msg = "not in a git repository: pass a build number"
        raise UsageError(msg)
    builds = client.builds(repo, commit=commit)["items"]
    if builds:
        return int(builds[0]["number"])
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    on_branch = branch and branch != "HEAD"
    builds = client.builds(repo, branch=branch)["items"] if on_branch else []
    if not builds:
        msg = f"no build for commit {commit[:12]} or branch {branch!r} in {repo}"
        raise UsageError(msg)
    build = builds[0]
    print(
        f"nbo: no build for commit {commit[:12]}, using build "
        f"#{build['number']} ({build['commit_sha'][:12]} on {branch})",
        file=sys.stderr,
    )
    return int(build["number"])


# --- commands ----------------------------------------------------------


def cmd_repo_list(client: NixbotClient, args: argparse.Namespace) -> int:
    repos = client.repos()
    if args.json is not None:
        print_json(repos, args.json)
        return EXIT_OK
    rows = [
        {
            **r,
            "repo": link(
                f"{r['forge']}/{r['owner']}/{r['name']}",
                f"{client.http.base_url}/repos/{r['forge']}/{r['owner']}/{r['name']}",
            ),
            "enabled": green("enabled") if r["enabled"] else dim("disabled"),
            "url": dim(r["url"]),
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
            "number": link(str(b["number"]), client.build_url(repo, b["number"])),
            "status": status_str(b["status"]),
            "branch": cyan(b["branch"]),
            "commit": dim(b["commit_sha"][:12]),
            "created_at": dim(b["created_at"]),
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
        webbrowser.open(client.build_url(repo, number))
        return EXIT_OK
    detail = client.build(repo, number)
    if args.json is not None:
        print_json(detail, args.json)
        return EXIT_OK
    build, attrs = detail["build"], detail["attributes"]
    print(
        f"{link(bold(f'build #{number}'), client.build_url(repo, number))} "
        f"{repo} {cyan(build['branch'])} "
        f"@ {dim(build['commit_sha'][:12])}"
    )
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
        log_url = client.log_url(repo, number, a["attr"], raw=False)
        print(f"  {status_str(a['status'])}  {link(a['attr'], log_url)}")
    if failed:
        print(f"logs: nbo log {number} <attr> [-R {repo}]")
    return EXIT_OK


def print_failures(
    client: NixbotClient, repo: RepoRef, number: int, status: str, finished: int
) -> None:
    """Result line plus a failure block with a clickable log URL per
    failed attribute."""
    summary = client.failures(repo, number, tail=20)
    counts = f"{finished} finished, {len(summary['failures'])} failed"
    print(f"{status_str(status)} build #{number}: {counts}")
    if summary.get("error"):
        print(summary["error"])
    for failure in summary["failures"]:
        print(f"\n── {failure['attr']} ── {status_str(failure['status'])}")
        url = client.log_url(repo, number, failure["attr"], raw=not sys.stdout.isatty())
        print(f"log: {url}")
        if failure.get("error"):
            print(failure["error"])
        if failure.get("log_tail"):
            print(sanitize_block(failure["log_tail"]))


def cmd_build_watch(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.attr:
        return watch_attrs(client, repo, number, args.attr)
    if sys.stdout.isatty():
        watcher = TtyWatch(client, repo, number, sys.stdout)
        status = watcher.run()
        print_failures(client, repo, number, status, watcher.finished)
        return EXIT_BUILD_FAILED if status in FAILED_STATUSES else EXIT_OK
    return watch_build(client, repo, number)


def watch_build(client: NixbotClient, repo: RepoRef, number: int) -> int:
    """Append-only progress: one verdict line per finished attribute,
    then the failure summary. On every /api/events hint only the
    attributes finished since the last cursor are fetched."""
    cursor: tuple[str, int] | None = None
    finished = 0
    events: Iterator[dict] | None = None
    while True:
        delta = client.finished_attrs(
            repo,
            number,
            finished_after=cursor[0] if cursor else None,
            after_id=cursor[1] if cursor else 0,
        )
        build, attrs = delta["build"], delta["items"]
        for a in attrs:
            verdict = status_str(a["status"], cached=bool(a.get("cached")))
            print(f"{verdict} {a['attr']}", flush=True)
        if attrs:
            cursor = (attrs[-1]["finished_at"], attrs[-1]["id"])
            finished += len(attrs)
        if build["status"] not in RUNNING_STATUSES:
            break
        if events is None:
            events = client.events(build=build["id"])
        # Any change hint or keepalive triggers a refetch. A closed
        # stream reconnects on the next round.
        if next(events, None) is None:
            events = None

    print_failures(client, repo, number, build["status"], finished)
    return EXIT_BUILD_FAILED if build["status"] in FAILED_STATUSES else EXIT_OK


def watch_attrs(
    client: NixbotClient, repo: RepoRef, number: int, selectors: list[str]
) -> int:
    """Wait for the given attributes to finish. Prints one verdict per
    attribute, and on failure its log tail plus a log URL. Exit 1 when
    any of them failed."""
    detail = client.build(repo, number)
    watched = {
        a["attr"]: a for s in selectors for a in [_match_attr(detail["attributes"], s)]
    }
    failed = False

    def report(attr: dict) -> None:
        nonlocal failed
        verdict = status_str(attr["status"], cached=bool(attr.get("cached")))
        print(f"{verdict} {attr['attr']}", flush=True)
        if attr["status"] not in FAILED_STATUSES:
            return
        failed = True
        if attr.get("error"):
            print(attr["error"])
        print(f"log: {client.log_url(repo, number, attr['attr'], raw=True)}")
        with contextlib.suppress(ApiError):
            print(sanitize_block(client.log_text(repo, number, attr["attr"], tail=20)))

    pending = {}
    for name, attr in watched.items():
        if attr["status"] in RUNNING_STATUSES:
            pending[name] = attr
        else:
            report(attr)
    events: Iterator[dict] | None = None
    while pending:
        if events is None:
            events = client.events(build=detail["build"]["id"])
        if next(events, None) is None:
            events = None
        delta = client.finished_attrs(repo, number)
        for a in delta["items"]:
            if a["attr"] in pending:
                del pending[a["attr"]]
                report(a)
        if pending and delta["build"]["status"] not in RUNNING_STATUSES:
            # The build ended without them finishing (e.g. eval failure).
            final = client.build(repo, number)["attributes"]
            for name in list(pending):
                report(_match_attr(final, name))
            break

    return EXIT_BUILD_FAILED if failed else EXIT_OK


def resolve_attr(
    client: NixbotClient, repo: RepoRef, number: int, selector: str
) -> str:
    """Full attribute name for a selector (exact, substring or drv path)."""
    return _match_attr(client.build(repo, number)["attributes"], selector)["attr"]


def cmd_build_restart(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.effects:
        client.restart_effects(repo, number)
        print(f"restarting effects of build #{number}")
    elif args.effect:
        detail = client.build(repo, number)
        for effect in [_match_effect(detail["effects"], sel) for sel in args.effect]:
            client.restart_effects(repo, number, effect)
            print(f"restarting effect {effect} of build #{number}")
    elif args.attr:
        rows = client.build(repo, number)["attributes"]
        for attr in [_match_attr(rows, a)["attr"] for a in args.attr]:
            client.restart_attr(repo, number, attr)
            print(f"restarting {attr} of build #{number}")
    else:
        client.restart_build(repo, number)
        print(f"restarting build #{number}")
    return EXIT_OK


def cmd_build_cancel(client: NixbotClient, args: argparse.Namespace) -> int:
    repo = resolve_repo(client, args.repo)
    number = resolve_build(client, repo, args.number)
    if args.attr:
        for attr in [resolve_attr(client, repo, number, a) for a in args.attr]:
            client.cancel_attr(repo, number, attr)
            print(f"cancelling {attr} of build #{number}")
    else:
        client.cancel_build(repo, number)
        print(f"cancelling build #{number}")
    return EXIT_OK


def _match_effect(effects: list[dict], selector: str) -> str:
    """Effect name by exact match or unambiguous substring."""
    names = [e["name"] for e in effects]
    if selector in names:
        return selector
    matches = [n for n in names if selector in n]
    if not matches:
        msg = f"no effect matches {selector!r}"
        raise UsageError(msg)
    if len(matches) > 1:
        msg = f"{selector!r} is ambiguous: {', '.join(matches)}"
        raise UsageError(msg)
    return matches[0]


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
                    print(sanitize_block(failure["log_tail"]))
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
            print(sanitize_line(data["text"]))
            streamed = True
        elif event == "drv-done":
            name = names.get(data["idx"], attr)
            print(f"── {name}: {status_str(data['status'])} ──")
        elif event == "done":
            break
    if not streamed:
        print(client.log_text(repo, number, attr, tail=tail), end="")


def cmd_auth_status(client: NixbotClient, args: argparse.Namespace) -> int:
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


class _Parser(argparse.ArgumentParser):
    """Suggests the closest command on typos (Python 3.14+). Subcommand
    parsers are created with type(parser) and inherit this."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        if sys.version_info >= (3, 14):
            kwargs.setdefault("suggest_on_error", True)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="nbo", description="nixbot CI client")
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo", help="manage repositories").add_subparsers(
        dest="subcommand", required=True
    )
    repo_list = repo.add_parser("list", help="enabled repositories")
    _add_json_arg(repo_list)
    repo_list.set_defaults(func=cmd_repo_list)
    for action in ("enable", "disable"):
        p = repo.add_parser(action, help=f"{action} CI for a repository")
        p.add_argument(
            "repository",
            nargs="?",
            metavar="[FORGE/]OWNER/NAME",
            help="repository (default: inferred from the git remote)",
        )
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

    b_watch = build.add_parser(
        "watch",
        help="follow a build until it finishes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nbo build watch                   the current commit's build, exit 1 on failure
  nbo build watch 412               build #412 of the repo in the CWD
  nbo build watch 412 -R github/Mic92/dotfiles   without a local checkout
  nbo build watch 412 --attr treefmt --attr nixos-eve   wait only for these, exit 1 if any fails

On a terminal this shows finished attributes above a live view of the
running ones. Piped or in CI it prints one line per finished attribute.""",
    )
    b_watch.add_argument("number", type=int, nargs="?")
    b_watch.add_argument(
        "--attr",
        action="append",
        metavar="ATTR|DRV-PATH",
        help="wait only for this attribute (repeatable)",
    )
    _add_repo_arg(b_watch)
    b_watch.set_defaults(func=cmd_build_watch)

    b_restart = build.add_parser(
        "restart",
        help="restart a build, attribute or effects",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nbo build restart                 rebuild the current commit's build
  nbo build restart 412             rebuild everything of build #412
  nbo build restart 412 --attr nixos-eve   one attribute only (substring is enough)
  nbo build restart 412 --effects   re-run the effects
  nbo build restart 412 --effect deploy   one effect only""",
    )
    b_restart.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_restart)
    b_restart.add_argument(
        "--attr",
        action="append",
        metavar="ATTR|DRV-PATH",
        help="restart only this attribute (repeatable)",
    )
    b_restart.add_argument("--effects", action="store_true", help="restart the effects")
    b_restart.add_argument(
        "--effect",
        action="append",
        help="restart only this effect (repeatable)",
    )
    b_restart.set_defaults(func=cmd_build_restart)

    b_cancel = build.add_parser("cancel", help="cancel a build or attribute")
    b_cancel.add_argument("number", type=int, nargs="?")
    _add_repo_arg(b_cancel)
    b_cancel.add_argument(
        "--attr",
        action="append",
        metavar="ATTR|DRV-PATH",
        help="cancel only this attribute (repeatable)",
    )
    b_cancel.set_defaults(func=cmd_build_cancel)

    log = sub.add_parser(
        "log",
        help="failure summary or an attribute's log",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nbo log                           why the current commit's build failed
  nbo log 412                       failure summary of build #412
  nbo log 412 nixos-eve             log of the attribute matching "nixos-eve"
  nbo log 412 nixos-eve --tail 100  only the last 100 lines
  nbo log 412 nixos-eve --follow    stream while it is still building
  nbo log 412 /nix/store/…-foo.drv  log of one derivation by store path
  nbo log 412 -R github/Mic92/dotfiles   without a local checkout""",
    )
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

    effects.register(sub)
    return parser


def make_client() -> NixbotClient:
    settings = Settings.load()
    return NixbotClient(settings.url, settings.token)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    args = build_parser().parse_args(argv)
    try:
        # Effects commands drive nix locally and need no API client.
        if getattr(args, "needs_client", True):
            with make_client() as client:
                code = args.func(client, args)
        else:
            code = args.func(args)
    except EffectError as err:
        # The effect run already logged its own diagnostics.
        print(f"nbo: {err}", file=sys.stderr)
        code = EXIT_BUILD_FAILED
    except (UsageError, ValueError) as err:
        print(f"nbo: {err}", file=sys.stderr)
        code = EXIT_USAGE
    except ApiError as err:
        print(f"nbo: {err}", file=sys.stderr)
        code = EXIT_AUTH if err.status in (401, 403) else EXIT_USAGE
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        # Downstream pager/head closed the pipe. Point stdout at devnull
        # so the interpreter's final flush stays quiet too.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = EXIT_OK
    sys.exit(code)


if __name__ == "__main__":
    main()
