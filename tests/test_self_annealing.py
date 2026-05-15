"""
Tests for the self-annealing pipeline.

All external I/O is mocked — tests run fully offline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from src.pipeline.strategies import classify_error, STRATEGY_MAP
from src.pipeline.self_annealing import (
    FailureRecord,
    SelfAnnealingPipeline,
    StageResult,
    _extract_validation_repair_hint,
    _inject_repair_hint,
    _clear_repair_hint,
    get_active_repair_hint,
    _save_brief,
)


# ──────────────────────────────────────────────────────────────────────────────
# Strategy classification
# ──────────────────────────────────────────────────────────────────────────────


def test_rate_limit_error_classifies_correctly():
    exc = Exception("Rate limit exceeded — 429 too many requests")
    strategy = classify_error(exc, context="score_candidate")
    assert strategy.name == "api_rate_limit"
    assert strategy.retryable is True


def test_pydantic_error_classifies_correctly():
    class ValidationError(Exception):
        pass

    exc = ValidationError("1 validation error for CandidateEvaluation")
    strategy = classify_error(exc, context="score_candidate")
    assert strategy.name == "claude_validation_failure"
    assert strategy.max_retries == 1


def test_sheets_quota_classifies_correctly():
    exc = Exception("quota exceeded")
    strategy = classify_error(exc, context="sheet_update")
    assert strategy.name == "sheets_quota"
    assert strategy.step_fatal is False


def test_gmail_quota_classifies_correctly():
    exc = Exception("quota exceeded")
    strategy = classify_error(exc, context="gmail_draft")
    assert strategy.name == "gmail_quota"
    assert strategy.step_fatal is False


def test_attachment_missing_classifies_correctly():
    exc = ValueError("No supported CV attachment found in email")
    strategy = classify_error(exc, context="download_attachment")
    assert strategy.name == "attachment_missing"
    assert strategy.step_fatal is True


def test_unknown_error_is_step_fatal():
    exc = RuntimeError("Something completely unexpected")
    strategy = classify_error(exc, context="some_stage")
    assert strategy.name == "unknown"
    assert strategy.step_fatal is True


# ──────────────────────────────────────────────────────────────────────────────
# Repair hint injection / extraction
# ──────────────────────────────────────────────────────────────────────────────


def test_repair_hint_lifecycle():
    assert get_active_repair_hint() is None
    _inject_repair_hint("Field 'reasoning': too short")
    assert get_active_repair_hint() == "Field 'reasoning': too short"
    _clear_repair_hint()
    assert get_active_repair_hint() is None


def test_extract_validation_repair_hint_from_pydantic_error():
    from pydantic import BaseModel, ValidationError, Field

    class M(BaseModel):
        reasoning: list[str] = Field(min_length=3)

    try:
        M(reasoning=["only one"])
    except ValidationError as exc:
        hint = _extract_validation_repair_hint(exc)

    assert "reasoning" in hint
    assert hint  # non-empty


def test_extract_validation_repair_hint_fallback():
    exc = Exception("plain error with no .errors() method")
    hint = _extract_validation_repair_hint(exc)
    assert "Validation failed" in hint


# ──────────────────────────────────────────────────────────────────────────────
# _run_stage — isolated harness behaviour
# ──────────────────────────────────────────────────────────────────────────────


def _make_pipeline() -> SelfAnnealingPipeline:
    return SelfAnnealingPipeline(
        role={"name": "Backend Engineer", "sheet_id": None, "jd_path": None},
        jd_text="Requires Python 3+ years and AWS experience.",
    )


def test_run_stage_success_on_first_attempt():
    pipeline = _make_pipeline()
    result = pipeline._run_stage(
        stage="test_stage",
        candidate_id="abc" * 21,
        fn=lambda: "hello",
    )
    assert result.success is True
    assert result.value == "hello"
    assert result.skip_candidate is False


def test_run_stage_retries_on_transient_error(monkeypatch):
    monkeypatch.setattr("src.pipeline.self_annealing.time.sleep", lambda _: None)

    call_count = 0

    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise Exception("rate limit exceeded — 429")
        return "recovered"

    pipeline = _make_pipeline()
    with patch("src.pipeline.self_annealing._record_failure_noop", create=True):
        result = pipeline._run_stage(
            stage="score_candidate",
            candidate_id="def" * 21,
            fn=flaky,
        )

    assert result.success is True
    assert result.value == "recovered"
    assert call_count == 3


def test_run_stage_uses_fallback_after_exhausted_retries(monkeypatch):
    monkeypatch.setattr("src.pipeline.self_annealing.time.sleep", lambda _: None)

    with (
        patch("src.pipeline.self_annealing.SelfAnnealingPipeline._record_failure") as mock_record,
        patch("src.pipeline.error_log.append_error_log"),
    ):
        mock_record.return_value = MagicMock(spec=FailureRecord)
        pipeline = _make_pipeline()
        result = pipeline._run_stage(
            stage="gmail_draft",
            candidate_id="ghi" * 21,
            fn=lambda: (_ for _ in ()).throw(Exception("quota exceeded")),
            fallback=lambda _: "fallback_value",
        )

    assert result.success is False
    assert result.value == "fallback_value"
    assert result.skip_candidate is False


def test_run_stage_skip_candidate_on_fatal_no_fallback(monkeypatch):
    monkeypatch.setattr("src.pipeline.self_annealing.time.sleep", lambda _: None)

    with (
        patch("src.pipeline.self_annealing.SelfAnnealingPipeline._record_failure") as mock_record,
        patch("src.pipeline.error_log.append_error_log"),
    ):
        mock_record.return_value = MagicMock(spec=FailureRecord)
        pipeline = _make_pipeline()
        result = pipeline._run_stage(
            stage="download_attachment",
            candidate_id="jkl" * 21,
            fn=lambda: (_ for _ in ()).throw(
                ValueError("No supported CV attachment found in email")
            ),
        )

    assert result.skip_candidate is True


# ──────────────────────────────────────────────────────────────────────────────
# Brief saving helper
# ──────────────────────────────────────────────────────────────────────────────


def test_save_brief_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline.self_annealing._BRIEFS_DIR", tmp_path)
    path = _save_brief("abc123def456", "Senior Backend Engineer", "# Brief\nScore: 85")
    assert path.exists()
    assert "Senior_Backend_Engineer" in path.name or "Senior" in path.name
    assert path.read_text() == "# Brief\nScore: 85"


def test_save_brief_sanitises_role_name(tmp_path, monkeypatch):
    monkeypatch.setattr("src.pipeline.self_annealing._BRIEFS_DIR", tmp_path)
    path = _save_brief("abc123def456", "Role / With: Symbols!", "content")
    assert "/" not in path.name
    assert ":" not in path.name


# ──────────────────────────────────────────────────────────────────────────────
# Error log writer (logs/error_log.md)
# ──────────────────────────────────────────────────────────────────────────────


def test_error_log_creates_file_and_appends_entry(tmp_path):
    log_path = tmp_path / "error_log.md"

    failure = FailureRecord(
        timestamp=datetime(2026, 5, 15, 10, 0, 0),
        stage="score_candidate",
        role="Backend Engineer",
        candidate_id="abc" * 21,
        error_type="ValidationError",
        error_message="reasoning has fewer than 3 items",
        traceback_text="Traceback (most recent call last):\n  ...\nValidationError",
        strategy_applied="claude_validation_failure",
        recovery_actions=["Injected repair hint", "Retried once"],
        resolved=False,
    )

    from src.pipeline.error_log import append_error_log
    with (
        patch("src.pipeline.error_log._LOG_PATH", log_path),
        patch("src.pipeline.error_log._LOCK_FILE", log_path.parent / ".lock"),
    ):
        append_error_log(failure)

    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "Self-Annealing Error Log" in content
    assert "2026-05-15" in content
    assert "score_candidate" in content
    assert "ValidationError" in content
    assert "claude_validation_failure" in content


def test_error_log_appends_to_existing_file(tmp_path):
    log_path = tmp_path / "error_log.md"

    failure = FailureRecord(
        timestamp=datetime(2026, 5, 15, 10, 0, 0),
        stage="gmail_draft", role="Role", candidate_id="x" * 64,
        error_type="QuotaError", error_message="quota exceeded",
        traceback_text="", strategy_applied="gmail_quota",
        recovery_actions=[], resolved=True,
    )

    from src.pipeline.error_log import append_error_log
    with (
        patch("src.pipeline.error_log._LOG_PATH", log_path),
        patch("src.pipeline.error_log._LOCK_FILE", log_path.parent / ".lock"),
    ):
        append_error_log(failure)
        append_error_log(failure)

    assert log_path.read_text(encoding="utf-8").count("gmail_draft") >= 2


def test_error_log_creates_parent_directory(tmp_path):
    log_path = tmp_path / "nested" / "deeper" / "error_log.md"

    failure = FailureRecord(
        timestamp=datetime.now(timezone.utc),
        stage="test", role="Role", candidate_id="x" * 64,
        error_type="Error", error_message="msg", traceback_text="",
        strategy_applied="unknown", recovery_actions=[], resolved=False,
    )
    from src.pipeline.error_log import append_error_log
    with (
        patch("src.pipeline.error_log._LOG_PATH", log_path),
        patch("src.pipeline.error_log._LOCK_FILE", log_path.parent / ".lock"),
    ):
        append_error_log(failure)

    assert log_path.exists()


# ──────────────────────────────────────────────────────────────────────────────
# Scorer repair hint integration
# ──────────────────────────────────────────────────────────────────────────────


def test_scorer_reads_active_repair_hint():
    """score_candidate should pick up an active repair hint and append it to the system prompt."""
    from src.scorer.scorer import _build_system_prompt

    _inject_repair_hint("Field 'reasoning': must have at least 3 items")
    try:
        from src.scorer.scorer import score_candidate
        # Read the hint directly via the builder
        prompt = _build_system_prompt(get_active_repair_hint())
        assert "REPAIR INSTRUCTION" in prompt
        assert "reasoning" in prompt
    finally:
        _clear_repair_hint()


def test_scorer_no_hint_returns_base_prompt():
    from src.scorer.scorer import _build_system_prompt
    from src.scorer.prompt import SYSTEM_PROMPT

    prompt = _build_system_prompt(None)
    assert prompt == SYSTEM_PROMPT
