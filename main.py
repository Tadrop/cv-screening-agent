"""
Main entry point — runs a single one-shot poll of all Gmail inboxes.

Use this for a manual trigger or a simple cron job.
For continuous polling, use:  python -m src.scheduler.runner
"""

from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from src.observability import setup_logging

load_dotenv()
setup_logging()

logger = logging.getLogger(__name__)


def main() -> int:
    from src.db.models import init_db  # noqa: PLC0415
    from src.inbox.poller import poll_all_roles  # noqa: PLC0415

    init_db()
    logger.info("BuildRight CV Screening Agent — one-shot poll starting")
    count = poll_all_roles()
    logger.info("Poll complete — %d new candidates processed", count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
