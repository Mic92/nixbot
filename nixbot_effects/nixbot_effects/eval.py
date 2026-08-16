"""Evaluation: turning EffectsOptions into herculesCI flake arguments,
listing effects/schedules and instantiating one effect derivation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import EffectError
from .graph import EffectMeta, validate_deps
from .proc import stream_command

if TYPE_CHECKING:
    from .options import EffectsOptions
    from .proc import LogWrite


def nix_command(*args: str) -> list[str]:
    return ["nix", "--extra-experimental-features", "nix-command flakes", *args]


async def git_command(args: list[str], path: Path) -> str:
    out = await stream_command(["git", "-C", str(path), *args], capture_stdout=True)
    return out.strip()


async def get_git_remote_url(path: Path) -> str | None:
    try:
        return await git_command(["remote", "get-url", "origin"], path)
    except EffectError:
        return None


async def git_get_tag(path: Path, rev: str) -> str | None:
    tags = await git_command(["tag", "--points-at", rev], path)
    if tags:
        return tags.splitlines()[0]
    return None


async def _is_git_repo(path: Path) -> bool:
    try:
        await git_command(["rev-parse", "--git-dir"], path)
    except EffectError:
        return False
    return True


async def effects_args(opts: EffectsOptions) -> dict[str, Any]:
    """The argument set herculesCI/effects flake outputs are called with."""
    has_git = await _is_git_repo(opts.path)
    if opts.rev:
        rev = opts.rev
    elif opts.branch and has_git:
        rev = await git_command(["rev-parse", "--verify", opts.branch], opts.path)
    elif has_git:
        rev = await git_command(["rev-parse", "--verify", "HEAD"], opts.path)
    else:
        msg = "No --rev specified and path is not a git repository"
        raise EffectError(msg)
    branch = opts.branch
    if branch is None and has_git:
        branch = await git_command(["rev-parse", "--abbrev-ref", "HEAD"], opts.path)
    repo = opts.repo or opts.path.name
    tag = opts.tag or (await git_get_tag(opts.path, rev) if has_git else None)
    # secret_context needs the tag (isTag conditions), also when resolved from git
    opts.tag = tag
    url = opts.url or (await get_git_remote_url(opts.path) if has_git else None)
    ref = f"refs/tags/{tag}" if tag else (f"refs/heads/{branch}" if branch else None)
    primary_repo = {
        "name": repo,
        "branch": branch,
        "ref": ref,
        "tag": tag,
        "rev": rev,
        "shortRev": rev[:7],
        "remoteHttpUrl": url,
    }
    return {
        "primaryRepo": primary_repo,
        **primary_repo,
        # HerculesCIMeta: ciSystems null = no platform default
        "herculesCI": {"apiBaseUrl": opts.api_base_url, "ciSystems": None},
    }


def _flake_url(opts: EffectsOptions, rev: str) -> str:
    """The flake URL for builtins.getFlake: the locked URL of a resolved
    remote flake ref, otherwise a git+file:// URL of the local path.
    ref=HEAD silences nix's "could not read HEAD ref" warning on
    detached worktrees."""
    if opts.locked_url:
        return opts.locked_url
    return f"git+file://{opts.path}?ref=HEAD&rev={rev}#"


async def _effects_expr(opts: EffectsOptions, body: str) -> str:
    """Nix expression evaluating `body` with `hci` bound to the flake's
    herculesCI value and `call` applying the herculesCI arguments to values
    that are functions."""
    args = await effects_args(opts)
    rev = args["rev"]
    escaped_args = json.dumps(json.dumps(args))
    url = json.dumps(_flake_url(opts, rev))
    # ciSystems normalization follows addDefaults in hercules-ci-agent's
    # default-herculesCI-for-flake.nix.
    return f"""
      let
        flake = builtins.getFlake {url};
        evalArgs = builtins.fromJSON {escaped_args};
        optionalCall = f: a: if builtins.isFunction f then f a else f;
        hci = optionalCall (flake.outputs.herculesCI or {{}}) evalArgs;
        args = evalArgs // {{
          ciSystems =
            if hci ? ciSystems
            then builtins.listToAttrs
              (map (s: {{ name = s; value = {{ }}; }}) hci.ciSystems)
            else evalArgs.herculesCI.ciSystems;
        }};
        call = f: optionalCall f args;
      in {body}
    """


async def effect_function(opts: EffectsOptions) -> str:
    """Effects of every onPush job: `{ <job> = <effects attrset>; }`.
    `outputs` and `outputs.effects` may be functions of the herculesCI
    arguments, as in hercules-ci-agent. Without `onPush`, the flake's
    top-level `effects` output becomes the default job's effects, matching
    hercules-ci-agent's default job."""
    return await _effects_expr(
        opts,
        "if hci ? onPush then builtins.mapAttrs"
        " (job: j: call ((call (j.outputs or {})).effects or {}))"
        " hci.onPush"
        " else { default = call (flake.outputs.effects or {}); }",
    )


async def scheduled_effect_function(opts: EffectsOptions) -> str:
    return await _effects_expr(opts, "hci.onSchedule or {}")


async def _nix_eval_json(expr: str, opts: EffectsOptions) -> Any:  # noqa: ANN401
    out = await stream_command(
        nix_command("eval", "--json", "--expr", expr),
        capture_stdout=True,
        log=opts.log,
        debug=opts.debug,
    )
    return json.loads(out)


async def list_effects(opts: EffectsOptions) -> dict[str, EffectMeta]:
    """Effects with their scheduling metadata.

    Names are dotted attribute paths starting with the onPush job, e.g.
    "default.env.staging". Traversal follows hercules-ci-agent: recurse into
    attribute sets unless `recurseForDerivations = false`, ignore `_type`
    sets. `after` entries are absolute attr paths (job first) and are
    returned as dotted names. Metadata comes from the effect or its `run`
    wrapper (hercules-ci's runIf).
    """
    expr = f"""
        let
          jobs = {await effect_function(opts)};
          isEffect = v: v ? isEffect || v ? effectScript || v ? run
            || v ? dependencies || (v.type or null) == "derivation";
          dep = name: a:
            if builtins.isList a then builtins.concatStringsSep "." a
            else throw "effect '${{name}}': 'after' entries must be attribute paths (lists of strings), job first";
          meta = name: e:
            let d = e.run or e; in {{
              after = map (dep name) (d.after or []);
              lock = d.lock or null;
            }};
          walk = prefix: v:
            let name = builtins.concatStringsSep "." prefix; in
            if !builtins.isAttrs v || v ? _type then []
            else if isEffect v then [ {{ inherit name; value = meta name v; }} ]
            else if v.recurseForDerivations or true then builtins.concatLists
              (map (n: walk (prefix ++ [ n ]) v.${{n}}) (builtins.attrNames v))
            else [];
        in builtins.listToAttrs (builtins.concatLists
          (map (job: walk [ job ] jobs.${{job}}) (builtins.attrNames jobs)))
        """
    effects = {
        name: EffectMeta(after=tuple(info["after"]), lock=info["lock"])
        for name, info in (await _nix_eval_json(expr, opts)).items()
    }
    # A bad DAG (cycle, unknown dependency) fails discovery right here.
    validate_deps(effects)
    return effects


async def list_scheduled_effects(opts: EffectsOptions) -> dict[str, Any]:
    """All onSchedule definitions: schedule name -> {when, effects}."""
    expr = f"""
        let
          schedules = {await scheduled_effect_function(opts)};
        in
          builtins.mapAttrs (name: schedule: {{
            when = schedule.when or {{}};
            effects = builtins.attrNames (schedule.outputs.effects or {{}});
          }}) schedules
        """
    return dict(await _nix_eval_json(expr, opts))


async def _instantiate(expr: str, opts: EffectsOptions, gcroot: Path) -> str:
    cmd = [
        "nix-instantiate",
        # the expression uses builtins.getFlake
        "--extra-experimental-features",
        "flakes",
        "--add-root",
        str(gcroot),
        "--expr",
        expr,
    ]
    out = await stream_command(cmd, capture_stdout=True, log=opts.log, debug=opts.debug)
    paths = out.split()
    if not paths:
        return ""
    # nix-instantiate prints one --add-root symlink per derivation.
    # An effect must be a single one, else `derivation show` gets a bad path.
    if len(paths) != 1:
        msg = f"expected effect to be a single derivation, got {len(paths)}: {out!r}"
        raise EffectError(msg)
    resolved = await asyncio.to_thread(Path(paths[0]).resolve)
    return str(resolved)


async def _select_effect(
    binding: str, opts: EffectsOptions, gcroot: Path
) -> tuple[str, bool]:
    """Instantiate an effect bound to `e` by `binding`.

    hercules-ci's `runIf false` replaces the effect with a wrapper set
    `{ dependencies; prebuilt; }` (recurseForDerivations) so nix-instantiate
    would emit a root per derivation. Select a single derivation: the
    runnable effect (`e.run`, or a bare effect derivation), otherwise the
    dependency-only build. Only run in the former case; the latter mirrors
    the agent, which just builds inputs when the effect is gated off.
    See https://github.com/Mic92/nixbot/issues/56.
    """
    drv_path = await _instantiate(
        f"{binding} e.run or e.dependencies or e", opts, gcroot
    )
    if drv_path == "":
        return "", False
    should_run = bool(
        await _nix_eval_json(f"{binding} e ? run || !(e ? dependencies)", opts)
    )
    return drv_path, should_run


async def instantiate_effects(
    effect: str, opts: EffectsOptions, gcroot: Path
) -> tuple[str, bool]:
    return await _select_effect(
        f"let e = ({await effect_function(opts)}).{effect}; in", opts, gcroot
    )


async def instantiate_scheduled_effect(
    schedule_name: str, effect: str, opts: EffectsOptions, gcroot: Path
) -> tuple[str, bool]:
    return await _select_effect(
        f"let e = ({await scheduled_effect_function(opts)})"
        f".{schedule_name}.outputs.effects.{effect}; in",
        opts,
        gcroot,
    )


async def build_derivation(drv_path: str, opts: EffectsOptions) -> None:
    """Realise a derivation without running it (gated-off effects only build
    their dependencies, matching hercules-ci-agent)."""
    await stream_command(
        nix_command("build", "--no-link", f"{drv_path}^*"),
        log=opts.log,
        debug=opts.debug,
    )


async def parse_derivation(
    path: str, *, log: LogWrite | None = None, debug: bool = False
) -> dict[str, Any]:
    out = await stream_command(
        nix_command("derivation", "show", f"{path}^*"),
        capture_stdout=True,
        log=log,
        debug=debug,
    )
    return json.loads(out)


async def resolve_flake(
    flake_ref: str,
    *,
    log: LogWrite | None = None,
    debug: bool = False,
    refresh: bool = True,
) -> dict[str, Any]:
    """`nix flake metadata --json` for a flake reference."""
    out = await stream_command(
        nix_command(
            "flake",
            "metadata",
            "--json",
            *(["--refresh"] if refresh else []),
            flake_ref,
        ),
        capture_stdout=True,
        log=log,
        debug=debug,
    )
    return json.loads(out)


async def options_from_flake_ref(
    flake_ref: str, base: EffectsOptions
) -> EffectsOptions:
    """Resolve a flake reference into a copy of `base` pointing at the
    fetched source (its store path has no .git)."""
    meta = await resolve_flake(
        flake_ref, log=base.log, debug=base.debug, refresh=base.refresh
    )
    locked = meta.get("locked", {})
    return replace(
        base,
        path=Path(meta.get("path", "")),
        rev=locked.get("rev") or locked.get("dirtyRev"),
        branch=locked.get("ref") or base.branch,
        url=meta.get("resolvedUrl", meta.get("url", "")),
        # lockedUrl can be null in JSON (None in Python), fall back to url
        locked_url=meta.get("lockedUrl") or meta.get("url", ""),
    )
