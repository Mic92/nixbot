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


async def test_tag_push_has_no_branch(tmp_path: Path) -> None:
    """A tag push names no branch, as on Hercules CI: the detached
    checkout must not turn into branch "HEAD"."""
    repo, rev = init_repo(tmp_path, {"file.txt": "v1"})
    git(repo, "checkout", "--detach")

    opts = EffectsOptions(path=repo, rev=rev, tag="v1.0")
    result = await effects_args(opts)
    assert result["branch"] is None
    assert result["tag"] == "v1.0"
    assert result["ref"] == "refs/tags/v1.0"


@pytest.mark.parametrize(
    ("repo", "owner", "name"),
    [("acme/widget", "acme", "widget"), ("group/sub/widget", "group/sub", "widget")],
)
async def test_repo_identity_as_on_hercules(
    tmp_path: Path, repo: str, owner: str, name: str
) -> None:
    """hercules-ci-effects' github-releases calls the GitHub API with
    `repo.owner` and `repo.name` and checks out by `repo.forgeType`.
    Hercules' `name` is the repository alone."""
    path, rev = init_repo(tmp_path)
    opts = EffectsOptions(path=path, rev=rev, repo=repo, forge_type="github")
    result = await effects_args(opts)
    primary = result["primaryRepo"]
    assert (primary["owner"], primary["name"]) == (owner, name)
    assert primary["forgeType"] == "github"


async def test_branch_run_ignores_a_tag_pushed_later(tmp_path: Path) -> None:
    """A branch push's effects are queued before the commit is tagged but
    may start after. The daemon names the tag it runs for (or none), so
    `git tag --points-at` must not turn the branch run into a tag run."""
    path, rev = init_repo(tmp_path)
    git(path, "tag", "v1.0")
    opts = EffectsOptions(path=path, rev=rev, branch="main", detect_tag=False)
    result = await effects_args(opts)
    primary = result["primaryRepo"]
    assert (primary["tag"], primary["branch"]) == (None, "main")
    assert opts.tag is None


async def test_local_run_detects_the_tag_at_the_commit(tmp_path: Path) -> None:
    path, rev = init_repo(tmp_path)
    git(path, "tag", "v1.0")
    result = await effects_args(EffectsOptions(path=path, rev=rev))
    assert result["primaryRepo"]["tag"] == "v1.0"


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
