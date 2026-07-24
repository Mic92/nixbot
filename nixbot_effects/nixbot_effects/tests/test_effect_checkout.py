"""Tests for the runner-prepared effect checkout mount."""

from __future__ import annotations

from pathlib import Path

import pytest

from nixbot_effects import NixbotEffectsError, effect_checkout_mount


def test_undeclared_checkout_is_ignored() -> None:
    assert effect_checkout_mount({}, Path("/clone")) == ([], {})
    assert effect_checkout_mount({}, None) == ([], {})


def test_declared_checkout_mounts_and_exports_path() -> None:
    args, env = effect_checkout_mount(
        {"__nixbot_effect_checkout": "/build/repo"}, Path("/var/lib/clone")
    )
    assert args == ["--bind", "/var/lib/clone", "/build/repo"]
    assert env == {"NIXBOT_EFFECT_CHECKOUT": "/build/repo"}


def test_declared_checkout_without_clone_or_bad_path_errors() -> None:
    with pytest.raises(NixbotEffectsError, match="--effect-checkout"):
        effect_checkout_mount({"__nixbot_effect_checkout": "/build/repo"}, None)
    with pytest.raises(NixbotEffectsError, match="absolute"):
        effect_checkout_mount({"__nixbot_effect_checkout": "repo"}, Path("/clone"))
