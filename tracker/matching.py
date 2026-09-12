"""Email → application matching and update decisions (plan §4.5-4.7).

Strict matching policy (user-selected): automatically link only on
requisition ID, exact job URL, or thread continuity. Company + title is offered
in the Review Inbox as a suggestion but never applied automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .integrations.extract import EmailExtraction, normalize_company
from .models import Application, Email, EventType

# Confidence floors for unattended writes.
MIN_UPDATE_CONFIDENCE = 0.60
MIN_CREATE_CONFIDENCE = 0.70

STAGE_RANK = {
    "saved": 0,
    "preparing": 1,
    "applied": 2,
    "screening": 3,
    "interviewing": 4,
    "offer": 5,
    "accepted": 6,
    "rejected": 7,
    "withdrawn": 7,
}

# event type → stage it implies (None means "record the event, no stage move")
EVENT_TARGET_STAGE = {
    EventType.APPLICATION_CONFIRMATION: "applied",
    EventType.ACKNOWLEDGMENT: "applied",
    EventType.ASSESSMENT: "screening",
    EventType.INTERVIEW_INVITATION: "interviewing",
    EventType.INTERVIEW_RESCHEDULE: "interviewing",
    EventType.INTERVIEW_CANCELLATION: None,
    EventType.OFFER: "offer",
    EventType.REJECTION: "rejected",
    EventType.WITHDRAWAL: "withdrawn",
    EventType.RECRUITER_CONTACT: None,
}


class Action:
    IGNORE = "ignore"
    REVIEW = "review"
    AUTO_CREATE = "auto_create"
    AUTO_UPDATE = "auto_update"


@dataclass
class Match:
    application: Application
    reason: str
    strong: bool = True


@dataclass
class Decision:
    action: str
    reason: str
    application: Application | None = None
    target_stage: str | None = None
    matches: list[Match] = field(default_factory=list)
    weak_candidates: list[Match] = field(default_factory=list)
    review_kind: str = ""
    apply_now: bool = True  # False ⇒ record history only, do not move stage


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _role_key(title: str, company: str) -> str:
    role = _norm(title).replace("’", "'")
    prefix = "job application for "
    suffix = f" at {_norm(company)} - career's page"
    if role.startswith(prefix) and role.endswith(suffix):
        return role[len(prefix) : -len(suffix)]
    return role


def _job_url_key(value: str) -> str:
    normalized = _norm(value).rstrip("/")
    if not normalized:
        return ""
    last_path_part = urlsplit(normalized).path.rstrip("/").rsplit("/", 1)[-1]
    if last_path_part in {"login", "signin", "sign-in", "candidate-home"}:
        return ""
    return normalized


def target_stage(event_type: str) -> str | None:
    return EVENT_TARGET_STAGE.get(event_type)


def is_regression(current: str, target: str) -> bool:
    return STAGE_RANK.get(target, 0) < STAGE_RANK.get(current, 0)


def find_matches(extraction: EmailExtraction, email: Email) -> list[Match]:
    """Strong matches only: requisition ID, exact job URL, thread continuity."""
    matches: dict[int, Match] = {}

    if extraction.requisition_id:
        qs = Application.objects.filter(requisition_id__iexact=extraction.requisition_id.strip())
        for app in qs:
            matches.setdefault(app.pk, Match(app, f"requisition {extraction.requisition_id}"))

    links = {_job_url_key(url) for url in extraction.links if _job_url_key(url)}
    if links:
        for app in Application.objects.exclude(job_url="").only("id", "job_url"):
            if _job_url_key(app.job_url) in links:
                matches.setdefault(app.pk, Match(app, "job URL"))

    if email.thread_id:
        thread_apps = (
            Email.objects.filter(
                account_id=email.account_id,
                thread_id=email.thread_id,
                application__isnull=False,
            )
            .exclude(pk=email.pk)
            .values_list("application_id", flat=True)
            .distinct()
        )
        for app in Application.objects.filter(pk__in=list(thread_apps)):
            matches.setdefault(app.pk, Match(app, "thread continuity"))

    return list(matches.values())


def weak_candidates(extraction: EmailExtraction) -> list[Match]:
    """Company + role suggestions, including known job-board title wrappers."""
    if not extraction.company or not extraction.role:
        return []
    company_key = normalize_company(extraction.company)
    role_key = _role_key(extraction.role, extraction.company)
    suggestions: list[Match] = []
    for app in Application.objects.filter(title__icontains=extraction.role.strip()):
        if _role_key(app.title, app.company) == role_key and normalize_company(app.company) == company_key:
            suggestions.append(Match(app, "company + title", strong=False))
    return suggestions


def decide(extraction: EmailExtraction, email: Email) -> Decision:
    if extraction.event_type == "unrelated":
        return Decision(Action.IGNORE, "unrelated message")

    strong = find_matches(extraction, email)
    weak = weak_candidates(extraction)
    target = target_stage(extraction.event_type)

    if len({m.application.pk for m in strong}) > 1:
        return Decision(
            Action.REVIEW,
            "multiple strong matches",
            matches=strong,
            weak_candidates=weak,
            review_kind="ambiguous",
        )

    if strong:
        match = strong[0]
        app = match.application
        if extraction.needs_review:
            return Decision(
                Action.REVIEW,
                "extraction flagged uncertainty",
                application=app,
                matches=strong,
                weak_candidates=weak,
                review_kind="needs_review",
            )
        if extraction.event_type in {
            EventType.INTERVIEW_RESCHEDULE,
            EventType.INTERVIEW_CANCELLATION,
        } or (
            extraction.event_type == EventType.INTERVIEW_INVITATION
            and not extraction.interview_start
        ):
            return Decision(
                Action.REVIEW,
                "interview schedule needs confirmation",
                application=app,
                matches=strong,
                weak_candidates=weak,
                review_kind="needs_review",
            )
        if extraction.confidence and extraction.confidence < MIN_UPDATE_CONFIDENCE:
            return Decision(
                Action.REVIEW,
                "low confidence",
                application=app,
                matches=strong,
                weak_candidates=weak,
                review_kind="needs_review",
            )
        if target and "stage" in (app.manual_override_fields or []):
            return Decision(
                Action.REVIEW,
                "stage was corrected by you",
                application=app,
                matches=strong,
                weak_candidates=weak,
                review_kind="conflict",
            )
        apply_now = True
        if target and is_regression(app.stage, target):
            apply_now = False  # keep history, never regress current stage
        return Decision(
            Action.AUTO_UPDATE,
            match.reason,
            application=app,
            target_stage=target,
            matches=strong,
            weak_candidates=weak,
            apply_now=apply_now,
        )

    if (
        extraction.event_type == EventType.APPLICATION_CONFIRMATION
        and extraction.company
        and extraction.role
        and extraction.confidence >= MIN_CREATE_CONFIDENCE
        and not extraction.needs_review
    ):
        if len(weak) == 1:
            existing = weak[0].application
            apply_now = not (target and is_regression(existing.stage, target))
            return Decision(
                Action.AUTO_UPDATE,
                "identical existing application",
                application=existing,
                target_stage=target,
                apply_now=apply_now,
                weak_candidates=weak,
            )
        if weak:
            return Decision(
                Action.REVIEW,
                "multiple matching applications",
                weak_candidates=weak,
                review_kind="ambiguous",
            )
        return Decision(
            Action.AUTO_CREATE,
            "clear application confirmation",
            weak_candidates=weak,
        )

    return Decision(
        Action.REVIEW,
        "no unambiguous match",
        weak_candidates=weak,
        review_kind="unmatched",
    )
