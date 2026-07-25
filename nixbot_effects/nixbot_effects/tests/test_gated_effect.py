"""Effects gated off by hercules-ci's `runIf false` become a wrapper set
`{ dependencies; prebuilt; }` (recurseForDerivations) instead of a single
derivation. nixbot must pick one derivation and not try to run it.

https://github.com/Mic92/nixbot/issues/56
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from nixbot_effects.eval import instantiate_effects
from nixbot_effects.options import EffectsOptions
from nixbot_effects.tests.support import init_repo

if TYPE_CHECKING:
    from pathlib import Path

# derivation {} instantiates without building, enough to exercise selection.
FLAKE_NIX = """\
{
  outputs = { self, ... }:
    let
      drv = name: derivation {
        inherit name;
        system = builtins.currentSystem;
        builder = "/bin/sh";
        args = [ "-c" "echo ${name} > $out" ];
      };
    in {
      herculesCI = args: {
        onPush.default.outputs.effects = {
          # runIf true
          runnable = { run = drv "runnable"; };
          # bare effect derivation (no runIf)
          bare = drv "bare";
          # runIf false: recurseForDerivations wrapper with multiple drvs
          gated = {
            recurseForDerivations = true;
            dependencies = drv "dependencies";
            prebuilt = drv "prebuilt";
          };
        };
      };
    };
}
"""


async def _instantiate(effect: str, repo: Path, tmp_path: Path) -> tuple[str, bool]:
    opts = EffectsOptions(path=repo)
    return await instantiate_effects(effect, opts, tmp_path / f"result-{effect}")


async def test_gated_effect_selects_dependencies_and_skips_run(tmp_path: Path) -> None:
    repo, _rev = init_repo(tmp_path, {"flake.nix": FLAKE_NIX})

    drv_path, should_run = await _instantiate("gated", repo, tmp_path)
    assert drv_path.endswith("-dependencies.drv")
    assert should_run is False


async def test_runnable_and_bare_effects_run(tmp_path: Path) -> None:
    repo, _rev = init_repo(tmp_path, {"flake.nix": FLAKE_NIX})

    for effect in ("runnable", "bare"):
        drv_path, should_run = await _instantiate(effect, repo, tmp_path)
        assert drv_path.endswith(f"-{effect}.drv")
        assert should_run is True
