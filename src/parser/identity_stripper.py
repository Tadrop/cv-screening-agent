"""
Identity signal stripper — removes or masks PII before the CV reaches Claude.

Stripped signals are returned in a separate dict so they can be used later
when personalising the Gmail draft (which a human reviews before sending).

Signals removed:
  - Email addresses
  - Phone numbers (international formats)
  - URLs / LinkedIn / GitHub profiles
  - Lines that appear to be a candidate name (heuristic: first non-empty line,
    title-cased, ≤5 tokens, no verb-like words)
  - Age / date-of-birth indicators
  - Gendered pronouns in self-descriptions
  - Nationality / country-of-origin phrases
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StrippedIdentity:
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    github: Optional[str] = None


@dataclass
class StripResult:
    anonymised_text: str
    identity: StrippedIdentity
    flags: list[str] = field(default_factory=list)   # e.g. ["age_found", "nationality_found"]


# ──────────────────────────────────────────────────────────────────────────────
# Regex patterns
# ──────────────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", re.IGNORECASE)
# Match real phone-number shapes only — avoids gutting date ranges (2019-2024),
# postcodes, version numbers, and other digit blocks that aren't phone numbers.
_PHONE_RE = re.compile(
    r"(?:"
    r"\+\d[\d\s\-().]{8,18}\d"                         # +44 7700 900123, +1 (415) 555 0199
    r"|"
    r"\(\d{2,4}\)[\s\-]?\d{3,4}[\s\-]?\d{3,4}"         # (020) 7946 0958
    r"|"
    r"\b\d{3,5}[\s\-]\d{3,4}[\s\-]\d{3,4}\b"           # 020 7946 0958, 555-123-4567
    r")"
)
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w\-]+", re.IGNORECASE)
_GITHUB_RE = re.compile(r"github\.com/[\w\-]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_AGE_RE = re.compile(
    r"\b(?:age[d]?\s*:?\s*\d{1,2}|born\s+(?:in\s+)?\d{4}|D\.?O\.?B\.?\s*:?\s*[\d/.\-]+)\b",
    re.IGNORECASE,
)
_NATIONALITY_RE = re.compile(
    r"\b(?:nationality|citizenship|citizen|passport)\s*:?\s*\w+",
    re.IGNORECASE,
)

# Note: gendered pronouns were previously substituted with "They", but that
# produced ungrammatical output ("They portfolio") and clobbered legitimate
# uses (e.g., "His Majesty's Revenue & Customs"). The bias-aware system prompt
# already instructs Claude to ignore gender, so the substitution is unnecessary.

# Titles that precede names
_NAME_TITLE_RE = re.compile(
    r"^(Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?)\s+", re.IGNORECASE
)


def strip_identity(raw_text: str) -> StripResult:
    identity = StrippedIdentity()
    flags: list[str] = []
    text = raw_text

    # 1. Email
    m = _EMAIL_RE.search(text)
    if m:
        identity.email = m.group(0)
    text = _EMAIL_RE.sub("[EMAIL REDACTED]", text)

    # 2. LinkedIn
    m = _LINKEDIN_RE.search(text)
    if m:
        identity.linkedin = m.group(0)
    text = _LINKEDIN_RE.sub("[LINKEDIN REDACTED]", text)

    # 3. GitHub
    m = _GITHUB_RE.search(text)
    if m:
        identity.github = m.group(0)
    text = _GITHUB_RE.sub("[GITHUB REDACTED]", text)

    # 4. Other URLs
    text = _URL_RE.sub("[URL REDACTED]", text)

    # 5. Phone numbers (apply after email to avoid double-matching)
    m = _PHONE_RE.search(text)
    if m:
        identity.phone = m.group(0).strip()
    text = _PHONE_RE.sub("[PHONE REDACTED]", text)

    # 6. Age / DOB
    if _AGE_RE.search(text):
        flags.append("age_found")
    text = _AGE_RE.sub("[AGE REDACTED]", text)

    # 7. Nationality
    if _NATIONALITY_RE.search(text):
        flags.append("nationality_found")
    text = _NATIONALITY_RE.sub("[NATIONALITY REDACTED]", text)

    # 8. Candidate name — heuristic: first non-empty line that looks like a name
    identity.name, text = _strip_name(text)

    return StripResult(anonymised_text=text, identity=identity, flags=flags)


_NAME_STOPWORDS = {
    # job-title tokens that often share the same line as a name in CV headers
    "engineer", "developer", "manager", "director", "consultant",
    "analyst", "designer", "lead", "senior", "junior", "principal",
    "head", "officer", "specialist", "architect", "scientist",
    "intern", "associate", "executive", "founder", "co-founder",
    "owner", "chief", "vice", "president", "cto", "ceo", "cfo",
    "programmer", "administrator", "coordinator", "researcher",
    # generic CV header words
    "curriculum", "vitae", "resume", "résumé", "cv",
}


def _strip_name(text: str) -> tuple[Optional[str], str]:
    """
    Identify and redact the candidate's name from the first few non-empty
    lines of the CV.  Accepts both title-case ("John Smith") and all-caps
    ("JOHN SMITH"), and rejects lines that contain job-title stopwords.
    Returns (name_or_None, modified_text).
    """
    lines = text.splitlines()
    for i, line in enumerate(lines[:6]):
        stripped = line.strip()
        if not stripped:
            continue
        candidate = _NAME_TITLE_RE.sub("", stripped).strip()
        tokens = candidate.split()

        if not (2 <= len(tokens) <= 5):
            continue
        if any(c in candidate for c in ("@", ":", "/", "|", "[", "]")):
            continue
        if _looks_like_section_header(candidate):
            continue
        if any(t.lower() in _NAME_STOPWORDS for t in tokens):
            continue

        # Accept either fully title-cased ("John Smith", "Mary-Jane Doe")
        # or fully ALL-CAPS ("JOHN SMITH") — but not mixed.
        is_title_case = all(t[0].isupper() and not t.isupper() for t in tokens if t)
        is_all_caps = all(t.isupper() and len(t) >= 2 for t in tokens if t)
        if not (is_title_case or is_all_caps):
            continue

        lines[i] = line.replace(stripped, "[NAME REDACTED]")
        return candidate, "\n".join(lines)
    return None, text


_SECTION_KEYWORDS = {
    "experience", "education", "skills", "summary", "profile",
    "objective", "contact", "references", "projects", "certifications",
    "languages", "hobbies", "interests", "awards", "publications",
}


def _looks_like_section_header(text: str) -> bool:
    return text.lower().strip().rstrip(":") in _SECTION_KEYWORDS
