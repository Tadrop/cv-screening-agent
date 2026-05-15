"""
SQLite schema and connection management.
All candidate data is stored with a hashed ID — raw PII never touches the DB.
"""

import sqlite3
from pathlib import Path
from typing import Generator
import os

DB_PATH = Path(os.getenv("DB_PATH", "data/candidates.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id              TEXT PRIMARY KEY,           -- SHA-256(sender_email + message_id)
    role            TEXT NOT NULL,
    email_hash      TEXT NOT NULL,              -- SHA-256 of sender email
    score           INTEGER,
    experience      TEXT,
    skills          TEXT,                       -- JSON array
    education       TEXT,
    location        TEXT,
    notice_period   TEXT,
    reasoning       TEXT,                       -- JSON array (≥3 items)
    brief_path      TEXT,
    attachment_path TEXT,
    gmail_message_id TEXT,
    gmail_draft_id  TEXT,
    sheet_row       INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at    TIMESTAMP,
    purged          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id   TEXT PRIMARY KEY,
    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roles (
    name                     TEXT PRIMARY KEY,
    jd_path                  TEXT,
    sheet_id                 TEXT,
    hiring_manager_slack_id  TEXT,
    created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_candidates_role     ON candidates (role);
CREATE INDEX IF NOT EXISTS idx_candidates_score    ON candidates (score DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_created  ON candidates (created_at);
CREATE INDEX IF NOT EXISTS idx_candidates_purged   ON candidates (purged);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
