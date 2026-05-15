"""
Structured logging setup.

Call `setup_logging()` once at process startup. With STRUCTURED_LOGS=1 it
emits one JSON object per log record, including any `extra={...}` fields
(candidate_id, stage, role, etc.) — making it cheap to correlate failures
across stages in a shipped log aggregator.

With STRUCTURED_LOGS unset it uses a human-readable plain-text format.

No third-party dependency: a tiny JSON formatter is defined inline.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

# Standard LogRecord attributes — anything else is treated as user-supplied context.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Pick up any extra={} context fields the caller passed in.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | None = None) -> None:
    """
    Configure the root logger. Safe to call multiple times — replaces handlers.

    Env vars:
        STRUCTURED_LOGS=1   emit JSON lines (default: plain text)
        LOG_LEVEL=DEBUG     override level (default: INFO)
    """
    level_name = level or os.getenv("LOG_LEVEL", "INFO")
    structured = os.getenv("STRUCTURED_LOGS") == "1"

    handler = logging.StreamHandler(sys.stderr)
    if structured:
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s — %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        ))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level_name.upper())
