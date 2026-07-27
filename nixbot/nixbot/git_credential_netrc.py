"""Git credential helper backed by a single netrc file."""

from __future__ import annotations

import netrc
import sys
from pathlib import Path
from urllib.parse import urlsplit

EXPECTED_ARGC = 3


def _credential_input() -> dict[str, str]:
    return {
        key: value
        for line in sys.stdin
        if "=" in line
        for key, value in (line.rstrip("\n").split("=", 1),)
    }


def main() -> None:
    if len(sys.argv) != EXPECTED_ARGC or sys.argv[2] != "get":
        return

    request = _credential_input()
    raw_host = request.get("host")
    if raw_host is None:
        return
    host = urlsplit(f"//{raw_host}").hostname
    if host is None:
        return

    auth = netrc.netrc(Path(sys.argv[1])).authenticators(host)
    if auth is None:
        return
    login, account, password = auth
    username = login or account
    if username is None or password is None:
        return
    if any(char in username or char in password for char in "\r\n"):
        return

    print(f"username={username}")
    print(f"password={password}")


if __name__ == "__main__":
    main()
