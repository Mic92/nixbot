"""Matching onEvent effects against a delivery payload.

Pure functions shared by the daemon and `nbo effects`, so `when`
semantics can be checked locally without a running nixbot.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import Any

from .errors import EffectError

KINDS = ("pull_request", "pull_request_closed", "comment", "build_finished")
WHEN_KEYS = frozenset(
    {"permission", "labels", "branches", "commands", "modified", "status", "transition"}
)
_LEVELS = {None: 0, "none": 0, "read": 1, "write": 2, "admin": 3}
_TRANSITIONS = {"broke", "fixed"}

Payload = dict[str, Any]


def validate_when(name: str, when: dict[str, Any]) -> None:
    unknown = set(when) - WHEN_KEYS
    if unknown:
        msg = f"effect '{name}': unknown `when` keys: {', '.join(sorted(unknown))}"
        raise EffectError(msg)
    if (p := when.get("permission")) is not None and p not in (
        "read",
        "write",
        "admin",
    ):
        msg = f"effect '{name}': when.permission must be read|write|admin, got {p!r}"
        raise EffectError(msg)
    if (t := when.get("transition")) is not None and t not in _TRANSITIONS:
        msg = f"effect '{name}': when.transition must be broke|fixed, got {t!r}"
        raise EffectError(msg)


def _permission(when: dict[str, Any], payload: Payload) -> str | None:
    want = when.get("permission")
    if want is None:
        return None
    actor = payload.get("actor") or {}
    author = (payload.get("pullRequest") or {}).get("author") or {}
    if payload.get("command") is not None:
        # A comment is vouched for by the commenter alone, otherwise
        # anyone could /apply on a maintainer's PR.
        author = {}
    have = max(
        _LEVELS.get(actor.get("permission"), 0),
        _LEVELS.get(author.get("permission"), 0),
    )
    if have >= _LEVELS[want]:
        return None
    who = ", ".join(
        f"{role} {s['name']}: {s.get('permission') or 'none'}"
        for role, s in (("actor", actor), ("author", author))
        if s.get("name")
    )
    return f"needs {want} access ({who or 'no actor'})"


def _labels(when: dict[str, Any], payload: Payload) -> str | None:
    want = when.get("labels")
    if not want:
        return None
    have = set((payload.get("pullRequest") or {}).get("labels") or [])
    missing = [label for label in want if label not in have]
    return f"missing labels: {', '.join(missing)}" if missing else None


def _branches(when: dict[str, Any], payload: Payload) -> str | None:
    globs = when.get("branches")
    if not globs:
        return None
    pr = payload.get("pullRequest")
    branch = pr.get("baseRef") if pr else (payload.get("build") or {}).get("branch")
    if branch and any(fnmatchcase(branch, g) for g in globs):
        return None
    return f"branch {branch!r} matches none of {', '.join(globs)}"


def _commands(when: dict[str, Any], payload: Payload) -> str | None:
    want = when.get("commands")
    if not want:
        return None
    cmd = payload.get("command")
    return None if cmd in want else f"command /{cmd} not in {', '.join(want)}"


def _modified(when: dict[str, Any], payload: Payload) -> str | None:
    globs = when.get("modified")
    if not globs:
        return None
    files = payload.get("modifiedFiles")
    if files is None:
        return "modified files are only known for pull request events"
    if any(fnmatchcase(f, g) for g in globs for f in files):
        return None
    return f"no modified file matches {', '.join(globs)}"


def _status(when: dict[str, Any], payload: Payload) -> str | None:
    want = when.get("status")
    if not want:
        return None
    build = payload.get("build")
    if not build:
        return "no build for this commit"
    status = build.get("status")
    return None if status in want else f"build is {status}, needs {'|'.join(want)}"


def _transition(when: dict[str, Any], payload: Payload) -> str | None:
    want = when.get("transition")
    if want is None:
        return None
    build = payload.get("build") or {}
    now, prev = build.get("status"), build.get("previousStatus")
    ok = (
        now == "failed" and prev in (None, "succeeded")
        if want == "broke"
        else now == "succeeded" and prev == "failed"
    )
    return None if ok else f"not a {want} transition ({prev or 'none'} -> {now})"


_CHECKS = (
    _commands,
    _permission,
    _labels,
    _branches,
    _modified,
    _status,
    _transition,
)


def skip_reason(when: dict[str, Any], payload: Payload) -> str | None:
    """Why `when` does not match `payload`, or None if it matches."""
    for check in _CHECKS:
        if (reason := check(when, payload)) is not None:
            return reason
    return None


def event_payload(  # noqa: PLR0913
    *,
    pr: int | None = None,
    branch: str | None = None,
    actor: str | None = None,
    permission: str | None = None,
    author: str | None = None,
    author_permission: str | None = None,
    labels: list[str] | None = None,
    modified: list[str] | None = None,
    command: str | None = None,
    args: str = "",
    status: str = "succeeded",
    previous_status: str | None = None,
    head_rev: str | None = None,
) -> Payload:
    """A payload like nixbot delivers, from the few fields `when` looks
    at. For trying effects out locally; the daemon builds its payloads
    from the forge."""
    payload: Payload = {"build": {"status": status, "branch": branch or "main"}}
    if previous_status is not None:
        payload["build"]["previousStatus"] = previous_status
    if actor is not None:
        payload["actor"] = {"name": actor, "permission": permission}
    if pr is not None:
        payload["pullRequest"] = {
            "number": pr,
            "baseRef": branch or "main",
            "headRev": head_rev or "0" * 40,
            "labels": labels or [],
            "author": {
                "name": author or actor,
                "permission": author_permission
                if author_permission is not None
                else permission,
            },
        }
        # A pull request always has a file list, even an empty one.
        payload["modifiedFiles"] = modified or []
    if command is not None:
        payload["command"] = command
        payload["args"] = args
    return payload


def expand_lock(lock: str | None, payload: Payload) -> str | None:
    if lock is None or "{pr}" not in lock:
        return lock
    pr = payload.get("pullRequest")
    if not pr:
        msg = f"lock {lock!r} uses {{pr}} but the event has no pull request"
        raise EffectError(msg)
    return lock.replace("{pr}", str(pr["number"]))


def payload_env(kind: str, payload: Payload) -> dict[str, str]:
    """Flat NIXBOT_* variables for the common fields. Scripts read the
    rest from $NIXBOT_EVENT_JSON."""
    # .get throughout: hand-written payloads for local runs may be partial.
    actor = payload.get("actor") or {}
    pr = payload.get("pullRequest") or {}
    build = payload.get("build") or {}
    env = {
        "NIXBOT_EVENT_KIND": kind,
        "NIXBOT_EVENT_JSON": "/run/event.json",
        "NIXBOT_ACTOR": actor.get("name"),
        "NIXBOT_PR_NUMBER": pr.get("number"),
        "NIXBOT_PR_HEAD": pr.get("headRev"),
        "NIXBOT_COMMAND": payload.get("command"),
        "NIXBOT_COMMAND_ARGS": payload.get("args"),
        "NIXBOT_BUILD_STATUS": build.get("status"),
        "NIXBOT_BUILD_URL": build.get("url"),
    }
    return {k: str(v) for k, v in env.items() if v is not None}
