"""hercules-ci-agent compatibility of the herculesCI arguments."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nixbot_effects.eval import list_effects
from nixbot_effects.options import EffectsOptions
from nixbot_effects.tests.support import init_repo

if TYPE_CHECKING:
    from pathlib import Path


FLAKE_NIX = """\
{
  outputs = { self, ... }: {
    herculesCI = { herculesCI, ... }:
      assert herculesCI ? ciSystems; {
        ciSystems = [ "x86_64-linux" "aarch64-darwin" ];
        onPush.default.outputs = { ciSystems, ... }:
          assert ciSystems == { x86_64-linux = { }; aarch64-darwin = { }; }; {
            effects.deploy = derivation {
              name = "deploy";
              system = builtins.currentSystem;
              builder = "/bin/sh";
              args = [ "-c" "echo deploy > $out" ];
            };
          };
      };
  };
}
"""


async def test_ci_systems_normalized_for_outputs(tmp_path: Path) -> None:
    repo, _rev = init_repo(tmp_path, {"flake.nix": FLAKE_NIX})
    effects = await list_effects(EffectsOptions(path=repo))
    assert list(effects) == ["default.deploy"]
