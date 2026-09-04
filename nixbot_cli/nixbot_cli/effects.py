"""`nbo effects`: run and inspect hercules-ci style effects locally.

Thin argparse layer over the nixbot_effects library. Unlike the rest of
nbo it talks to nix on this machine, not to the nixbot API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nixbot_effects.eval import options_from_flake_ref
from nixbot_effects.graph import render_tree
from nixbot_effects.match import KINDS, event_payload, skip_reason

from nixbot_effects import (
    EffectsOptions,
    list_effects,
    list_event_effects,
    list_scheduled_effects,
    run_effect,
    run_event_effect,
    run_scheduled_effect,
)


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
    """Split flakeref#name syntax (e.g. github:org/repo/branch#my-effect).
    A bare flake ref is rejected, it would otherwise be treated as a name."""
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


def _payload_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """The event to match against: a file, or one described by flags."""
    if args.payload is not None:
        try:
            return json.loads(args.payload.read_text())
        except (OSError, json.JSONDecodeError) as e:
            sys.exit(f"nbo: cannot read payload {args.payload}: {e}")
    return event_payload(
        pr=args.pr,
        branch=args.branch,
        actor=args.actor,
        permission=args.permission,
        author_permission=args.author_permission,
        labels=args.labels,
        modified=args.modified,
        command=args.command,
        args=args.args,
        status=args.build_status,
        previous_status=args.previous_build_status,
    )


async def list_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    if args.event:
        payload = _payload_from_args(args)
        out = {}
        for name, meta in (await list_event_effects(options, args.event)).items():
            out[name] = asdict(meta)
            out[name]["skipReason"] = skip_reason(meta.when, payload)
    else:
        out = {
            name: asdict(meta) for name, meta in (await list_effects(options)).items()
        }
    json.dump(out, fp=sys.stdout, indent=2)
    print()


async def graph_command(args: argparse.Namespace) -> None:
    """Print the effect DAG as an ASCII tree."""
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    if tree := render_tree(await list_effects(options)):
        print(tree)


async def list_schedules_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    if args.flake_ref:
        options = await options_from_flake_ref(args.flake_ref, options)
    json.dump(await list_scheduled_effects(options), fp=sys.stdout, indent=2)
    print()


async def run_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    effect, options = await _split_flake_ref(
        args.effect, options, f"nbo effects run {args.effect}#<effect-name>"
    )
    if args.event:
        await run_event_effect(options, args.event, effect, _payload_from_args(args))
        return
    await run_effect(options, effect)


async def run_scheduled_command(args: argparse.Namespace) -> None:
    options = _options_from_args(args)
    schedule_name, options = await _split_flake_ref(
        args.schedule_name,
        options,
        f"nbo effects run-scheduled {args.schedule_name}#<schedule-name> <effect>",
    )
    await run_scheduled_effect(options, schedule_name, args.effect)


def cmd_effects(args: argparse.Namespace) -> int:
    asyncio.run(args.effects_func(args))
    return 0


def _key_value(option: str) -> tuple[str, str]:
    key, sep, value = option.partition("=")
    if not sep or not key:
        msg = f"expected KEY=VALUE, got {option!r}"
        raise argparse.ArgumentTypeError(msg)
    return (key, value)


def _add_event_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--event",
        choices=KINDS,
        metavar="KIND",
        help=f"use onEvent.KIND instead of onPush ({', '.join(KINDS)})",
    )
    parser.add_argument(
        "--payload",
        type=Path,
        metavar="FILE",
        help="event payload JSON, as delivered by nixbot; otherwise the "
        "flags below describe the event",
    )
    parser.add_argument("--pr", type=int, metavar="N", help="pull request number")
    parser.add_argument(
        "--actor", metavar="USER", help="who triggered the event, e.g. github:alice"
    )
    parser.add_argument(
        "--permission",
        choices=("read", "write", "admin"),
        help="the actor's permission on the repository",
    )
    parser.add_argument(
        "--author-permission",
        choices=("read", "write", "admin"),
        help="the PR author's permission (defaults to --permission)",
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        dest="labels",
        metavar="LABEL",
        help="a label on the pull request (repeatable)",
    )
    parser.add_argument(
        "--modified",
        action="append",
        default=[],
        metavar="PATH",
        help="a file the pull request changes (repeatable)",
    )
    parser.add_argument("--command", metavar="NAME", help="the /command commented")
    parser.add_argument(
        "--args", default="", metavar="ARGS", help="arguments after the /command"
    )
    parser.add_argument(
        "--build-status",
        default="succeeded",
        choices=("succeeded", "failed"),
        help="status of the build the event is about",
    )
    parser.add_argument(
        "--previous-build-status",
        choices=("succeeded", "failed"),
        metavar="STATUS",
        help="status of the previous build, for when.transition",
    )


def _add_common_flags(parser: argparse.ArgumentParser) -> None:
    """Add flags shared by all effects subcommands."""
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


def register(sub: argparse._SubParsersAction) -> None:
    effects = sub.add_parser(
        "effects",
        help="run and inspect a flake's effects locally",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  nbo effects list                  effects of the flake in the CWD, with after/lock
  nbo effects graph                 the dependency DAG as an ASCII tree
  nbo effects run default.deploy    run one effect in the local sandbox
  nbo effects run github:org/repo/branch#default.deploy   without a checkout
  nbo effects run-scheduled github:org/repo#nightly flake-update
  nbo effects list --event pull_request --pr 7 --label preview   onEvent effects with skip reasons
  nbo effects run --event comment --command apply --permission admin apply""",
    )
    effects.set_defaults(func=cmd_effects, needs_client=False)
    esub = effects.add_subparsers(dest="effects_command", required=True)

    list_parser = esub.add_parser(
        "list",
        help="List available effects (optionally from a flake reference)",
    )
    _add_common_flags(list_parser)
    _add_event_flags(list_parser)
    list_parser.set_defaults(effects_func=list_command)
    list_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    graph_parser = esub.add_parser(
        "graph",
        help="Show the effect dependency graph as an ASCII tree",
    )
    _add_common_flags(graph_parser)
    graph_parser.set_defaults(effects_func=graph_command)
    graph_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_parser = esub.add_parser(
        "run",
        help="Run an effect (supports flakeref#effect syntax)",
    )
    _add_common_flags(run_parser)
    _add_event_flags(run_parser)
    run_parser.set_defaults(effects_func=run_command)
    run_parser.add_argument(
        "effect",
        help="Effect to run, or flakeref#effect (e.g. github:org/repo/branch#default.deploy)",
    )

    list_schedules_parser = esub.add_parser(
        "list-schedules",
        help="List all scheduled effects (optionally from a flake reference)",
    )
    _add_common_flags(list_schedules_parser)
    list_schedules_parser.set_defaults(effects_func=list_schedules_command)
    list_schedules_parser.add_argument(
        "flake_ref",
        nargs="?",
        help="Flake reference (e.g. github:org/repo/branch)",
    )

    run_scheduled_parser = esub.add_parser(
        "run-scheduled",
        help="Run a specific effect from a schedule",
    )
    _add_common_flags(run_scheduled_parser)
    run_scheduled_parser.set_defaults(effects_func=run_scheduled_command)
    run_scheduled_parser.add_argument(
        "schedule_name",
        help="Schedule name, or flakeref#schedule (e.g. github:org/repo#my-schedule)",
    )
    run_scheduled_parser.add_argument(
        "effect",
        help="Effect to run within the schedule",
    )
