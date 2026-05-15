"""
Tests for the Claude scorer — API calls are mocked so these run offline.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.scorer.schema import CandidateEvaluation, EVALUATION_TOOL_SCHEMA
from src.scorer.scorer import _parse_response, score_candidate


# ──────────────────────────────────────────────────────────────────────────────
# Schema validation
# ──────────────────────────────────────────────────────────────────────────────


_VALID_PAYLOAD = {
    "score": 82,
    "reasoning": [
        "5 years Python matches the 3+ year requirement.",
        "AWS certification present as required.",
        "No direct Kubernetes experience — minor gap.",
    ],
    "experience": "5 years backend engineering in fintech.",
    "skills": ["Python", "FastAPI", "AWS"],
    "education": "BSc Computer Science",
    "location": "London, UK",
    "notice_period": "1 month",
    "acknowledgement_email_draft": (
        "Dear Applicant, thank you for applying. "
        "Please note that AI-assisted tools are used in our initial screening process. "
        "All final decisions are made by our recruitment team."
    ),
    "one_page_brief": "# Candidate Brief\n**Score:** 82/100\n## Why...",
}


def test_valid_payload_parses():
    ev = CandidateEvaluation(**_VALID_PAYLOAD)
    assert ev.score == 82
    assert len(ev.reasoning) == 3


def test_score_out_of_range_rejected():
    bad = {**_VALID_PAYLOAD, "score": 150}
    with pytest.raises(ValidationError):
        CandidateEvaluation(**bad)


def test_reasoning_fewer_than_3_rejected():
    bad = {**_VALID_PAYLOAD, "reasoning": ["Only one reason."]}
    with pytest.raises(ValidationError):
        CandidateEvaluation(**bad)


def test_reasoning_capped_at_8():
    many = {**_VALID_PAYLOAD, "reasoning": [f"Reason {i}" for i in range(12)]}
    ev = CandidateEvaluation(**many)
    assert len(ev.reasoning) == 8


def test_missing_ai_disclosure_rejected():
    bad = {**_VALID_PAYLOAD, "acknowledgement_email_draft": "Thank you for applying."}
    with pytest.raises(ValidationError, match="AI disclosure"):
        CandidateEvaluation(**bad)


def test_tool_schema_has_required_fields():
    required = EVALUATION_TOOL_SCHEMA["input_schema"]["required"]
    for field in ["score", "reasoning", "experience", "skills", "one_page_brief"]:
        assert field in required


# ──────────────────────────────────────────────────────────────────────────────
# scorer.py — mocked Claude API
# ──────────────────────────────────────────────────────────────────────────────


def _make_mock_response(payload: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "submit_candidate_evaluation"
    block.input = payload

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    return response


def test_score_candidate_returns_evaluation():
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        patch("src.scorer.scorer.anthropic.Anthropic") as mock_anthropic,
    ):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(_VALID_PAYLOAD)

        result = score_candidate(
            role_name="Senior Backend Engineer",
            anonymised_cv="[NAME REDACTED]\nPython developer 5 years",
            jd_context="We need 3+ years Python and AWS certification.",
        )

    assert isinstance(result, CandidateEvaluation)
    assert result.score == 82


def test_score_candidate_raises_on_validation_failure():
    bad_payload = {**_VALID_PAYLOAD, "score": 999, "reasoning": []}

    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}),
        patch("src.scorer.scorer.anthropic.Anthropic") as mock_anthropic,
    ):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        mock_client.messages.create.return_value = _make_mock_response(bad_payload)

        with pytest.raises(ValidationError):
            score_candidate("Role", "cv text", "jd context")


def test_parse_response_missing_reasoning_raises():
    bad = {**_VALID_PAYLOAD}
    del bad["reasoning"]
    with pytest.raises((ValidationError, TypeError)):
        _parse_response(bad)
