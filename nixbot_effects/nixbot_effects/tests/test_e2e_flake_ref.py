"""End-to-end test: nixbot-effects list on a git+file:// flake reference.

Verifies that the CLI can resolve a flake ref, fetch metadata, and
evaluate effects without a local checkout — the store path has no .git.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from nixbot_effects.tests.support import init_repo

# Minimal flake that defines a herculesCI output with named effects.
# No external dependencies — uses builtins only.
FLAKE_NIX = """\
{
  description = "Test flake for nixbot-effects";
  outputs = { self, ... }: {
    herculesCI = args: {
      onPush.default.outputs.effects = {
        # Effects can nest; they are addressed by attribute path.
        env.staging = {
          effectScript = "echo deploying staging";
          after = [ [ "default" "notify" ] ];
          lock = "hw-lab";
        };
        notify = {
          effectScript = "echo notifying";
        };
        # Non-effect values and opted-out attrsets are ignored.
        skipped = { _type = "ignore-me"; };
        not-recursed = {
          recurseForDerivations = false;
          hidden = { effectScript = "echo hidden"; };
        };
      };
      # A second job; effect names are prefixed with the job.
      onPush.docs.outputs = {
        effects = {
          publish = {
            effectScript = "echo publishing docs";
            # Cross-job dependency: paths are absolute (job first).
            after = [ [ "default" "notify" ] [ "docs" "lint" ] ];
          };
          lint = { effectScript = "echo linting docs"; };
        };
      };
    };
  };
}
"""


@pytest.fixture
def flake_repo(tmp_path: Path) -> Path:
    """Create a git repo with a minimal flake that has effects."""
    repo, _rev = init_repo(tmp_path, {"flake.nix": FLAKE_NIX})
    return repo


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['nixbot-effects'] + sys.argv[1:]; "
            "from nixbot_effects.cli import main; main()",
            *args,
        ],
        check=True,
        text=True,
        capture_output=True,
        # The package is importable from the source tree, not the
        # pytest invocation directory.
        cwd=Path(__file__).parents[2],
    )


def test_list_via_flake_ref(flake_repo: Path) -> None:
    """nixbot-effects list <git+file://repo> should work end-to-end,
    flatten nested effects across all onPush jobs to job-prefixed dotted
    names, and include after/lock."""
    effects = json.loads(_cli("list", f"git+file://{flake_repo}").stdout)
    assert sorted(effects) == [
        "default.env.staging",
        "default.notify",
        "docs.lint",
        "docs.publish",
    ]
    assert effects["default.env.staging"] == {
        "after": ["default.notify"],
        "lock": "hw-lab",
    }
    assert effects["default.notify"] == {"after": [], "lock": None}
    assert effects["docs.publish"] == {
        "after": ["default.notify", "docs.lint"],
        "lock": None,
    }


TOPLEVEL_EFFECTS_FLAKE_NIX = """\
{
  description = "Flake with a top-level effects output, no herculesCI";
  outputs = { self, ... }: {
    effects = { primaryRepo, ... }: {
      notify = { effectScript = "echo notifying"; };
    };
  };
}
"""


def test_list_toplevel_effects_output(tmp_path: Path) -> None:
    """Without onPush, the flake's top-level `effects` output becomes the
    default job's effects (hercules-ci-agent default job behavior)."""
    repo, _rev = init_repo(tmp_path, {"flake.nix": TOPLEVEL_EFFECTS_FLAKE_NIX})
    effects = json.loads(_cli("list", f"git+file://{repo}").stdout)
    assert effects == {"default.notify": {"after": [], "lock": None}}


def test_graph_via_flake_ref(flake_repo: Path) -> None:
    """nixbot-effects graph renders the DAG from the flake's metadata."""
    out = _cli("graph", f"git+file://{flake_repo}").stdout
    assert out == (
        "default.notify\n"
        "├── default.env.staging [lock: hw-lab]\n"
        "└── docs.publish\n"
        "docs.lint\n"
        "└── docs.publish\n"
    )
