"""Structured logging for the service.

Emits one JSON object per line on stderr so journald/log shippers can
parse fields without fragile regex. Extra fields passed via
`logger.info("msg", extra={"build_id": 42})` are included verbatim.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

# Attributes present on every LogRecord. Everything else is user-supplied
# via `extra=` and gets serialized into the JSON line.
_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        entry.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED
            }
        )
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


# Successful reads of these paths are polled or streamed by the UI and
# would otherwise be most of the journal.
_QUIET_PREFIXES = ("/static/", "/events")
_QUIET_INFIX = "/logs/"


class AccessLogFilter(logging.Filter):
    """Keeps uvicorn's access log for what matters: errors and anything
    but the high-volume log/static/event-stream reads, which only show
    up at debug level."""

    def filter(self, record: logging.LogRecord) -> bool:
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            return True
        # uvicorn: (client_addr, method, full_path, http_version, status)
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:  # noqa: PLR2004
            return True
        path = str(args[2]).split("?", 1)[0]
        status = args[4]
        if not isinstance(status, int) or status >= 400:  # noqa: PLR2004
            return True
        return not (path.startswith(_QUIET_PREFIXES) or _QUIET_INFIX in path)


def setup_logging(level: str = "info", *, json_format: bool = True) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx logs every outgoing request at info without saying why; the
    # forge clients log the interesting ones themselves.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(
            logging.DEBUG if root.isEnabledFor(logging.DEBUG) else logging.WARNING
        )
    logging.getLogger("uvicorn.access").addFilter(AccessLogFilter())
