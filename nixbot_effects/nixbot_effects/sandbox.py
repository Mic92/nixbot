"""Sandbox construction: which secrets and mounts an effect derivation
gets, its environment, and the bubblewrap command line. Everything here
is pure (no subprocesses)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .errors import EffectError
from .secrets import (
    SecretContext,
    SecretsError,
    check_mounts,
    gather_secrets,
    parse_secrets_map,
)

if TYPE_CHECKING:
    from pathlib import Path

    from .options import EffectsOptions


def secret_context(opts: EffectsOptions) -> SecretContext:
    branch = opts.branch or ""
    return SecretContext(
        owner_name=opts.repo.split("/")[0],
        repo_name=opts.repo.rsplit("/", 1)[-1],
        is_default_branch=opts.default_branch is not None
        and branch == opts.default_branch,
        ref=f"refs/tags/{opts.tag}" if opts.tag else f"refs/heads/{branch}",
    )


def select_mounts(
    drv: dict[str, Any], opts: EffectsOptions
) -> list[tuple[str, str, bool]]:
    """Bind mounts requested via __hci_effect_mounts, validated
    against the configured mountables."""
    raw = drv.get("env", {}).get("__hci_effect_mounts")
    if not raw:
        return []
    try:
        mounts = json.loads(raw)
    except json.JSONDecodeError as e:
        msg = f"could not parse __hci_effect_mounts in the derivation: {e}"
        raise SecretsError(msg) from e
    if not isinstance(mounts, dict) or not all(
        isinstance(name, str) for name in mounts.values()
    ):
        msg = (
            "__hci_effect_mounts in the derivation must be a JSON object "
            "mapping mount paths to mountable names"
        )
        raise SecretsError(msg)
    mountables: dict[str, Any] = {}
    if opts.mountables_file is not None:
        mountables = json.loads(opts.mountables_file.read_text())
    return check_mounts(mountables, secret_context(opts), mounts)


def select_secrets(
    drv: dict[str, Any],
    secrets: dict[str, Any],
    opts: EffectsOptions,
) -> dict[str, Any]:
    """Hercules semantics when the effect declares secretsMap: an
    empty declared map grants nothing. An effect without any
    secretsMap still gets the whole secrets file, so existing repos
    keep working."""
    secrets_map = parse_secrets_map(drv.get("env", {}))
    if secrets_map is None:
        return secrets
    return gather_secrets(secrets_map, secrets, secret_context(opts), opts.git_token)


# Effect derivations opt into the runner-prepared repository clone by
# setting this environment attribute to true.
EFFECT_CHECKOUT_ATTR = "__nixbot_effect_checkout"
# Where the clone is mounted inside the sandbox.
EFFECT_CHECKOUT_PATH = "/build/checkout"


def effect_checkout_mount(
    drv_env: dict[str, str], effect_checkout: Path | None, etc_dir: Path | None = None
) -> tuple[list[str], dict[str, str]]:
    """bwrap arguments and environment for the pre-prepared repository
    checkout requested via __nixbot_effect_checkout. The checkout is
    mounted at EFFECT_CHECKOUT_PATH and becomes the working directory."""
    if not drv_env.get(EFFECT_CHECKOUT_ATTR):
        return [], {}
    if effect_checkout is None:
        msg = (
            f"effect declares {EFFECT_CHECKOUT_ATTR} but the runner"
            " provided no checkout clone"
        )
        raise EffectError(msg)
    if etc_dir is not None:
        # The clone is owned by the runner user while the effect runs
        # under its own uid. Without this git rejects it as "dubious
        # ownership". etc_dir is bound at /etc, so this is the system
        # gitconfig inside the sandbox.
        (etc_dir / "gitconfig").write_text("[safe]\n\tdirectory = *\n")
    return [
        "--bind",
        str(effect_checkout),
        EFFECT_CHECKOUT_PATH,
        # Replaces the default /build working directory.
        "--chdir",
        EFFECT_CHECKOUT_PATH,
    ], {"NIXBOT_EFFECT_CHECKOUT": EFFECT_CHECKOUT_PATH}


def sandbox_env(drv_env: dict[str, str]) -> dict[str, str]:
    """Environment matching hercules-ci-agent: every temp variable
    points at the disk-backed /build, plus its fixed env. HOME is
    overridable by the derivation but never inherited from the host
    (nix develop would leak the service user's)."""
    return {
        "HOME": drv_env.get("HOME", "/homeless-shelter"),
        "IN_HERCULES_CI_EFFECT": "true",
        "HERCULES_CI_SECRETS_JSON": "/run/secrets.json",
        "NIX_BUILD_TOP": "/build",
        "TMPDIR": "/build",
        "TMP": "/build",
        "TEMP": "/build",
        "TEMPDIR": "/build",
        "NIX_REMOTE": "daemon",
        "NIX_LOG_FD": "2",
        "TERM": "xterm-256color",
    }


def pass_as_file_env(
    drv_env: dict[str, str], build_dir: Path
) -> tuple[dict[str, str], set[str]]:
    """Nix's passAsFile: write each listed variable into the build
    directory and point <name>Path at it. hercules-ci-agent does not
    implement this, but plain runCommand effects need it for
    buildCommand."""
    env: dict[str, str] = {}
    clear: set[str] = set()
    for name in drv_env.get("passAsFile", "").split():
        (build_dir / f".attr-{name}").write_text(drv_env.get(name, ""))
        env[f"{name}Path"] = f"/build/.attr-{name}"
        clear.add(name)
    return env, clear


def virtual_ids(drv_env: dict[str, str]) -> tuple[int, int]:
    """`__hci_effect_virtual_uid`/`gid` from the derivation. The
    defaults (0, uid) match hercules-ci-agent."""

    def parse(name: str, default: int) -> int:
        value = drv_env.get(name)
        if not value:
            return default
        try:
            return int(value)
        except ValueError as e:
            msg = f"invalid {name} in the derivation: {value!r} is not an integer"
            raise EffectError(msg) from e

    uid = parse("__hci_effect_virtual_uid", 0)
    gid = parse("__hci_effect_virtual_gid", uid)
    return uid, gid


def task_env(
    drv_env: dict[str, str], opts: EffectsOptions
) -> tuple[dict[str, str], str]:
    """Sandbox env plus the state API token. Without a token it is
    "dummy", like hercules-ci-agent's local mode."""
    env = sandbox_env(drv_env)
    if opts.api_base_url:
        env["HERCULES_CI_API_BASE_URL"] = opts.api_base_url
    if opts.project_id:
        env["HERCULES_CI_PROJECT_ID"] = opts.project_id
    if opts.project_path:
        env["HERCULES_CI_PROJECT_PATH"] = opts.project_path
    return env, opts.task_token or "dummy"


def env_args(env: dict[str, str], clear_env: set[str]) -> list[str]:
    result = []
    for k, v in env.items():
        result += ["--setenv", k, v]
    for k in clear_env:
        result += ["--unsetenv", k]
    return result
