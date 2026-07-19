"""Effect DAG validation and ASCII rendering."""

from __future__ import annotations

import pytest

from nixbot_effects.graph import EffectGraphError, EffectMeta, render_tree


def test_unknown_dep_rejected() -> None:
    with pytest.raises(EffectGraphError, match="unknown effect 'missing'"):
        render_tree({"deploy": EffectMeta(after=("missing",))})


def test_cycle_rejected() -> None:
    with pytest.raises(EffectGraphError, match="cycle"):
        render_tree({"a": EffectMeta(after=("b",)), "b": EffectMeta(after=("a",))})


def test_render_tree_shows_locks_and_nesting() -> None:
    meta = {
        "build-image": EffectMeta(),
        "deploy-staging": EffectMeta(after=("build-image",), lock="hw-lab"),
        "deploy-prod": EffectMeta(after=("deploy-staging",), lock="hw-lab"),
        "notify": EffectMeta(),
    }
    assert render_tree(meta) == (
        "build-image\n"
        "└── deploy-staging [lock: hw-lab]\n"
        "    └── deploy-prod [lock: hw-lab]\n"
        "notify"
    )
