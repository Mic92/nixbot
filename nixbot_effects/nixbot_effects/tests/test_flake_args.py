"""How EffectsOptions turn into flake arguments: the _flake_url
bridge to builtins.getFlake, and rev resolution in effects_args()
(--branch without --rev must use the branch tip, not the checkout's
HEAD; https://github.com/nix-community/nixbot/issues/583)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nixbot_effects.eval import _flake_url, effects_args, git_get_tag
from nixbot_effects.options import EffectsOptions
from nixbot_effects.sandbox import secret_context
from nixbot_effects.secrets import SimpleSecret, gather_secrets
from nixbot_effects.tests.support import git, init_repo


async def test_branch_resolves_to_branch_tip(tmp_path: Path) -> None:
    """--branch should resolve rev to the tip of that branch, not HEAD."""
    repo, main_rev = init_repo(tmp_path, {"file.txt": "main"})

    # Create a feature branch with a new commit
    git(repo, "checkout", "-b", "feature")
    (repo / "file.txt").write_text("feature")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature commit")
    feature_rev = git(repo, "rev-parse", "HEAD")

    # Go back to main — HEAD is now main_rev
    git(repo, "checkout", "main")
    assert git(repo, "rev-parse", "HEAD") == main_rev

    # Ask for --branch=feature without --rev: should get feature_rev, not main_rev
    opts = EffectsOptions(path=repo, branch="feature")
    result = await effects_args(opts)

    assert result["rev"] == feature_rev, (
        f"Expected rev from 'feature' branch ({feature_rev[:7]}), "
        f"got HEAD ({result['rev'][:7]})"
    )
    assert result["branch"] == "feature"


async def test_git_tag_propagates_to_secret_context(tmp_path: Path) -> None:
    """A tag resolved from git must end up in opts.tag so that
    isTag-conditioned secrets can be granted."""
    repo, _rev = init_repo(tmp_path, {"file.txt": "v1"})
    git(repo, "tag", "v1.0")

    opts = EffectsOptions(path=repo, repo="acme/widget")
    result = await effects_args(opts)
    assert result["tag"] == "v1.0"
    assert opts.tag == "v1.0"

    ctx = secret_context(opts)
    assert ctx.ref == "refs/tags/v1.0"
    out = gather_secrets(
        {"deploy": SimpleSecret("release-key")},
        {"release-key": {"data": {"token": "s3cret"}, "condition": "isTag"}},
        ctx,
        None,
    )
    assert out == {"deploy": {"data": {"token": "s3cret"}}}


class TestFlakeUrl:
    @pytest.mark.parametrize("locked_url", [None, ""], ids=["absent", "empty"])
    def test_local_path_fallback(self, locked_url: str | None) -> None:
        opts = EffectsOptions(path=Path("/home/user/my-repo"), locked_url=locked_url)
        assert (
            _flake_url(opts, "abc1234")
            == "git+file:///home/user/my-repo?ref=HEAD&rev=abc1234#"
        )

    def test_locked_url_used(self) -> None:
        opts = EffectsOptions(
            path=Path("/nix/store/xyz-source"),
            locked_url="github:org/repo/abc1234def5678",
        )
        assert _flake_url(opts, "abc1234") == "github:org/repo/abc1234def5678"


async def test_git_get_tag_no_tag(tmp_path: Path) -> None:
    repo, rev = init_repo(tmp_path)

    assert await git_get_tag(repo, rev) is None
