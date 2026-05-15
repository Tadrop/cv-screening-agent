"""
Gmail inbox poller — detects new application emails and hands each one to
the SelfAnnealingPipeline for end-to-end processing with automatic recovery.

Label convention:  Applications/<RoleName>
                   e.g.  Applications/Senior Backend Engineer
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..db import models as db_models
from ..db import operations as db_ops
from ..outputs import gmail as gmail_out
from ..pipeline.self_annealing import SelfAnnealingPipeline

logger = logging.getLogger(__name__)

_APPLICATION_LABEL_PREFIX = "Applications/"


def poll_all_roles() -> int:
    """
    Poll every configured role's Gmail label and process new applications.
    Returns the total number of candidates processed this cycle.
    """
    db_models.init_db()
    roles = db_ops.list_roles()
    if not roles:
        logger.warning("No roles configured. Run setup_role.py to add a role.")
        return 0

    total = 0
    for role in roles:
        label = f"{_APPLICATION_LABEL_PREFIX}{role['name']}"
        try:
            total += _process_label(role, label)
        except Exception as exc:
            logger.error("Failed processing label '%s': %s", label, exc, exc_info=True)
    return total


def _process_label(role: dict, label_name: str) -> int:
    messages = gmail_out.list_messages_with_label(label_name)

    # Load JD text once per label so the pipeline can fall back to it if Pinecone is down
    jd_text: Optional[str] = None
    if role.get("jd_path"):
        try:
            jd_text = Path(role["jd_path"]).read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read JD file for role '%s': %s", role["name"], exc)

    pipeline = SelfAnnealingPipeline(role=role, jd_text=jd_text)
    processed = 0

    for stub in messages:
        message_id = stub["id"]
        if db_ops.is_message_processed(message_id):
            continue
        try:
            # pipeline.run() owns mark_message_processed — it only fires
            # after the candidate row is fully scored & persisted, so a
            # crash mid-pipeline leaves the message unprocessed for retry.
            success = pipeline.run(message_id)
            if success:
                processed += 1
        except Exception as exc:
            # The pipeline itself should not raise — this is a last-resort catch
            logger.error(
                "Unhandled exception for message_id=%s role=%s: %s",
                message_id, role["name"], exc, exc_info=True,
            )

    logger.info("Processed %d new applications for role '%s'", processed, role["name"])
    return processed
