"""
CRUD operations for candidate records and role configuration.
Every function uses parameterised queries — no string interpolation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .models import get_connection

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Candidates
# ──────────────────────────────────────────────────────────────────────────────


def insert_candidate(
    candidate_id: str,
    role: str,
    email_hash: str,
    gmail_message_id: str,
    attachment_path: str,
) -> None:
    sql = """
        INSERT OR IGNORE INTO candidates
            (id, role, email_hash, gmail_message_id, attachment_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        conn.execute(sql, (candidate_id, role, email_hash, gmail_message_id, attachment_path, datetime.now(timezone.utc)))
    logger.info("Inserted candidate record id=%s role=%s", candidate_id, role)


def update_candidate_score(
    candidate_id: str,
    score: int,
    experience: str,
    skills: list[str],
    education: str,
    location: str,
    notice_period: str,
    reasoning: list[str],
    brief_path: str,
    gmail_draft_id: Optional[str],
    sheet_row: Optional[int],
) -> None:
    sql = """
        UPDATE candidates SET
            score        = ?,
            experience   = ?,
            skills       = ?,
            education    = ?,
            location     = ?,
            notice_period = ?,
            reasoning    = ?,
            brief_path   = ?,
            gmail_draft_id = ?,
            sheet_row    = ?,
            processed_at = ?
        WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (
            score, experience,
            json.dumps(skills), education, location, notice_period,
            json.dumps(reasoning), brief_path, gmail_draft_id, sheet_row,
            datetime.now(timezone.utc), candidate_id,
        ))
    logger.info("Updated score for candidate id=%s score=%d", candidate_id, score)


def get_candidate(candidate_id: str) -> Optional[dict]:
    """Return the candidate row, or None if it doesn't exist."""
    sql = "SELECT * FROM candidates WHERE id = ?"
    with get_connection() as conn:
        row = conn.execute(sql, (candidate_id,)).fetchone()
    return dict(row) if row else None


def get_candidates_for_role(role: str) -> list[dict]:
    sql = "SELECT * FROM candidates WHERE role = ? AND purged = 0 ORDER BY score DESC"
    with get_connection() as conn:
        rows = conn.execute(sql, (role,)).fetchall()
    return [dict(r) for r in rows]


def get_unpurged_older_than(cutoff: datetime) -> list[dict]:
    sql = "SELECT * FROM candidates WHERE created_at < ? AND purged = 0"
    with get_connection() as conn:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


def mark_purged(candidate_id: str) -> None:
    sql = "UPDATE candidates SET purged = 1, attachment_path = NULL, brief_path = NULL WHERE id = ?"
    with get_connection() as conn:
        conn.execute(sql, (candidate_id,))


# ──────────────────────────────────────────────────────────────────────────────
# Processed messages (deduplication)
# ──────────────────────────────────────────────────────────────────────────────


def is_message_processed(message_id: str) -> bool:
    sql = "SELECT 1 FROM processed_messages WHERE message_id = ?"
    with get_connection() as conn:
        return conn.execute(sql, (message_id,)).fetchone() is not None


def mark_message_processed(message_id: str) -> None:
    sql = "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)"
    with get_connection() as conn:
        conn.execute(sql, (message_id, datetime.now(timezone.utc)))


# ──────────────────────────────────────────────────────────────────────────────
# Roles
# ──────────────────────────────────────────────────────────────────────────────


def upsert_role(
    name: str,
    jd_path: str,
    sheet_id: str,
    hiring_manager_slack_id: Optional[str] = None,
) -> None:
    sql = """
        INSERT INTO roles (name, jd_path, sheet_id, hiring_manager_slack_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            jd_path = excluded.jd_path,
            sheet_id = excluded.sheet_id,
            hiring_manager_slack_id = excluded.hiring_manager_slack_id
    """
    with get_connection() as conn:
        conn.execute(sql, (name, jd_path, sheet_id, hiring_manager_slack_id))


def get_role(name: str) -> Optional[dict]:
    sql = "SELECT * FROM roles WHERE name = ?"
    with get_connection() as conn:
        row = conn.execute(sql, (name,)).fetchone()
    return dict(row) if row else None


def list_roles() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM roles ORDER BY name").fetchall()
    return [dict(r) for r in rows]
