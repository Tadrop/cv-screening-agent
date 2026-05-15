"""
Recovery strategy definitions — one per failure category.

Each strategy declares:
  - which exception types it handles
  - whether the step is retryable
  - how many retries are allowed
  - how long to wait between retries (seconds)
  - whether a permanent failure is fatal to the whole pipeline run
    (fatal=True → skip this candidate; fatal=False → skip just this step)
  - a human-readable description used in Slack alerts and the Error Log
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Type


@dataclass(frozen=True)
class RecoveryStrategy:
    name: str
    exception_types: tuple[Type[BaseException], ...]
    retryable: bool
    max_retries: int
    retry_delay_seconds: float
    step_fatal: bool          # True → skip candidate; False → skip step, continue
    description: str


# ──────────────────────────────────────────────────────────────────────────────
# Strategy registry (evaluated top-to-bottom — first match wins)
# ──────────────────────────────────────────────────────────────────────────────

STRATEGIES: list[RecoveryStrategy] = [

    RecoveryStrategy(
        name="api_rate_limit",
        exception_types=(Exception,),       # matched by name check in classifier
        retryable=True,
        max_retries=4,
        retry_delay_seconds=30.0,
        step_fatal=False,
        description=(
            "Claude / OpenAI / Pinecone rate limit hit. "
            "Back off and retry up to 4 times before giving up."
        ),
    ),

    RecoveryStrategy(
        name="claude_validation_failure",
        exception_types=(Exception,),       # matched by name check
        retryable=True,
        max_retries=1,
        retry_delay_seconds=3.0,
        step_fatal=True,
        description=(
            "Claude returned a response that failed Pydantic validation. "
            "Retry once with a repair hint naming the missing field. "
            "If it fails again, skip this candidate."
        ),
    ),

    RecoveryStrategy(
        name="pdf_parse_failure",
        exception_types=(Exception,),
        retryable=False,
        max_retries=0,
        retry_delay_seconds=0.0,
        step_fatal=False,
        description=(
            "Both pypdf and pdfplumber failed to extract text. "
            "Mark as image-based, assign score=0, flag for manual review."
        ),
    ),

    RecoveryStrategy(
        name="gmail_quota",
        exception_types=(Exception,),
        retryable=True,
        max_retries=2,
        retry_delay_seconds=60.0,
        step_fatal=False,
        description=(
            "Gmail API quota exceeded. "
            "Wait 60 s and retry twice. "
            "If still failing, skip draft creation (candidate is still scored)."
        ),
    ),

    RecoveryStrategy(
        name="sheets_quota",
        exception_types=(Exception,),
        retryable=True,
        max_retries=2,
        retry_delay_seconds=60.0,
        step_fatal=False,
        description=(
            "Google Sheets API quota exceeded. "
            "Retry twice. If still failing, skip Sheet update for this candidate."
        ),
    ),

    RecoveryStrategy(
        name="pinecone_unavailable",
        exception_types=(Exception,),
        retryable=True,
        max_retries=2,
        retry_delay_seconds=10.0,
        step_fatal=False,
        description=(
            "Pinecone unreachable. "
            "Fall back to passing the full raw JD text to Claude."
        ),
    ),

    RecoveryStrategy(
        name="attachment_missing",
        exception_types=(Exception,),
        retryable=False,
        max_retries=0,
        retry_delay_seconds=0.0,
        step_fatal=True,
        description=(
            "No supported CV attachment found in the email. "
            "Skip this candidate — nothing to score."
        ),
    ),

    RecoveryStrategy(
        name="unknown",
        exception_types=(Exception,),
        retryable=False,
        max_retries=0,
        retry_delay_seconds=0.0,
        step_fatal=True,
        description=(
            "Unexpected error not covered by a specific strategy. "
            "Log full traceback, alert team via Slack, skip candidate."
        ),
    ),
]

# Fast lookup by name
STRATEGY_MAP: dict[str, RecoveryStrategy] = {s.name: s for s in STRATEGIES}


def classify_error(exc: BaseException, context: str = "") -> RecoveryStrategy:
    """
    Map an exception to the closest recovery strategy.
    Uses exception class name and message keywords for classification.
    """
    exc_name = type(exc).__name__
    exc_msg = str(exc).lower()
    context_lower = context.lower()

    # Rate limits — all major APIs use similar patterns
    if any(kw in exc_msg for kw in ("rate limit", "ratelimit", "quota", "429", "too many requests")):
        if "sheet" in context_lower or "gspread" in exc_name.lower():
            return STRATEGY_MAP["sheets_quota"]
        if "gmail" in context_lower or "httpError" in exc_name:
            return STRATEGY_MAP["gmail_quota"]
        return STRATEGY_MAP["api_rate_limit"]

    # Pydantic validation
    if "validationerror" in exc_name.lower() or "pydantic" in exc_name.lower():
        return STRATEGY_MAP["claude_validation_failure"]

    # PDF parse
    if "pdf" in context_lower and any(kw in exc_msg for kw in ("no text", "extract", "parse")):
        return STRATEGY_MAP["pdf_parse_failure"]

    # Pinecone
    if any(kw in exc_msg for kw in ("pinecone", "vector", "namespace", "index")):
        return STRATEGY_MAP["pinecone_unavailable"]

    # Gmail
    if "gmail" in context_lower or ("httperror" in exc_name.lower() and "gmail" in exc_msg):
        return STRATEGY_MAP["gmail_quota"]

    # Sheets
    if "sheet" in context_lower or "gspread" in exc_name.lower():
        return STRATEGY_MAP["sheets_quota"]

    # No attachment
    if "attachment" in exc_msg or "no cv" in exc_msg:
        return STRATEGY_MAP["attachment_missing"]

    return STRATEGY_MAP["unknown"]
