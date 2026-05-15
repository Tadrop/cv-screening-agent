"""
GDPR purge job — deletes candidate data older than GDPR_RETENTION_DAYS (default 90).

What gets deleted:
  ✓  CV attachment file (disk)
  ✓  Candidate brief file (disk)
  ✓  Candidate DB row — fields nulled, 'purged' flag set (row kept for audit trail)
  ✓  Pinecone vectors are NOT per-candidate (they're per-role JD) — no action needed

What is preserved (audit trail):
  - candidate_id (hashed — no PII)
  - role
  - score (numeric — no PII)
  - created_at / processed_at timestamps
  - purged=1 flag

This gives BuildRight an auditable record that the candidate was screened
without retaining any personal data.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import operations as db_ops

logger = logging.getLogger(__name__)

def run_purge() -> dict[str, int]:
    """
    Delete all candidate data older than the retention window.
    Returns a summary dict: {checked, purged, errors}.
    """
    retention_days = int(os.getenv("GDPR_RETENTION_DAYS", "90"))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    candidates = db_ops.get_unpurged_older_than(cutoff)

    logger.info(
        "GDPR purge starting: retention=%d days, cutoff=%s, candidates_to_purge=%d",
        retention_days, cutoff.date().isoformat(), len(candidates),
    )

    checked = len(candidates)
    purged = 0
    errors = 0

    for candidate in candidates:
        try:
            _purge_candidate(candidate)
            purged += 1
        except Exception as exc:
            logger.error(
                "Failed to purge candidate_id=%s: %s",
                candidate["id"][:12], exc, exc_info=True,
            )
            errors += 1

    summary = {"checked": checked, "purged": purged, "errors": errors}
    logger.info("GDPR purge complete: %s", summary)
    return summary


def _purge_candidate(candidate: dict) -> None:
    candidate_id = candidate["id"]

    # 1. Delete attachment file
    _delete_file(candidate.get("attachment_path"), candidate_id, "attachment")

    # 2. Delete brief file
    _delete_file(candidate.get("brief_path"), candidate_id, "brief")

    # 3. Mark purged in DB (nulls paths, sets purged=1)
    db_ops.mark_purged(candidate_id)

    logger.info(
        "Purged candidate_id=%s role=%s score=%s",
        candidate_id[:12], candidate.get("role"), candidate.get("score"),
    )


def _delete_file(path_str: str | None, candidate_id: str, label: str) -> None:
    if not path_str:
        return
    path = Path(path_str)
    if path.exists():
        path.unlink()
        logger.debug("Deleted %s file for candidate_id=%s: %s", label, candidate_id[:12], path.name)
    else:
        logger.debug("%s file already gone for candidate_id=%s", label, candidate_id[:12])
