"""Standalone CLI wrapper around the async nixbot_effects library."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import (
    EffectError,
    EffectsOptions,
    list_effects,
    list_scheduled_effects,
    run_effect,
    run_scheduled_effect,
)
from .eval import options_from_flake_ref
from .graph import EffectGraphError, render_tree


async def _log_stderr(data: bytes) -> None:
    sys.stderr.buffer.write(data)
    sys.stderr.buffer.flush()


def _options_from_args(args: argparse.Namespace) -> EffectsOptions:
    return EffectsOptions(
        secrets=json.loads(args.secrets.read_text()) if args.secrets else None,
        branch=args.branch,
        rev=args.rev,
        repo=args.repo or "",
        tag=args.tag,
        path=args.path.resolve(),
        default_branch=args.default_branch,
        git_token=(
            args.git_token_file.read_text().strip() if args.git_token_file else None
        ),
        task_token=(
            args.task_token_file.read_text().strip() if args.task_token_file else None
        ),
        mountables_file=args.mountables_file,
        api_base_url=args.api_base_url,
        project_id=args.project_id,
        project_path=args.project_path,
        extra_nix_options=args.extra_nix_option,
        debug=args.debug,
        refresh=not args.no_refresh,
        extra_sandbox_paths=args.extra_sandbox_path,
        effect_checkout=args.effect_checkout,
        # Keep stdout for JSON output. All child output goes to stderr.
        log=_log_stderr,
    )


async def _split_flake_ref(
    name: str, options: EffectsOptions, usage: str
) -> tuple[str, EffectsOptions]:
    """Split flakeref#name syntax (e.g. github:org/repo/branch#my-effect);
    reject a bare flake ref, which would otherwise be treated as a name."""
    if "#" in name:
        flake_ref, _, name = name.partition("#")
        return name, await options_from_flake_ref(flake_ref, options)
    if ":" in name or name.startswith("/"):
        print(
            f"error: '{name}' looks like a flake reference but is missing the"
            f" '#' fragment\n  usage: {usage}",
            file=sys.stderr,
        )
        sys.exit(1)
    return name, options


async def list_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    effects = await list_effects(options)
    json.dump(
        {name: asdict(meta) for name, meta in effects.items()},
        fp=sys.stdout,
        indent=2,
    )


async def graph_command(args: argparse.Namespace) -> None:
    """Print the effect DAG as an ASCII tree."""
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    try:
        tree = render_tree(await list_effects(options))
    except EffectGraphError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    if tree:
        print(tree)


async def list_schedules_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    json.dump(await list_scheduled_effects(options), fp=sys.stdout, indent=2)


async def run_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    effect, options = await _split_flake_ref(
        args.effect, options, f"nixbot-effects run {args.effect}#<effect-name>"
    )
    await run_effect(options, effect)


async def run_scheduled_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    schedule_name, options = await _split_flake_ref(
        args.schedule_name,
        options,
        f"nixbot-effects run-scheduled {args.schedule_name}#<schedule-name> <effect>",
    )
    await run_scheduled_effect(options, schedule_name, args.effect)


def _key_value(option: str) -> tuple[str, str]:
    key, sep, value = option.partition("=")
    if not sep or not key:
        msg = f"expected KEY=VALUE, got {option!r}"
        raise argparse.ArgumentTypeError(msg)
    return (key, value)


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Add flags shared by all subcommands."""
    parser.add_argument(
        "--rev",
        type=str,
        help="Git revision to use",
    )
    parser.add_argument(
        "--branch",
        type=str,
        help="Git branch to use",
    )
    parser.add_argument(
        "--repo",
        type=str,
        help="Git repo name",
    )
    parser.add_argument(
        "--tag",
        type=str,
        help="Git tag of the revision (for isTag secret conditions)",
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path(),
        help="Path to the repository",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Enable debug mode (may leak secrets such as GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--default-branch",
        type=str,
        help="Default branch of the repository (for secret conditions)",
    )
    parser.add_argument(
        "--git-token-file",
        type=Path,
        help="File with a forge token for GitToken secret references",
    )
    parser.add_argument(
        "--mountables-file",
        type=Path,
        help="JSON file with mountables effects may request via __hci_effect_mounts",
    )
    parser.add_argument(
        "--api-base-url",
        type=str,
        help="Hercules state API base URL (HERCULES_CI_API_BASE_URL)",
    )
    parser.add_argument(
        "--task-token-file",
        type=Path,
        help="File with the bearer token for the state API",
    )
    parser.add_argument(
        "--project-id",
        type=str,
        help="Value for HERCULES_CI_PROJECT_ID",
    )
    parser.add_argument(
        "--project-path",
        type=str,
        help="Value for HERCULES_CI_PROJECT_PATH",
    )
    parser.add_argument(
        "--extra-nix-option",
        action="append",
        default=[],
        type=_key_value,
        metavar="KEY=VALUE",
        help="nix option for the effect's private daemon",
    )
    parser.add_argument(
        "--extra-sandbox-path",
        type=Path,
        action="append",
        default=[],
        help="Path that should be included in the sandbox from the host.",
    )
    parser.add_argument(
        "--no-refresh",
        default=False,
        action="store_true",
        help="Do not pass --refresh when resolving flake references",
    )
    parser.add_argument(
        "--effect-checkout",
        type=Path,
        help="Pre-prepared repository clone mounted for effects that"
        " declare __nixbot_effect_checkout",
    )
    parser.add_argument(
        "--secrets",
        type=Path,
        help="Path to a json file with secrets",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run effects from a hercules-ci flake",
        epilog=(
            "Flake reference syntax:\n"
            "  Commands accept flake references to operate on remote repositories\n"
            "  without requiring a local checkout:\n\n"
            "  nixbot-effects run github:org/repo/branch#my-effect\n"
            "  nixbot-effects list github:org/repo/branch\n"
            "  nixbot-effects run-scheduled github:org/repo#schedule effect\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparser = parser.add_subparsers(
        dest="command",
        required=True,
        help="Command to run",
    )

    list_parser = subparser.add_parser(
        "list",
        help="List available effects (optionally from a flake reference)",
    )
    _add_common_flags(list_parser)
    list_parser.set_defaults(func=list_command)
    list_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    graph_parser = subparser.add_parser(
        "graph",
        help="Show the effect dependency graph as an ASCII tree",
    )
    _add_common_flags(graph_parser)
    graph_parser.set_defaults(func=graph_command)
    graph_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_parser = subparser.add_parser(
        "run",
        help="Run an effect (supports flakeref#effect syntax)",
    )
    _add_common_flags(run_parser)
    run_parser.set_defaults(func=run_command)
    run_parser.add_argument(
        "effect",
        help="Effect to run, or flakeref#effect (e.g. github:org/repo/branch#deploy)",
    )

    list_schedules_parser = subparser.add_parser(
        "list-schedules",
        help="List all scheduled effects (optionally from a flake reference)",
    )
    _add_common_flags(list_schedules_parser)
    list_schedules_parser.set_defaults(func=list_schedules_command)
    list_schedules_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_scheduled_parser = subparser.add_parser(
        "run-scheduled",
        help="Run a specific effect from a schedule",
    )
    _add_common_flags(run_scheduled_parser)
    run_scheduled_parser.set_defaults(func=run_scheduled_command)
    run_scheduled_parser.add_argument(
        "schedule_name",
        help="Schedule name, or flakeref#schedule (e.g. github:org/repo#my-schedule)",
    )
    run_scheduled_parser.add_argument(
        "effect",
        help="Effect to run within the schedule",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(args.func(args))
    except EffectError as e:
        # The underlying command already logged its own diagnostics.
        # A Python traceback on top is just noise in the run log.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
