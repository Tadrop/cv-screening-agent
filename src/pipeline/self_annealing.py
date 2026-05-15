"""
Self-annealing pipeline orchestrator.

Wraps every stage of candidate processing in a recovery harness.
When a stage fails, the harness:

  1. Classifies the error → picks a RecoveryStrategy
  2. Applies the strategy (wait + retry, or alternative path)
  3. If the stage cannot recover → decides whether to skip the step or the whole candidate
  4. Records every failure in a FailureRecord
  5. On permanent failure: appends to CLAUDE.md Error Log + sends a Slack alert

Stages and their fallback behaviour
─────────────────────────────────────────────────────────────────────────────
  PARSE          → fallback: mark as scanned, score=0, flag for manual review
  STRIP          → fallback: pass raw text (no identity stripping) — logged as [PII risk]
  RAG RETRIEVE   → fallback: pass full JD text to Claude (already built into retrieval.py)
  SCORE (Claude) → retry once with repair hint; permanent fail → skip candidate
  GMAIL DRAFT    → skip step, candidate still scored/ranked
  SHEET UPDATE   → skip step, candidate still scored/briefed
  SLACK ALERT    → skip step (already non-fatal)
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, TypeVar

from .strategies import RecoveryStrategy, classify_error

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}
_BRIEFS_DIR = Path(os.getenv("BRIEFS_DIR", "data/briefs"))
_ATTACHMENTS_DIR = Path(os.getenv("ATTACHMENTS_DIR", "data/attachments"))


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FailureRecord:
    timestamp: datetime
    stage: str
    role: str
    candidate_id: str
    error_type: str
    error_message: str
    traceback_text: str
    strategy_applied: str
    recovery_actions: list[str]
    resolved: bool


@dataclass
class StageResult:
    success: bool
    value: object = None
    failure: Optional[FailureRecord] = None
    skip_candidate: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# Core harness
# ──────────────────────────────────────────────────────────────────────────────


class SelfAnnealingPipeline:
    """
    Instantiate one per poll cycle.  Call `run()` for each Gmail message.
    """

    def __init__(self, role: dict, jd_text: Optional[str] = None) -> None:
        self.role = role
        self.jd_text = jd_text
        self.failures: list[FailureRecord] = []

    # ── public entry point ────────────────────────────────────────────────────

    def run(self, message_id: str) -> bool:
        """
        Process one candidate message end-to-end with full self-healing.
        Returns True if the candidate was fully processed, False if skipped.
        """
        from ..outputs import gmail as gmail_out      # noqa: PLC0415
        from ..outputs import sheets as sheets_out    # noqa: PLC0415
        from ..outputs import slack as slack_out      # noqa: PLC0415
        from ..db import operations as db_ops         # noqa: PLC0415

        raw_message = gmail_out.get_message(message_id)
        sender_email = _extract_header(raw_message, "From")
        email_hash = hashlib.sha256(sender_email.encode()).hexdigest()
        candidate_id = hashlib.sha256(f"{sender_email}{message_id}".encode()).hexdigest()

        # Idempotency guard: if this candidate has already been fully scored,
        # don't re-run the pipeline (would duplicate Gmail drafts / Slack alerts).
        existing = db_ops.get_candidate(candidate_id)
        if existing and existing.get("score") is not None:
            logger.info(
                "[self-annealing] candidate_id=%s already scored (score=%s) — skipping",
                candidate_id[:12], existing["score"],
            )
            db_ops.mark_message_processed(message_id)
            return True

        log_ctx = {"candidate_id": candidate_id[:12], "role": self.role["name"]}
        logger.info(
            "[self-annealing] Starting candidate_id=%s role=%s",
            candidate_id[:12], self.role["name"],
            extra=log_ctx,
        )

        # ── Stage 1: Download attachment ─────────────────────────────────────
        attach_result = self._run_stage(
            stage="download_attachment",
            candidate_id=candidate_id,
            fn=lambda: _download_attachment(raw_message, candidate_id),
        )
        if attach_result.skip_candidate:
            # Legitimate, non-retryable skip (e.g. no supported attachment).
            # Mark the message processed so we don't poll it forever.
            db_ops.mark_message_processed(message_id)
            return False

        attachment_path: Path = attach_result.value
        db_ops.insert_candidate(
            candidate_id=candidate_id,
            role=self.role["name"],
            email_hash=email_hash,
            gmail_message_id=message_id,
            attachment_path=str(attachment_path),
        )

        # ── Stage 2: Parse CV ─────────────────────────────────────────────────
        parse_result = self._run_stage(
            stage="parse_cv",
            candidate_id=candidate_id,
            fn=lambda: _parse_cv(attachment_path),
            fallback=lambda _: ("[CV PARSE FAILURE — manual review required]", True),
        )
        raw_text, is_image_based = parse_result.value

        # ── Stage 3: Strip identity ───────────────────────────────────────────
        strip_result = self._run_stage(
            stage="strip_identity",
            candidate_id=candidate_id,
            fn=lambda: _strip_identity(raw_text),
            fallback=lambda exc: _strip_fallback(raw_text, exc, candidate_id),
        )
        anonymised_cv, identity = strip_result.value

        # ── Stage 4: Retrieve JD context ──────────────────────────────────────
        rag_result = self._run_stage(
            stage="rag_retrieve",
            candidate_id=candidate_id,
            fn=lambda: _retrieve_jd_context(self.role["name"], anonymised_cv, self.jd_text),
            fallback=lambda _: (self.jd_text[:4000] if self.jd_text else "[JD unavailable]"),
        )
        jd_context: str = rag_result.value

        # ── Stage 5: Score with Claude ────────────────────────────────────────
        cv_text_for_scoring = (
            "[SCANNED PDF — no text layer. Set score=0 and flag for manual review.]"
            if is_image_based else anonymised_cv
        )
        score_result = self._run_stage(
            stage="score_candidate",
            candidate_id=candidate_id,
            fn=lambda: _score_candidate(self.role["name"], cv_text_for_scoring, jd_context),
        )

        if score_result.skip_candidate:
            self._permanent_failure_alert(candidate_id, score_result.failure)
            # Permanent score failure → Slack alert was sent; don't keep retrying.
            db_ops.mark_message_processed(message_id)
            return False

        evaluation = score_result.value

        # ── Stage 6: Save brief ───────────────────────────────────────────────
        # Render deterministically from the structured fields rather than
        # trusting Claude's free-form markdown — guarantees consistent format.
        from ..scorer.brief import render_brief  # noqa: PLC0415
        brief_md = render_brief(evaluation, self.role["name"])
        brief_path = _save_brief(candidate_id, self.role["name"], brief_md)

        # ── Stage 7: Gmail draft (non-fatal) ──────────────────────────────────
        draft_result = self._run_stage(
            stage="gmail_draft",
            candidate_id=candidate_id,
            fn=lambda: gmail_out.create_acknowledgement_draft(
                to_email=sender_email,
                subject=f"Re: Your application for {self.role['name']} at BuildRight",
                body=evaluation.acknowledgement_email_draft,
                thread_id=raw_message.get("threadId"),
            ),
            fallback=lambda _: None,
        )

        # ── Stage 8: Sheet update (non-fatal) ─────────────────────────────────
        sheet_result = self._run_stage(
            stage="sheet_update",
            candidate_id=candidate_id,
            fn=lambda: sheets_out.upsert_candidate_row(
                sheet_id=self.role["sheet_id"],
                role_name=self.role["name"],
                candidate_id=candidate_id,
                score=evaluation.score,
                experience=evaluation.experience,
                skills=evaluation.skills,
                education=evaluation.education,
                location=evaluation.location,
                notice_period=evaluation.notice_period,
                reasoning=evaluation.reasoning,
                brief_path=str(brief_path),
                applied_at=datetime.now(timezone.utc).isoformat(),
            ) if self.role.get("sheet_id") else None,
            fallback=lambda _: None,
        )

        # ── Stage 9: Slack standout alert (non-fatal) ─────────────────────────
        slack_user = self.role.get("hiring_manager_slack_id") or os.getenv("SLACK_HIRING_MANAGER_USER_ID")
        if slack_user:
            self._run_stage(
                stage="slack_alert",
                candidate_id=candidate_id,
                fn=lambda: slack_out.notify_standout_candidate(
                    hiring_manager_user_id=slack_user,
                    role_name=self.role["name"],
                    score=evaluation.score,
                    reasoning=evaluation.reasoning,
                    brief_markdown=evaluation.one_page_brief,
                    candidate_id=candidate_id,
                ),
                fallback=lambda _: False,
            )

        # ── Stage 10: Persist to DB ───────────────────────────────────────────
        db_ops.update_candidate_score(
            candidate_id=candidate_id,
            score=evaluation.score,
            experience=evaluation.experience,
            skills=evaluation.skills,
            education=evaluation.education,
            location=evaluation.location,
            notice_period=evaluation.notice_period,
            reasoning=evaluation.reasoning,
            brief_path=str(brief_path),
            gmail_draft_id=draft_result.value,
            sheet_row=sheet_result.value,
        )

        # Only mark the message processed once the candidate row has been
        # fully scored & persisted — protects against crashes mid-pipeline.
        db_ops.mark_message_processed(message_id)

        logger.info(
            "[self-annealing] Completed candidate_id=%s score=%d failures=%d",
            candidate_id[:12], evaluation.score, len(self.failures),
        )
        return True

    # ──────────────────────────────────────────────────────────────────────────
    # Stage runner — the heart of self-annealing
    # ──────────────────────────────────────────────────────────────────────────

    def _run_stage(
        self,
        stage: str,
        candidate_id: str,
        fn: Callable[[], T],
        fallback: Optional[Callable[[Exception], T]] = None,
    ) -> StageResult:
        """
        Execute `fn`. On failure, classify the error, apply the recovery strategy,
        and retry with backoff.  If retries are exhausted, try the fallback.
        If the fallback also fails (or none provided), mark as permanent failure.
        """
        last_exc: Optional[Exception] = None
        recovery_actions: list[str] = []
        current_fn = fn

        for attempt in range(5):   # hard cap — strategy controls real retry limit
            try:
                value = current_fn()
                if attempt > 0:
                    logger.info("[self-annealing] '%s' recovered on attempt %d", stage, attempt + 1)
                return StageResult(success=True, value=value)

            except Exception as exc:
                last_exc = exc
                strategy = classify_error(exc, context=stage)

                if attempt == 0:
                    logger.warning(
                        "[self-annealing] '%s' failed: %s — strategy: %s",
                        stage, type(exc).__name__, strategy.name,
                    )

                # Inject a Pydantic repair hint into the scorer on first validation failure
                if strategy.name == "claude_validation_failure" and attempt == 0:
                    hint = _extract_validation_repair_hint(exc)
                    recovery_actions.append(f"Injecting repair hint: {hint}")
                    _inject_repair_hint(hint)
                    # current_fn already closes over the scorer call — hint is read via module global

                if not strategy.retryable or attempt >= strategy.max_retries:
                    _clear_repair_hint()
                    break

                wait = strategy.retry_delay_seconds * (2 ** attempt)
                recovery_actions.append(f"Attempt {attempt + 2}: waiting {wait:.0f}s")
                logger.info("[self-annealing] Waiting %.0fs before retry on '%s'", wait, stage)
                time.sleep(wait)

        _clear_repair_hint()

        # Retries exhausted — try fallback
        if fallback is not None:
            try:
                value = fallback(last_exc)
                recovery_actions.append(f"Used fallback for '{stage}'")
                logger.warning("[self-annealing] '%s' fell back after %d attempt(s)", stage, attempt + 1)
                failure = self._record_failure(
                    stage=stage, candidate_id=candidate_id, exc=last_exc,
                    strategy=classify_error(last_exc, stage),
                    recovery_actions=recovery_actions, resolved=True,
                )
                return StageResult(success=False, value=value, failure=failure)
            except Exception as fb_exc:
                logger.error("[self-annealing] Fallback also failed for '%s': %s", stage, fb_exc)
                last_exc = fb_exc

        # Permanent failure
        strategy = classify_error(last_exc, context=stage)
        failure = self._record_failure(
            stage=stage, candidate_id=candidate_id, exc=last_exc,
            strategy=strategy, recovery_actions=recovery_actions, resolved=False,
        )
        if strategy.step_fatal:
            logger.error(
                "[self-annealing] '%s' permanently failed — skipping candidate_id=%s",
                stage, candidate_id[:12],
            )
        return StageResult(success=False, value=None, failure=failure, skip_candidate=strategy.step_fatal)

    # ──────────────────────────────────────────────────────────────────────────
    # Failure recording and permanent-failure alert
    # ──────────────────────────────────────────────────────────────────────────

    def _record_failure(
        self,
        stage: str,
        candidate_id: str,
        exc: Exception,
        strategy: RecoveryStrategy,
        recovery_actions: list[str],
        resolved: bool,
    ) -> FailureRecord:
        record = FailureRecord(
            timestamp=datetime.now(timezone.utc),
            stage=stage,
            role=self.role["name"],
            candidate_id=candidate_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
            traceback_text=traceback.format_exc(),
            strategy_applied=strategy.name,
            recovery_actions=recovery_actions,
            resolved=resolved,
        )
        self.failures.append(record)

        try:
            from .error_log import append_error_log  # noqa: PLC0415
            append_error_log(record)
        except Exception as log_exc:
            logger.error("[self-annealing] Failed to write CLAUDE.md error log: %s", log_exc)

        return record

    def _permanent_failure_alert(self, candidate_id: str, failure: Optional[FailureRecord]) -> None:
        if not failure:
            return
        slack_user = self.role.get("hiring_manager_slack_id") or os.getenv("SLACK_HIRING_MANAGER_USER_ID")
        if not slack_user:
            return
        try:
            from slack_sdk import WebClient  # noqa: PLC0415
            client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
            actions = "; ".join(failure.recovery_actions) or "none"
            client.chat_postMessage(
                channel=slack_user,
                text=(
                    f":warning: *CV Screening Alert — Manual Review Required*\n"
                    f"*Role:* {failure.role}\n"
                    f"*Candidate ID:* `{candidate_id[:12]}…`\n"
                    f"*Stage failed:* `{failure.stage}`\n"
                    f"*Error:* `{failure.error_type}: {failure.error_message[:200]}`\n"
                    f"*Strategy tried:* {failure.strategy_applied}\n"
                    f"*Actions taken:* {actions}\n"
                    f"This candidate could not be processed automatically. Please review their email manually."
                ),
            )
            logger.info("[self-annealing] Permanent failure Slack alert sent to user=%s", slack_user)
        except Exception as exc:
            logger.error("[self-annealing] Failed to send Slack alert: %s", exc)


# ──────────────────────────────────────────────────────────────────────────────
# Module-level helpers (no circular imports — no poller dependency)
# ──────────────────────────────────────────────────────────────────────────────


def _download_attachment(raw_message: dict, candidate_id: str) -> Path:
    from ..outputs.gmail import download_attachment as gmail_download  # noqa: PLC0415

    _ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    for part in _iter_parts(raw_message.get("payload", {})):
        filename = part.get("filename", "")
        suffix = Path(filename).suffix.lower()
        if suffix not in _SUPPORTED_EXTENSIONS:
            continue
        attachment_id = part.get("body", {}).get("attachmentId")
        if not attachment_id:
            continue
        data = gmail_download(raw_message["id"], attachment_id)
        dest = _ATTACHMENTS_DIR / f"{candidate_id}{suffix}"
        dest.write_bytes(data)
        logger.info("Saved attachment %s (%d bytes)", dest.name, len(data))
        return dest
    raise ValueError("No supported CV attachment found in email")


def _iter_parts(payload: dict):
    yield payload
    for part in payload.get("parts", []):
        yield from _iter_parts(part)


def _parse_cv(path: Path) -> tuple[str, bool]:
    from ..parser.extractor import extract_text  # noqa: PLC0415
    result = extract_text(path)
    return result.text, result.is_image_based


def _strip_identity(text: str) -> tuple[str, object]:
    from ..parser.identity_stripper import strip_identity  # noqa: PLC0415
    result = strip_identity(text)
    return result.anonymised_text, result.identity


def _strip_fallback(raw_text: str, exc: Exception, candidate_id: str) -> tuple[str, None]:
    logger.error(
        "[self-annealing][PII RISK] Identity stripping failed for candidate_id=%s: %s. "
        "Passing raw text — PII may be visible to Claude.",
        candidate_id[:12], exc,
    )
    return raw_text, None


def _retrieve_jd_context(role_name: str, cv_text: str, jd_text: Optional[str]) -> str:
    from ..jd_rag.retrieval import retrieve_jd_context  # noqa: PLC0415
    return retrieve_jd_context(role_name=role_name, cv_text=cv_text, fallback_jd=jd_text)


def _score_candidate(role_name: str, anonymised_cv: str, jd_context: str):
    from ..scorer.scorer import score_candidate  # noqa: PLC0415
    return score_candidate(role_name, anonymised_cv, jd_context)


def _save_brief(candidate_id: str, role_name: str, brief_md: str) -> Path:
    _BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    safe_role = "".join(c if c.isalnum() or c in "-_ " else "_" for c in role_name)
    path = _BRIEFS_DIR / f"{candidate_id[:16]}_{safe_role}.md"
    path.write_text(brief_md, encoding="utf-8")
    return path


def _extract_header(message: dict, name: str) -> str:
    for header in message.get("payload", {}).get("headers", []):
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# Repair hint — ContextVar so concurrent candidate runs don't cross-contaminate.
# Each thread / asyncio task carries its own value; the public API of
# score_candidate() reads it via get_active_repair_hint().
# ──────────────────────────────────────────────────────────────────────────────

_active_repair_hint: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_active_repair_hint", default=None
)


def _inject_repair_hint(hint: str) -> None:
    _active_repair_hint.set(hint)


def _clear_repair_hint() -> None:
    _active_repair_hint.set(None)


def get_active_repair_hint() -> Optional[str]:
    return _active_repair_hint.get()


def _extract_validation_repair_hint(exc: Exception) -> str:
    try:
        errors = exc.errors()  # type: ignore[attr-defined]
        hints = []
        for err in errors[:3]:
            field_path = " → ".join(str(loc) for loc in err.get("loc", []))
            msg = err.get("msg", "invalid value")
            hints.append(f"Field '{field_path}': {msg}")
        return "; ".join(hints)
    except Exception:
        return f"Validation failed: {exc}"
