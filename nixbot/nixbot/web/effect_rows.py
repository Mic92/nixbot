"""One display row per effect run, shared by the build page and the
project effects page. Evaluation failures become rows too, so a
problem always appears in the same place."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

FAILED = ("failed", "dependency_failed")
WAITING = ("pending", "running")

TRIGGERS = ("push", "check", "tag", "schedule", "event")

# Which rows each ?status= chip selects.
STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "failed": FAILED,
    "waiting": WAITING,
    "succeeded": ("succeeded",),
    "skipped": ("skipped",),
}


def trigger(
    kind: str, schedule_name: str | None, branch: str | None, name: str = ""
) -> tuple[str, str]:
    """(label, value) of what started a run."""
    if kind.startswith("tag:"):
        return "tag", kind.removeprefix("tag:")
    if kind == "push":
        return "push", branch or ""
    if kind == "schedule":
        # The effect's own name says it already.
        return "schedule", "" if schedule_name == name else schedule_name or ""
    if kind == "check":
        return "check", ""
    return "event", kind


def _restart_allowed(run: dict[str, Any], build: dict[str, Any] | None) -> str:
    """ "yes", "cancel" (no restart) or "" for a row's action buttons."""
    if build is None or run.get("build_id") is None:
        return ""
    if run["kind"] == "push":
        ok = build["status"] == "succeeded" and run["status"] != "skipped"
        return "yes" if ok else ""
    if run["kind"] == "check":
        return "cancel"
    return "yes" if run.get("payload") is not None else ""


def _row(run: dict[str, Any], build: dict[str, Any] | None) -> dict[str, Any]:
    label, value = trigger(
        run["kind"],
        run.get("schedule_name"),
        (build or {}).get("branch"),
        run["name"],
    )
    return {
        **run,
        "build_number": run.get("build_number") or (build or {}).get("number"),
        "restart_allowed": _restart_allowed(run, build),
        "trigger": label,
        "trigger_value": value,
        "why": run.get("skip_reason") or run.get("error"),
        "needs_attention": run["status"] in FAILED + WAITING,
    }


def from_runs(
    runs: list[dict[str, Any]], build: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return [_row(r, build) for r in runs]


def from_eval_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for x in errors:
        label, value = {
            "tag": ("tag", ""),
            "delivery": ("event", ""),
        }.get(x["source"], ("push", ""))
        rows.append(
            {
                "id": None,
                "kind": x["source"],
                "name": "evaluation",
                "status": "failed",
                "trigger": label,
                "trigger_value": value,
                "why": x["error"],
                "error": x["error"],
                "needs_attention": True,
                "log_size": 0,
                "started_at": None,
                "finished_at": None,
                "payload": None,
            }
        )
    return rows


def attention_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Failures and waiting runs on top. Only for a single build's few rows."""
    return sorted(rows, key=lambda r: not r["needs_attention"])


def _url(base: str, filters: dict[str, str]) -> str:
    return f"{base}?{urlencode(filters)}" if filters else base


def filter_links(
    base: str, filters: dict[str, str], by_status: dict[str, int]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """(status chips, removable filter pills) of the project effects page.
    Every link keeps the other filters."""
    others = {k: v for k, v in filters.items() if k != "status"}
    active = filters.get("status")
    chips = [
        {
            "label": f"{name} runs {sum(by_status.get(s, 0) for s in statuses)}",
            "url": _url(base, {**others, "status": name}),
            "on": active == name,
        }
        for name, statuses in STATUS_FILTERS.items()
    ]
    chips.append({"label": "all", "url": _url(base, others), "on": active is None})
    pills = [
        {
            "label": k,
            "value": v,
            "url": _url(base, {a: b for a, b in filters.items() if a != k}),
        }
        for k, v in others.items()
    ]
    return chips, pills
