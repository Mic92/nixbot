"""Tests for the flake input prefetch step (pure parts plus an
optional integration test against a real nix)."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from nixbot.flake_prefetch import (
    PrefetchError,
    _prefetch_env,
    build_prefetch_command,
    collect_input_paths,
    prefetch_flake_inputs,
)
from nixbot.gitrepo import FetchCredentials
from nixbot.nix_eval import EvalRunner, EvalSettings
from nixbot.repo_config import BranchConfig
from nixbot.tests.support import init_upstream

if TYPE_CHECKING:
    from pathlib import Path

needs_nix = pytest.mark.skipif(shutil.which("nix") is None, reason="nix not available")


def test_prefetch_command(tmp_path: Path) -> None:
    cmd = build_prefetch_command(tmp_path, BranchConfig())
    assert cmd[0] == "nix"
    assert "prefetch-inputs" in cmd
    assert "--no-write-lock-file" in cmd
    assert "--reference-lock-file" not in cmd
    assert cmd[-1] == str(tmp_path)

    cmd = build_prefetch_command(tmp_path, BranchConfig(lock_file="alt.lock"))
    assert cmd[cmd.index("--reference-lock-file") + 1] == "alt.lock"


def test_collect_input_paths_recursive() -> None:
    archive = {
        "path": "/nix/store/aaa-source",
        "inputs": {
            "a": {
                "path": "/nix/store/bbb-source",
                "inputs": {"nested": {"path": "/nix/store/ccc-source", "inputs": {}}},
            },
            # follows-style entries without a path are skipped.
            "b": {"inputs": {}},
        },
    }
    assert collect_input_paths(archive) == {
        "/nix/store/bbb-source",
        "/nix/store/ccc-source",
    }


def test_prefetch_env_uses_credentials(tmp_path: Path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine x login y password z\n")
    home = tmp_path / "home"
    home.mkdir()
    creds = FetchCredentials(netrc_file=netrc, ssh_private_key_file=tmp_path / "key")
    env = _prefetch_env(creds, home)
    assert str(tmp_path / "key") in env["GIT_SSH_COMMAND"]
    assert (home / ".netrc").readlink() == netrc
    assert env["NIX_CONFIG"] == f"netrc-file = {netrc}"
    assert env["HOME"] == str(home)
    assert "NIX_CACHE_HOME" not in env

    home2 = tmp_path / "home2"
    home2.mkdir()
    env = _prefetch_env(creds, home2, cache_dir=tmp_path / "cache")
    assert env["NIX_CACHE_HOME"] == str(tmp_path / "cache")


async def test_prefetch_skips_repo_without_flake(tmp_path: Path) -> None:
    await prefetch_flake_inputs(tmp_path, BranchConfig(), tmp_path / "gcroots")
    assert not (tmp_path / "gcroots").exists()


def _nix_flake_lock(repo: Path) -> None:
    subprocess.run(
        [
            "nix",
            "--option",
            "extra-experimental-features",
            "nix-command flakes",
            "flake",
            "lock",
        ],
        cwd=repo,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null"},
    )


@needs_nix
async def test_prefetch_integration(tmp_path: Path) -> None:
    """A locked git input is copied to the store and gc-rooted; after
    the input's source repo is gone, evaluation still succeeds purely
    from the store (the sandboxed eval has no credentials for private
    inputs)."""
    dep = init_upstream(
        tmp_path / "dep",
        {"flake.nix": '{ outputs = { self }: { greeting = "hello"; }; }'},
    )
    repo = init_upstream(
        tmp_path / "repo",
        {
            "flake.nix": f"""
            {{
              inputs.dep.url = "git+file://{dep}";
              outputs = {{ self, dep }}: {{
                checks.x86_64-linux.ok = derivation {{
                  name = "ok-" + dep.greeting;
                  system = "x86_64-linux";
                  builder = "/bin/sh";
                  args = [ "-c" "echo ok > $out" ];
                }};
              }};
            }}
            """
        },
    )
    _nix_flake_lock(repo)

    gcroots = tmp_path / "gcroots"
    await prefetch_flake_inputs(repo, BranchConfig(), gcroots)

    roots = list((gcroots / "inputs").iterdir())
    assert len(roots) == 1
    assert roots[0].is_symlink()
    assert roots[0].resolve().is_dir()

    # The eval must not need the input's origin anymore.
    shutil.rmtree(dep)

    if shutil.which("nix-eval-jobs") is None:
        pytest.skip("nix-eval-jobs not available")
    settings = EvalSettings(
        gc_roots_dir=tmp_path / "eval-gcroots", sandbox=False, systemd_scope=False
    )
    result = await EvalRunner().run(repo, BranchConfig(), settings)
    assert [j.attr for j in result.jobs] == ["default.checks.x86_64-linux.ok"]


@needs_nix
async def test_prefetch_failure_raises(tmp_path: Path) -> None:
    repo = init_upstream(
        tmp_path / "repo",
        {
            "flake.nix": """
            {
              inputs.missing.url = "git+file:///does/not/exist";
              outputs = { self, missing }: { };
            }
            """
        },
    )
    with pytest.raises(PrefetchError):
        await prefetch_flake_inputs(repo, BranchConfig(), tmp_path / "gcroots")
