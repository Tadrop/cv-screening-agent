"""
Gmail integration — creates acknowledgement DRAFTS only.

SAFETY GUARANTEE: This module never calls users.messages.send().
The only write operation is users.drafts.create().
Human recruiter must open Gmail Drafts and click Send manually.
"""

from __future__ import annotations

import base64
import email.mime.text
import logging
import os
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",     # needed for draft creation + labelling
]


# ──────────────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────────────


def _get_gmail_service():
    creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json"))
    token_path = Path(os.environ.get("GMAIL_TOKEN_PATH", "token.json"))

    creds: Optional[Credentials] = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), _SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ──────────────────────────────────────────────────────────────────────────────
# Draft creation
# ──────────────────────────────────────────────────────────────────────────────


def create_acknowledgement_draft(
    to_email: str,
    subject: str,
    body: str,
    thread_id: Optional[str] = None,
) -> str:
    """
    Create a Gmail draft addressed to the candidate.
    Returns the draft ID.

    NEVER SENDS — only creates a draft for human review.
    """
    try:
        service = _get_gmail_service()

        mime_msg = email.mime.text.MIMEText(body, "plain", "utf-8")
        mime_msg["To"] = to_email
        mime_msg["Subject"] = subject
        if thread_id:
            mime_msg["threadId"] = thread_id

        encoded = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode()
        draft_body: dict = {"message": {"raw": encoded}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        result = service.users().drafts().create(userId="me", body=draft_body).execute()
        draft_id: str = result["id"]
        logger.info("Created Gmail draft id=%s for to=%s", draft_id, _hash_email(to_email))
        return draft_id

    except HttpError as exc:
        logger.error(
            "Gmail draft creation failed (status=%s body=%s)",
            exc.status_code, exc.error_details,
        )
        raise


# ──────────────────────────────────────────────────────────────────────────────
# Inbox polling helpers (used by src/inbox/poller.py)
# ──────────────────────────────────────────────────────────────────────────────


def list_messages_with_label(label_name: str, max_results: int = 50) -> list[dict]:
    """Return raw Gmail message stubs (id + threadId) for a given label."""
    try:
        service = _get_gmail_service()
        response = (
            service.users()
            .messages()
            .list(userId="me", labelIds=[_get_label_id(service, label_name)], maxResults=max_results)
            .execute()
        )
        return response.get("messages", [])
    except HttpError as exc:
        logger.error("Gmail list messages failed (status=%s)", exc.status_code)
        raise


def get_message(message_id: str) -> dict:
    service = _get_gmail_service()
    return service.users().messages().get(userId="me", id=message_id, format="full").execute()


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    service = _get_gmail_service()
    result = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    return base64.urlsafe_b64decode(result["data"])


def create_label_if_missing(label_name: str) -> str:
    """Create a Gmail label and return its ID (idempotent)."""
    service = _get_gmail_service()
    return _get_label_id(service, label_name, create_if_missing=True)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _get_label_id(service, label_name: str, create_if_missing: bool = False) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for label in labels:
        if label["name"].lower() == label_name.lower():
            return label["id"]

    if create_if_missing:
        result = service.users().labels().create(
            userId="me",
            body={"name": label_name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()
        logger.info("Created Gmail label '%s' id=%s", label_name, result["id"])
        return result["id"]

    raise ValueError(f"Gmail label '{label_name}' not found")


def _hash_email(email_addr: str) -> str:
    import hashlib  # noqa: PLC0415
    return hashlib.sha256(email_addr.encode()).hexdigest()[:12]
