"""Helpers around sqlc-generated query functions."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable


def row_dict(row: Any) -> dict[str, Any]:
    """Generated row dataclass -> dict keyed by SQL column names.

    sqlc-gen-better-python suffixes builtin-shadowing fields (``id`` ->
    ``id_``); templates and the JSON API keep the column name."""
    d = dataclasses.asdict(row)
    if "id_" in d:
        d["id"] = d.pop("id_")
    return d


def row_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [row_dict(r) for r in rows]


def expect[T](value: T | None) -> T:
    """Unwrap an Optional result from a generated :one query that is
    structurally guaranteed to return a row (e.g. INSERT ... RETURNING
    or an aggregate without GROUP BY)."""
    if value is None:
        msg = "query unexpectedly returned no row"
        raise RuntimeError(msg)
    return value
