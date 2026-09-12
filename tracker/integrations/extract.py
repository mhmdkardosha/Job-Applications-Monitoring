"""AI classification and structured extraction for emails and postings.

All extraction goes through the DeepSeek client. Structured responses are
validated and rejected when malformed; missing values stay unknown and are
never invented. See IMPLEMENTATION_PLAN.md §4.3-4.4 and §5.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .ai_client import AiResult, DeepSeekClient
from .gmail import Message

EVENT_TYPES = (
    "application_confirmation",
    "acknowledgment",
    "recruiter_contact",
    "assessment",
    "interview_invitation",
    "interview_reschedule",
    "interview_cancellation",
    "offer",
    "rejection",
    "withdrawal",
    "unrelated",
    "unknown",
)

RECOMMEND_ACTIONS = (
    "create_application",
    "update_stage",
    "add_interview",
    "create_task",
    "none",
)

EMAIL_SYSTEM = (
    """You extract structured job-application events from a single email.
Return one JSON object only, matching this schema:
{
  "event_type": one of """
    + ", ".join(EVENT_TYPES)
    + """,
  "company": string (empty if unknown),
  "role": string (empty if unknown),
  "requisition_id": string (empty if unknown),
  "event_date": "YYYY-MM-DD" or null,
  "interview_start": ISO-8601 with offset or null,
  "location": string (empty if unknown),
  "links": [string],
  "contacts": [{"name": string, "email": string, "role": string}],
  "recommended_action": one of """
    + ", ".join(RECOMMEND_ACTIONS)
    + """,
  "confidence": number 0..1,
  "needs_review": boolean,
  "evidence": [{"field": string, "excerpt": string}]
}
Rules:
- Never invent companies, roles, requisition IDs, or dates. Use empty string/null.
- Copy exact short excerpts (<=200 chars) from the email as evidence.
- Confirmations of receipt are "application_confirmation"; pure acknowledgments with no role are "acknowledgment".
- Newsletters, marketing, and unrelated mail are "unrelated".
- Set needs_review=true when evidence is ambiguous or matching is uncertain.
- Make sure the response is valid json."""
)

REQUIREMENTS_SYSTEM = """You extract job requirements from job-posting text.
Return one JSON object only:
{
  "responsibilities": [string],
  "required_skills": [string],
  "preferred_skills": [string],
  "qualifications": [string],
  "experience": string,
  "education": string,
  "location": string,
  "work_arrangement": string,
  "employment_type": string,
  "compensation": string,
  "benefits": [string],
  "evidence": [{"field": string, "excerpt": string}]
}
Rules:
- Only use information explicitly present in the text. Use empty values otherwise.
- Do not summarise away specifics; keep each item short and faithful.
- Make sure the response is valid json."""


class InvalidExtraction(ValueError):
    """Raised when the model returns a structurally invalid response."""


def _iso_or_none(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _date_or_none(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()[:10]
    try:
        return dt.date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


_QUOTE_MARKERS = (
    re.compile(r"^On .{5,80} wrote:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^From: .+$", re.MULTILINE),
)


def strip_quoted_reply(text: str) -> str:
    """Remove quoted reply history and inline quotes before sending to AI."""
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        match = marker.search(text)
        if match and match.start() < cut:
            cut = match.start()
    text = text[:cut]
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


@dataclass
class EmailExtraction:
    event_type: str
    company: str = ""
    role: str = ""
    requisition_id: str = ""
    event_date: str | None = None
    interview_start: str | None = None
    location: str = ""
    links: list[str] = field(default_factory=list)
    contacts: list[dict[str, str]] = field(default_factory=list)
    recommended_action: str = "none"
    confidence: float = 0.0
    needs_review: bool = True
    evidence: list[dict[str, str]] = field(default_factory=list)
    cost_usd: float = 0.0
    model: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _email_prompt(message: Message) -> str:
    h = message.headers
    body = strip_quoted_reply(message.body_text)
    return (
        f"From: {h.get('From', '')}\n"
        f"To: {h.get('To', '')}\n"
        f"Subject: {h.get('Subject', '')}\n"
        f"Date: {h.get('Date', '')}\n"
        f"Thread-ID: {message.thread_id}\n\n"
        f"{body[:8000]}"
    )


def extract_email(client: DeepSeekClient, message: Message) -> EmailExtraction:
    result: AiResult = client.complete(
        EMAIL_SYSTEM, _email_prompt(message), json_mode=True, temperature=0.0
    )
    return parse_email_result(result)


def parse_email_result(result: AiResult) -> EmailExtraction:
    parsed = result.parsed
    if not parsed:
        raise InvalidExtraction("Model did not return a JSON object.")
    event_type = str(parsed.get("event_type", "unknown")).strip()
    if event_type not in EVENT_TYPES:
        if event_type.lower() in EVENT_TYPES:
            event_type = event_type.lower()
        else:
            raise InvalidExtraction(f"Unknown event_type: {event_type!r}")

    contacts: list[dict[str, str]] = []
    raw_contacts = parsed.get("contacts")
    if isinstance(raw_contacts, list):
        for item in raw_contacts:
            if isinstance(item, dict):
                contacts.append(
                    {
                        "name": str(item.get("name", "")).strip(),
                        "email": str(item.get("email", "")).strip(),
                        "role": str(item.get("role", "")).strip(),
                    }
                )

    evidence: list[dict[str, str]] = []
    raw_evidence = parsed.get("evidence")
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            if isinstance(item, dict) and item.get("field"):
                evidence.append(
                    {
                        "field": str(item["field"]).strip(),
                        "excerpt": str(item.get("excerpt", ""))[:200],
                    }
                )

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    action = str(parsed.get("recommended_action", "none")).strip()
    if action not in RECOMMEND_ACTIONS:
        action = "none"

    return EmailExtraction(
        event_type=event_type,
        company=str(parsed.get("company", "") or "").strip(),
        role=str(parsed.get("role", "") or "").strip(),
        requisition_id=str(parsed.get("requisition_id", "") or "").strip(),
        event_date=_date_or_none(parsed.get("event_date")),
        interview_start=_iso_or_none(parsed.get("interview_start")),
        location=str(parsed.get("location", "") or "").strip(),
        links=[u for u in _str_list(parsed.get("links")) if u.startswith("http")],
        contacts=contacts,
        recommended_action=action,
        confidence=confidence,
        needs_review=bool(parsed.get("needs_review", True)),
        evidence=evidence,
        cost_usd=result.cost_usd,
        model=result.model,
    )


@dataclass
class RequirementsExtraction:
    responsibilities: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    qualifications: list[str] = field(default_factory=list)
    experience: str = ""
    education: str = ""
    location: str = ""
    work_arrangement: str = ""
    employment_type: str = ""
    compensation: str = ""
    benefits: list[str] = field(default_factory=list)
    evidence: list[dict[str, str]] = field(default_factory=list)
    cost_usd: float = 0.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def extract_requirements(
    client: DeepSeekClient, posting_text: str, *, max_chars: int = 12000
) -> RequirementsExtraction:
    result = client.complete(
        REQUIREMENTS_SYSTEM,
        posting_text[:max_chars],
        json_mode=True,
        temperature=0.0,
    )
    parsed = result.parsed
    if not parsed:
        raise InvalidExtraction("Model did not return a JSON object.")
    evidence = []
    raw = parsed.get("evidence")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("field"):
                evidence.append(
                    {
                        "field": str(item["field"]),
                        "excerpt": str(item.get("excerpt", ""))[:200],
                    }
                )
    return RequirementsExtraction(
        responsibilities=_str_list(parsed.get("responsibilities")),
        required_skills=_str_list(parsed.get("required_skills")),
        preferred_skills=_str_list(parsed.get("preferred_skills")),
        qualifications=_str_list(parsed.get("qualifications")),
        experience=str(parsed.get("experience", "") or "").strip(),
        education=str(parsed.get("education", "") or "").strip(),
        location=str(parsed.get("location", "") or "").strip(),
        work_arrangement=str(parsed.get("work_arrangement", "") or "").strip(),
        employment_type=str(parsed.get("employment_type", "") or "").strip(),
        compensation=str(parsed.get("compensation", "") or "").strip(),
        benefits=_str_list(parsed.get("benefits")),
        evidence=evidence,
        cost_usd=result.cost_usd,
    )


def normalize_company(name: str) -> str:
    """Loose company-key normalisation for matching (not a unique key by itself)."""
    text = name.lower().strip()
    text = re.sub(r"\b(inc|llc|ltd|corp|corporation|company|co)\b\.?", "", text)
    return re.sub(r"[^a-z0-9]+", "", text)
