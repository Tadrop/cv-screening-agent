"""
Slack integration — sends a DM to the hiring manager when a standout
candidate is found (score >= STANDOUT_SCORE_THRESHOLD).

The message attaches the one-page brief inline as a Slack block — no file
upload needed.  The hiring manager can action directly from Slack.
"""

from __future__ import annotations

import logging
import os

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)

_THRESHOLD = int(os.getenv("STANDOUT_SCORE_THRESHOLD", "85"))


def notify_standout_candidate(
    hiring_manager_user_id: str,
    role_name: str,
    score: int,
    reasoning: list[str],
    brief_markdown: str,
    candidate_id: str,
) -> bool:
    """
    Send a Slack DM to the hiring manager about a standout candidate.
    Returns True on success, False on failure (non-fatal).

    Only fires when score >= STANDOUT_SCORE_THRESHOLD.
    """
    if score < _THRESHOLD:
        return False

    try:
        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])

        # Truncate brief for Slack's 3001-char block limit
        brief_preview = brief_markdown[:2000] + ("…" if len(brief_markdown) > 2000 else "")
        reasoning_bullets = "\n".join(f"• {r}" for r in reasoning)

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"⭐ Standout Candidate — {role_name}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Score:*\n{score}/100"},
                    {"type": "mrkdwn", "text": f"*Candidate ID:*\n`{candidate_id[:12]}…`"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Scoring Reasons:*\n{reasoning_bullets}"},
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*One-Page Brief:*\n```\n{brief_preview}\n```"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "🤖 Scored by AI — all hiring decisions require human sign-off.",
                    }
                ],
            },
        ]

        response = client.chat_postMessage(
            channel=hiring_manager_user_id,
            text=f"Standout candidate for {role_name}: {score}/100",
            blocks=blocks,
        )
        logger.info(
            "Slack standout alert sent to user=%s role=%s score=%d ts=%s",
            hiring_manager_user_id, role_name, score, response["ts"],
        )
        return True

    except SlackApiError as exc:
        logger.error("Slack notification failed: %s", exc.response["error"])
        return False  # non-fatal — the rest of the pipeline continues
