"""
Tests for Gmail draft, Google Sheets, and Slack integrations.
All external API clients are mocked — no network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.outputs.slack import notify_standout_candidate


# ──────────────────────────────────────────────────────────────────────────────
# Gmail draft
# ──────────────────────────────────────────────────────────────────────────────


def test_gmail_draft_uses_only_drafts_create(monkeypatch):
    """Verify we call drafts().create() and NEVER messages().send()."""
    mock_service = MagicMock()
    mock_service.users().drafts().create().execute.return_value = {"id": "draft_abc123"}

    with patch("src.outputs.gmail._get_gmail_service", return_value=mock_service):
        from src.outputs.gmail import create_acknowledgement_draft

        draft_id = create_acknowledgement_draft(
            to_email="applicant@example.com",
            subject="Re: Your application",
            body="Dear Applicant, ... AI-assisted tools are used ... thank you.",
        )

    assert draft_id == "draft_abc123"
    mock_service.users().messages().send.assert_not_called()


def test_gmail_draft_creation_logs_hashed_email(monkeypatch, caplog):
    mock_service = MagicMock()
    mock_service.users().drafts().create().execute.return_value = {"id": "d1"}

    import logging
    with patch("src.outputs.gmail._get_gmail_service", return_value=mock_service):
        from src.outputs.gmail import create_acknowledgement_draft
        with caplog.at_level(logging.INFO, logger="src.outputs.gmail"):
            create_acknowledgement_draft("secret@real.com", "Subject", "Body AI disclosure")

    # Raw email must NOT appear in any log message
    assert "secret@real.com" not in caplog.text


# ──────────────────────────────────────────────────────────────────────────────
# Slack standout alert
# ──────────────────────────────────────────────────────────────────────────────


def test_slack_alert_fires_above_threshold(monkeypatch):
    monkeypatch.setenv("STANDOUT_SCORE_THRESHOLD", "85")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    mock_client = MagicMock()
    mock_client.chat_postMessage.return_value = {"ts": "12345.0", "ok": True}

    with patch("src.outputs.slack.WebClient", return_value=mock_client):
        result = notify_standout_candidate(
            hiring_manager_user_id="U123",
            role_name="Backend Engineer",
            score=90,
            reasoning=["Strong Python", "Has AWS cert", "Good comms"],
            brief_markdown="# Brief\nScore 90/100",
            candidate_id="abc" * 10,
        )

    assert result is True
    mock_client.chat_postMessage.assert_called_once()


def test_slack_alert_suppressed_below_threshold(monkeypatch):
    monkeypatch.setenv("STANDOUT_SCORE_THRESHOLD", "85")

    mock_client = MagicMock()
    with patch("src.outputs.slack.WebClient", return_value=mock_client):
        result = notify_standout_candidate(
            hiring_manager_user_id="U123",
            role_name="Backend Engineer",
            score=70,
            reasoning=["Moderate match"],
            brief_markdown="# Brief",
            candidate_id="xyz" * 10,
        )

    assert result is False
    mock_client.chat_postMessage.assert_not_called()


def test_slack_alert_returns_false_on_api_error(monkeypatch):
    monkeypatch.setenv("STANDOUT_SCORE_THRESHOLD", "85")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from slack_sdk.errors import SlackApiError

    mock_client = MagicMock()
    mock_client.chat_postMessage.side_effect = SlackApiError(
        "channel_not_found", {"error": "channel_not_found"}
    )

    with patch("src.outputs.slack.WebClient", return_value=mock_client):
        result = notify_standout_candidate(
            hiring_manager_user_id="UBAD",
            role_name="Role",
            score=95,
            reasoning=["R1", "R2", "R3"],
            brief_markdown="Brief",
            candidate_id="id1" * 10,
        )

    assert result is False  # non-fatal


# ──────────────────────────────────────────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────────────────────────────────────────


def test_sheet_upsert_calls_append_for_new_candidate():
    mock_ws = MagicMock()
    mock_ws.col_values.return_value = ["Candidate ID"]  # header only — no existing rows
    mock_ws.get_all_values.return_value = [
        ["Rank", "Score", "Experience", "Skills", "Education", "Location",
         "Notice Period", "Reasoning", "Brief Path", "Applied At", "Candidate ID"]
    ]

    mock_spreadsheet = MagicMock()
    mock_spreadsheet.worksheet.return_value = mock_ws

    mock_client = MagicMock()
    mock_client.open_by_key.return_value = mock_spreadsheet

    with patch("src.outputs.sheets._get_client", return_value=mock_client):
        from src.outputs.sheets import upsert_candidate_row

        upsert_candidate_row(
            sheet_id="SHEET123",
            role_name="Backend Engineer",
            candidate_id="cand_abc",
            score=80,
            experience="5 years Python",
            skills=["Python", "AWS"],
            education="BSc CS",
            location="London",
            notice_period="1 month",
            reasoning=["Good match", "Has cert", "Minor gap"],
            brief_path="/data/briefs/cand_abc.md",
            applied_at="2026-05-15T10:00:00",
        )

    mock_ws.append_row.assert_called_once()
