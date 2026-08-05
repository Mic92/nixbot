"""Tests for clone/worktree management using real git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from nixbot import gitrepo
from nixbot.gitrepo import (
    FetchCredentials,
    GitError,
    MergeConflictError,
    RepoManager,
    StaticCredentialsProvider,
    _parse_pull_request_diff,
    run_git,
)

from .support import git, init_upstream

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

KEY = "github/acme/widget"


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    return init_upstream(tmp_path / "upstream", {"file.txt": "hello\n"})


@pytest.fixture
def manager(tmp_path: Path) -> RepoManager:
    return RepoManager(tmp_path / "state")


async def fetch(manager: RepoManager, upstream: Path) -> None:
    await manager.fetch(KEY, str(upstream), ["+refs/heads/*:refs/heads/*"])


async def test_fetch_and_worktree(manager: RepoManager, upstream: Path) -> None:
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    wt = await manager.checkout_for_build(KEY, "build-1", base_commit=sha)
    assert (wt.path / "file.txt").read_text() == "hello\n"
    assert await wt.rev_parse("HEAD") == sha
    await manager.remove_worktree(wt)
    assert not wt.path.exists()


async def test_fetch_updates_existing_clone(
    manager: RepoManager, upstream: Path
) -> None:
    await fetch(manager, upstream)
    (upstream / "file.txt").write_text("v2\n")
    git(upstream, "commit", "-am", "update")
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    wt = await manager.checkout_for_build(KEY, "build-2", base_commit=sha)
    assert (wt.path / "file.txt").read_text() == "v2\n"
    await manager.remove_worktree(wt)


async def test_pr_merge_and_tree_hash_dedup(
    manager: RepoManager, upstream: Path
) -> None:
    base = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "-b", "pr")
    (upstream / "feature.txt").write_text("feature\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "feature")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    await fetch(manager, upstream)

    wt1 = await manager.checkout_for_build(
        KEY, "b1", base_commit=base, head_commit=head
    )
    tree1 = await wt1.tree_hash()
    await manager.remove_worktree(wt1)
    # Same content again (re-push scenario): tree hash identical
    # even though the merge commits differ.
    wt2 = await manager.checkout_for_build(
        KEY, "b2", base_commit=base, head_commit=head
    )
    tree2 = await wt2.tree_hash()
    await manager.remove_worktree(wt2)
    assert tree1 == tree2


async def test_pr_diff_represents_paths_and_change_kinds(
    manager: RepoManager, upstream: Path
) -> None:
    for name, content in {
        "modified.txt": "before\n",
        "deleted.txt": "delete me\n",
        "old name.txt": "rename me\n",
        "copy-source.txt": "copy me\n",
    }.items():
        (upstream / name).write_text(content)
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "diff base")
    base = git(upstream, "rev-parse", "HEAD")

    git(upstream, "checkout", "-b", "pr-diff")
    (upstream / "modified.txt").write_text("after\n")
    (upstream / "deleted.txt").unlink()
    (upstream / "old name.txt").rename(upstream / "renamed name.txt")
    shutil.copyfile(upstream / "copy-source.txt", upstream / "copied name.txt")
    unusual = 'space tab\tquote" newline\n.txt'
    (upstream / unusual).write_text("odd\n")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-m", "mixed changes")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    await fetch(manager, upstream)

    wt = await manager.checkout_for_build(
        KEY, "diff", base_commit=base, head_commit=head
    )
    diff = await wt.pull_request_diff()
    assert diff is not None
    assert diff.complete
    assert diff.base_rev == base
    assert diff.merged_rev == await wt.rev_parse("HEAD")
    by_path = {change.path: change for change in diff.files}
    assert by_path["modified.txt"].status == "modified"
    assert by_path["deleted.txt"].status == "deleted"
    assert by_path["renamed name.txt"].status == "renamed"
    assert by_path["renamed name.txt"].previous_path == "old name.txt"
    assert by_path["copied name.txt"].status == "copied"
    assert by_path["copied name.txt"].previous_path == "copy-source.txt"
    assert by_path[unusual].status == "added"
    await manager.remove_worktree(wt)


def test_pr_diff_falls_back_for_unrepresentable_path() -> None:
    diff = _parse_pull_request_diff("base", "merged", b"M\0bad-\xff\0")
    assert not diff.complete
    assert diff.reason == "unrepresentable_or_malformed_path"
    assert diff.files == ()


async def test_merge_conflict_raises(manager: RepoManager, upstream: Path) -> None:
    git(upstream, "checkout", "-b", "pr")
    (upstream / "file.txt").write_text("pr version\n")
    git(upstream, "commit", "-am", "pr change")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")
    (upstream / "file.txt").write_text("main version\n")
    git(upstream, "commit", "-am", "main change")
    base = git(upstream, "rev-parse", "HEAD")
    await fetch(manager, upstream)

    with pytest.raises(MergeConflictError):
        await manager.checkout_for_build(KEY, "b3", base_commit=base, head_commit=head)
    # Worktree cleaned up after conflict.
    assert not (manager.worktrees_dir / "b3").exists()


async def test_reclone_on_corruption(manager: RepoManager, upstream: Path) -> None:
    await fetch(manager, upstream)
    clone = manager.clone_path(KEY)
    # Destroy the object store. Next fetch must transparently re-clone.
    shutil.rmtree(clone / "objects")
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    wt = await manager.checkout_for_build(KEY, "b4", base_commit=sha)
    await manager.remove_worktree(wt)


async def test_transient_fetch_error_keeps_clone(
    manager: RepoManager, upstream: Path
) -> None:
    await fetch(manager, upstream)
    clone = manager.clone_path(KEY)
    # Unreachable remote but healthy clone: the clone (which backs
    # in-flight builds' worktrees) must survive.
    with pytest.raises(GitError):
        await manager.fetch(
            KEY, str(upstream / "does-not-exist"), ["+refs/heads/*:refs/heads/*"]
        )
    assert (clone / "HEAD").exists()
    assert any((clone / "objects").rglob("*"))


async def test_cleanup_resolves_symlinked_paths(tmp_path: Path, upstream: Path) -> None:
    # git reports symlink-resolved worktree paths. Cleanup must compare
    # resolved paths or it deletes live worktrees.
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(real_state)
    manager = RepoManager(link)
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    live = await manager.checkout_for_build(KEY, "live", base_commit=sha)
    await manager.cleanup()
    assert live.path.exists()


@pytest.fixture
def submodule(upstream: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Add a file:// submodule to upstream. Allows the file protocol
    (blocked by default since CVE-2022-39253) via environment-based
    git config passed through by run_git."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "always")
    sub = upstream.parent / "submodule"
    sub.mkdir()
    git(sub, "init", "-b", "main")
    (sub / "inner.txt").write_text("inner\n")
    git(sub, "add", ".")
    git(sub, "commit", "-m", "inner")
    git(
        upstream,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(sub),
        "vendored",
    )
    git(upstream, "commit", "-m", "add submodule")
    return sub


@pytest.mark.usefixtures("submodule")
async def test_submodules_checked_out(manager: RepoManager, upstream: Path) -> None:
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    wt = await manager.checkout_for_build(KEY, "sub-build", base_commit=sha)
    assert (wt.path / "vendored" / "inner.txt").read_text() == "inner\n"
    await manager.remove_worktree(wt)


async def test_failed_submodule_checkout_removes_worktree(
    manager: RepoManager, upstream: Path, submodule: Path
) -> None:
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    # Submodule source gone: the update fails and the half-initialized
    # worktree must not leak (it would stay registered forever).
    shutil.rmtree(submodule)

    with pytest.raises(GitError):
        await manager.checkout_for_build(KEY, "sub-fail", base_commit=sha)
    assert not (manager.worktrees_dir / "sub-fail").exists()


async def test_cleanup_sweeps_orphans(manager: RepoManager, upstream: Path) -> None:
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")

    live = await manager.checkout_for_build(KEY, "live", base_commit=sha)
    orphan = manager.worktrees_dir / "orphan"
    orphan.mkdir()
    (orphan / "junk").write_text("x")

    await manager.cleanup()
    assert not orphan.exists()
    assert live.path.exists()
    await manager.gc()


async def test_cleanup_prunes_stale_pr_refs(
    manager: RepoManager, upstream: Path
) -> None:
    """PR refs accumulate forever otherwise: --prune only covers the
    refspecs of the current fetch."""
    sha = git(upstream, "rev-parse", "HEAD")
    old_env = {
        "GIT_COMMITTER_DATE": "2005-04-07T22:13:13",
        "GIT_AUTHOR_DATE": "2005-04-07T22:13:13",
    }
    subprocess.run(  # noqa: S603
        ["git", "-C", str(upstream), "commit", "--allow-empty", "-m", "old pr"],
        env={**os.environ, **old_env},
        check=True,
    )
    old_sha = git(upstream, "rev-parse", "HEAD")
    git(upstream, "update-ref", "refs/pull/1/head", old_sha)
    git(upstream, "update-ref", "refs/merge-requests/2/head", old_sha)
    git(upstream, "update-ref", "refs/pull/3/head", sha)  # recent
    git(upstream, "reset", "--hard", sha)
    await manager.fetch(
        KEY,
        str(upstream),
        [
            "+refs/heads/*:refs/heads/*",
            "+refs/pull/1/*:refs/pull/1/*",
            "+refs/merge-requests/2/*:refs/merge-requests/2/*",
            "+refs/pull/3/*:refs/pull/3/*",
        ],
    )
    await manager.cleanup()
    clone = manager.clone_path(KEY)
    refs = git(clone, "for-each-ref", "--format=%(refname)")
    assert "refs/pull/1/head" not in refs
    assert "refs/merge-requests/2/head" not in refs
    assert "refs/pull/3/head" in refs
    assert "refs/heads/main" in refs or "refs/heads/master" in refs


async def test_cleanup_removes_stale_orphan_files(
    manager: RepoManager, upstream: Path
) -> None:
    """Crash-leaked side-files next to worktrees (e.g. effects secrets)
    must be swept once old enough. Fresh files stay."""
    await fetch(manager, upstream)
    stale = manager.worktrees_dir / "stale-secret"
    stale.write_text("s3cret")
    old = time.time() - 2 * 86400
    os.utime(stale, (old, old))
    fresh = manager.worktrees_dir / "fresh-file"
    fresh.write_text("x")
    await manager.cleanup()
    assert not stale.exists()
    assert fresh.exists()


async def test_cleanup_aborts_when_worktree_list_fails(
    manager: RepoManager, upstream: Path
) -> None:
    """A failing `git worktree list` must abort the sweep (fail
    closed), not be treated as "no worktrees"."""
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    live = await manager.checkout_for_build(KEY, "live", base_commit=sha)
    # Forget the in-memory registration to exercise the git-metadata
    # path alone, then corrupt the clone so `git worktree list` fails.
    manager._active_worktrees.clear()  # noqa: SLF001
    (manager.clone_path(KEY) / "HEAD").unlink()
    await manager.cleanup()
    assert live.path.exists()


async def test_cleanup_keeps_registered_worktrees_after_reclone(
    manager: RepoManager, upstream: Path
) -> None:
    """After a corruption re-clone the new clone knows no worktrees;
    live builds' worktrees must survive via the in-memory registry."""
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    live = await manager.checkout_for_build(KEY, "live", base_commit=sha)
    # Simulate corruption + re-clone: fresh clone, no registered worktrees.
    shutil.rmtree(manager.clone_path(KEY))
    await fetch(manager, upstream)
    await manager.cleanup()
    assert live.path.exists()


@pytest.mark.usefixtures("submodule")
async def test_submodules_fetched_without_credentials(
    manager: RepoManager,
    upstream: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.gitmodules is PR-controlled: a malicious PR could point a
    submodule at another private repo on the same forge and exfiltrate
    it via build outputs. Submodule checkout must not see the fetch
    credentials."""
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    netrc = tmp_path / "netrc"
    netrc.write_text("machine example.com login x password y\n")
    creds = FetchCredentials(netrc_file=netrc)

    calls: list[tuple[list[str], FetchCredentials | None]] = []
    orig_run_git = gitrepo.run_git

    async def spy_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        credentials: FetchCredentials | None = None,
    ) -> str:
        calls.append((args, credentials))
        return await orig_run_git(args, cwd=cwd, credentials=credentials)

    monkeypatch.setattr(gitrepo, "run_git", spy_run_git)

    # Default: no credentials reach the submodule checkout.
    wt = await manager.checkout_for_build(
        KEY, "sub-creds", base_commit=sha, credentials=creds
    )
    await manager.remove_worktree(wt)
    # Explicit opt-in forwards them.
    wt = await manager.checkout_for_build(
        KEY,
        "sub-creds-2",
        base_commit=sha,
        credentials=creds,
        submodule_credentials=creds,
    )
    await manager.remove_worktree(wt)
    submodule_calls = [c for c in calls if c[0][0] == "submodule"]
    assert len(submodule_calls) == 2  # noqa: PLR2004 — one call per checkout
    assert submodule_calls[0][1] is None
    assert submodule_calls[1][1] is creds


async def test_checkout_lazy_fetch_needs_credentials(
    manager: RepoManager,
    upstream: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob-less clone lazy-fetches missing objects from origin during
    worktree checkout and merge. When origin needs authentication, those
    fetches only succeed if the primary repo's credentials are
    forwarded, reproducing the private-repo build failure.

    A fake `ssh` gates on the `-i <key>` flag that
    FetchCredentials.git_ssh_command emits: no credentials means no key
    means the promisor fetch fails just like the real bug."""
    git(upstream, "config", "uploadpack.allowFilter", "true")
    base = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "-b", "pr")
    (upstream / "feature.txt").write_text("feature\n")
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "feature")
    head = git(upstream, "rev-parse", "HEAD")
    git(upstream, "checkout", "main")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_ssh = bindir / "ssh"
    fake_ssh.write_text(
        "#!/bin/sh\n"
        'case " $* " in *" -i "*) ;; *) echo "no key" >&2; exit 255;; esac\n'
        'for a in "$@"; do last=$a; done\n'
        'exec sh -c "$last"\n'
    )
    fake_ssh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

    key = tmp_path / "id"
    key.write_text("dummy\n")
    creds = FetchCredentials(ssh_private_key_file=key)
    # ssh://fake<abs-path> routes through the fake ssh, which runs
    # git-upload-pack against the local upstream repo.
    url = f"ssh://fake{upstream}"
    await manager.fetch(KEY, url, ["+refs/heads/*:refs/heads/*"], credentials=creds)

    with pytest.raises(GitError, match="promisor remote"):
        await manager.checkout_for_build(
            KEY, "no-creds", base_commit=base, head_commit=head
        )

    wt = await manager.checkout_for_build(
        KEY, "with-creds", base_commit=base, head_commit=head, credentials=creds
    )
    try:
        assert (wt.path / "feature.txt").read_text() == "feature\n"
    finally:
        await manager.remove_worktree(wt)


async def test_static_credentials_provider(tmp_path: Path) -> None:
    netrc = tmp_path / "netrc"
    netrc.write_text("machine example.com login x password y\n")
    provider = StaticCredentialsProvider(netrc)
    creds = await provider.get("https://example.com/r.git")
    assert creds.netrc_file == netrc
    assert (await StaticCredentialsProvider().get("https://x/y.git")).netrc_file is None


async def test_run_git_error_includes_stderr(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="failed"):
        await run_git(["rev-parse", "HEAD"], cwd=tmp_path)


async def test_merge_infra_failure_is_not_conflict(
    manager: RepoManager, upstream: Path
) -> None:
    # A git failure that is not a content conflict (here: merging a
    # nonexistent ref) must surface as GitError so callers treat it as
    # transient/infra, not as a permanent merge conflict.
    base = git(upstream, "rev-parse", "HEAD")
    await fetch(manager, upstream)

    wt = await manager.create_worktree(KEY, "b-infra", base)
    try:
        with pytest.raises(GitError):
            await wt.merge("0" * 40)
    finally:
        await manager.remove_worktree(wt)


async def test_run_git_passes_proxy_env_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fetches go through libcurl: the service's proxy/CA configuration
    # must survive run_git's env scrubbing.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text('#!/bin/sh\necho "proxy=$https_proxy ca=$SSL_CERT_FILE"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("https_proxy", "http://proxy:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/ca.pem")
    out = await run_git(["version"])
    assert out.strip() == "proxy=http://proxy:3128 ca=/etc/ssl/ca.pem"


@pytest.mark.usefixtures("submodule")
async def test_clone_for_effect(
    manager: RepoManager, upstream: Path, tmp_path: Path
) -> None:
    await fetch(manager, upstream)
    sha = git(upstream, "rev-parse", "HEAD")
    # Production ordering: the build worktree runs first and materializes
    # the blobs of this commit in the blobless mirror.
    await manager.checkout_for_build(KEY, "eval", base_commit=sha)
    dest = tmp_path / "effect-checkout"

    await manager.clone_for_effect(
        KEY,
        dest,
        commit=sha,
        push_url="https://x-access-token:tok123@github.com/acme/widget",
    )
    # Standalone: own object store, no alternates into the shared clone.
    assert git(dest, "rev-parse", "--git-common-dir") == ".git"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert git(dest, "rev-parse", "HEAD") == sha
    assert (dest / "vendored" / "inner.txt").read_text() == "inner\n"
    # Pushable origin plus ssh/https rewrites, in the clone's config only.
    assert git(dest, "remote", "get-url", "origin") == (
        "https://x-access-token:tok123@github.com/acme/widget"
    )
    assert "git@github.com:" in (dest / ".git" / "config").read_text()
    assert "tok123" not in (manager.clone_path(KEY) / "config").read_text()
