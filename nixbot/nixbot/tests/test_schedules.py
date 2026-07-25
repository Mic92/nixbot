"""Tests for scheduled effects: parsing, cron matching, persistence."""

# ruff: noqa: PLR2004 (test literals, secret_name is a credential id)
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from nixbot_effects import EffectError

from nixbot.config import ScheduleWhen
from nixbot.effects import EffectsContext, run_scheduled_effect
from nixbot.schedules import (
    DueEffect,
    ScheduledEffectsStore,
    due_occurrence,
    is_due,
    next_occurrence,
    parse_schedules_from_json,
    schedule_overview,
)

from .support import insert_project

if TYPE_CHECKING:
    from pathlib import Path

    import asyncpg


def test_parse_schedules() -> None:
    schedules = parse_schedules_from_json(
        {
            "nightly": {
                "when": {"minute": 30, "hour": 2, "dayOfWeek": ["Mon", "Fri"]},
                "effects": ["deploy", "backup"],
            },
            "monthly": {"when": {"dayOfMonth": [1]}, "effects": ["report"]},
        }
    )
    assert schedules["nightly"].when.minute == 30
    assert schedules["nightly"].effects == ["deploy", "backup"]
    assert schedules["monthly"].when.dayOfMonth == [1]


def test_is_due() -> None:
    # Scalar hour (separate branch from the hour-list case below).
    when = ScheduleWhen(minute=30, hour=2)
    assert is_due(when, "s", datetime(2026, 6, 5, 2, 30, tzinfo=UTC))
    assert not is_due(when, "s", datetime(2026, 6, 5, 2, 31, tzinfo=UTC))
    assert not is_due(when, "s", datetime(2026, 6, 5, 3, 30, tzinfo=UTC))

    when = ScheduleWhen(minute=0, hour=[6, 18], dayOfWeek=["Mon"])
    monday = datetime(2026, 6, 1, 6, 0, tzinfo=UTC)  # a Monday
    tuesday = datetime(2026, 6, 2, 6, 0, tzinfo=UTC)
    assert is_due(when, "s", monday)
    assert not is_due(when, "s", tuesday)
    assert is_due(when, "s", monday.replace(hour=18))
    assert not is_due(when, "s", monday.replace(hour=12))

    monthly = ScheduleWhen(minute=0, hour=0, dayOfMonth=[15])
    assert is_due(monthly, "s", datetime(2026, 6, 15, 0, 0, tzinfo=UTC))
    assert not is_due(monthly, "s", datetime(2026, 6, 16, 0, 0, tzinfo=UTC))


def test_next_occurrence() -> None:
    when = ScheduleWhen(minute=30, hour=2)
    now = datetime(2026, 6, 5, 2, 0, tzinfo=UTC)
    assert next_occurrence(when, "s", now) == datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
    after = datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
    assert next_occurrence(when, "s", after) == datetime(2026, 6, 6, 2, 30, tzinfo=UTC)
    weekly = ScheduleWhen(minute=0, hour=[6, 18], dayOfWeek=["Mon"])
    friday = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)
    assert next_occurrence(weekly, "s", friday) == datetime(
        2026, 6, 8, 6, 0, tzinfo=UTC
    )
    monthly = ScheduleWhen(minute=0, hour=0, dayOfMonth=[15])
    assert next_occurrence(monthly, "s", friday) == datetime(
        2026, 6, 15, 0, 0, tzinfo=UTC
    )


def test_deterministic_defaults() -> None:
    when = ScheduleWhen()
    fields1 = when.resolved("nightly")
    assert fields1 == when.resolved("nightly")
    assert fields1["minute"] != when.resolved("weekly")["minute"]
    assert fields1["hour"] == list(range(24))
    for hour in (0, 13, 23):
        due_at = datetime(2026, 6, 5, hour, fields1["minute"], tzinfo=UTC)
        assert is_due(when, "nightly", due_at)


# --- persistence -------------------------------------------------------------


async def test_replace_schedules_preserves_last_run_for_unchanged_spec(
    pool: asyncpg.Pool,
) -> None:
    """A default-branch push within the due window must not re-fire the
    same occurrence: re-discovery of an unchanged schedule keeps
    last_run."""

    project_id = await insert_project(pool, forge_repo_id="sched-keep")
    store = ScheduledEffectsStore(pool)
    schedules = parse_schedules_from_json(
        {"nightly": {"when": {"minute": 7, "hour": 3}, "effects": ["d"]}}
    )
    await store.replace_schedules(project_id, schedules)
    due_time = datetime(2026, 6, 5, 3, 7, tzinfo=UTC)

    def mine(due_list: list) -> list:
        # The module-shared database holds other tests' rows.
        return [d for d in due_list if d.project_id == project_id]

    (due,) = mine(await store.due_effects(due_time))
    await store.mark_run(due, due_time)

    await store.replace_schedules(project_id, schedules)
    assert mine(await store.due_effects(due_time)) == []

    changed = parse_schedules_from_json(
        {"nightly": {"when": {"minute": 8, "hour": 3}, "effects": ["d"]}}
    )
    await store.replace_schedules(project_id, changed)
    assert len(mine(await store.due_effects(due_time.replace(minute=8)))) == 1


async def test_replace_schedules_tolerates_duplicate_effect_names(
    pool: asyncpg.Pool,
) -> None:
    """Effect lists are repo-controlled. A duplicate name must not crash
    the update and permanently block schedule refreshes."""

    project_id = await insert_project(pool, forge_repo_id="sched-dupe")
    store = ScheduledEffectsStore(pool)
    schedules = parse_schedules_from_json(
        {
            "nightly": {
                "when": {"minute": 9, "hour": 4},
                "effects": ["deploy", "deploy"],
            }
        }
    )
    await store.replace_schedules(project_id, schedules)
    rows = await pool.fetch(
        "SELECT effect FROM scheduled_effects WHERE project_id = $1",
        project_id,
    )
    assert [row["effect"] for row in rows] == ["deploy"]


async def test_store_roundtrip_and_due(pool: asyncpg.Pool) -> None:
    project_id = await insert_project(pool, forge_repo_id="sched-1")
    store = ScheduledEffectsStore(pool)
    schedules = parse_schedules_from_json(
        {
            "nightly": {
                "when": {"minute": 30, "hour": 2},
                "effects": ["deploy"],
            }
        }
    )
    await store.replace_schedules(project_id, schedules)

    due_time = datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
    due = await store.due_effects(due_time)
    assert len(due) == 1
    assert due[0].schedule_name == "nightly"
    assert due[0].effect == "deploy"

    # Not due at other times (outside the sweep window).
    assert await store.due_effects(due_time.replace(minute=36)) == []

    # Marked as run: not due again in the same minute.
    await store.mark_run(due[0], due_time)
    assert await store.due_effects(due_time) == []
    # Due again the next day.
    next_day = due_time.replace(day=6)
    assert len(await store.due_effects(next_day)) == 1

    # Re-discovery replaces the schedule set.
    await store.replace_schedules(project_id, {})
    assert await store.due_effects(due_time.replace(day=7)) == []


def test_due_occurrence_window() -> None:
    # The sweep loop drifts past minute boundaries. Occurrences within
    # the window must still be found.
    when = ScheduleWhen(minute=30, hour=2)
    occ = due_occurrence(when, "s", datetime(2026, 6, 5, 2, 33, 10, tzinfo=UTC))
    assert occ == datetime(2026, 6, 5, 2, 30, tzinfo=UTC)
    assert due_occurrence(when, "s", datetime(2026, 6, 5, 2, 36, tzinfo=UTC)) is None
    assert due_occurrence(when, "s", datetime(2026, 6, 5, 2, 29, tzinfo=UTC)) is None


async def test_due_effects_window_and_bad_spec(pool: asyncpg.Pool) -> None:
    project_id = await insert_project(pool, "gadget", forge_repo_id="sched-2")
    store = ScheduledEffectsStore(pool)
    schedules = parse_schedules_from_json(
        {
            "nightly": {
                "when": {"minute": 30, "hour": 2},
                "effects": ["deploy"],
            },
            # Repo-controlled misspelling: must not abort the sweep.
            "broken": {
                "when": {"minute": 0, "hour": 0, "dayOfWeek": ["monday"]},
                "effects": ["report"],
            },
        }
    )
    await store.replace_schedules(project_id, schedules)

    # The sweep drifted past 2:30: the occurrence is still found,
    # and the malformed schedule is skipped instead of raising.
    # (mark_run/no-refire lifecycle is covered by
    # test_store_roundtrip_and_due.)
    late = datetime(2026, 6, 5, 2, 31, 40, tzinfo=UTC)
    due = [d for d in await store.due_effects(late) if d.project_id == project_id]
    assert [d.schedule_name for d in due] == ["nightly"]


async def test_run_scheduled_effect_secret_read_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same contract as push effects: the service records the raised
    # error as a failed run.
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    ctx = EffectsContext(
        path=tmp_path,
        rev="deadbeef",
        branch="main",
        repo="acme/widget",
        secret_name="effects-secret",
    )
    with pytest.raises(EffectError, match="CREDENTIALS_DIRECTORY"):
        await run_scheduled_effect(ctx, "nightly", "deploy")


async def test_run_recording_and_overview(pool: asyncpg.Pool) -> None:
    project_id = await insert_project(pool, "gadget", forge_repo_id="sched-4")
    store = ScheduledEffectsStore(pool)
    schedules = parse_schedules_from_json(
        {"heartbeat": {"when": {}, "effects": ["beat"]}}
    )
    await store.replace_schedules(project_id, schedules)

    overview = schedule_overview(
        await store.schedules_for_project(project_id),
        await store.latest_runs_for_project(project_id),
    )
    assert overview == [
        {
            "schedule": "heartbeat",
            "effect": "beat",
            "when": overview[0]["when"],
            "next": overview[0]["next"],
            "run": None,
        }
    ]
    assert overview[0]["when"].startswith("hourly at :")

    due = DueEffect(
        project_id=project_id,
        schedule_name="heartbeat",
        effect="beat",
        when=ScheduleWhen(),
    )
    run_id = await store.start_run(due)
    await store.finish_run(run_id, success=False, error="boom")
    overview = schedule_overview(
        await store.schedules_for_project(project_id),
        await store.latest_runs_for_project(project_id),
    )
    assert overview[0]["run"]["status"] == "failed"
    assert overview[0]["run"]["error"] == "boom"
