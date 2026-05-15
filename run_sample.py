"""
Local sample runner — score a folder of CVs without Gmail/Sheets/Slack.

Usage:
    python run_sample.py \\
        --cv-dir   tests/fixtures/cvs \\
        --jd-file  tests/fixtures/jds/senior_backend_engineer.txt \\
        --role     "Senior Backend Engineer"

Output:
    - Prints a ranked summary table to stdout
    - Saves one-page briefs to data/briefs/
    - Does NOT touch Gmail, Sheets, or Slack
"""

from __future__ import annotations

import argparse
import json
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

_BRIEFS_DIR = Path("data/briefs")
_SUPPORTED = {".pdf", ".docx", ".doc"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Score CVs locally — no external integrations.")
    parser.add_argument("--cv-dir", required=True, type=Path, help="Folder containing CV files")
    parser.add_argument("--jd-file", required=True, type=Path, help="Plain-text job description")
    parser.add_argument("--role", required=True, help="Role name (used in the brief)")
    parser.add_argument("--no-rag", action="store_true", help="Skip Pinecone; pass full JD to Claude")
    args = parser.parse_args()

    if not args.cv_dir.is_dir():
        logger.error("--cv-dir %s is not a directory", args.cv_dir)
        return 1
    if not args.jd_file.exists():
        logger.error("--jd-file %s not found", args.jd_file)
        return 1

    jd_text = args.jd_file.read_text(encoding="utf-8")
    cv_files = sorted(p for p in args.cv_dir.iterdir() if p.suffix.lower() in _SUPPORTED)

    if not cv_files:
        logger.error("No supported CV files found in %s", args.cv_dir)
        return 1

    logger.info("Found %d CVs to score for role '%s'", len(cv_files), args.role)

    if not args.no_rag:
        _ensure_jd_ingested(args.role, jd_text)

    results: list[dict] = []
    for cv_path in cv_files:
        result = _score_cv(cv_path, args.role, jd_text, use_rag=not args.no_rag)
        if result:
            results.append(result)

    results.sort(key=lambda r: r["score"], reverse=True)
    _print_table(results)
    _save_briefs(results)
    return 0


def _ensure_jd_ingested(role_name: str, jd_text: str) -> None:
    try:
        from src.jd_rag.ingestion import ingest_job_description  # noqa: PLC0415
        count = ingest_job_description(role_name, jd_text)
        logger.info("JD ingested into Pinecone: %d chunks", count)
    except Exception as exc:
        logger.warning("Pinecone ingestion failed (%s) — falling back to full JD", exc)


def _score_cv(cv_path: Path, role_name: str, jd_text: str, use_rag: bool) -> dict | None:
    from src.parser.extractor import extract_text  # noqa: PLC0415
    from src.parser.identity_stripper import strip_identity  # noqa: PLC0415
    from src.scorer.scorer import score_candidate  # noqa: PLC0415

    logger.info("Scoring %s …", cv_path.name)
    try:
        extraction = extract_text(cv_path)
        strip_result = strip_identity(extraction.text)

        if use_rag:
            from src.jd_rag.retrieval import retrieve_jd_context  # noqa: PLC0415
            jd_context = retrieve_jd_context(role_name, strip_result.anonymised_text, jd_text)
        else:
            jd_context = jd_text[:4000]

        evaluation = score_candidate(role_name, strip_result.anonymised_text, jd_context)

        return {
            "file": cv_path.name,
            "score": evaluation.score,
            "experience": evaluation.experience,
            "skills": evaluation.skills,
            "education": evaluation.education,
            "location": evaluation.location,
            "notice_period": evaluation.notice_period,
            "reasoning": evaluation.reasoning,
            "brief": evaluation.one_page_brief,
            "identity_flags": strip_result.flags,
            "parser": extraction.parser_used,
            "is_image_based": extraction.is_image_based,
        }

    except Exception as exc:
        logger.error("Failed to score %s: %s", cv_path.name, exc, exc_info=True)
        return None


def _print_table(results: list[dict]) -> None:
    print("\n" + "═" * 80)
    print(f"  CANDIDATE RANKING  ({len(results)} candidates)")
    print("═" * 80)
    print(f"  {'Rank':<5} {'Score':<7} {'File':<35} {'Location':<20} {'Notice'}")
    print("─" * 80)
    for i, r in enumerate(results, 1):
        flag = "⭐" if r["score"] >= 85 else "  "
        print(
            f"  {flag}{i:<4} {r['score']:<7} {r['file'][:33]:<35} "
            f"{r['location'][:18]:<20} {r['notice_period']}"
        )
        for reason in r["reasoning"]:
            print(f"         → {reason}")
        print()
    print("═" * 80 + "\n")


def _save_briefs(results: list[dict]) -> None:
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    for r in results:
        stem = Path(r["file"]).stem
        out = _BRIEFS_DIR / f"{stem}_brief.md"
        out.write_text(r["brief"], encoding="utf-8")
    logger.info("Briefs saved to %s/", _BRIEFS_DIR)


if __name__ == "__main__":
    sys.exit(main())
