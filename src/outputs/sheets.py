"""
Google Sheets integration — maintains a live candidate ranking sheet per role.

Sheet layout (one tab per role):
  Col A  Rank
  Col B  Score
  Col C  Experience
  Col D  Skills
  Col E  Education
  Col F  Location
  Col G  Notice Period
  Col H  Reasoning (pipe-separated)
  Col I  Brief Path
  Col J  Applied At
  Col K  Candidate ID (hashed — no PII)

The sheet is always re-sorted by score descending after each update.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

_HEADERS = [
    "Rank", "Score", "Experience", "Skills", "Education",
    "Location", "Notice Period", "Reasoning", "Brief Path", "Applied At", "Candidate ID",
]


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────


def _get_client() -> gspread.Client:
    sa_path = Path(os.environ.get("GOOGLE_SERVICE_ACCOUNT_PATH", "service_account.json"))
    creds = Credentials.from_service_account_file(str(sa_path), scopes=_SCOPES)
    return gspread.authorize(creds)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def upsert_candidate_row(
    sheet_id: str,
    role_name: str,
    candidate_id: str,
    score: int,
    experience: str,
    skills: list[str],
    education: str,
    location: str,
    notice_period: str,
    reasoning: list[str],
    brief_path: str,
    applied_at: str,
) -> int:
    """
    Add or update the candidate row in the role's Google Sheet.
    Returns the new row number (1-indexed, after re-sort).
    """
    try:
        client = _get_client()
        sheet = _get_or_create_worksheet(client, sheet_id, role_name)

        row_data = [
            "",                                         # Rank (computed after sort)
            score,
            experience[:300],                           # truncate for readability
            ", ".join(skills[:10]),
            education,
            location,
            notice_period,
            " | ".join(reasoning),
            brief_path,
            applied_at,
            candidate_id,
        ]

        existing = _find_row_by_candidate_id(sheet, candidate_id)
        if existing:
            sheet.update(f"A{existing}:K{existing}", [row_data])
            logger.info("Updated Sheet row=%d for candidate_id=%s", existing, candidate_id)
        else:
            sheet.append_row(row_data, value_input_option="USER_ENTERED")
            logger.info("Appended Sheet row for candidate_id=%s", candidate_id)

        return _sort_and_rank(sheet)

    except gspread.exceptions.APIError as exc:
        logger.error("Google Sheets API error: %s", exc)
        raise


def ensure_sheet_exists(sheet_id: str, role_name: str) -> None:
    """Idempotently create the worksheet for this role."""
    client = _get_client()
    _get_or_create_worksheet(client, sheet_id, role_name)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _get_or_create_worksheet(client: gspread.Client, sheet_id: str, role_name: str):
    spreadsheet = client.open_by_key(sheet_id)
    try:
        ws = spreadsheet.worksheet(role_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=role_name, rows=500, cols=len(_HEADERS))
        ws.append_row(_HEADERS, value_input_option="USER_ENTERED")
        ws.format("A1:K1", {"textFormat": {"bold": True}})
        logger.info("Created worksheet '%s' in spreadsheet %s", role_name, sheet_id)
    return ws


def _find_row_by_candidate_id(sheet, candidate_id: str) -> int | None:
    """Return 1-indexed row number of candidate, or None."""
    col_k = sheet.col_values(11)  # Candidate ID is column K (index 11)
    for i, val in enumerate(col_k):
        if val == candidate_id:
            return i + 1
    return None


def _sort_and_rank(sheet) -> int:
    """Sort rows by Score descending (skipping header), then fill Rank column."""
    all_rows = sheet.get_all_values()
    if len(all_rows) <= 1:
        return 1

    header = all_rows[0]
    data_rows = all_rows[1:]

    # Sort by score (column index 1) descending, treating blanks as 0
    data_rows.sort(key=lambda r: int(r[1]) if r[1].isdigit() else 0, reverse=True)

    # Assign ranks
    for i, row in enumerate(data_rows):
        row[0] = str(i + 1)

    updated = [header] + data_rows
    sheet.update("A1", updated)
    return 1  # top row is rank 1
