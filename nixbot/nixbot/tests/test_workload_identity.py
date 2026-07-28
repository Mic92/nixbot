"""Issuer core for effect workload identity: key handling, discovery
documents, and claim construction. Endpoint behaviour is covered in
test_web.py."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

if TYPE_CHECKING:
    from pathlib import Path

from nixbot.workload_identity import (
    EffectIdentity,
    IdentityIssuer,
    load_issuer,
)

ISSUER_URL = "https://nixbot.example.com"


def push_identity(**overrides: object) -> EffectIdentity:
    fields: dict = {
        "forge": "github",
        "owner": "acme",
        "repo": "widgets",
        "event": "push",
        "effect": "default.deploy",
        "build_id": 42,
        "sha": "0" * 40,
        "branch": "main",
        "allowed_audiences": ("https://cache.example.com",),
    }
    fields.update(overrides)
    return EffectIdentity(**fields)


def make_issuer(tmp_path: Path) -> IdentityIssuer:
    return load_issuer(tmp_path, ISSUER_URL)


def decode(issuer: IdentityIssuer, token: str) -> dict:
    key_set = KeySet.import_key_set(issuer.jwks())
    return jwt.decode(token, key_set, algorithms=["RS256"]).claims


# --- keys ---------------------------------------------------------------


def test_key_generated_and_persisted(tmp_path: Path) -> None:
    a = load_issuer(tmp_path, ISSUER_URL)
    b = load_issuer(tmp_path, ISSUER_URL)
    assert (tmp_path / "workload-identity-key.pem").exists()
    assert a.jwks() == b.jwks()
    assert len(a.jwks()["keys"]) == 1
    assert a.jwks()["keys"][0]["kid"]


def test_key_rotation_keeps_previous_in_jwks(tmp_path: Path) -> None:
    old = load_issuer(tmp_path, ISSUER_URL)
    old_kid = old.jwks()["keys"][0]["kid"]
    # Age the key file beyond the rotation interval.
    key_file = tmp_path / "workload-identity-key.pem"
    past = datetime.now(tz=UTC) - timedelta(days=40)
    os.utime(key_file, (past.timestamp(), past.timestamp()))

    rotated = load_issuer(tmp_path, ISSUER_URL)
    kids = [k["kid"] for k in rotated.jwks()["keys"]]
    assert old_kid in kids
    assert len(kids) == 2  # noqa: PLR2004
    assert kids[0] != old_kid  # new key signs
    # Tokens signed by the old issuer still verify against the new JWKS.
    issued = old.mint(push_identity(), "https://cache.example.com")
    key_set = KeySet.import_key_set(rotated.jwks())
    assert jwt.decode(issued.token, key_set, algorithms=["RS256"]).claims


def test_operator_provided_key_is_not_rotated(tmp_path: Path) -> None:
    pem = RSAKey.generate_key(2048).as_pem(private=True)
    key_file = tmp_path / "provided.pem"
    key_file.write_bytes(pem)
    past = datetime.now(tz=UTC) - timedelta(days=400)
    os.utime(key_file, (past.timestamp(), past.timestamp()))
    issuer = load_issuer(tmp_path, ISSUER_URL, signing_key_file=key_file)
    assert len(issuer.jwks()["keys"]) == 1
    assert not (tmp_path / "workload-identity-key.pem").exists()


# --- discovery ----------------------------------------------------------


def test_discovery_document(tmp_path: Path) -> None:
    doc = make_issuer(tmp_path).discovery_document()
    assert doc["issuer"] == ISSUER_URL
    assert doc["jwks_uri"] == f"{ISSUER_URL}/.well-known/jwks.json"
    assert "RS256" in doc["id_token_signing_alg_values_supported"]
    assert "sub" in doc["claims_supported"]


# --- claims -------------------------------------------------------------


def test_push_claims(tmp_path: Path) -> None:
    issuer = make_issuer(tmp_path)
    issued = issuer.mint(push_identity(), "https://cache.example.com")
    claims = decode(issuer, issued.token)
    assert claims["iss"] == ISSUER_URL
    assert claims["aud"] == "https://cache.example.com"
    assert claims["sub"] == "repo:github:acme/widgets:ref:refs/heads/main"
    assert claims["ref"] == "refs/heads/main"
    assert claims["event"] == "push"
    assert claims["repository"] == "acme/widgets"
    assert claims["repository_owner"] == "acme"
    assert claims["forge"] == "github"
    assert claims["effect"] == "default.deploy"
    assert claims["build_id"] == 42  # noqa: PLR2004
    assert claims["sha"] == "0" * 40
    assert claims["exp"] > claims["iat"]
    assert claims["nbf"] < claims["iat"]
    assert claims["jti"]
    assert issued.expires_at.tzinfo is not None


def test_pull_request_claims_have_no_ref(tmp_path: Path) -> None:
    issuer = make_issuer(tmp_path)
    identity = push_identity(
        event="pull_request", pr_number=7, base_ref="refs/heads/main", branch="main"
    )
    claims = decode(issuer, issuer.mint(identity, "aud").token)
    assert claims["sub"] == "repo:github:acme/widgets:pull_request"
    assert claims["event"] == "pull_request"
    assert claims["pr_number"] == 7  # noqa: PLR2004
    assert claims["base_ref"] == "refs/heads/main"
    assert "ref" not in claims


def test_schedule_claims(tmp_path: Path) -> None:
    issuer = make_issuer(tmp_path)
    identity = push_identity(
        event="schedule", schedule="flake-update", branch=None, sha=None, build_id=None
    )
    claims = decode(issuer, issuer.mint(identity, "aud").token)
    assert claims["sub"] == "repo:github:acme/widgets:schedule:flake-update"
    assert claims["event"] == "schedule"
    assert claims["schedule"] == "flake-update"
    assert "ref" not in claims
    assert "sha" not in claims
    assert "build_id" not in claims
