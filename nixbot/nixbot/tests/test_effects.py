"""Tests for effects secret resolution, scope rules, and the daemon's
wrapper around the nixbot_effects library (secrets, redaction, timeout)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from nixbot_effects import EffectError

from nixbot.effects import (
    EffectsContext,
    effect_push_url,
    list_effects,
    resolve_effects_secret,
    run_effect,
    should_run_effects,
)
from nixbot.repo_config import BranchConfig

if TYPE_CHECKING:
    from pathlib import Path

    from nixbot_effects import EffectsOptions

SECRETS = {
    "github:acme/widget": "widget-secret",
    "gitea:acme/*": "acme-org-secret",
}


def test_resolve_exact_match() -> None:
    assert (
        resolve_effects_secret(SECRETS, "github", "acme", "widget") == "widget-secret"
    )


def test_resolve_org_wildcard() -> None:
    assert (
        resolve_effects_secret(SECRETS, "gitea", "acme", "anything")
        == "acme-org-secret"
    )


def test_resolve_no_match() -> None:
    assert resolve_effects_secret(SECRETS, "github", "other", "repo") is None
    # Wildcard is per-forge: github:acme/* is not configured.
    assert resolve_effects_secret(SECRETS, "github", "acme", "other") is None


def test_should_run_effects_default_branch() -> None:
    assert should_run_effects(BranchConfig(), "main", "main", is_pull_request=False)


def test_should_run_effects_pr_gated() -> None:
    assert not should_run_effects(
        BranchConfig(), "main", "pr-branch", is_pull_request=True
    )
    assert should_run_effects(
        BranchConfig(effects_on_pull_requests=True),
        "main",
        "pr-branch",
        is_pull_request=True,
    )


def test_should_run_effects_pr_targeting_default_branch_gated() -> None:
    """Webhooks store the PR's BASE ref in event.branch, so a PR
    targeting the default branch must still respect the PR gate."""
    assert not should_run_effects(BranchConfig(), "main", "main", is_pull_request=True)
    assert should_run_effects(
        BranchConfig(effects_on_pull_requests=True),
        "main",
        "main",
        is_pull_request=True,
    )


def test_should_run_effects_branch_globs() -> None:
    config = BranchConfig(effects_branches=["release-*", "staging"])
    assert should_run_effects(config, "main", "release-1.0", is_pull_request=False)
    assert should_run_effects(config, "main", "staging", is_pull_request=False)
    assert not should_run_effects(config, "main", "feature", is_pull_request=False)


def test_effect_push_url() -> None:
    assert effect_push_url("github", "https://github.com/acme/widget", "tok") == (
        "https://x-access-token:tok@github.com/acme/widget"
    )
    assert effect_push_url("gitlab", "https://gitlab.example:8443/g/p.git", "tok") == (
        "https://oauth2:tok@gitlab.example:8443/g/p.git"
    )
    # ssh clone URLs have no token equivalent to push over.
    assert effect_push_url("github", "git@github.com:acme/widget.git", "tok") is None


def make_ctx(tmp_path: Path, secret_name: str | None = None) -> EffectsContext:
    return EffectsContext(
        path=tmp_path,
        rev="abc123",
        branch="main",
        repo="acme/widget",
        secret_name=secret_name,
    )


class LogSink:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.chunks.append(data)

    def text(self) -> str:
        return b"".join(self.chunks).decode()


async def test_run_effect_loads_secrets_and_redacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "widget-secret").write_text('{"token": "s3-longsecret"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    async def fake_run(opts: EffectsOptions, _effect: str) -> None:
        assert opts.log is not None
        await opts.log(b"deploying with s3-longsecret and forge-token-1\n")

    monkeypatch.setattr("nixbot_effects.run_effect", fake_run)
    ctx = make_ctx(tmp_path, secret_name="widget-secret")
    ctx.git_token = "forge-token-1"
    log = LogSink()
    assert await run_effect(ctx, "deploy", log.write)
    # The library got the parsed secrets. The log never shows them.
    assert ctx.secrets == {"token": "s3-longsecret"}
    assert "s3-longsecret" not in log.text()
    assert "forge-token-1" not in log.text()
    assert "deploying with *** and ***" in log.text()


async def test_run_effect_failure_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(_opts: EffectsOptions, _effect: str) -> None:
        msg = "bwrap failed with exit code 1"
        raise EffectError(msg)

    monkeypatch.setattr("nixbot_effects.run_effect", fake_run)
    log = LogSink()
    assert not await run_effect(make_ctx(tmp_path), "deploy", log.write)
    assert "error: bwrap failed with exit code 1" in log.text()


async def test_run_effect_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hanging_run(_opts: EffectsOptions, _effect: str) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr("nixbot_effects.run_effect", hanging_run)
    monkeypatch.setattr("nixbot.effects.DEFAULT_TIMEOUT", 0.1)
    log = LogSink()
    assert not await run_effect(make_ctx(tmp_path), "deploy", log.write)
    assert "timed out" in log.text()


async def test_list_effects_wraps_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hanging_list(_opts: EffectsOptions) -> list[str]:
        await asyncio.sleep(60)
        return []

    monkeypatch.setattr("nixbot_effects.list_effects", hanging_list)
    monkeypatch.setattr("nixbot.effects.DEFAULT_TIMEOUT", 0.1)
    with pytest.raises(EffectError, match="timed out"):
        await list_effects(make_ctx(tmp_path))
