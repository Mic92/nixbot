"""`when.changed` of onPush effects."""

from __future__ import annotations

import pytest

from nixbot_effects import EffectError
from nixbot_effects.match import push_changed, validate_when


def test_string_rejected() -> None:
    with pytest.raises(EffectError, match="must be an attribute set of strings"):
        push_changed("hil", {"changed": "/nix/store/x-fw"})


def test_attrset_is_kept() -> None:
    changed = {"firmware": "/nix/store/x-fw", "flasher": "/nix/store/y-fl"}
    assert push_changed("hil", {"changed": changed}) == changed


def test_no_changed_is_none() -> None:
    assert push_changed("hil", {}) is None


def test_non_string_input_rejected() -> None:
    with pytest.raises(EffectError, match="must be an attribute set of strings"):
        push_changed("hil", {"changed": {"rev": 1}})


def test_other_when_keys_rejected_on_push() -> None:
    with pytest.raises(EffectError, match=r"only support `when\.changed`.*labels"):
        push_changed("hil", {"changed": {"a": "x"}, "labels": ["a"]})


def test_changed_rejected_on_event_effects() -> None:
    with pytest.raises(EffectError, match="only supported for onPush"):
        validate_when("hil", {"changed": {"a": "x"}})


def test_limits_accept_the_maximum() -> None:
    changed = {f"i{n}": "x" * 1024 for n in range(32)}
    assert push_changed("hil", {"changed": changed}) == changed


def test_too_many_inputs_rejected() -> None:
    changed = {f"i{n}": "x" for n in range(33)}
    with pytest.raises(EffectError, match="at most 32 inputs"):
        push_changed("hil", {"changed": changed})


def test_long_value_rejected() -> None:
    with pytest.raises(EffectError, match=r"'firmware'.*limit of 1024 bytes"):
        push_changed("hil", {"changed": {"firmware": "x" * 1025}})


def test_value_limit_counts_bytes() -> None:
    with pytest.raises(EffectError, match="limit of 1024 bytes"):
        push_changed("hil", {"changed": {"firmware": "ä" * 513}})


def test_empty_input_name_rejected() -> None:
    with pytest.raises(EffectError, match="names must not be empty"):
        push_changed("hil", {"changed": {"": "x"}})
