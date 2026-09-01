"""onEvent effects: listing from the flake and matching `when` against
delivery payloads without Nix."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nixbot_effects import EffectError, EventEffectMeta
from nixbot_effects.eval import (
    instantiate_event_effect,
    list_all_event_effects,
    list_event_effects,
)
from nixbot_effects.match import expand_lock, payload_env, skip_reason
from nixbot_effects.options import EffectsOptions
from nixbot_effects.tests.support import init_repo

if TYPE_CHECKING:
    from pathlib import Path

FLAKE_NIX = """\
{
  outputs = { self, ... }:
    let
      # mkDerivation merges passthru into the result set. Emulate that.
      effect = attrs: derivation {
        name = "effect";
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [ "-c" "echo > $out" ];
        isEffect = true;
      } // attrs;
    in {
      herculesCI = { primaryRepo, ... }: {
        onEvent.pull_request = {
          plan = effect {
            when = { permission = "write"; };
            lock = "infra-{pr}";
            __nixbot_effect_checkout = true;
          };
          env.notify = effect { };
        };
        onEvent.comment.bad = effect { when.typo = 1; };
      };
    };
}
"""


async def test_list_event_effects(tmp_path: Path) -> None:
    repo, _ = init_repo(tmp_path, {"flake.nix": FLAKE_NIX})
    opts = EffectsOptions(path=repo)
    assert await list_event_effects(opts, "pull_request") == {
        "plan": EventEffectMeta(
            when={"permission": "write"}, lock="infra-{pr}", checkout=True
        ),
        "env.notify": EventEffectMeta(),
    }
    assert await list_event_effects(opts, "build_finished") == {}
    with pytest.raises(EffectError, match="unknown `when` keys: typo"):
        await list_event_effects(opts, "comment")
    with pytest.raises(EffectError, match="unknown event kind"):
        await list_event_effects(opts, "push")
    # The service lists all kinds at once. One bad `when` anywhere
    # fails the whole listing, like a broken default branch.
    with pytest.raises(EffectError, match="unknown `when` keys: typo"):
        await list_all_event_effects(opts)
    (tmp_path / "fixed").mkdir()
    fixed, _ = init_repo(
        tmp_path / "fixed",
        {"flake.nix": FLAKE_NIX.replace("when.typo = 1;", "")},
    )
    listing = await list_all_event_effects(EffectsOptions(path=fixed))
    assert set(listing) == {"pull_request", "comment"}
    assert set(listing["pull_request"]) == {"plan", "env.notify"}
    drv, should_run = await instantiate_event_effect(
        "pull_request", "env.notify", opts, tmp_path / "gcroot"
    )
    assert drv.endswith("-effect.drv")
    assert should_run


PR = {
    "number": 7,
    "headRev": "abc",
    "baseRef": "main",
    "labels": ["preview"],
    "author": {"name": "github:dave", "permission": "read"},
}
BUILD = {"status": "succeeded", "url": "https://ci/b/1", "branch": "main"}


def test_permission_actor_or_author() -> None:
    when = {"permission": "write"}
    assert skip_reason(when, {"pullRequest": PR}) == (
        "needs write access (author github:dave: read)"
    )
    assert skip_reason(when, {}) == "needs write access (no actor)"
    actor = {"name": "github:alice", "permission": "admin"}
    assert skip_reason(when, {"pullRequest": PR, "actor": actor}) is None
    author_ok = {**PR, "author": {"name": "github:dave", "permission": "write"}}
    assert skip_reason(when, {"pullRequest": author_ok, "actor": None}) is None
    # Commands count the commenter only.
    outsider = {"name": "github:eve", "permission": "read"}
    assert (
        skip_reason(
            when, {"pullRequest": author_ok, "actor": outsider, "command": "apply"}
        )
        == "needs write access (actor github:eve: read)"
    )


def test_labels_branches_commands_status() -> None:
    payload = {"pullRequest": PR, "build": BUILD, "command": "plan"}
    assert skip_reason({"labels": ["preview"]}, payload) is None
    assert skip_reason({"labels": ["preview", "x"]}, payload) == "missing labels: x"
    assert skip_reason({"branches": ["release-*"]}, payload) == (
        "branch 'main' matches none of release-*"
    )
    assert skip_reason({"branches": ["ma*"]}, payload) is None
    # Without a PR the build branch counts.
    assert skip_reason({"branches": ["main"]}, {"build": BUILD}) is None
    assert skip_reason({"commands": ["apply"]}, payload) == (
        "command /plan not in apply"
    )
    assert skip_reason({"status": ["succeeded"]}, payload) is None
    assert skip_reason({"status": ["succeeded"]}, {"pullRequest": PR}) == (
        "no build for this commit"
    )


def test_transition() -> None:
    def build(now: str, prev: str | None) -> dict:
        return {"build": {**BUILD, "status": now, "previousStatus": prev}}

    assert skip_reason({"transition": "broke"}, build("failed", "succeeded")) is None
    assert skip_reason({"transition": "broke"}, build("failed", None)) is None
    assert skip_reason({"transition": "broke"}, build("failed", "failed")) == (
        "not a broke transition (failed -> failed)"
    )
    assert skip_reason({"transition": "fixed"}, build("succeeded", "failed")) is None
    assert skip_reason({"transition": "fixed"}, build("succeeded", None)) == (
        "not a fixed transition (none -> succeeded)"
    )


def test_expand_lock_and_env() -> None:
    assert expand_lock("infra-{pr}", {"pullRequest": PR}) == "infra-7"
    assert expand_lock("infra", {}) == "infra"
    assert expand_lock(None, {}) is None
    with pytest.raises(EffectError, match="no pull request"):
        expand_lock("x-{pr}", {"build": BUILD})
    env = payload_env(
        "comment",
        {
            "actor": {"name": "github:alice", "permission": "write"},
            "pullRequest": PR,
            "build": BUILD,
            "command": "plan",
            "args": "-target=foo",
        },
    )
    assert env == {
        "NIXBOT_EVENT_KIND": "comment",
        "NIXBOT_EVENT_JSON": "/run/event.json",
        "NIXBOT_ACTOR": "github:alice",
        "NIXBOT_PR_NUMBER": "7",
        "NIXBOT_PR_HEAD": "abc",
        "NIXBOT_COMMAND": "plan",
        "NIXBOT_COMMAND_ARGS": "-target=foo",
        "NIXBOT_BUILD_STATUS": "succeeded",
        "NIXBOT_BUILD_URL": "https://ci/b/1",
    }
    # Partial hand-written payloads (local runs) must not crash.
    assert payload_env("comment", {"pullRequest": {"number": 1}}) == {
        "NIXBOT_EVENT_KIND": "comment",
        "NIXBOT_EVENT_JSON": "/run/event.json",
        "NIXBOT_PR_NUMBER": "1",
    }
