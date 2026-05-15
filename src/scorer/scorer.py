"""
Claude scorer — calls the Anthropic API with tool use to get a guaranteed-JSON
structured evaluation, then validates it through the Pydantic schema.

Retry logic:
  - Up to 3 attempts on transient API errors (rate limit, server error).
  - On Pydantic validation failure, the self-annealing pipeline injects a
    repair hint into the system prompt and retries once.
  - If validation still fails, raises — no silent fallback that could hide bias.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import anthropic
from pydantic import ValidationError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .budget import check_within_budget, record_usage
from .prompt import SYSTEM_PROMPT, build_user_message
from .schema import EVALUATION_TOOL_SCHEMA, CandidateEvaluation

logger = logging.getLogger(__name__)

_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_MAX_TOKENS = 4096


def score_candidate(
    role_name: str,
    anonymised_cv: str,
    jd_context: str,
    repair_hint: Optional[str] = None,
) -> CandidateEvaluation:
    """
    Score a single candidate CV against the given JD context.

    `repair_hint` is injected by the self-annealing pipeline on a retry after
    a Pydantic validation failure — it names the specific field(s) that were
    wrong so Claude can correct them.

    Returns a validated CandidateEvaluation.
    Raises ValidationError if the response is structurally invalid.
    Raises anthropic.APIError on unrecoverable API failures.
    """
    # Check for an active repair hint from the self-annealing pipeline
    if repair_hint is None:
        try:
            from ..pipeline.self_annealing import get_active_repair_hint  # noqa: PLC0415
            repair_hint = get_active_repair_hint()
        except ImportError:
            pass

    user_message = build_user_message(role_name, anonymised_cv, jd_context)
    system = _build_system_prompt(repair_hint)
    raw_input = _call_claude(user_message, system)
    return _parse_response(raw_input)


# ──────────────────────────────────────────────────────────────────────────────
# System prompt builder — appends repair hint when retrying
# ──────────────────────────────────────────────────────────────────────────────


def _build_system_prompt(repair_hint: Optional[str]) -> str:
    if not repair_hint:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + f"\n\n══════════════════════════════════════════════════════════════════\n"
        f" REPAIR INSTRUCTION (previous response was rejected)\n"
        f"══════════════════════════════════════════════════════════════════\n"
        f"Your previous response failed validation with this error:\n\n"
        f"  {repair_hint}\n\n"
        f"Correct ONLY the identified field(s) and resubmit via the tool.\n"
        f"All other fields must remain consistent with your previous evaluation.\n"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Claude API call — tool-use mode forces structured output
# ──────────────────────────────────────────────────────────────────────────────


@retry(
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.InternalServerError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
)
def _call_claude(user_message: str, system: str = SYSTEM_PROMPT) -> dict[str, Any]:
    # Refuse the call before spending money if today's budget is already exhausted.
    check_within_budget()

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    response = client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system,
        tools=[EVALUATION_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_candidate_evaluation"},
        messages=[{"role": "user", "content": user_message}],
    )

    # Record actual usage. Wrapped in try/except so a logging failure never
    # breaks a scoring call, and so MagicMock'd responses in tests are tolerated.
    try:
        usage = getattr(response, "usage", None)
        record_usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )
    except (TypeError, ValueError, OSError) as exc:
        logger.debug("Skipping usage recording: %s", exc)

    # With tool_choice forced to our tool, the first content block is always a tool_use block
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_candidate_evaluation":
            return block.input

    # Defensive — should never reach here with tool_choice set
    raise ValueError(
        f"Claude did not return submit_candidate_evaluation tool call. "
        f"Stop reason: {response.stop_reason}. Content: {response.content!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic validation
# ──────────────────────────────────────────────────────────────────────────────


def _parse_response(raw: dict[str, Any]) -> CandidateEvaluation:
    try:
        return CandidateEvaluation(**raw)
    except ValidationError as exc:
        # Log the raw tool input (no CV text — only structured fields) for debugging
        logger.error(
            "Pydantic validation failed for Claude response.\n"
            "Raw tool input (safe to log — no PII): %s\n"
            "Validation errors: %s",
            {k: v for k, v in raw.items() if k not in ("acknowledgement_email_draft", "one_page_brief")},
            exc.errors(),
        )
        raise
