"""Prefetch flake inputs outside the eval sandbox.

The sandboxed evaluator has no SSH keys, so it cannot fetch private
`ssh://` flake inputs (issue #86). `nix flake prefetch-inputs` copies
all locked inputs into the local store beforehand, with the same
credentials as the git fetch. Locked inputs are addressed by narHash,
so the evaluator then finds them in the store without network access.
`nix flake archive --dry-run --json` then reports the input store
paths (computed from the locked narHashes, no fetching) so each one
can be gc-rooted under the per-build gc-roots directory.

The worktree itself is never re-fetched as a git input: Nix's workdir
and rev-based exports of a repo with submodules produce different
narHashes, which fails the `__final` lock check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .environ import passthrough_env
from .gcroots import register_gcroot
from .nix_eval import EvalError

if TYPE_CHECKING:
    from .gitrepo import FetchCredentials
    from .repo_config import BranchConfig

logger = logging.getLogger(__name__)

STDERR_TAIL_LINES = 50


class PrefetchError(EvalError):
    """Prefetching flake inputs failed. Settled like an eval failure."""


def _nix_flake_command(
    subcommand: list[str], flake_dir: Path, branch_config: BranchConfig
) -> list[str]:
    return [
        "nix",
        # Flakes may not be enabled in the system nix.conf.
        "--option",
        "extra-experimental-features",
        "nix-command flakes",
        "--option",
        "accept-flake-config",
        "true",
        "flake",
        *subcommand,
        "--no-write-lock-file",
        *(
            ["--reference-lock-file", branch_config.lock_file]
            if branch_config.lock_file != "flake.lock"
            else []
        ),
        str(flake_dir),
    ]


def build_prefetch_command(flake_dir: Path, branch_config: BranchConfig) -> list[str]:
    return _nix_flake_command(["prefetch-inputs"], flake_dir, branch_config)


def build_input_paths_command(
    flake_dir: Path, branch_config: BranchConfig
) -> list[str]:
    return _nix_flake_command(
        ["archive", "--dry-run", "--json"], flake_dir, branch_config
    )


def collect_input_paths(archive_json: dict[str, Any]) -> set[str]:
    """Store paths of all transitive inputs from `nix flake archive
    --json` output. The top-level flake itself needs no gc-root."""
    paths: set[str] = set()

    def walk(inputs: dict[str, Any]) -> None:
        for node in inputs.values():
            if node.get("path"):
                paths.add(node["path"])
            walk(node.get("inputs", {}))

    walk(archive_json.get("inputs", {}))
    return paths


def _prefetch_env(credentials: FetchCredentials | None, home: Path) -> dict[str, str]:
    """nix shells out to `git fetch` for git inputs, which honors
    GIT_SSH_COMMAND and $HOME/.netrc. nix's own downloader reads the
    netrc-file setting instead."""
    env = {
        **passthrough_env(),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home),
    }
    # Environment-based git config, e.g. protocol.file.allow in tests.
    for key, value in os.environ.items():
        if key.startswith("GIT_CONFIG_") and key not in env:
            env[key] = value
    if credentials is None:
        return env
    ssh_command = credentials.git_ssh_command()
    if ssh_command is not None:
        env["GIT_SSH_COMMAND"] = ssh_command
    if credentials.netrc_file is not None:
        (home / ".netrc").symlink_to(credentials.netrc_file)
        env["NIX_CONFIG"] = f"netrc-file = {credentials.netrc_file}"
    return env


async def _run(cmd: list[str], env: dict[str, str], cwd: Path) -> bytes:
    """Run one prefetch command. On cancellation the process group is
    killed so git/ssh children don't linger."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await proc.communicate()
    except BaseException:
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if proc.returncode != 0:
        tail = "\n".join(
            stderr.decode(errors="replace").splitlines()[-STDERR_TAIL_LINES:]
        )
        msg = f"{' '.join(cmd[:2])} failed with exit code {proc.returncode}:\n{tail}"
        raise PrefetchError(msg)
    return stdout


async def _register_gcroots(paths: set[str], gc_roots_dir: Path) -> None:
    for path in sorted(paths):
        await register_gcroot(gc_roots_dir, "inputs", Path(path).name, path)


async def prefetch_flake_inputs(
    worktree_path: Path,
    branch_config: BranchConfig,
    gc_roots_dir: Path,
    credentials: FetchCredentials | None = None,
) -> None:
    """Copy all locked flake inputs into the local store and gc-root
    them. No-op without a flake. Callers bound the runtime with
    asyncio.timeout()."""
    flake_dir = worktree_path / branch_config.flake_dir
    if not await asyncio.to_thread((flake_dir / "flake.nix").exists):
        return
    # Throwaway HOME for the scoped .netrc and the fetcher cache.
    home = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="flake-prefetch-"))
    try:
        env = _prefetch_env(credentials, home)
        logger.info("prefetching flake inputs", extra={"flake_dir": str(flake_dir)})
        await _run(build_prefetch_command(flake_dir, branch_config), env, worktree_path)
        stdout = await _run(
            build_input_paths_command(flake_dir, branch_config), env, worktree_path
        )
        try:
            archive = json.loads(stdout)
        except json.JSONDecodeError as e:
            msg = f"failed to parse nix flake archive output: {e}"
            raise PrefetchError(msg) from None
        await _register_gcroots(collect_input_paths(archive), gc_roots_dir)
    finally:
        await asyncio.to_thread(shutil.rmtree, home, ignore_errors=True)
