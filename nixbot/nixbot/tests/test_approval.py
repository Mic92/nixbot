"""Which pull requests are external (approval.external)."""

from __future__ import annotations

import dataclasses

from nixbot import approval
from nixbot.config import PrApprovalConfig
from nixbot.webhooks import ChangeRequest

FORK_PR = ChangeRequest(
    forge="github",
    forge_repo_id="1",
    branch="main",
    commit_sha="a" * 40,
    pr_number=7,
    author_association="NONE",
)


def test_outside_pull_request_is_external_with_or_without_the_gate() -> None:
    # The gate is optional. Where the build runs is not.
    for enable in (True, False):
        config = PrApprovalConfig(enable=enable)
        assert approval.external(config, FORK_PR)
        assert approval.untrusted(config, FORK_PR) is enable


def test_our_own_changes_are_internal() -> None:
    config = PrApprovalConfig(enable=True)
    member = dataclasses.replace(FORK_PR, author_association="MEMBER")
    same_repo = dataclasses.replace(FORK_PR, head_in_base_repo=True)
    push = dataclasses.replace(FORK_PR, pr_number=None)
    gitea = dataclasses.replace(FORK_PR, forge="gitea", author_association=None)
    for change in (member, same_repo, push, gitea):
        assert not approval.external(config, change)


def test_trusted_associations_decide() -> None:
    contributor = dataclasses.replace(FORK_PR, author_association="CONTRIBUTOR")
    assert not approval.external(PrApprovalConfig(), contributor)
    strict = PrApprovalConfig(trusted_associations=["OWNER", "MEMBER", "COLLABORATOR"])
    assert approval.external(strict, contributor)
