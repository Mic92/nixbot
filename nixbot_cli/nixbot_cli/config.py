"""Server URL and token from the environment or hosts.toml."""

from __future__ import annotations

import os
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
        """NIXBOT_URL/NIXBOT_TOKEN override hosts.toml. Without a URL the
        config's single host is used."""
        url = os.environ.get("NIXBOT_URL")
        token = os.environ.get("NIXBOT_TOKEN")
        hosts: dict[str, dict] = {}
        path = config_path()
        if path.exists():
            hosts = tomllib.loads(path.read_text())
        if not url:
            if len(hosts) != 1:
                msg = f"no server configured: set NIXBOT_URL or add one host to {path}"
                raise ValueError(msg)
            url = next(iter(hosts))
        entry = hosts.get(url, {})
        token = token or entry.get("token") or _run_token_command(entry)
        return cls(url.rstrip("/"), token)


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
