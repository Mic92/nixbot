"""Scheduled Hercules effects (`onSchedule`), tasks 3.3.

On each successful default-branch build the orchestrator discovers
schedules and persists them. A periodic loop runs due effects.

Cron-like `when` specs. Defaults resolve in
config.ScheduleWhen.resolved. All times are UTC.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from .config import ScheduledEffectConfig, ScheduleWhen
from .db_gen import scheduled as q
from .effects import list_scheduled_effects
from .sql_util import expect

if TYPE_CHECKING:
    import asyncpg

    from .effects import EffectsContext

logger = logging.getLogger(__name__)


def parse_schedules_from_json(
    schedules_json: dict[str, Any],
) -> dict[str, ScheduledEffectConfig]:
    """Parse the onSchedule definitions listed by nixbot_effects."""
    result: dict[str, ScheduledEffectConfig] = {}
    for schedule_name, schedule_data in schedules_json.items():
        when_data = schedule_data.get("when", {})
        result[schedule_name] = ScheduledEffectConfig(
            name=schedule_name,
            when=ScheduleWhen(
                minute=when_data.get("minute"),
                hour=when_data.get("hour"),
                dayOfWeek=when_data.get("dayOfWeek"),
                dayOfMonth=when_data.get("dayOfMonth"),
            ),
            effects=schedule_data.get("effects", []),
        )
    return result


async def discover_schedules(
    ctx: EffectsContext,
) -> dict[str, ScheduledEffectConfig]:
    """The onSchedule definitions on a default-branch checkout."""
    return parse_schedules_from_json(await list_scheduled_effects(ctx))


def is_due(when: ScheduleWhen, schedule_name: str, now: datetime) -> bool:
    """Whether the schedule matches the current UTC minute."""
    fields = when.resolved(schedule_name)
    if now.minute != fields["minute"]:
        return False
    hour = fields["hour"]
    hours = hour if isinstance(hour, list) else [hour]
    if now.hour not in hours:
        return False
    if "dayOfWeek" in fields and now.weekday() not in fields["dayOfWeek"]:
        return False
    return not ("dayOfMonth" in fields and now.day not in fields["dayOfMonth"])


# How far back a sweep looks for a matching minute: the sweep loop
# sleeps 60s between iterations, so its sampling points drift and can
# skip a whole minute.
DUE_WINDOW_MINUTES = 5


def due_occurrence(
    when: ScheduleWhen, schedule_name: str, now: datetime
) -> datetime | None:
    """The most recent scheduled occurrence within the sweep window, or
    None when the schedule did not fire in that window."""
    for offset in range(DUE_WINDOW_MINUTES):
        candidate = (now - timedelta(minutes=offset)).replace(second=0, microsecond=0)
        if is_due(when, schedule_name, candidate):
            return candidate
    return None


HOURS_PER_DAY = 24


def describe_when(when: ScheduleWhen, schedule_name: str) -> str:
    """Human-readable resolved schedule, e.g. "hourly at :39 UTC"."""
    fields = when.resolved(schedule_name)
    minute = fields["minute"]
    hour = fields["hour"]
    hours = hour if isinstance(hour, list) else [hour]
    if len(hours) == HOURS_PER_DAY:
        text = f"hourly at :{minute:02d} UTC"
    else:
        text = "at " + ", ".join(f"{h:02d}:{minute:02d}" for h in hours) + " UTC"
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if "dayOfWeek" in fields:
        text += " on " + ", ".join(days[d] for d in fields["dayOfWeek"])
    if "dayOfMonth" in fields:
        text += " on day " + ", ".join(str(d) for d in fields["dayOfMonth"])
    return text


def next_occurrence(
    when: ScheduleWhen, schedule_name: str, now: datetime
) -> datetime | None:
    """The next fire after `now`, or None for impossible day filters."""
    fields = when.resolved(schedule_name)
    hour = fields["hour"]
    hours = sorted(hour) if isinstance(hour, list) else [hour]
    start = now.replace(second=0, microsecond=0)
    for day_offset in range(366):
        day = start + timedelta(days=day_offset)
        for h in hours:
            candidate = day.replace(hour=h, minute=fields["minute"])
            if candidate > now and is_due(when, schedule_name, candidate):
                return candidate
    return None


def schedule_overview(schedules: list[dict], runs: list[dict]) -> list[dict[str, Any]]:
    """Schedules joined with their latest recorded run for the UI."""
    latest: dict[tuple[str, str], dict] = {}
    for entry in runs:  # newest first
        latest.setdefault((entry["schedule_name"], entry["effect"]), entry)
    overview = []
    now = datetime.now(tz=UTC)
    for schedule in schedules:
        when = ScheduleWhen.model_validate_json(schedule["when_spec"])
        overview.append(
            {
                "schedule": schedule["schedule_name"],
                "effect": schedule["effect"],
                "when": describe_when(when, schedule["schedule_name"]),
                "next": next_occurrence(when, schedule["schedule_name"], now),
                "run": latest.get((schedule["schedule_name"], schedule["effect"])),
            }
        )
    return overview


@dataclass(frozen=True)
class DueEffect:
    project_id: int
    schedule_name: str
    effect: str
    when: ScheduleWhen


class ScheduledEffectsStore:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    async def replace_schedules(
        self, project_id: int, schedules: dict[str, ScheduledEffectConfig]
    ) -> None:
        """Replace a project's schedules with the freshly discovered set.

        last_run is preserved when the when-spec is unchanged: every
        default-branch push re-discovers schedules, and resetting
        last_run would re-fire an occurrence that already ran.
        Duplicate effect names (repo-controlled) are dropped instead of
        crashing the update on the primary key."""
        async with self.pool.acquire() as conn, conn.transaction():
            previous = {
                (row.schedule_name, row.effect): row
                for row in await q.schedules_for_update(conn, project_id=project_id)
            }
            await q.delete_project_schedules(conn, project_id=project_id)
            seen: set[tuple[str, str]] = set()
            rows: list[tuple[str, str, str, datetime | None]] = []
            for config in schedules.values():
                for effect in config.effects:
                    key = (config.name, effect)
                    if key in seen:
                        logger.warning(
                            "duplicate scheduled effect ignored",
                            extra={"schedule": config.name, "effect": effect},
                        )
                        continue
                    seen.add(key)
                    when_spec = config.when.model_dump_json(exclude_none=True)
                    old = previous.get(key)
                    last_run = (
                        old.last_run
                        if old is not None
                        and json.loads(old.when_spec) == json.loads(when_spec)
                        else None
                    )
                    rows.append((config.name, effect, when_spec, last_run))
            if rows:
                await q.insert_schedules(
                    conn,
                    project_id=project_id,
                    schedule_names=[r[0] for r in rows],
                    effects=[r[1] for r in rows],
                    when_specs=[r[2] for r in rows],
                    # The generated signature types timestamptz[]
                    # elements non-null, but Postgres arrays carry
                    # NULLs fine (last_run is nullable).
                    last_runs=cast("list[datetime]", [r[3] for r in rows]),
                )

    async def due_effects(self, now: datetime | None = None) -> list[DueEffect]:
        """Effects whose schedule fired within the sweep window and
        which have not run for that occurrence yet."""
        now = now or datetime.now(tz=UTC)
        rows = await q.due_schedule_rows(self.pool, now=now)
        due = []
        for row in rows:
            try:
                when = ScheduleWhen.model_validate(json.loads(row.when_spec))
                occurrence = due_occurrence(when, row.schedule_name, now)
            except Exception:
                # when-specs are repo-controlled. One malformed spec
                # must not abort the sweep for all other projects.
                logger.exception(
                    "invalid schedule spec, skipping",
                    extra={
                        "project_id": row.project_id,
                        "schedule": row.schedule_name,
                    },
                )
                continue
            last_run = row.last_run
            if occurrence is not None and (last_run is None or last_run < occurrence):
                due.append(
                    DueEffect(
                        project_id=row.project_id,
                        schedule_name=row.schedule_name,
                        effect=row.effect,
                        when=when,
                    )
                )
        return due

    async def start_run(self, due: DueEffect) -> int:
        return expect(
            await q.start_scheduled_run(
                self.pool,
                project_id=due.project_id,
                schedule_name=due.schedule_name,
                effect=due.effect,
            )
        )

    async def finish_run(
        self, run_id: int, *, success: bool, error: str | None = None
    ) -> None:
        await q.finish_scheduled_run(
            self.pool,
            id_=run_id,
            status="succeeded" if success else "failed",
            error=error,
        )

    async def latest_runs_for_project(self, project_id: int) -> list[dict]:
        """Most recent run per (schedule, effect)."""
        rows = await q.latest_scheduled_runs(self.pool, project_id=project_id)
        return [dataclasses.asdict(row) for row in rows]

    async def schedules_for_project(self, project_id: int) -> list[dict]:
        rows = await q.project_schedules(self.pool, project_id=project_id)
        return [dataclasses.asdict(row) for row in rows]

    async def mark_run(self, due: DueEffect, now: datetime | None = None) -> None:
        await q.mark_schedule_run(
            self.pool,
            project_id=due.project_id,
            schedule_name=due.schedule_name,
            effect=due.effect,
            last_run=now or datetime.now(tz=UTC),
        )
