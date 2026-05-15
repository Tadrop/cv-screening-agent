"""
Deterministic one-page candidate brief renderer.

Renders the brief locally from the structured `CandidateEvaluation` fields
rather than trusting Claude's free-form markdown. Benefits:

  • Consistent format across every brief (no drift between runs)
  • Saves output tokens on every scoring call
  • Easier to A/B test the layout without re-prompting

Claude's `one_page_brief` field is still validated by the schema (it acts as
a check that Claude produced a coherent summary) but is no longer the source
of truth for what recruiters and hiring managers see.
"""

from __future__ import annotations

from .schema import CandidateEvaluation


def render_brief(evaluation: CandidateEvaluation, role_name: str) -> str:
    """
    Render the one-page candidate brief in Markdown.
    Pure function — same inputs always produce the same output.
    """
    skills_list = ", ".join(evaluation.skills) if evaluation.skills else "—"
    reasoning_bullets = "\n".join(f"- {item}" for item in evaluation.reasoning)
    recommendation = _recommendation_for_score(evaluation.score)

    return (
        f"# Candidate Brief — {role_name}\n"
        f"**Score:** {evaluation.score}/100\n"
        f"\n"
        f"## Why This Score\n"
        f"{reasoning_bullets}\n"
        f"\n"
        f"## Experience\n"
        f"{evaluation.experience}\n"
        f"\n"
        f"## Key Skills\n"
        f"{skills_list}\n"
        f"\n"
        f"## Education\n"
        f"{evaluation.education}\n"
        f"\n"
        f"## Logistics\n"
        f"- **Location:** {evaluation.location}\n"
        f"- **Notice Period:** {evaluation.notice_period}\n"
        f"\n"
        f"## Recruiter Recommendation\n"
        f"{recommendation}\n"
    )


def _recommendation_for_score(score: int) -> str:
    if score >= 85:
        return "**Recommend for interview** — standout match against the JD."
    if score >= 70:
        return "Recommend for interview — strong match with minor gaps."
    if score >= 55:
        return "Hold for pipeline — moderate match; revisit if shortlist thins."
    return "Pass — does not meet the core requirements of this role."
