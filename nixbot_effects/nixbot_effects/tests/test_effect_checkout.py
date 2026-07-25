"""Tests for the runner-prepared effect checkout mount."""

from __future__ import annotations

from pathlib import Path

import pytest

from nixbot_effects import EffectError
from nixbot_effects.run import _bubblewrap_command
from nixbot_effects.sandbox import effect_checkout_mount


def test_undeclared_checkout_is_ignored() -> None:
    assert effect_checkout_mount({}, Path("/clone")) == ([], {})
    assert effect_checkout_mount({}, None) == ([], {})
    # mkDerivation renders `false` as the empty string.
    assert effect_checkout_mount({"__nixbot_effect_checkout": ""}, None) == ([], {})


def test_declared_checkout_mounts_and_exports_path() -> None:
    args, env = effect_checkout_mount(
        {"__nixbot_effect_checkout": "1"}, Path("/var/lib/clone")
    )
    assert args == [
        "--bind",
        "/var/lib/clone",
        "/build/checkout",
        "--chdir",
        "/build/checkout",
    ]
    assert env == {"NIXBOT_EFFECT_CHECKOUT": "/build/checkout"}


def test_declared_checkout_without_clone_errors() -> None:
    with pytest.raises(EffectError, match="no checkout clone"):
        effect_checkout_mount({"__nixbot_effect_checkout": "1"}, None)


def test_checkout_chdir_replaces_default() -> None:
    # bwrap warns when --chdir is passed twice, so the checkout's
    # working directory must replace the default /build one.
    checkout_args, _ = effect_checkout_mount(
        {"__nixbot_effect_checkout": "1"}, Path("/var/lib/clone")
    )
    cmd = _bubblewrap_command(
        "/nix/store/x.drv",
        "bwrap",
        build_dir=Path("/work/build"),
        etc_dir=Path("/work/etc"),
        daemon_socket=Path("/work/socket"),
        uid=1000,
        gid=100,
        extra_sandbox_paths=[],
        bind_mounts=[],
        checkout_args=checkout_args,
    )
    assert cmd.count("--chdir") == 1
    assert cmd[cmd.index("--chdir") + 1] == "/build/checkout"


def test_default_chdir_without_checkout() -> None:
    cmd = _bubblewrap_command(
        "/nix/store/x.drv",
        "bwrap",
        build_dir=Path("/work/build"),
        etc_dir=Path("/work/etc"),
        daemon_socket=Path("/work/socket"),
        uid=1000,
        gid=100,
        extra_sandbox_paths=[],
        bind_mounts=[],
        checkout_args=[],
    )
    assert cmd.count("--chdir") == 1
    assert cmd[cmd.index("--chdir") + 1] == "/build"
