"""
Self-annealing error log writer.

When the self-annealing pipeline records a permanent failure it calls
`append_error_log()`, which appends a formatted entry to a dedicated log
file (default: `logs/error_log.md`).

Why a separate file?  Production failure data shouldn't live in CLAUDE.md
— that file is hand-curated project documentation checked into git.  A
dedicated log file is easier to rotate, exclude from VCS, and ship to a
centralised logging system later.

Thread-safe via a simple lock file (avoids a `filelock` dependency).
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LOG_PATH = Path(os.getenv("ERROR_LOG_PATH", "logs/error_log.md"))
_LOCK_FILE = _LOG_PATH.parent / ".error_log.lock"
_LOCK_TIMEOUT = 10.0          # seconds
_LOG_HEADER = "# Self-Annealing Error Log\n\n"


def append_error_log(failure) -> None:
    """
    Append a formatted error entry to the error log file.
    `failure` is a FailureRecord from self_annealing.py.

    Creates the file (and parent directory) on first use.
    """
    entry = _format_entry(failure)

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[error_log] Could not create log directory %s: %s",
                       _LOG_PATH.parent, exc)
        return

    _with_lock(lambda: _append_entry(entry))
    logger.info("[error_log] Appended error log entry to %s (stage=%s)",
                _LOG_PATH.name, failure.stage)


# ──────────────────────────────────────────────────────────────────────────────
# Entry formatting
# ──────────────────────────────────────────────────────────────────────────────


def _format_entry(failure) -> str:
    date_str = failure.timestamp.strftime("%Y-%m-%d %H:%M:%S")
    short_title = f"{failure.stage} — {failure.error_type}"

    tb_lines = failure.traceback_text.strip().splitlines()
    tb_snippet = "\n".join(tb_lines[-8:]) if tb_lines else "(no traceback)"

    actions = "; ".join(failure.recovery_actions) if failure.recovery_actions else "None"
    resolved_str = (
        "Partially recovered via fallback" if failure.resolved
        else "Unresolved — candidate skipped"
    )

    return f"""
### [{date_str}] — {short_title}

**Error Type:** `{failure.error_type}`

**Full Error Message:**
```
{tb_snippet}
```

**What I Was Doing:**
Processing candidate `{failure.candidate_id[:16]}…` for role '{failure.role}' at stage `{failure.stage}`.

**Root Cause:**
{failure.error_message[:400]}

**Fix Applied:**
Strategy `{failure.strategy_applied}` — actions taken: {actions}. {resolved_str}.

**Lesson Learned:**
Stage `{failure.stage}` raised `{failure.error_type}` during production; self-annealing applied `{failure.strategy_applied}` strategy.

"""


# ──────────────────────────────────────────────────────────────────────────────
# File append
# ──────────────────────────────────────────────────────────────────────────────


def _append_entry(entry: str) -> None:
    if not _LOG_PATH.exists():
        _LOG_PATH.write_text(_LOG_HEADER, encoding="utf-8")
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)


# ──────────────────────────────────────────────────────────────────────────────
# Lock (prevents concurrent writes from parallel poll runs)
# ──────────────────────────────────────────────────────────────────────────────


def _with_lock(fn) -> None:
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while time.monotonic() < deadline:
        try:
            fd = os.open(str(_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            time.sleep(0.1)
    else:
        logger.warning("[error_log] Could not acquire lock after %.1f s — writing anyway",
                       _LOCK_TIMEOUT)

    try:
        fn()
    finally:
        try:
            _LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass
