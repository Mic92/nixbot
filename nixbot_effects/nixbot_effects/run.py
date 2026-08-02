"""Running one effect: instantiate/gate it, build the bubblewrap
sandbox and execute it with a private nix daemon."""

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .daemon_proxy import nix_daemon_proxy
from .errors import EffectError
from .eval import (
    build_derivation,
    instantiate_effects,
    instantiate_scheduled_effect,
    nix_command,
    parse_derivation,
)
from .proc import stream_command
from .sandbox import (
    effect_checkout_mount,
    env_args,
    id_token_audiences,
    pass_as_file_env,
    select_mounts,
    select_secrets,
    task_env,
    virtual_ids,
)

if TYPE_CHECKING:
    from .options import EffectsOptions


def _work_dirs() -> tuple[Path, Path, Path]:
    work_dir = Path(tempfile.mkdtemp(prefix="effect-"))
    build_dir = work_dir / "build"
    etc_dir = work_dir / "etc"
    build_dir.mkdir()
    etc_dir.mkdir()
    return work_dir, build_dir, etc_dir


def _bubblewrap_command(  # noqa: PLR0913
    drv_path: str,
    bwrap: str,
    *,
    build_dir: Path,
    etc_dir: Path,
    daemon_socket: Path,
    uid: int,
    gid: int,
    extra_sandbox_paths: list[Path],
    bind_mounts: list[tuple[str, str, bool]],
    checkout_args: list[str],
) -> list[str]:
    # Mirrors hercules-ci implementation: https://github.com/hercules-ci/hercules-ci-agent/blob/57c564298bafde509bd23f4d5862574c94be01ba/hercules-ci-agent/src/Hercules/Effect.hs#L285
    return [
        *nix_command("develop", "-i", f"{drv_path}^*", "-c"),
        bwrap,
        "--unshare-all",
        "--share-net",
        "--new-session",
        "--die-with-parent",
        "--dir",
        "/build",
        # bwrap warns about repeated --chdir, so the default working
        # directory only applies when the checkout does not set one.
        *([] if "--chdir" in checkout_args else ["--chdir", "/build"]),
        # Disk-backed like hercules-ci-agent's work dirs: deploys
        # unpack closures that don't fit a tmpfs.
        "--bind",
        str(build_dir),
        "/build",
        "--tmpfs",
        "/tmp",  # noqa: S108
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--uid",
        str(uid),
        "--gid",
        str(gid),
        # Writable /etc like hercules-ci-agent's fresh etc dir.
        # resolv.conf is bound inside it from the host.
        "--bind",
        str(etc_dir),
        "/etc",
        "--ro-bind",
        "/etc/resolv.conf",
        "/etc/resolv.conf",
        "--ro-bind",
        "/nix/store",
        "/nix/store",
        *[
            arg
            for path in extra_sandbox_paths
            for arg in ("--ro-bind", str(path), str(path))
        ],
        *[
            arg
            for dest, source, read_only in bind_mounts
            for arg in ("--ro-bind" if read_only else "--bind", source, dest)
        ],
        *checkout_args,
        "--hostname",
        "hercules-ci",
        "--bind",
        str(daemon_socket),
        "/nix/var/nix/daemon-socket/socket",
    ]


async def _run_in_sandbox(
    drv_path: str, drv: dict[str, Any], opts: EffectsOptions
) -> None:
    """Execute one already-instantiated effect derivation in the sandbox."""
    drv_env = drv.get("env", {})
    # Copy: the hercules-ci task token is added below and must not
    # leak into opts.secrets.
    secrets = dict(select_secrets(drv, opts.secrets or {}, opts))
    bind_mounts = select_mounts(drv, opts)
    env, task_token = task_env(drv_env, opts)
    audiences = id_token_audiences(drv_env)
    if audiences and opts.api_base_url and opts.task_token:
        if opts.bind_id_token_audiences is not None:
            opts.bind_id_token_audiences(audiences)
        env["NIXBOT_ID_TOKEN_REQUEST_URL"] = f"{opts.api_base_url}/api/v1/id-token"
        env["NIXBOT_ID_TOKEN_REQUEST_TOKEN"] = opts.task_token
    uid, gid = virtual_ids(drv_env)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        msg = "bwrap executable not found"
        raise EffectError(msg)
    work_dir, build_dir, etc_dir = _work_dirs()
    try:
        pass_env, clear_env = pass_as_file_env(drv_env, build_dir)
        env.update(pass_env)
        checkout_args, checkout_env = effect_checkout_mount(
            drv_env, opts.effect_checkout, etc_dir
        )
        env.update(checkout_env)
        # Private daemon socket: each connection gets its own untrusted
        # nix-daemon, so effects cannot use trusted-user privileges.
        daemon_socket = work_dir / "daemon-socket"
        async with AsyncExitStack() as stack:
            if shutil.which("nix-daemon") is not None:
                await stack.enter_async_context(
                    nix_daemon_proxy(daemon_socket, opts.extra_nix_options)
                )
            else:
                daemon_socket = Path("/nix/var/nix/daemon-socket/socket")
            secrets["hercules-ci"] = {"data": {"token": task_token}}
            secrets_file = work_dir / "secrets.json"
            secrets_file.touch(mode=0o600)
            secrets_file.write_text(json.dumps(secrets))
            cmd = [
                *_bubblewrap_command(
                    drv_path,
                    bwrap,
                    build_dir=build_dir,
                    etc_dir=etc_dir,
                    daemon_socket=daemon_socket,
                    uid=uid,
                    gid=gid,
                    extra_sandbox_paths=opts.extra_sandbox_paths,
                    bind_mounts=bind_mounts,
                    checkout_args=checkout_args,
                ),
                "--ro-bind",
                str(secrets_file),
                "/run/secrets.json",
                *env_args(env, clear_env),
                "--",
                drv["builder"],
                *drv["args"],
            ]
            await stream_command(cmd, log=opts.log, debug=opts.debug)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


async def _run_derivation(drv_path: str, opts: EffectsOptions) -> None:
    drvs = await parse_derivation(drv_path, log=opts.log, debug=opts.debug)
    if "derivations" in drvs:
        drvs = drvs["derivations"]
    drv = next(iter(drvs.values()))
    await _run_in_sandbox(drv_path, drv, opts)


async def _run_selected(
    opts: EffectsOptions, name: str, drv_path: str, *, should_run: bool
) -> None:
    if drv_path == "":
        msg = f"effect {name} not found or not runnable"
        raise EffectError(msg)
    if not should_run:
        # Gated off (runIf false): only build its dependencies,
        # matching hercules-ci-agent.
        if opts.log is not None:
            await opts.log(
                f"effect {name} is gated off; building dependencies only\n".encode()
            )
        await build_derivation(drv_path, opts)
        return
    await _run_derivation(drv_path, opts)


async def run_effect(opts: EffectsOptions, effect: str) -> None:
    """Run one onPush effect. Raises EffectError on any failure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        gcroot = Path(tmpdir) / "result"
        drv_path, should_run = await instantiate_effects(effect, opts, gcroot)
        await _run_selected(opts, effect, drv_path, should_run=should_run)


async def run_scheduled_effect(
    opts: EffectsOptions, schedule_name: str, effect: str
) -> None:
    """Run one effect from an onSchedule definition."""
    with tempfile.TemporaryDirectory() as tmpdir:
        gcroot = Path(tmpdir) / "result"
        drv_path, should_run = await instantiate_scheduled_effect(
            schedule_name, effect, opts, gcroot
        )
        await _run_selected(
            opts, f"{schedule_name}/{effect}", drv_path, should_run=should_run
        )
