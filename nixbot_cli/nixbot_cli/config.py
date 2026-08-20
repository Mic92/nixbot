"""Server URL and token from the environment or hosts.toml."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import tomllib


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(base) / "nixbot" / "hosts.toml"


@dataclass(frozen=True)
class Settings:
    url: str
    token: str | None

    @classmethod
    def load(cls) -> Settings:
        """Server: NIXBOT_URL, `git config nixbot.url`, the hosts.toml
        entry whose `remotes` match the origin URL, or the only entry.
        NIXBOT_TOKEN overrides the token from hosts.toml."""
        url = os.environ.get("NIXBOT_URL")
        token = os.environ.get("NIXBOT_TOKEN")
        hosts: dict[str, dict] = {}
        path = config_path()
        if path.exists():
            hosts = tomllib.loads(path.read_text())
        url = url or _select_host(hosts)
        if not url:
            remote = _origin_remote()
            where = (
                f"no host in {path} has a remotes pattern matching {remote!r}"
                if remote and hosts
                else f"set NIXBOT_URL, git config nixbot.url, or a host in {path}"
            )
            msg = f"no server configured: {where}"
            raise ValueError(msg)
        # A trailing slash must still find the host's token.
        url = url.rstrip("/")
        entry = next(
            (e for u, e in hosts.items() if u.rstrip("/") == url),
            {},
        )
        token = token or entry.get("token") or _run_token_command(entry)
        return cls(url, token)


def _git_config(key: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "config", "--get", key], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return out.stdout.strip() or None


def _origin_remote() -> str | None:
    """The origin URL reduced to "host/owner/repo", so one pattern
    covers SSH and HTTPS remotes."""
    remote = _git_config("remote.origin.url")
    if not remote:
        return None
    remote = re.sub(r"^\w+://|^\w+@", "", remote)
    return remote.replace(":", "/").removesuffix(".git")


def _select_host(hosts: dict[str, dict]) -> str | None:
    if url := _git_config("nixbot.url"):
        return url
    if len(hosts) == 1:
        return next(iter(hosts))
    remote = _origin_remote()
    if not remote:
        return None
    for url, entry in hosts.items():
        if any(
            fnmatch.fnmatch(remote.lower(), pat.lower())
            for pat in entry.get("remotes", [])
        ):
            return url
    return None


def _run_token_command(entry: dict) -> str | None:
    """Fetch the token from a secret manager (pass, rbw, ...) via the
    host's token_command. The first line of its stdout is the token."""
    command = entry.get("token_command")
    if not command:
        return None
    try:
        out = subprocess.run(
            shlex.split(command), capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as err:
        msg = f"token_command {command!r} failed: {err}"
        raise ValueError(msg) from err
    return out.stdout.splitlines()[0].strip() if out.stdout.strip() else None
