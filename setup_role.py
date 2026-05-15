"""
Role setup wizard — run this once per new job opening.

What it does:
  1. Reads the job description file you provide
  2. Ingests it into Pinecone under the role's namespace
  3. Creates the Gmail label  "Applications/<RoleName>"
  4. Registers the role in the SQLite DB (with sheet_id + Slack user)
  5. Ensures the Google Sheet worksheet exists

Usage:
    python setup_role.py \\
        --role        "Senior Backend Engineer" \\
        --jd-file     jds/senior_backend_engineer.txt \\
        --sheet-id    1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms \\
        --slack-user  U0123456789
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level="INFO",
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Register a new role with the screening agent.")
    parser.add_argument("--role", required=True, help="Human-readable role name")
    parser.add_argument("--jd-file", required=True, type=Path, help="Plain-text job description file")
    parser.add_argument("--sheet-id", required=True, help="Google Sheets spreadsheet ID")
    parser.add_argument("--slack-user", help="Slack user ID of hiring manager (e.g. U0123456789)")
    args = parser.parse_args()

    if not args.jd_file.exists():
        logger.error("JD file not found: %s", args.jd_file)
        return 1

    jd_text = args.jd_file.read_text(encoding="utf-8")
    logger.info("Setting up role: '%s'", args.role)

    # 1. Ingest JD into Pinecone
    try:
        from src.jd_rag.ingestion import ingest_job_description  # noqa: PLC0415
        count = ingest_job_description(args.role, jd_text)
        logger.info("✓ Pinecone: ingested %d chunks", count)
    except Exception as exc:
        logger.error("Pinecone ingestion failed: %s", exc)
        return 1

    # 2. Create Gmail label
    try:
        from src.outputs.gmail import create_label_if_missing  # noqa: PLC0415
        label_name = f"Applications/{args.role}"
        label_id = create_label_if_missing(label_name)
        logger.info("✓ Gmail: label '%s' ready (id=%s)", label_name, label_id)
    except Exception as exc:
        logger.error("Gmail label creation failed: %s", exc)
        return 1

    # 3. Register role in DB
    try:
        from src.db.models import init_db  # noqa: PLC0415
        from src.db.operations import upsert_role  # noqa: PLC0415
        init_db()
        upsert_role(
            name=args.role,
            jd_path=str(args.jd_file.resolve()),
            sheet_id=args.sheet_id,
            hiring_manager_slack_id=args.slack_user,
        )
        logger.info("✓ Database: role '%s' registered", args.role)
    except Exception as exc:
        logger.error("DB registration failed: %s", exc)
        return 1

    # 4. Ensure Sheet worksheet exists
    try:
        from src.outputs.sheets import ensure_sheet_exists  # noqa: PLC0415
        ensure_sheet_exists(args.sheet_id, args.role)
        logger.info("✓ Google Sheet: worksheet '%s' ready", args.role)
    except Exception as exc:
        logger.error("Sheet setup failed: %s", exc)
        return 1

    print(f"\n✅ Role '{args.role}' is ready.")
    print(f"   Send CVs to Gmail with label: Applications/{args.role}")
    print(f"   Scheduler will pick them up every {__import__('os').getenv('POLL_INTERVAL_MINUTES', '10')} minutes.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
