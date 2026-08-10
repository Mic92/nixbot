"""Git clone and worktree management.

One central persistent bare clone per project. Each build gets its own
git worktree from that clone so concurrent builds of the same project
never re-fetch. Worktrees are removed after the build. Clones are a
cache: on corruption they are deleted and re-cloned. A per-project lock
serializes fetches; `git worktree prune` plus an orphan sweep runs at
startup and periodically, as does `git gc`.

PR builds merge the PR head into the base branch locally in the
worktree. A conflict is a failed build. Build identity is the
post-merge tree hash (`HEAD^{tree}`).

Fetch credentials come from a provider interface. The static/netrc
implementation covers public repos and operator-supplied netrc;
GitHub App per-fetch installation tokens plug in via forge integration.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .environ import passthrough_env

logger = logging.getLogger(__name__)

# PR/MR refs are fetched per PR and never covered by --prune of later
# fetches. Without an age-based sweep they accumulate forever.
PR_REF_MAX_AGE = 90 * 86400
# Crash-leaked plain files next to worktrees (e.g. effects side-files)
# are swept once clearly older than any running build.
ORPHAN_FILE_MAX_AGE = 86400


def pr_refspec(forge: str, pr_number: int) -> str:
    """GitLab serves MR heads under refs/merge-requests/<iid>/*;
    GitHub and Gitea use refs/pull/<number>/*."""
    ref = (
        f"refs/merge-requests/{pr_number}"
        if forge == "gitlab"
        else f"refs/pull/{pr_number}"
    )
    return f"+{ref}/*:{ref}/*"


class GitError(Exception):
    """A git command failed."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr}")


class MergeConflictError(Exception):
    """Local merge of the PR head into the base branch conflicted."""


@dataclass(frozen=True)
class FetchCredentials:
    """Credentials for fetching a repository.

    `netrc_file` is bind-mounted/read for the duration of one fetch or
    eval only. It must be scoped to the repository being fetched.
    SSH key/known-hosts files cover per-repo SSH fetch (pull-based
    repos and the gitea.sshPrivateKeyFile option).
    """

    netrc_file: Path | None = None
    # True when the netrc covers only the repository being fetched
    # (GitHub per-repo installation tokens). Instance-wide tokens
    # (Gitea/GitLab) must not reach PR-controlled paths such as eval.
    repo_scoped: bool = False
    # Raw forge token for hercules GitToken secret references.
    token: str | None = None
    ssh_private_key_file: Path | None = None
    ssh_known_hosts_file: Path | None = None

    def git_ssh_command(self) -> str | None:
        if self.ssh_private_key_file is None and self.ssh_known_hosts_file is None:
            return None
        parts = ["ssh", "-o", "BatchMode=yes"]
        if self.ssh_private_key_file is not None:
            parts += ["-i", str(self.ssh_private_key_file)]
        if self.ssh_known_hosts_file is not None:
            parts += [
                "-o",
                f"UserKnownHostsFile={self.ssh_known_hosts_file}",
            ]
        return " ".join(parts)


class CredentialsProvider(Protocol):
    """Provides per-fetch credentials for a repository URL."""

    async def get(self, repo_url: str) -> FetchCredentials: ...


class StaticCredentialsProvider:
    """Static provider: a fixed netrc file (or nothing, for public repos)."""

    def __init__(self, netrc_file: Path | None = None) -> None:
        self.netrc_file = netrc_file

    async def get(self, repo_url: str) -> FetchCredentials:  # noqa: ARG002
        return FetchCredentials(netrc_file=self.netrc_file)


async def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
    credentials: FetchCredentials | None = None,
) -> str:
    """Run a git command, returning stdout. Raises GitError on failure."""
    env = {
        # Proxy/TLS/NIX_* passthrough: fetches go through libcurl,
        # which needs the proxy and CA configuration of the service.
        **passthrough_env(),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    # Pass through environment-based git config (GIT_CONFIG_COUNT and
    # friends), e.g. for protocol.file.allow.
    for key, value in os.environ.items():
        if key.startswith("GIT_CONFIG_") and key not in env:
            env[key] = value
    if credentials is not None:
        ssh_command = credentials.git_ssh_command()
        if ssh_command is not None:
            env["GIT_SSH_COMMAND"] = ssh_command
    if credentials is not None and credentials.netrc_file is not None:
        # git's libcurl reads $HOME/.netrc (CURL_NETRC_OPTIONAL). Point
        # HOME at a throwaway directory containing only the scoped netrc.
        home = Path(tempfile.mkdtemp(prefix="git-netrc-"))
        (home / ".netrc").symlink_to(credentials.netrc_file)
        env["HOME"] = str(home)
    else:
        home = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise GitError(args, proc.returncode or -1, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace")
    finally:
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)


def _push_url_rewrites(push_url: str) -> list[tuple[str, str]]:
    """(config key, prefix) insteadOf entries rewriting the forge's
    plain https and ssh remotes to the token URL, so submodule
    fetches/pushes against the same host also authenticate."""
    parts = urlsplit(push_url)
    if parts.hostname is None:
        return []
    key = f"url.{parts.scheme}://{parts.netloc}/.insteadOf"
    return [
        (key, prefix)
        for prefix in (f"https://{parts.hostname}/", f"git@{parts.hostname}:")
    ]


def _worktree_paths(porcelain_output: str) -> set[Path]:
    """Worktree paths from `git worktree list --porcelain`, resolved
    because git reports symlink-resolved paths."""
    return {
        Path(line.removeprefix("worktree ")).resolve()
        for line in porcelain_output.splitlines()
        if line.startswith("worktree ")
    }


@dataclass
class Worktree:
    """A per-build checkout backed by a project's central clone."""

    path: Path
    clone_path: Path

    async def rev_parse(self, rev: str) -> str:
        return (await run_git(["rev-parse", rev], cwd=self.path)).strip()

    async def tree_hash(self) -> str:
        """Post-merge tree hash: the build identity."""
        return await self.rev_parse("HEAD^{tree}")

    async def commit_message(self, rev: str = "HEAD") -> str:
        return await run_git(["log", "-1", "--format=%B", rev], cwd=self.path)

    async def merge(
        self, head_sha: str, credentials: FetchCredentials | None = None
    ) -> None:
        """Merge `head_sha` into the currently checked-out base branch.

        Raises MergeConflictError on conflict. The caller fails the
        build and reports the status on the head SHA.
        """
        try:
            await run_git(
                [
                    "-c",
                    "user.name=nixbot",
                    "-c",
                    "user.email=nixbot@localhost",
                    "merge",
                    "--no-ff",
                    "-m",
                    f"merge {head_sha} into base",
                    head_sha,
                ],
                cwd=self.path,
                credentials=credentials,
            )
        except GitError as e:
            # Only a genuine content conflict (unmerged index entries)
            # is a permanent MergeConflictError. Everything else
            # (index.lock contention, missing objects, disk full) must
            # stay a GitError so callers treat it as transient/infra.
            conflicted = False
            with contextlib.suppress(GitError):
                unmerged = await run_git(["ls-files", "--unmerged"], cwd=self.path)
                conflicted = bool(unmerged.strip())
            with contextlib.suppress(GitError):
                await run_git(["merge", "--abort"], cwd=self.path)
            if not conflicted:
                raise
            msg = f"merge of {head_sha} conflicts with base branch: {e.stderr}"
            raise MergeConflictError(msg) from e


class RepoManager:
    """Manages central per-project clones and per-build worktrees."""

    def __init__(self, state_dir: Path) -> None:
        self.clones_dir = state_dir / "clones"
        self.worktrees_dir = state_dir / "worktrees"
        self.clones_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self._fetch_locks: dict[str, asyncio.Lock] = {}
        # Live checkouts, tracked independently of git metadata: after
        # a corruption re-clone the new clone knows no worktrees, and
        # the orphan sweep must not delete those of running builds.
        self._active_worktrees: set[Path] = set()
        self._worktree_seq = itertools.count()

    def clone_path(self, project_key: str) -> Path:
        # project_key like "github/owner/repo". One directory per project.
        return self.clones_dir / project_key / "clone.git"

    def _lock(self, project_key: str) -> asyncio.Lock:
        return self._fetch_locks.setdefault(project_key, asyncio.Lock())

    async def fetch(
        self,
        project_key: str,
        url: str,
        refspecs: list[str],
        credentials: FetchCredentials | None = None,
    ) -> None:
        """Clone or update the central clone, serialized per project.

        On git corruption the clone is deleted and re-created (clones
        are a cache).
        """
        async with self._lock(project_key):
            path = self.clone_path(project_key)
            try:
                await self._fetch_once(path, url, refspecs, credentials)
            except GitError as e:
                # GitError also covers transient network/auth failures;
                # only delete the clone (which backs live builds'
                # worktrees) when it is actually corrupted.
                if await self._clone_healthy(path):
                    raise
                logger.warning(
                    "clone corrupted, re-cloning",
                    extra={"project": project_key, "stderr": e.stderr},
                )
                shutil.rmtree(path, ignore_errors=True)
                await self._fetch_once(path, url, refspecs, credentials)

    @staticmethod
    async def _clone_healthy(path: Path) -> bool:
        """Cheap local corruption check: HEAD's commit object must be
        readable from the object store."""
        if not (path / "HEAD").exists():
            return False
        try:
            await run_git(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path)
        except GitError:
            return False
        return True

    async def _fetch_once(
        self,
        path: Path,
        url: str,
        refspecs: list[str],
        credentials: FetchCredentials | None,
    ) -> None:
        if not (path / "HEAD").exists():
            shutil.rmtree(path, ignore_errors=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Blobless partial clone: history blobs are fetched lazily at
            # checkout instead of upfront, cutting clone cost for repos
            # with large history (nixpkgs).
            await run_git(
                ["clone", "--bare", "--filter=blob:none", url, str(path)],
                credentials=credentials,
            )
        await run_git(
            ["fetch", "--force", "--prune", "--filter=blob:none", url, *refspecs],
            cwd=path,
            credentials=credentials,
        )

    async def show_file(
        self,
        project_key: str,
        ref: str,
        file_path: str,
        credentials: FetchCredentials | None = None,
    ) -> str | None:
        """Read one file from a ref of the bare clone (no worktree).
        Returns None when the ref or file does not exist."""
        try:
            return await run_git(
                ["show", f"{ref}:{file_path}"],
                cwd=self.clone_path(project_key),
                credentials=credentials,
            )
        except GitError:
            return None

    async def create_worktree(
        self,
        project_key: str,
        worktree_id: str,
        commit: str,
        credentials: FetchCredentials | None = None,
    ) -> Worktree:
        """Create a detached worktree for one build at `commit`."""
        clone = self.clone_path(project_key)
        path = self.worktrees_dir / f"{worktree_id}-{next(self._worktree_seq)}"
        # Stale directory from a previous process.
        if path.exists():
            await self.remove_worktree(Worktree(path=path, clone_path=clone))
        path.parent.mkdir(parents=True, exist_ok=True)
        await run_git(
            ["worktree", "add", "--detach", str(path), commit],
            cwd=clone,
            credentials=credentials,
        )
        self._active_worktrees.add(path.resolve())
        return Worktree(path=path, clone_path=clone)

    async def remove_worktree(self, worktree: Worktree) -> None:
        self._active_worktrees.discard(worktree.path.resolve())
        try:
            await run_git(
                ["worktree", "remove", "--force", str(worktree.path)],
                cwd=worktree.clone_path,
            )
        except GitError:
            shutil.rmtree(worktree.path, ignore_errors=True)
            with contextlib.suppress(GitError):
                await run_git(["worktree", "prune"], cwd=worktree.clone_path)

    async def cleanup(self) -> None:
        """Prune stale worktrees/PR refs and sweep orphans. Run at
        startup and periodically."""
        for clone in self.clones_dir.rglob("clone.git"):
            await self._prune_stale_pr_refs(clone)
        # Snapshot candidates before scanning the clones: a worktree
        # created mid-scan would be missing from `registered` and must
        # not become a sweep candidate.
        candidates = {entry.resolve() for entry in self.worktrees_dir.iterdir()}
        registered = set(self._active_worktrees)
        for clone in self.clones_dir.rglob("clone.git"):
            with contextlib.suppress(GitError):
                await run_git(["worktree", "prune"], cwd=clone)
            try:
                output = await run_git(["worktree", "list", "--porcelain"], cwd=clone)
            except GitError as e:
                # Fail closed: treating an unreadable clone as "no
                # worktrees" would sweep live build checkouts.
                logger.warning(
                    "git worktree list failed, skipping orphan sweep",
                    extra={"clone": str(clone), "stderr": e.stderr},
                )
                return
            registered.update(_worktree_paths(output))
        for entry in candidates - registered:
            if entry.is_dir():
                # Orphan sweep: worktree directories no clone knows about.
                logger.info("removing orphan worktree", extra={"path": str(entry)})
                shutil.rmtree(entry, ignore_errors=True)
            else:
                # Crash-leaked side-file (e.g. effects secrets): plain
                # files are never worktrees and would persist forever.
                # OSError: the entry may vanish concurrently.
                with contextlib.suppress(OSError):
                    if time.time() - entry.stat().st_mtime > ORPHAN_FILE_MAX_AGE:
                        logger.info("removing orphan file", extra={"path": str(entry)})
                        entry.unlink()

    async def _prune_stale_pr_refs(self, clone: Path) -> None:
        """Delete PR/MR refs whose tip commit is old: fetches only ever
        add the current PR's refs, so abandoned ones pile up."""
        with contextlib.suppress(GitError):
            output = await run_git(
                [
                    "for-each-ref",
                    "--format=%(refname) %(committerdate:unix)",
                    "refs/pull",
                    "refs/merge-requests",
                ],
                cwd=clone,
            )
            cutoff = time.time() - PR_REF_MAX_AGE
            for line in output.splitlines():
                refname, _, date = line.rpartition(" ")
                if refname and date.isdigit() and int(date) < cutoff:
                    with contextlib.suppress(GitError):
                        await run_git(["update-ref", "-d", refname], cwd=clone)

    async def gc(self) -> None:
        """Periodic `git gc` over all clones."""
        for clone in self.clones_dir.rglob("clone.git"):
            with contextlib.suppress(GitError):
                await run_git(["gc", "--auto"], cwd=clone)

    async def checkout_for_build(  # noqa: PLR0913
        self,
        project_key: str,
        worktree_id: str,
        *,
        base_commit: str,
        head_commit: str | None = None,
        credentials: FetchCredentials | None = None,
        submodule_credentials: FetchCredentials | None = None,
    ) -> Worktree:
        """Create the build worktree: base commit, optionally merging a
        PR head into it. Submodules are checked out recursively (the
        bare clone does not carry submodule objects) WITHOUT the fetch
        credentials unless explicitly opted in. Returns the worktree;
        identity is its tree hash."""
        worktree = await self.create_worktree(
            project_key, worktree_id, base_commit, credentials
        )
        try:
            if head_commit is not None and head_commit != base_commit:
                await worktree.merge(head_commit, credentials)
            # .gitmodules is PR-controlled: with the primary repo's
            # credentials a malicious PR could exfiltrate any other
            # private repo on the same forge via build outputs.
            await self._init_submodules(worktree.path, submodule_credentials)
        except BaseException:
            # Callers only remove worktrees they received. A failed
            # merge or submodule checkout (or cancellation) must not
            # leak a registered worktree.
            await self.remove_worktree(worktree)
            raise
        return worktree

    @staticmethod
    async def _init_submodules(
        path: Path, credentials: FetchCredentials | None
    ) -> None:
        """Recursive submodule checkout, shared by the build worktree
        and the effect clone so both follow the same credential policy."""
        if (path / ".gitmodules").exists():
            await run_git(
                ["submodule", "update", "--init", "--recursive"],
                cwd=path,
                credentials=credentials,
            )

    async def clone_for_effect(
        self,
        project_key: str,
        dest: Path,
        *,
        commit: str,
        push_url: str | None = None,
        submodule_credentials: FetchCredentials | None = None,
    ) -> Path:
        """Standalone, pushable clone for one effect run, cloned locally
        from the bare mirror (the build worktree already materialized
        the commit's blobs there). `origin` becomes `push_url` (token
        https URL) plus insteadOf rewrites for the forge's plain
        https/ssh remotes. The token never touches the shared mirror or
        worktree. Removed by the caller via `remove_effect_clone`."""
        clone = self.clone_path(project_key)
        # A leftover from a crashed run must not survive into git clone.
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await run_git(["clone", "--no-checkout", str(clone), str(dest)])
            if push_url is not None:
                # Before checkout: any lazy blob fetch (blobless mirror)
                # then goes to the forge with the token.
                await run_git(["remote", "set-url", "origin", push_url], cwd=dest)
                for key, prefix in _push_url_rewrites(push_url):
                    await run_git(["config", "--add", key, prefix], cwd=dest)
            await run_git(["checkout", "--detach", commit], cwd=dest)
            await self._init_submodules(dest, submodule_credentials)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        # Track like a worktree so the periodic orphan sweep does not
        # delete the directory of a running effect.
        self._active_worktrees.add(await asyncio.to_thread(dest.resolve))
        return dest

    def remove_effect_clone(self, dest: Path) -> None:
        self._active_worktrees.discard(dest.resolve())
        shutil.rmtree(dest, ignore_errors=True)
