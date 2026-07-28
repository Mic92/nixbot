"""Per-run task tokens and the Hercules state API for effects.

Effects persist small files between runs (`getStateFile`/
`putStateFile` in hercules-ci-effects: nixops state, ssh known
hosts). The agent proxies these to hercules-ci. We serve them from
the state directory, scoped per project, authorized by a per-run
bearer token that the service mints for each effect invocation.

The same token authenticates the workload-identity endpoint; runs of
actual effects (not discovery) carry an EffectIdentity there.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from pathlib import Path

    from .workload_identity import EffectIdentity


@dataclass
class TaskClaim:
    project_id: int
    # None for discovery-phase runs: they must not mint ID tokens.
    identity: EffectIdentity | None = None
    # ID tokens minted so far, for the per-run rate limit.
    id_tokens_issued: int = 0


class TaskTokens:
    """In-memory per-run tokens. An effect only runs while the service
    is up, so restart-safety is not needed."""

    def __init__(self) -> None:
        self._tokens: dict[str, TaskClaim] = {}

    def issue(self, project_id: int, identity: EffectIdentity | None = None) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = TaskClaim(project_id=project_id, identity=identity)
        return token

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def project_for(self, token: str) -> int | None:
        claim = self._tokens.get(token)
        return claim.project_id if claim is not None else None

    def claim_for(self, token: str) -> TaskClaim | None:
        return self._tokens.get(token)


def state_file_path(state_dir: Path, project_id: int, name: str) -> Path:
    """State names come from untrusted flakes. Percent-encode so they
    cannot escape the per-project directory. quote() keeps dots, so
    "." and ".." (the directory and its parent) need rejecting."""
    if name in {".", ".."}:
        msg = f"invalid state name: {name!r}"
        raise ValueError(msg)
    return state_dir / "effects-state" / str(project_id) / quote(name, safe="")
