"""
Pydantic schema for Claude's structured output.

Every field is required — the scorer rejects any response missing 'reasoning'.
This is enforced both by the tool-use schema and by Pydantic validation.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Accepts any of: "AI-assisted", "artificial intelligence", "AI is used",
# "AI system", "AI-powered", "AI tool(s)". A bare "AI" substring is not enough.
_AI_DISCLOSURE_RE = re.compile(
    r"(?:AI[\s\-]assisted|artificial\s+intelligence|AI\s+(?:is|are)\s+used|"
    r"AI\s+system|AI[\s\-]powered|AI\s+tools?)",
    re.IGNORECASE,
)


class CandidateEvaluation(BaseModel):
    score: int = Field(ge=0, le=100, description="Overall suitability score 0–100")
    reasoning: list[str] = Field(
        min_length=3,
        description="Explicit, multi-point reasoning. Min 3 items, max 8.",
    )
    experience: str = Field(description="Summary of relevant professional experience")
    skills: list[str] = Field(description="List of key skills identified")
    education: str = Field(description="Highest relevant qualification")
    location: str = Field(description="Candidate location, or 'Not specified'")
    notice_period: str = Field(description="Notice period, or 'Not specified'")
    acknowledgement_email_draft: str = Field(
        description="Personalized acknowledgement email body for human review"
    )
    one_page_brief: str = Field(
        description="Markdown one-page candidate brief for the hiring manager"
    )

    @field_validator("reasoning")
    @classmethod
    def reasoning_must_have_min_items(cls, v: list[str]) -> list[str]:
        if len(v) < 3:
            raise ValueError(f"reasoning must have at least 3 items, got {len(v)}")
        return v[:8]  # cap at 8 to keep briefs concise

    @field_validator("acknowledgement_email_draft")
    @classmethod
    def must_contain_ai_disclosure(cls, v: str) -> str:
        if not _AI_DISCLOSURE_RE.search(v):
            raise ValueError(
                "acknowledgement_email_draft is missing the AI disclosure statement "
                "(must contain a phrase like 'AI-assisted', 'artificial intelligence', "
                "'AI is used', 'AI system', or 'AI tool(s)')"
            )
        return v

    model_config = {
        "json_schema_extra": {
            "example": {
                "score": 82,
                "reasoning": [
                    "5 years of Python experience matches the 3+ year requirement.",
                    "Holds an AWS Solutions Architect certification as required.",
                    "Missing direct Kubernetes experience — a minor gap.",
                ],
                "experience": "5 years backend engineering at two fintech startups.",
                "skills": ["Python", "FastAPI", "PostgreSQL", "AWS", "Docker"],
                "education": "BSc Computer Science, University of Manchester (2019)",
                "location": "London, UK",
                "notice_period": "1 month",
                "acknowledgement_email_draft": "Dear Applicant, ...",
                "one_page_brief": "# Candidate Brief\n...",
            }
        }
    }


# Tool-use schema fed directly to the Claude API
EVALUATION_TOOL_SCHEMA: dict = {
    "name": "submit_candidate_evaluation",
    "description": (
        "Submit the structured evaluation of a candidate CV against a job description. "
        "All fields are mandatory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall suitability score (0 = no match, 100 = perfect match)",
            },
            "reasoning": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 3,
                "maxItems": 8,
                "description": "Explicit bullet-point reasoning referencing specific JD requirements",
            },
            "experience": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
            "education": {"type": "string"},
            "location": {"type": "string"},
            "notice_period": {"type": "string"},
            "acknowledgement_email_draft": {"type": "string"},
            "one_page_brief": {"type": "string"},
        },
        "required": [
            "score", "reasoning", "experience", "skills", "education",
            "location", "notice_period", "acknowledgement_email_draft", "one_page_brief",
        ],
    },
}
