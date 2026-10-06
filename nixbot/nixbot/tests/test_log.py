"""Access-log noise filter."""

from __future__ import annotations

import logging

import pytest

from nixbot.log import AccessLogFilter


def _access(path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", path, "1.1", status),
        None,
    )


@pytest.mark.parametrize(
    ("path", "status", "kept"),
    [
        ("/repos/github/a/b/builds/1/logs/x/raw", 200, False),
        ("/static/style.css?v=1", 200, False),
        ("/events?build=1", 200, False),
        ("/repos/github/a/b/builds/1/logs/x/raw", 404, True),
        ("/repos/github/a/b", 200, True),
        ("/webhooks/gitea", 202, True),
    ],
)
def test_access_log_filter(
    path: str, status: int, kept: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(logging.getLogger(), "isEnabledFor", lambda _lvl: False)
    assert AccessLogFilter().filter(_access(path, status)) is kept
