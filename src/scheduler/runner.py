"""
APScheduler entrypoint.

Jobs:
  - inbox_poll   — every POLL_INTERVAL_MINUTES (default 10 min)
  - gdpr_purge   — daily at 02:00 UTC

Run with:  python -m src.scheduler.runner
"""

from __future__ import annotations

import logging
import os
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from ..observability import setup_logging

load_dotenv()
setup_logging()
logger = logging.getLogger(__name__)

_POLL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "10"))


def _run_inbox_poll() -> None:
    from src.inbox.poller import poll_all_roles  # noqa: PLC0415
    try:
        count = poll_all_roles()
        logger.info("Inbox poll complete — %d new candidates processed", count)
    except Exception as exc:
        logger.error("Inbox poll error: %s", exc, exc_info=True)


def _run_gdpr_purge() -> None:
    from src.gdpr.purge import run_purge  # noqa: PLC0415
    try:
        summary = run_purge()
        logger.info("GDPR purge complete — %s", summary)
    except Exception as exc:
        logger.error("GDPR purge error: %s", exc, exc_info=True)


def main() -> None:
    scheduler = BlockingScheduler(timezone="UTC")

    scheduler.add_job(
        _run_inbox_poll,
        trigger=IntervalTrigger(minutes=_POLL_MINUTES),
        id="inbox_poll",
        name="Gmail inbox poller",
        max_instances=1,                 # never overlap runs
        coalesce=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        _run_gdpr_purge,
        trigger=CronTrigger(hour=2, minute=0),
        id="gdpr_purge",
        name="GDPR 90-day purge",
        max_instances=1,
        coalesce=True,
    )

    def _shutdown(signum, frame):
        logger.info("Shutting down scheduler (signal %s)…", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    logger.info(
        "Scheduler started — inbox poll every %d min, GDPR purge daily at 02:00 UTC",
        _POLL_MINUTES,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
