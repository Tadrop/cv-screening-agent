"""
Per-day token budget guard for Claude API calls.

Records token usage in a JSON file keyed by UTC date. Before each scoring call,
the scorer asks `check_within_budget()` whether today's usage is under the
configured ceiling; after each call, it records the actual usage via
`record_usage()`.

Configured via env vars:

    CLAUDE_DAILY_TOKEN_LIMIT   maximum input+output tokens per UTC day
                               (default: unset → unlimited)
    USAGE_LOG_PATH             where to store the JSON ledger
                               (default: data/usage_log.json)

Designed to be cheap: one file read per call, no external dependencies, no
locking (a brief over-count under contention is acceptable for a budget guard).
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_USAGE_LOG_PATH = Path(os.getenv("USAGE_LOG_PATH", "data/usage_log.json"))


class BudgetExceeded(RuntimeError):
    """Raised when today's token usage has hit the configured limit."""


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    if not _USAGE_LOG_PATH.exists():
        return {}
    try:
        return json.loads(_USAGE_LOG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[budget] Could not read usage log %s: %s — starting fresh",
                       _USAGE_LOG_PATH, exc)
        return {}


def _save(data: dict) -> None:
    try:
        _USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _USAGE_LOG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("[budget] Could not write usage log: %s", exc)


def today_usage() -> dict:
    """Return {'input': N, 'output': N, 'calls': N} for today (UTC)."""
    return _load().get(_today_key(), {"input": 0, "output": 0, "calls": 0})


def check_within_budget() -> None:
    """
    Raise BudgetExceeded if today's total tokens have already hit the limit.
    Does nothing if CLAUDE_DAILY_TOKEN_LIMIT is unset.
    """
    limit_raw = os.getenv("CLAUDE_DAILY_TOKEN_LIMIT")
    if not limit_raw:
        return
    limit = int(limit_raw)

    usage = today_usage()
    total = usage["input"] + usage["output"]
    if total >= limit:
        raise BudgetExceeded(
            f"Daily Claude token budget exhausted: {total:,} / {limit:,} tokens used today "
            f"({usage['calls']} calls). Increase CLAUDE_DAILY_TOKEN_LIMIT or wait for UTC rollover."
        )


def record_usage(input_tokens: int, output_tokens: int) -> None:
    """Append today's usage to the ledger."""
    data = _load()
    key = _today_key()
    entry = data.get(key, {"input": 0, "output": 0, "calls": 0})
    entry["input"] += int(input_tokens or 0)
    entry["output"] += int(output_tokens or 0)
    entry["calls"] += 1
    data[key] = entry
    _save(data)
