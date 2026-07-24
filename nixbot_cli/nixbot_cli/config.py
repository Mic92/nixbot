"""Server URL and token from the environment or hosts.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


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
        return cls(url.rstrip("/"), token or hosts.get(url, {}).get("token"))
