"""onEvent deliveries: matching against the default branch's listing,
skipped rows with reasons, lock sharing with onPush effects,
supersession of pending rows, and running with the payload."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from nixbot_effects import EffectError, EventEffectMeta

from nixbot.db import get_or_create_build
from nixbot.db_gen import builds as builds_q
from nixbot.db_gen import maintenance as maint_q
from nixbot.deliver import EventListingCache
from nixbot.events import ChangeEvent
from nixbot.forge import ForgeError
from nixbot.forge_pr import PullRequestInfo
from nixbot.repos import repo_info
from nixbot.webhooks import PrClosed, PrComment, PrLabeled

from .support import FakeEffects, git, insert_build
from .test_service import git_repo, make_service, seed_project, service  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path

    from nixbot.service import CIService

PR = PullRequestInfo(
    number=7,
    title="t",
    url="u",
    draft=False,
    is_fork=True,
    head_rev="",
    head_ref="feature",
    base_ref="main",
    labels=("preview",),
    author="github:dave",
    merged=False,
    open=True,
)


class FakeForgePr:
    def __init__(self, pr: PullRequestInfo, perms: dict[str, str]) -> None:
        self.pr = pr
        self.perms = perms
        self.fail = False
        self.comments: list[str] = []

    async def comment(self, _info: Any, _n: int, body: str, _marker: Any) -> None:
        self.comments.append(body)

    async def pull_request(self, _info: Any, number: int) -> PullRequestInfo:
        if self.fail:
            msg = "boom"
            raise ForgeError(msg, status_code=502)
        assert number == self.pr.number
        return self.pr

    async def is_self(self, user: str) -> bool:
        return user == "github:bot"

    async def permission(self, _project: Any, user: str | None) -> str | None:
        return self.perms.get(user or "")


@pytest.fixture
async def env(
    service: CIService,  # noqa: F811
    git_repo: tuple[Path, str],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    repo, sha = git_repo
    pool = service.pool
    project_id = await seed_project(pool, f"file://{repo}")
    build_id = await insert_build(
        pool,
        project_id,
        number=1,
        branch="main",
        commit_sha=sha,
        tree_hash="t1",
        status="succeeded",
        pr_number=7,
        pr_author="github:dave",
    )
    forge = FakeForgePr(replace(PR, head_rev=sha), {"github:alice": "write"})
    monkeypatch.setattr(type(service), "forge_pr", property(lambda _s: forge))
    listing: dict[str, EventEffectMeta] = {
        "plan": EventEffectMeta(when={"permission": "write"}, lock="infra"),
        "preview": EventEffectMeta(
            when={"labels": ["nope"]}, lock="preview-{pr}", checkout=True
        ),
    }

    listings: dict[str, dict[str, EventEffectMeta]] = {
        "pull_request": listing,
        "comment": {"apply": EventEffectMeta(when={"commands": ["apply"]})},
        "pull_request_closed": {"teardown": EventEffectMeta(lock="preview-{pr}")},
        "build_finished": {
            "broke": EventEffectMeta(
                when={"branches": ["main"], "transition": "broke"}
            ),
            "fixed": EventEffectMeta(when={"transition": "fixed"}),
        },
    }

    fake = FakeEffects(events=listings)
    service.orchestrator.effects = fake
    ran: list[tuple[str, str, dict, Any]] = []

    async def fake_run(
        ctx: Any,
        kind: str,
        name: str,
        payload: dict,
        log_write: Any = None,  # noqa: ARG001
    ) -> bool:
        checkout = None
        if ctx.effect_checkout is not None:
            checkout = (
                git(ctx.effect_checkout, "rev-parse", "HEAD"),
                git(ctx.effect_checkout, "remote", "get-url", "origin"),
            )
        ran.append((kind, name, payload, checkout))
        return True

    fake.run_event_effect = fake_run  # type: ignore[method-assign,assignment]
    return {
        "service": service,
        "project_id": project_id,
        "build_id": build_id,
        "sha": sha,
        "forge": forge,
        "ran": ran,
        "repo": repo,
        "listing": listing,
        "listings": listings,
    }


async def _rows(svc: CIService, build_id: int) -> dict[str, dict]:
    rows = await svc.pool.fetch(
        "SELECT * FROM effect_runs WHERE build_id = $1 AND kind <> 'push'", build_id
    )
    return {r["name"]: dict(r) for r in rows}


async def _deliver(svc: CIService, build_id: int, actor: str | None) -> None:
    await svc.enqueue_work(
        "deliver",
        f"d-{build_id}",
        {"kind": "pull_request", "build_id": build_id, "actor": actor, "pr_number": 7},
    )
    await svc.drain_work()


async def test_match_skip_and_run(env: dict[str, Any]) -> None:
    svc, build_id = env["service"], env["build_id"]
    await _deliver(svc, build_id, "github:alice")
    rows = await _rows(svc, build_id)
    assert rows["preview"]["status"] == "skipped"
    assert rows["preview"]["skip_reason"] == "missing labels: nope"
    assert rows["plan"]["status"] == "succeeded"
    assert rows["plan"]["actor"] == "github:alice"
    assert rows["plan"]["code_rev"] == env["sha"]
    [(kind, name, payload, _checkout)] = env["ran"]
    assert (kind, name) == ("pull_request", "plan")
    assert payload["actor"] == {"name": "github:alice", "permission": "write"}
    assert payload["pullRequest"]["author"] == {
        "name": "github:dave",
        "permission": None,
    }
    assert payload["pullRequest"]["labels"] == ["preview"]
    assert payload["build"]["status"] == "succeeded"
    # No forge summary status for event effects.
    assert (
        await svc.pool.fetchval(
            "SELECT count(*) FROM effect_runs WHERE build_id = $1 AND kind = 'push'",
            build_id,
        )
        == 0
    )


async def test_permission_denied_records_reason(env: dict[str, Any]) -> None:
    svc, build_id = env["service"], env["build_id"]
    await _deliver(svc, build_id, "github:mallory")
    rows = await _rows(svc, build_id)
    assert rows["plan"]["status"] == "skipped"
    assert rows["plan"]["skip_reason"] == (
        "needs write access (actor github:mallory: none, author github:dave: none)"
    )
    assert env["ran"] == []


async def test_forge_failure_fails_rows(env: dict[str, Any]) -> None:
    svc, build_id = env["service"], env["build_id"]
    env["forge"].fail = True
    await _deliver(svc, build_id, "github:alice")
    rows = await _rows(svc, build_id)
    assert {r["status"] for r in rows.values()} == {"failed"}
    assert "forge lookup failed" in rows["plan"]["skip_reason"]
    assert env["ran"] == []


async def test_lock_shared_with_push_effect_and_supersession(
    env: dict[str, Any],
) -> None:
    """`plan` (lock infra) queues behind a pending onPush item holding
    the same lock. A newer delivery for the PR cancels the older
    pending row."""
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    project_id = env["project_id"]
    # A push effect holding the lock, claimed (running) so nothing behind it starts.
    await pool.execute(
        "INSERT INTO work_queue (kind, dedup_key, payload, status, claimed_at)"
        " VALUES ('effect', $1, '{}'::jsonb, 'running', now())",
        f"effect-lock-{project_id}-infra",
    )
    await _deliver(svc, build_id, "github:alice")
    assert (await _rows(svc, build_id))["plan"]["status"] == "pending"
    assert env["ran"] == []
    # New push to the PR: newer build, new delivery.
    newer = await insert_build(
        pool,
        project_id,
        number=2,
        branch="main",
        commit_sha=env["sha"],
        tree_hash="t2",
        status="succeeded",
        pr_number=7,
    )
    await _deliver(svc, newer, "github:alice")
    old = await _rows(svc, build_id)
    assert old["plan"]["status"] == "cancelled"
    assert (await _rows(svc, newer))["plan"]["status"] == "pending"
    # Release the lock: only the newer plan runs.
    await pool.execute("UPDATE work_queue SET status = 'done' WHERE status = 'running'")
    await svc.drain_work()
    assert [(k, n, p["build"]["number"]) for k, n, p, _ in env["ran"]] == [
        ("pull_request", "plan", 2)
    ]


async def test_checkout_is_pr_head_without_token(env: dict[str, Any]) -> None:
    svc, build_id = env["service"], env["build_id"]
    env["listing"]["preview"] = EventEffectMeta(checkout=True)
    await _deliver(svc, build_id, "github:alice")
    ran = {n: c for _, n, _p, c in env["ran"]}
    # Prepared for every event effect like onPush does. Mounted only
    # when declared. PR head checked out, origin is the plain clone URL
    # (no token).
    assert ran["preview"] == (env["sha"], f"file://{env['repo']}")


async def test_build_success_enqueues_delivery(env: dict[str, Any]) -> None:
    """The hook: a PR build settling green delivers pull_request."""
    svc, pool = env["service"], env["service"].pool

    build = await builds_q.get_build(pool, id_=env["build_id"])
    project = await svc.repo_store.by_id(env["project_id"])
    assert build is not None
    assert project is not None
    event = ChangeEvent(
        repo=repo_info(project),
        branch="main",
        commit_sha=env["sha"],
        pr_number=7,
        actor="github:alice",
    )
    await svc.orchestrator.deliver_events(event, build)
    await svc.drain_work()
    rows = await _rows(svc, env["build_id"])
    assert rows["plan"]["status"] == "succeeded"
    assert "broke" in rows
    # A push reusing this build re-delivers pull_request only.
    await svc.pool.execute(
        "DELETE FROM effect_runs WHERE build_id = $1", env["build_id"]
    )
    await svc.orchestrator.deliver_events(event, build, finished=False)
    await svc.drain_work()
    assert set(await _rows(svc, env["build_id"])) == {"plan", "preview"}


async def test_comment_and_close_webhooks_deliver(env: dict[str, Any]) -> None:
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", env["project_id"]
    )
    base = {"forge": "github", "forge_repo_id": forge_repo_id, "pr_number": 7}
    # nixbot's own "/apply" comment must not trigger anything.
    await svc.submit(PrComment(**base, actor="github:bot", command="apply", args=""))
    await svc.submit(PrComment(**base, actor="github:alice", command="nope", args=""))
    await svc.submit(PrComment(**base, actor="github:alice", command="apply", args="x"))
    await svc.submit(PrClosed(**base, merged=True, actor="github:alice"))
    await svc.drain_work()
    rows = await _rows(svc, build_id)
    # The second comment replaced the first's skipped row.
    assert rows["apply"]["status"] == "succeeded"
    assert rows["teardown"]["status"] == "succeeded"
    ran = {n: p for _k, n, p, _ in env["ran"]}
    assert (ran["apply"]["command"], ran["apply"]["args"]) == ("apply", "x")
    assert ran["teardown"]["pullRequest"]["number"] == PR.number
    # Merge did not cancel the PR build.
    assert (
        await pool.fetchval("SELECT status FROM builds WHERE id = $1", build_id)
        == "succeeded"
    )


async def test_close_after_merge_push_reused_build(env: dict[str, Any]) -> None:
    """Gitea sends the merge push before the close. The push reuses the
    PR build for the default branch, and the close must still find it."""
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    forge_repo_id, sha = await pool.fetchrow(
        "SELECT p.forge_repo_id, b.commit_sha FROM projects p"
        " JOIN builds b ON b.project_id = p.id WHERE b.id = $1",
        build_id,
    )
    reused, created = await get_or_create_build(
        pool, env["project_id"], "t1", sha, "main", pr_number=None
    )
    assert (reused.id_, created, reused.pr_number) == (build_id, False, None)
    await svc.submit(
        PrClosed(
            forge="github",
            forge_repo_id=forge_repo_id,
            pr_number=7,
            merged=True,
            actor="github:alice",
        )
    )
    await svc.drain_work()
    assert (await _rows(svc, build_id))["teardown"]["status"] == "succeeded"


async def _finish(svc: CIService, env: dict[str, Any], build_id: int) -> None:
    pool = svc.pool
    build = await builds_q.get_build(pool, id_=build_id)
    project = await svc.repo_store.by_id(env["project_id"])
    assert build is not None
    assert project is not None
    event = ChangeEvent(
        repo=repo_info(project),
        branch=build.branch,
        commit_sha=env["sha"],
        pr_number=build.pr_number,
        actor="github:alice",
    )
    await svc.orchestrator.deliver_events(event, build)
    await svc.drain_work()


async def test_build_finished_transitions(env: dict[str, Any]) -> None:
    """broke fires on the first red main build after green, fixed on
    the PR going green after red. Branch and PR histories are separate."""
    svc, pool, project_id = env["service"], env["service"].pool, env["project_id"]
    green = await insert_build(
        pool, project_id, number=10, commit_sha="a", tree_hash="m1", status="succeeded"
    )
    red = await insert_build(
        pool, project_id, number=11, commit_sha="b", tree_hash="m2", status="failed"
    )
    await pool.execute(
        "INSERT INTO build_attributes (build_id, attr, status)"
        " VALUES ($1, 'x86_64-linux.hello', 'failed')",
        red,
    )
    red2 = await insert_build(
        pool, project_id, number=12, commit_sha="c", tree_hash="m3", status="failed"
    )
    await _finish(svc, env, green)
    await _finish(svc, env, red)
    await _finish(svc, env, red2)
    assert (await _rows(svc, green))["broke"]["skip_reason"].startswith("not a broke")
    assert (await _rows(svc, red))["broke"]["status"] == "succeeded"
    assert (await _rows(svc, red2))["broke"]["status"] == "skipped"
    ran = [(n, p["build"]) for k, n, p, _ in env["ran"] if k == "build_finished"]
    assert [
        (n, b["number"], b["previousStatus"], b["failedAttrs"]) for n, b in ran
    ] == [("broke", 11, "succeeded", ["x86_64-linux.hello"])]
    # PR 7: build 1 (succeeded, from the fixture) has no predecessor.
    pr_red = await insert_build(
        pool,
        project_id,
        number=13,
        commit_sha="d",
        tree_hash="p1",
        status="failed",
        pr_number=7,
    )
    pr_green = await insert_build(
        pool,
        project_id,
        number=14,
        commit_sha="e",
        tree_hash="p2",
        status="succeeded",
        pr_number=7,
    )
    env["ran"].clear()
    await _finish(svc, env, pr_red)
    await _finish(svc, env, pr_green)
    fixed = [p["build"]["number"] for k, n, p, _ in env["ran"] if n == "fixed"]
    assert fixed == [14]
    # main's red build 12 is not the PR's predecessor.
    assert (await _rows(svc, pr_red))["fixed"]["skip_reason"] == (
        "not a fixed transition (succeeded -> failed)"
    )


async def test_command_while_running_gets_notice(env: dict[str, Any]) -> None:
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", env["project_id"]
    )
    await pool.execute(
        "INSERT INTO effect_runs (project_id, kind, build_id, name, status)"
        " VALUES ($1, 'comment', $2, 'apply', 'running')",
        env["project_id"],
        build_id,
    )
    await svc.submit(
        PrComment(
            forge="github",
            forge_repo_id=forge_repo_id,
            pr_number=7,
            actor="github:alice",
            command="apply",
            args="",
        )
    )
    await svc.drain_work()
    assert env["ran"] == []
    assert env["forge"].comments == [
        "`/apply`: apply still running, comment again once it finished."
    ]
    # A different command is not about apply, so no notice.
    await svc.submit(
        PrComment(
            forge="github",
            forge_repo_id=forge_repo_id,
            pr_number=7,
            actor="github:alice",
            command="other",
            args="",
        )
    )
    await svc.drain_work()
    assert len(env["forge"].comments) == 1


async def test_other_command_does_not_supersede(env: dict[str, Any]) -> None:
    """A /ping after a new push must not cancel the /apply still queued
    on the previous build of the PR."""
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    project_id = env["project_id"]
    await pool.execute(
        "INSERT INTO work_queue (kind, dedup_key, payload, status, claimed_at)"
        " VALUES ('effect', $1, '{}'::jsonb, 'running', now())",
        f"effect-lock-{project_id}-deploy",
    )
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", project_id
    )
    base = {"forge": "github", "forge_repo_id": forge_repo_id, "pr_number": 7}
    env["listings"]["comment"] = {
        "apply": EventEffectMeta(when={"commands": ["apply"]}, lock="deploy"),
        "ping": EventEffectMeta(when={"commands": ["ping"]}),
    }
    await svc.submit(PrComment(**base, actor="github:alice", command="apply", args=""))
    await svc.drain_work()
    assert (await _rows(svc, build_id))["apply"]["status"] == "pending"
    newer = await insert_build(
        pool,
        project_id,
        number=2,
        commit_sha=env["sha"],
        tree_hash="t2",
        status="succeeded",
        pr_number=7,
    )
    await svc.submit(PrComment(**base, actor="github:alice", command="ping", args=""))
    await svc.drain_work()
    assert (await _rows(svc, newer))["ping"]["status"] == "succeeded"
    assert (await _rows(svc, build_id))["apply"]["status"] == "pending"
    await svc.submit(PrComment(**base, actor="github:alice", command="apply", args=""))
    await svc.drain_work()
    assert (await _rows(svc, build_id))["apply"]["status"] == "cancelled"
    assert (await _rows(svc, newer))["apply"]["status"] == "pending"


async def test_label_change_redelivers_pull_request(env: dict[str, Any]) -> None:
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    forge_repo_id = await pool.fetchval(
        "SELECT forge_repo_id FROM projects WHERE id = $1", env["project_id"]
    )
    env["forge"].pr = dataclasses.replace(env["forge"].pr, labels=())
    labeled = PrLabeled(
        forge="github", forge_repo_id=forge_repo_id, pr_number=7, actor="github:bob"
    )
    await svc.submit(labeled)
    await svc.drain_work()
    assert (await _rows(svc, build_id))["preview"]["status"] == "skipped"
    env["forge"].pr = dataclasses.replace(env["forge"].pr, labels=("nope",))
    await svc.submit(labeled)
    await svc.drain_work()
    assert (await _rows(svc, build_id))["preview"]["status"] == "succeeded"
    # A red head gets nothing: pull_request means the head built green.
    await pool.execute("UPDATE builds SET status = 'failed' WHERE id = $1", build_id)
    await pool.execute("DELETE FROM effect_runs WHERE build_id = $1", build_id)
    await svc.submit(labeled)
    await svc.drain_work()
    assert await _rows(svc, build_id) == {}


async def test_build_outcome_and_restart_leave_event_history(
    env: dict[str, Any],
) -> None:
    svc, build_id, pool = env["service"], env["build_id"], env["service"].pool
    for name, status in (("done", "succeeded"), ("queued", "pending")):
        await pool.execute(
            "INSERT INTO effect_runs (project_id, kind, build_id, name, status)"
            " VALUES ($1, 'comment', $2, $3, $4)",
            env["project_id"],
            build_id,
            name,
            status,
        )
    # A failing rebuild owns its onPush rows, not the event rows.
    await builds_q.set_build_status(
        pool, status="failed", error=None, terminal=["failed"], id_=build_id
    )
    rows = await _rows(svc, build_id)
    assert (rows["done"]["status"], rows["queued"]["status"]) == (
        "succeeded",
        "pending",
    )
    await maint_q.reset_build_for_restart(pool, attr=None, build_id=build_id)
    rows = await _rows(svc, build_id)
    assert (rows["done"]["status"], rows["queued"]["status"]) == (
        "succeeded",
        "cancelled",
    )


async def test_restart_event_effect_reruns_with_stored_payload(
    env: dict[str, Any],
) -> None:
    svc, build_id = env["service"], env["build_id"]
    await _deliver(svc, build_id, "github:alice")
    assert (await _rows(svc, build_id))["preview"]["lock"] == "preview-7"
    env["ran"].clear()
    await svc.restart_effects(build_id, "plan", kind="pull_request")
    await svc.drain_work()
    [(kind, name, payload, _)] = env["ran"]
    assert (kind, name, payload["pullRequest"]["number"]) == ("pull_request", "plan", 7)
    # Skipped rows have a payload too and may be forced this way.
    await svc.restart_effects(build_id, "preview", kind="pull_request")
    await svc.drain_work()
    assert (await _rows(svc, build_id))["preview"]["status"] == "succeeded"


async def test_listing_failure_recorded_on_build(env: dict[str, Any]) -> None:
    svc, build_id = env["service"], env["build_id"]

    fake = svc.orchestrator.effects
    fake.events_error = EffectError("error: attribute 'typo' missing")
    svc.event_listings = EventListingCache()  # as if main moved
    await _deliver(svc, build_id, "github:alice")
    errors = await builds_q.effect_eval_errors(svc.pool, build_id=build_id)
    assert [(x.source, "typo" in x.error) for x in errors] == [("delivery", True)]
    assert env["ran"] == []
    # Fixed on the default branch: the next delivery clears it.
    fake.events_error = None
    svc.event_listings = EventListingCache()  # as if main moved
    await _deliver(svc, build_id, "github:alice")
    assert await builds_q.effect_eval_errors(svc.pool, build_id=build_id) == []
