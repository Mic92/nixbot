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
          after = [ [ "notify" ] ];
          lock = "hw-lab";
        };
        notify = {
          effectScript = "echo notifying";
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
    flatten nested effects to dotted names, and include after/lock."""
    effects = json.loads(_cli("list", f"git+file://{flake_repo}").stdout)
    assert sorted(effects) == ["env.staging", "notify"]
    assert effects["env.staging"] == {"after": ["notify"], "lock": "hw-lab"}
    assert effects["notify"] == {"after": [], "lock": None}


def test_graph_via_flake_ref(flake_repo: Path) -> None:
    """nixbot-effects graph renders the DAG from the flake's metadata."""
    out = _cli("graph", f"git+file://{flake_repo}").stdout
    assert out == "notify\n└── env.staging [lock: hw-lab]\n"
