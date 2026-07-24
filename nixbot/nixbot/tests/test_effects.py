"""Tests for effects secret resolution, scope rules, and CLI driving
(with a fake nixbot-effects on PATH)."""

# ruff: noqa: ARG001, S106 (fixtures used for side effects. Test secret names)

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from nixbot.effects import (
    EffectsContext,
    EffectsError,
    effect_push_url,
    list_effects,
    resolve_effects_secret,
    run_effect,
    should_run_effects,
)
from nixbot.repo_config import BranchConfig

if TYPE_CHECKING:
    from pathlib import Path

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


def install_fake_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """Install a fake nixbot-effects shell script on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "nixbot-effects"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")


@pytest.fixture
def fake_effects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    install_fake_effects(
        tmp_path,
        monkeypatch,
        f"""\
echo "$@" >> {tmp_path}/calls
case "$1" in
  list)
    # nix routinely logs to stderr while evaluating. Must not corrupt JSON.
    echo 'warning: Git tree is dirty' >&2
    echo '["deploy", "notify"]'
    ;;
  run)
    # Echo whether a secrets file was passed, its content, and its mode.
    prev=""
    for arg in "$@"; do
      if [ "$prev" = "--secrets" ]; then cat "$arg"; stat -c 'mode=%a' "$arg"; fi
      prev="$arg"
    done
    echo "HOME=$HOME"
    echo "running effect"
    ;;
esac
""",
    )
    return tmp_path


def make_ctx(tmp_path: Path, secret_name: str | None = None) -> EffectsContext:
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return EffectsContext(
        worktree_path=worktree,
        rev="abc123",
        branch="main",
        repo="acme/widget",
        secret_name=secret_name,
    )


async def test_list_effects(tmp_path: Path, fake_effects: Path) -> None:
    effects = await list_effects(make_ctx(tmp_path))
    assert effects == ["deploy", "notify"]
    calls = (fake_effects / "calls").read_text()
    assert "--rev abc123" in calls
    assert "--branch main" in calls
    assert "--repo acme/widget" in calls


async def test_run_effect_with_secrets(
    tmp_path: Path, fake_effects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "widget-secret").write_text('{"token": "s3-longsecret"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))

    chunks: list[bytes] = []

    async def log_write(data: bytes) -> None:
        chunks.append(data)

    ctx = make_ctx(tmp_path, secret_name="widget-secret")
    ok = await run_effect(ctx, "deploy", log_write)
    assert ok
    output = b"".join(chunks).decode()
    # The effect echoes the secrets file. The deploy secret must be
    # masked in the log even though the CLI received the real file.
    assert "s3-longsecret" not in output
    assert "***" in output
    # Deploy credentials must not be world/group readable while on disk.
    assert "mode=600" in output
    assert "running effect" in output
    # Secrets file cleaned up afterwards.
    assert not list(tmp_path.glob("*-secrets.json"))


async def test_run_effect_inherits_environment(
    tmp_path: Path, fake_effects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Remote builders need $HOME for ~/.ssh. The service environment
    # must be inherited by nixbot-effects.
    monkeypatch.setenv("HOME", "/var/lib/nixbot-test")
    chunks: list[bytes] = []

    async def log_write(data: bytes) -> None:
        chunks.append(data)

    ok = await run_effect(make_ctx(tmp_path), "deploy", log_write)
    assert ok
    assert b"HOME=/var/lib/nixbot-test\n" in b"".join(chunks)


async def test_long_output_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single output line larger than asyncio's 64 KiB StreamReader
    # default must not abort the run.
    install_fake_effects(
        tmp_path, monkeypatch, 'head -c 200000 /dev/zero | tr "\\0" x\necho\n'
    )

    ok = await run_effect(make_ctx(tmp_path), "deploy")
    assert ok


async def test_timeout_kills_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_effects(tmp_path, monkeypatch, "sleep 60\n")

    ctx = make_ctx(tmp_path)
    ctx.timeout = 0.2
    with pytest.raises(EffectsError, match="timed out"):
        await run_effect(ctx, "deploy")


async def test_run_effect_extra_sandbox_paths(
    tmp_path: Path, fake_effects: Path
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.extra_sandbox_paths = [tmp_path / "extra"]
    await run_effect(ctx, "deploy")
    calls = (fake_effects / "calls").read_text()
    assert f"--extra-sandbox-path {tmp_path / 'extra'}" in calls


async def test_run_effect_passes_task_flags(tmp_path: Path, fake_effects: Path) -> None:
    ctx = make_ctx(tmp_path)
    ctx.default_branch = "main"
    ctx.git_token = "forge-tok"  # noqa: S105
    ctx.task_token = "task-tok"  # noqa: S105
    ctx.api_base_url = "https://ci.example"
    ctx.project_id = "42"
    assert await run_effect(ctx, "deploy")
    call = (tmp_path / "calls").read_text().splitlines()[-1]
    assert "--default-branch main" in call
    assert "--git-token-file" in call
    assert "--task-token-file" in call
    assert "--api-base-url https://ci.example" in call
    assert "--project-id 42" in call
    assert "--project-path" in call
    # Token side files are cleaned up after the run.
    assert not list(tmp_path.glob("*git-token*"))
    assert not list(tmp_path.glob("*task-token*"))


def test_effect_push_url() -> None:
    assert effect_push_url("github", "https://github.com/acme/widget", "tok") == (
        "https://x-access-token:tok@github.com/acme/widget"
    )
    assert effect_push_url("gitlab", "https://gitlab.example:8443/g/p.git", "tok") == (
        "https://oauth2:tok@gitlab.example:8443/g/p.git"
    )
    # ssh clone URLs have no token equivalent to push over.
    assert effect_push_url("github", "git@github.com:acme/widget.git", "tok") is None


async def test_run_effect_passes_effect_checkout(
    tmp_path: Path, fake_effects: Path
) -> None:
    ctx = make_ctx(tmp_path)
    ctx.effect_checkout = tmp_path / "clone"
    assert await run_effect(ctx, "deploy")
    call = (tmp_path / "calls").read_text().splitlines()[-1]
    assert f"--effect-checkout {tmp_path / 'clone'}" in call
