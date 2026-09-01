"""OIDC issuer for effect workload identity.

nixbot mints short-lived RS256 ID tokens for running effects, so they
can authenticate to relying parties (Vault, AWS, niks3, ...) without
static deploy secrets. Key handling, the discovery documents, and
claim construction live here; the HTTP endpoints are in
web/workload_routes.py and the per-run authorization (which effect may
request which audience) in effects_state.TaskTokens.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from joserfc import jwt
from joserfc.jwk import KeySetSerialization, RSAKey

if TYPE_CHECKING:
    from pathlib import Path

    from .events import ChangeEvent

# Relying parties commonly allow only small skew; nbf is backdated so a
# slightly-behind verifier clock does not reject fresh tokens.
CLOCK_SKEW = 60

KEY_FILENAME = "workload-identity-key.pem"
PREVIOUS_KEY_FILENAME = "workload-identity-key.previous.pem"

DEFAULT_TOKEN_TTL = 300
DEFAULT_ROTATION_INTERVAL = timedelta(days=30)

RSA_KEY_SIZE = 2048


@dataclass(frozen=True)
class EffectIdentity:
    """What one effect run is, as attested in its ID tokens."""

    forge: str
    owner: str
    repo: str
    # "push" | "pull_request" | "schedule"
    event: str
    # Dotted effect name, e.g. "default.deploy".
    effect: str
    build_id: int | None = None
    sha: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    base_ref: str | None = None
    schedule: str | None = None
    # Audiences the effect declared via idTokenAudiences. Enforced by
    # the token endpoint, not by mint().
    allowed_audiences: tuple[str, ...] = ()
    actor: str | None = None

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def sub(self) -> str:
        prefix = f"repo:{self.forge}:{self.repository}"
        match self.event:
            case "pull_request":
                return f"{prefix}:pull_request"
            case "schedule":
                return f"{prefix}:schedule:{self.schedule}"
            case "push":
                return f"{prefix}:ref:refs/heads/{self.branch}"
            case _:
                # onEvent kinds. Code is the default branch, but the
                # trigger is untrusted, so never look like a push.
                return f"{prefix}:event:{self.event}"

    def claims(self) -> dict[str, Any]:
        claims: dict[str, Any] = {
            "sub": self.sub,
            "event": self.event,
            "forge": self.forge,
            "repository": self.repository,
            "repository_owner": self.owner,
            "effect": self.effect,
        }
        if self.build_id is not None:
            claims["build_id"] = self.build_id
        if self.sha is not None:
            claims["sha"] = self.sha
        match self.event:
            case "push":
                claims["ref"] = f"refs/heads/{self.branch}"
            case "pull_request":
                # No "ref": nixbot stores the PR's *base* branch, which
                # must not satisfy branch-based relying-party conditions.
                claims["pr_number"] = self.pr_number
                if self.base_ref is not None:
                    claims["base_ref"] = self.base_ref
            case "schedule":
                claims["schedule"] = self.schedule
            case _:
                if self.pr_number is not None:
                    claims["pr_number"] = self.pr_number
        if self.actor is not None:
            claims["actor"] = self.actor
        return claims


def identity_from_event(
    event: ChangeEvent, effect: str, build_id: int
) -> EffectIdentity:
    """The identity of one push- or PR-triggered effect run. Audiences
    are bound later, once the effect derivation is evaluated."""
    repo = event.repo
    identity = EffectIdentity(
        forge=repo.forge,
        owner=repo.owner,
        repo=repo.repo,
        event="push",
        effect=effect,
        build_id=build_id,
        sha=event.commit_sha,
        branch=event.branch,
    )
    if event.pr_number is None:
        return identity
    return dataclasses.replace(
        identity,
        event="pull_request",
        pr_number=event.pr_number,
        branch=None,
        # ChangeEvent.branch of a PR is the base branch.
        base_ref=f"refs/heads/{event.branch}",
    )


@dataclass(frozen=True)
class IssuedToken:
    token: str
    expires_at: datetime


class IdentityIssuer:
    """Signs ID tokens with the current key; retired keys stay in the
    JWKS so in-flight tokens keep verifying."""

    def __init__(
        self,
        issuer_url: str,
        keys: list[RSAKey],
        token_ttl: int = DEFAULT_TOKEN_TTL,
    ) -> None:
        self.issuer_url = issuer_url.rstrip("/")
        self._keys = keys
        self.token_ttl = token_ttl

    @property
    def _signing_key(self) -> RSAKey:
        return self._keys[0]

    def jwks(self) -> KeySetSerialization:
        return {
            "keys": [key.as_dict(private=False) for key in self._keys],
        }

    def discovery_document(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer_url,
            "jwks_uri": f"{self.issuer_url}/.well-known/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
            "response_types_supported": ["id_token"],
            "subject_types_supported": ["public"],
            "claims_supported": [
                "sub",
                "aud",
                "exp",
                "iat",
                "iss",
                "jti",
                "nbf",
                "event",
                "forge",
                "repository",
                "repository_owner",
                "ref",
                "base_ref",
                "sha",
                "pr_number",
                "schedule",
                "effect",
                "build_id",
            ],
        }

    def mint(self, identity: EffectIdentity, audience: str) -> IssuedToken:
        now = int(time.time())
        expires_at = datetime.fromtimestamp(now + self.token_ttl, tz=UTC)
        claims = {
            "iss": self.issuer_url,
            "aud": audience,
            "iat": now,
            "nbf": now - CLOCK_SKEW,
            "exp": now + self.token_ttl,
            "jti": secrets.token_urlsafe(16),
            **identity.claims(),
        }
        header = {"alg": "RS256", "kid": self._signing_key.kid, "typ": "JWT"}
        token = jwt.encode(header, claims, self._signing_key)
        return IssuedToken(token=token, expires_at=expires_at)


def _load_key(pem: bytes) -> RSAKey:
    key = RSAKey.import_key(pem)
    key.ensure_kid()
    return key


def _generate_key(path: Path) -> RSAKey:
    key = RSAKey.generate_key(RSA_KEY_SIZE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key.as_pem(private=True))
    tmp.chmod(0o600)
    tmp.rename(path)
    key.ensure_kid()
    return key


def load_issuer(
    state_dir: Path,
    issuer_url: str,
    *,
    signing_key_file: Path | None = None,
    token_ttl: int = DEFAULT_TOKEN_TTL,
    key_rotation_interval: timedelta = DEFAULT_ROTATION_INTERVAL,
) -> IdentityIssuer:
    """The issuer with its current key, generating or rotating the
    auto-managed key as needed. An operator-provided key is used as-is
    and never rotated."""
    if signing_key_file is not None:
        return IdentityIssuer(
            issuer_url, [_load_key(signing_key_file.read_bytes())], token_ttl
        )

    key_file = state_dir / KEY_FILENAME
    previous_file = state_dir / PREVIOUS_KEY_FILENAME
    keys: list[RSAKey] = []
    if key_file.exists():
        age = datetime.now(tz=UTC) - datetime.fromtimestamp(
            key_file.stat().st_mtime, tz=UTC
        )
        if age > key_rotation_interval:
            # Rotate: the old key moves to .previous so tokens signed
            # before the rotation keep verifying until they expire.
            key_file.replace(previous_file)
            keys = [_generate_key(key_file), _load_key(previous_file.read_bytes())]
        else:
            keys = [_load_key(key_file.read_bytes())]
            if previous_file.exists():
                keys.append(_load_key(previous_file.read_bytes()))
    else:
        keys = [_generate_key(key_file)]
    return IdentityIssuer(issuer_url, keys, token_ttl)
