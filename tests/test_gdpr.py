"""
Tests for the GDPR 90-day purge job.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from src.gdpr.purge import run_purge, _purge_candidate


# ──────────────────────────────────────────────────────────────────────────────
# Purge logic
# ──────────────────────────────────────────────────────────────────────────────


def test_purge_deletes_attachment_file(tmp_path):
    attachment = tmp_path / "cv_abc123.pdf"
    attachment.write_bytes(b"fake cv content")

    candidate = {
        "id": "abc" * 21 + "abc",    # 64 hex chars
        "role": "Backend Engineer",
        "score": 72,
        "attachment_path": str(attachment),
        "brief_path": None,
    }

    with patch("src.gdpr.purge.db_ops.mark_purged") as mock_mark:
        _purge_candidate(candidate)

    assert not attachment.exists()
    mock_mark.assert_called_once_with(candidate["id"])


def test_purge_deletes_brief_file(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("# Candidate Brief")

    candidate = {
        "id": "xyz" * 21 + "xyz",
        "role": "Designer",
        "score": 55,
        "attachment_path": None,
        "brief_path": str(brief),
    }

    with patch("src.gdpr.purge.db_ops.mark_purged"):
        _purge_candidate(candidate)

    assert not brief.exists()


def test_purge_handles_already_deleted_files(tmp_path):
    candidate = {
        "id": "gone" * 16,
        "role": "Role",
        "score": 0,
        "attachment_path": str(tmp_path / "nonexistent.pdf"),
        "brief_path": str(tmp_path / "nonexistent.md"),
    }

    with patch("src.gdpr.purge.db_ops.mark_purged") as mock_mark:
        _purge_candidate(candidate)   # must not raise

    mock_mark.assert_called_once()


def test_run_purge_summary(monkeypatch):
    stale_candidates = [
        {
            "id": f"cand{i}" * 12,
            "role": "Role",
            "score": 50,
            "attachment_path": None,
            "brief_path": None,
        }
        for i in range(3)
    ]

    monkeypatch.setenv("GDPR_RETENTION_DAYS", "90")

    with (
        patch("src.gdpr.purge.db_ops.get_unpurged_older_than", return_value=stale_candidates),
        patch("src.gdpr.purge.db_ops.mark_purged"),
    ):
        summary = run_purge()

    assert summary["checked"] == 3
    assert summary["purged"] == 3
    assert summary["errors"] == 0


def test_run_purge_counts_errors(monkeypatch):
    bad_candidate = {
        "id": "err" * 21 + "err",
        "role": "Role",
        "score": 40,
        "attachment_path": None,
        "brief_path": None,
    }

    monkeypatch.setenv("GDPR_RETENTION_DAYS", "90")

    with (
        patch("src.gdpr.purge.db_ops.get_unpurged_older_than", return_value=[bad_candidate]),
        patch("src.gdpr.purge.db_ops.mark_purged", side_effect=RuntimeError("DB error")),
    ):
        summary = run_purge()

    assert summary["errors"] == 1
    assert summary["purged"] == 0


def test_purge_respects_retention_days(monkeypatch):
    monkeypatch.setenv("GDPR_RETENTION_DAYS", "30")
    captured_cutoff: list[datetime] = []

    def capture_cutoff(cutoff):
        captured_cutoff.append(cutoff)
        return []

    with patch("src.gdpr.purge.db_ops.get_unpurged_older_than", side_effect=capture_cutoff):
        run_purge()

    assert len(captured_cutoff) == 1
    delta = datetime.now(timezone.utc) - captured_cutoff[0]
    assert 29 <= delta.days <= 31
