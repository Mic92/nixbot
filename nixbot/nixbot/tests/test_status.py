"""Tests for commit status reporting: contexts, caps, success-flip,
stale-generation dropping (with fake posters and in-memory store)."""

# ruff: noqa: PLR2004, ARG002, FBT003, RUF059 (test fakes and literals)

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from nixbot.forge import GitHubAppClient

import httpx
import pytest

from nixbot.build_scheduler import AttributeResult, AttributeStatus
from nixbot.db_gen.models import Build as BuildRecord
from nixbot.events import BuildResult, ChangeEvent, RepoInfo
from nixbot.forge import GitlabClient
from nixbot.models import NixEvalJobError
from nixbot.status import (
    CHECK_RUN_TEXT_LIMIT,
    POSTED_GENERATIONS_MAX,
    CheckPermissionError,
    ForgeStatusReporter,
    GitHubCheckRunPoster,
    GitlabStatusPoster,
    StatusState,
    _check_run_output,
    attr_status_context,
    effect_status_context,
    eval_description,
)
from nixbot.tests.support import mk_job


@dataclass
class Posted:
    sha: str
    context: str
    state: StatusState
    description: str
    target_url: str


@dataclass
class FakePoster:
    posts: list[Posted] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)

    async def post(  # noqa: PLR0913
        self,
        owner: str,
        repo: str,
        sha: str,
        context: str,
        state: StatusState,
        description: str,
        target_url: str,
        **extra: object,
    ) -> None:
        self.posts.append(Posted(sha, context, state, description, target_url))
        self.extras.append(extra)


class MemoryFailedStatuses:
    def __init__(self) -> None:
        self.failed: dict[str, set[str]] = {}

    async def mark_failed(self, revision: str, status_name: str) -> None:
        self.failed.setdefault(revision, set()).add(status_name)

    async def get_failed(self, revision: str) -> set[str]:
        return set(self.failed.get(revision, set()))

    async def clear(self, revision: str, status_name: str) -> None:
        self.failed.get(revision, set()).discard(status_name)


PROJECT = RepoInfo(
    id=1,
    key="github/acme/widget",
    name="acme/widget",
    owner="acme",
    repo="widget",
    forge="github",
    clone_url="https://github.com/acme/widget.git",
    default_branch="main",
)

EVENT = ChangeEvent(repo=PROJECT, branch="main", commit_sha="sha1")

BUILD = BuildRecord(
    id=10,
    project_id=1,
    number=42,
    tree_hash="tree",
    commit_sha="sha1",
    branch="main",
    pr_number=None,
    pr_author=None,
    status="building",
    status_generation=0,
    effects_started=False,
    error=None,
    created_at=datetime(2024, 1, 1, tzinfo=UTC),
    started_at=None,
    finished_at=None,
    eval_warnings=None,
    eval_completed=False,
)


def attr_result(
    attr: str, status: AttributeStatus, error: str | None = None
) -> AttributeResult:
    return AttributeResult(
        attr=attr,
        status=status,
        job=NixEvalJobError(error=error or "", attr=attr, attr_path=[attr]),
        error=error,
    )


def make_reporter(
    limit: int = 47,
) -> tuple[ForgeStatusReporter, FakePoster, MemoryFailedStatuses]:
    poster = FakePoster()
    store = MemoryFailedStatuses()
    reporter = ForgeStatusReporter(
        {"github": poster, "gitea": poster},
        store,
        "https://ci.test",
        failed_build_report_limit=limit,
    )
    return reporter, poster, store


async def test_posted_generations_bounded() -> None:
    """One entry per build forever is a slow leak in a long-lived
    process; the generation guard only matters short-term."""

    reporter, _poster, _store = make_reporter()
    for build_id in range(POSTED_GENERATIONS_MAX + 100):
        build = replace(BUILD, id=build_id)
        await reporter.build_finished(EVENT, build, BuildResult("succeeded", 1, []))
    assert len(reporter._posted_generations) <= POSTED_GENERATIONS_MAX  # noqa: SLF001
    assert (POSTED_GENERATIONS_MAX + 99) in reporter._posted_generations  # noqa: SLF001


def test_context_names_default() -> None:
    assert (
        attr_status_context("github", "acme/widget", "x86_64-linux.foo")
        == "nixbot/nix-build github:acme/widget#checks.x86_64-linux.foo"
    )


async def test_context_prefix_configurable() -> None:
    """buildbot-nix migrations keep their branch protection rules by
    setting the buildbot-era prefix."""
    assert (
        attr_status_context(
            "github", "acme/widget", "x86_64-linux.foo", context_prefix="buildbot"
        )
        == "buildbot/nix-build github:acme/widget#checks.x86_64-linux.foo"
    )
    poster = FakePoster()
    reporter = ForgeStatusReporter(
        {"github": poster},
        MemoryFailedStatuses(),
        "https://ci.test",
        context_prefix="buildbot",
    )

    # The eval and build contexts are built at separate call sites;
    # both must honor the prefix. The phase ordering itself is covered
    # by test_phase_statuses_and_target_url.
    await reporter.build_started(EVENT, BUILD)
    await reporter.build_finished(EVENT, BUILD, BuildResult("succeeded", 1, []))
    assert {p.context for p in poster.posts} == {
        "buildbot/nix-eval",
        "buildbot/nix-build",
    }


def test_eval_description_warning_count() -> None:
    assert eval_description(True, []) == "evaluation succeeded"
    assert eval_description(True, ["w"]) == "evaluation succeeded (1 warning)"
    assert eval_description(False, ["a", "b"]) == "evaluation failed (2 warnings)"


async def test_phase_statuses_and_target_url() -> None:
    reporter, poster, _ = make_reporter()

    await reporter.build_started(EVENT, BUILD)
    await reporter.eval_finished(EVENT, BUILD, success=True, warnings=["w"])
    await reporter.build_finished(EVENT, BUILD, BuildResult("succeeded", 1, []))
    contexts = [(p.context, p.state) for p in poster.posts]
    assert contexts == [
        ("nixbot/nix-eval", StatusState.pending),
        ("nixbot/nix-eval", StatusState.success),
        ("nixbot/nix-build", StatusState.pending),
        ("nixbot/nix-build", StatusState.success),
    ]
    assert all(
        p.target_url == "https://ci.test/repos/github/acme/widget/builds/42"
        for p in poster.posts
    )
    assert "(1 warning)" in poster.posts[1].description


async def test_eval_finished_build_plan_in_nix_build_body() -> None:
    """Pending nix-build run lists the attributes to build, each
    linking to its raw log."""
    reporter, poster, _ = make_reporter()
    jobs = [
        mk_job("checks.x86_64-linux.b"),
        mk_job("checks.x86_64-linux.a"),
    ]
    await reporter.eval_finished(EVENT, BUILD, success=True, warnings=[], jobs=jobs)

    build_post = next(
        i for i, p in enumerate(poster.posts) if p.context.endswith("nix-build")
    )
    text = poster.extras[build_post]["text"]
    assert "Building 2 attribute(s):" in text
    # Sorted, so `a` precedes `b`; pending plan omits the status column.
    assert text.index("checks.x86_64-linux.a") < text.index("checks.x86_64-linux.b")
    assert "status" not in text
    # attribute name links to the live viewer; raw log is a separate link.
    assert (
        "[`checks.x86_64-linux.a`](https://ci.test/repos/github/acme/widget/builds/42/logs/checks.x86_64-linux.a)"
        in text
    )
    assert "/builds/42/logs/raw/checks.x86_64-linux.a" in text


async def test_build_finished_table_sorts_failures_first() -> None:
    """Terminal nix-build run re-posts the table with each attribute's
    status, failures listed first."""
    reporter, poster, _ = make_reporter()
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            1,
            [
                attr_result("a", AttributeStatus.succeeded),
                attr_result("z", AttributeStatus.failed),
            ],
            attr_statuses={"a": "succeeded", "z": "failed"},
        ),
    )
    summary_idx = next(
        i for i, p in enumerate(poster.posts) if p.context == "nixbot/nix-build"
    )
    text = poster.extras[summary_idx]["text"]
    assert "Built 2 attribute(s):" in text
    # Failure leads even though `z` sorts after `a` alphabetically.
    assert text.index("`z`") < text.index("`a`")
    assert "| ❌ failed |" in text
    assert "| ✅ succeeded |" in text


async def test_per_attribute_failure_statuses_capped() -> None:
    reporter, poster, store = make_reporter(limit=2)
    results = [
        attr_result(f"a{i}", AttributeStatus.failed, error=f"boom {i}")
        for i in range(4)
    ]

    await reporter.build_finished(EVENT, BUILD, BuildResult("failed", 1, results))
    failure_posts = [
        p for p in poster.posts if p.context.startswith("nixbot/nix-build ")
    ]
    assert len(failure_posts) == 2  # capped at the limit
    # Combined nix-build context still reports the full picture.
    combined = next(p for p in poster.posts if p.context == "nixbot/nix-build")
    assert combined.state == StatusState.failure
    assert "4 of 4" in combined.description


async def test_success_flip_on_rebuild() -> None:
    reporter, poster, store = make_reporter()
    context = attr_status_context("github", "acme/widget", "flaky")

    # First build: flaky fails.
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult("failed", 1, [attr_result("flaky", AttributeStatus.failed)]),
    )
    assert context in await store.get_failed("sha1")
    # Rebuild succeeds: status flipped, record cleared.
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult("succeeded", 2, [attr_result("flaky", AttributeStatus.succeeded)]),
    )
    assert context not in await store.get_failed("sha1")
    flip = [p for p in poster.posts if p.context == context]
    assert [p.state for p in flip] == [StatusState.failure, StatusState.success]


async def test_stale_generation_dropped() -> None:
    reporter, poster, _ = make_reporter()

    await reporter.build_finished(EVENT, BUILD, BuildResult("succeeded", 5, []))
    posts_before = len(poster.posts)
    # Stale post with lower generation: dropped entirely.
    await reporter.build_finished(EVENT, BUILD, BuildResult("failed", 3, []))
    assert len(poster.posts) == posts_before


async def test_cancelled_attributes_recorded_as_failed_statuses() -> None:
    reporter, poster, store = make_reporter()
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult("cancelled", 1, [attr_result("a", AttributeStatus.cancelled)]),
    )
    context = attr_status_context("github", "acme/widget", "a")
    assert context in await store.get_failed("sha1")
    combined = next(p for p in poster.posts if p.context == "nixbot/nix-build")
    assert combined.state == StatusState.error


async def test_attribute_cancel_summary_is_not_superseded() -> None:
    """Cancelling one attribute must aggregate like a failure, not
    claim the whole build was superseded."""
    reporter, poster, _ = make_reporter()
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "cancelled",
            1,
            [
                attr_result("a", AttributeStatus.cancelled),
                attr_result("b", AttributeStatus.succeeded),
                attr_result("c", AttributeStatus.succeeded),
            ],
        ),
    )
    combined = next(p for p in poster.posts if p.context == "nixbot/nix-build")
    assert combined.state == StatusState.error
    assert combined.description == "1 cancelled, 2 succeeded"
    assert "superseded" not in combined.description


async def test_build_level_cancel_keeps_supersede_wording() -> None:
    reporter, poster, _ = make_reporter()
    await reporter.build_finished(EVENT, BUILD, BuildResult("cancelled", 1, []))
    combined = next(p for p in poster.posts if p.context == "nixbot/nix-build")
    assert combined.description == "build cancelled (superseded)"


async def test_attribute_descriptions_are_ansi_stripped() -> None:
    """failure_excerpt keeps ANSI colors for the web UI; forge statuses
    must not carry raw escape codes."""
    reporter, poster, _ = make_reporter()
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            1,
            [
                attr_result(
                    "a", AttributeStatus.failed, error="\x1b[31merror: boom\x1b[0m"
                )
            ],
        ),
    )
    attr_post = next(
        p for p in poster.posts if p.context.startswith("nixbot/nix-build ")
    )
    assert attr_post.description == "error: boom"


async def test_previously_failed_reposts_do_not_consume_budget() -> None:
    """Re-posts of previously-failed contexts must not eat the report
    limit for new failures on a rebuild."""
    reporter, poster, store = make_reporter(limit=2)

    # First build: a0, a1 fail (consume the full budget).
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            1,
            [attr_result(f"a{i}", AttributeStatus.failed) for i in range(2)],
        ),
    )
    poster.posts.clear()
    # Rebuild: same two still fail, plus one new failure.
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            2,
            [attr_result(f"a{i}", AttributeStatus.failed) for i in range(3)],
        ),
    )
    failure_posts = {
        p.context for p in poster.posts if p.context.startswith("nixbot/nix-build ")
    }
    # a2 is reported: the re-posts did not exhaust the budget of 2.
    assert attr_status_context("github", "acme/widget", "a2") in failure_posts


async def test_summary_counts_use_all_attribute_statuses() -> None:
    """Reruns pass only the re-run subset as results; the summary
    description must still cover the whole build."""
    reporter, poster, _ = make_reporter()
    all_statuses = {f"ok{i}": "succeeded" for i in range(99)} | {"flaky": "succeeded"}
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "succeeded",
            1,
            [attr_result("flaky", AttributeStatus.succeeded)],
            attr_statuses=all_statuses,
        ),
    )
    combined = next(p for p in poster.posts if p.context == "nixbot/nix-build")
    assert combined.description == "100 attributes built"


async def test_failed_effect_posts_failure_status() -> None:
    """A failed effect must flip the commit status to failure; a green
    status on a failed deploy hides the breakage (issue #30)."""
    reporter, poster, _ = make_reporter()

    await reporter.effect_started(EVENT, BUILD, "deploy")
    await reporter.effect_finished(
        EVENT, BUILD, "deploy", success=False, error="boom\nmore"
    )
    context = effect_status_context("github", "acme/widget", "deploy")
    states = [(p.context, p.state) for p in poster.posts]
    assert states == [
        (context, StatusState.pending),
        (context, StatusState.failure),
    ]
    assert poster.posts[1].description == "boom\nmore"
    assert poster.extras[1]["text"] == "```\nboom\nmore\n```"


async def test_effects_summary_status() -> None:
    """Effects get an aggregate status alongside the per-effect ones,
    like nix-build's summary."""
    reporter, poster, _ = make_reporter()
    await reporter.effects_started(EVENT, BUILD, 3)
    await reporter.effects_finished(EVENT, BUILD, failed=1, succeeded=2)
    summary = [p for p in poster.posts if p.context == "nixbot/effects"]
    assert [(p.state, p.description) for p in summary] == [
        (StatusState.pending, "running 3 effects"),
        (StatusState.failure, "1 of 3 effects failed"),
    ]


async def test_attr_prefix_follows_repo_configuration() -> None:
    """Repos with attribute = "hydraJobs" keep their old context names."""
    reporter, poster, store = make_reporter()

    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            1,
            [attr_result("foo", AttributeStatus.failed)],
            attr_prefix="hydraJobs",
        ),
    )
    assert any(
        p.context == "nixbot/nix-build github:acme/widget#hydraJobs.foo"
        for p in poster.posts
    )


async def test_poster_network_errors_do_not_propagate() -> None:
    """Transport failures on non-terminal posts must not wedge the
    pipeline; the terminal summary propagates to drive the queued
    retry (RetryingReporter catches it)."""

    class ExplodingPoster:
        async def post(self, *args: object, **kwargs: object) -> None:
            msg = "forge unreachable"
            raise httpx.ConnectError(msg)

    store = MemoryFailedStatuses()
    reporter = ForgeStatusReporter({"github": ExplodingPoster()}, store, "https://ci")

    await reporter.build_started(EVENT, BUILD)  # must not raise
    with pytest.raises(httpx.ConnectError):
        await reporter.build_finished(EVENT, BUILD, BuildResult("succeeded", 1, []))


async def test_reporter_forwards_attr_and_text() -> None:
    """Per-attr posts carry the attr (so the check-run store records
    it for rerequested) and the full error as markdown text; the eval
    run carries the warnings."""
    reporter, poster, _ = make_reporter()

    await reporter.eval_finished(EVENT, BUILD, success=True, warnings=["w1", "w2"])
    eval_extra = poster.extras[0]
    assert eval_extra["text"] == "```\nw1\nw2\n```"
    assert eval_extra["project_id"] == PROJECT.id
    assert eval_extra["build_id"] == BUILD.id

    poster.posts.clear()
    poster.extras.clear()
    await reporter.build_finished(
        EVENT,
        BUILD,
        BuildResult(
            "failed",
            1,
            [attr_result("flaky", AttributeStatus.failed, error="log line 1\nline 2")],
        ),
    )
    attr_idx = next(
        i
        for i, p in enumerate(poster.posts)
        if p.context.startswith("nixbot/nix-build ")
    )
    assert poster.extras[attr_idx]["attr"] == "flaky"
    assert poster.extras[attr_idx]["text"] == "```\nlog line 1\nline 2\n```"


async def test_check_permission_error_does_not_disable_forge() -> None:
    """One repo's missing Checks grant must not stop posting for every
    other repo: a 403 is logged and swallowed, never latched off."""
    calls = 0

    class ForbiddenPoster:
        async def post(self, *args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            msg = "403"
            raise CheckPermissionError(msg)

    reporter = ForgeStatusReporter(
        {"github": ForbiddenPoster()}, MemoryFailedStatuses(), "https://ci"
    )
    await reporter.build_started(EVENT, BUILD)
    await reporter.build_finished(EVENT, BUILD, BuildResult("succeeded", 1, []))
    # Both phases still attempt to post; the forge is never latched off.
    assert calls == 2


def test_check_run_output_title_and_truncate() -> None:
    out = _check_run_output("nixbot/nix-build repo#a", "summary", "body")
    assert out == {"title": "nixbot/nix-build", "summary": "summary", "text": "body"}
    out = _check_run_output("ctx", "s", "x" * (CHECK_RUN_TEXT_LIMIT + 100))
    assert "truncated" in out["text"]
    assert "text" not in _check_run_output("ctx", "s", None)


# --- GitHub check-run poster -----------------------------------------------


class _MemoryCheckRunIds:
    def __init__(self) -> None:
        self.ids: dict[tuple[int, str, str], tuple[str | None, int]] = {}

    async def get(self, project_id: int, sha: str, name: str) -> int | None:
        entry = self.ids.get((project_id, sha, name))
        return entry[1] if entry else None

    async def set(
        self, project_id: int, sha: str, name: str, attr: str | None, external_id: int
    ) -> None:
        self.ids[(project_id, sha, name)] = (attr, external_id)


class _StubGitHub:
    """Just enough GitHubAppClient for GitHubCheckRunPoster."""

    api_url = "https://api.github.com"

    def __init__(self, transport: httpx.MockTransport) -> None:
        self.http = httpx.AsyncClient(transport=transport)

    async def installation_for_repo(self, name: str) -> int | None:
        return 11

    async def installation_token(self, installation_id: int) -> str:
        return "ghs_token"


def _check_run_poster(
    handler: httpx.MockTransport, store: _MemoryCheckRunIds
) -> GitHubCheckRunPoster:
    return GitHubCheckRunPoster(cast("GitHubAppClient", _StubGitHub(handler)), store)


async def test_github_check_run_poster_upsert() -> None:
    requests: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.method == "POST":
            return httpx.Response(201, json={"id": 555})
        return httpx.Response(
            200, json={"id": int(request.url.path.rsplit("/", 1)[-1])}
        )

    store = _MemoryCheckRunIds()
    poster = _check_run_poster(httpx.MockTransport(handler), store)

    await poster.post(
        "acme",
        "widget",
        "sha1",
        "nixbot/nix-eval",
        StatusState.pending,
        "d",
        "u",
        project_id=1,
        build_id=10,
    )
    await poster.post(
        "acme",
        "widget",
        "sha1",
        "nixbot/nix-eval",
        StatusState.error,
        "d",
        "u",
        project_id=1,
        build_id=10,
    )
    assert [r[0] for r in requests] == ["POST", "PATCH"]
    assert requests[0][1] == "/repos/acme/widget/check-runs"
    assert requests[1][1] == "/repos/acme/widget/check-runs/555"
    create = requests[0][2]
    assert create["head_sha"] == "sha1"
    assert create["external_id"] == "10"
    assert create["status"] == "in_progress"
    assert "conclusion" not in create
    patch = requests[1][2]
    assert patch["status"] == "completed"
    assert patch["conclusion"] == "cancelled"
    assert store.ids[(1, "sha1", "nixbot/nix-eval")] == (None, 555)


async def test_github_check_run_patch_404_recreates() -> None:
    """The DB row outlives the GitHub run; without the fallback the
    terminal summary would retry forever via the report work item."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "PATCH":
            return httpx.Response(404, json={})
        return httpx.Response(201, json={"id": 777})

    store = _MemoryCheckRunIds()
    store.ids[(1, "sha1", "ctx")] = (None, 1)  # stale
    poster = _check_run_poster(httpx.MockTransport(handler), store)

    await poster.post(
        "acme",
        "widget",
        "sha1",
        "ctx",
        StatusState.success,
        "d",
        "u",
        project_id=1,
        build_id=10,
    )
    assert methods == ["PATCH", "POST"]
    assert store.ids[(1, "sha1", "ctx")] == (None, 777)


async def test_github_check_run_403_is_permission_error() -> None:
    poster = _check_run_poster(
        httpx.MockTransport(lambda _r: httpx.Response(403, json={})),
        _MemoryCheckRunIds(),
    )
    with pytest.raises(CheckPermissionError):
        await poster.post(
            "acme",
            "widget",
            "sha1",
            "ctx",
            StatusState.pending,
            "d",
            "u",
            project_id=1,
            build_id=10,
        )


async def test_gitlab_status_states() -> None:
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # raw_path: httpx decodes %2F in .path, hiding a broken encoding.
        assert (
            request.url.raw_path == b"/api/v4/projects/Mic92%2Fdotfiles/statuses/abc123"
        )
        posted.append(json.loads(request.content))
        return httpx.Response(201, json={})

    poster = GitlabStatusPoster(
        GitlabClient(
            "https://gitlab.com",
            "t",
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
    )

    for state in (StatusState.pending, StatusState.error):
        await poster.post(
            "Mic92", "dotfiles", "abc123", "nix-eval", state, "d" * 300, "u"
        )
    assert [p["state"] for p in posted] == ["pending", "failed"]
    assert len(posted[0]["description"]) == 255
