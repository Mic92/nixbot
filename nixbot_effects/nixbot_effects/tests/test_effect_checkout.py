"""Tests for the runner-prepared effect checkout mount."""

from __future__ import annotations

from pathlib import Path

import pytest

from nixbot_effects import EffectError
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
