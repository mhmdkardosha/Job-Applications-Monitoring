"""Reporting calculations (plan §6).

Distinguishes current-stage counts from historical milestones. Interview and
offer rates use the same denominator: submitted applications in the selected
application-date cohort. Acknowledgments are excluded from response times.
Sample sizes and pending outcomes are surfaced; no causal claims are made.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from .models import Application, EventType, InterviewOutcome, Stage

SUBMITTED_STAGES = {
    Stage.APPLIED,
    Stage.SCREENING,
    Stage.INTERVIEWING,
    Stage.OFFER,
    Stage.ACCEPTED,
    Stage.REJECTED,
    Stage.WITHDRAWN,
}
INTERVIEW_STAGES = {Stage.INTERVIEWING, Stage.OFFER, Stage.ACCEPTED}
OFFER_STAGES = {Stage.OFFER, Stage.ACCEPTED}
INTERVIEW_EVENT_TYPES = {
    EventType.INTERVIEW_INVITATION,
    EventType.INTERVIEW_RESCHEDULE,
    EventType.INTERVIEW_CANCELLATION,
}
# Events that count as a substantive response (acknowledgments excluded).
SUBSTANTIVE_EVENT_TYPES = {
    EventType.RECRUITER_CONTACT,
    EventType.ASSESSMENT,
    EventType.INTERVIEW_INVITATION,
    EventType.INTERVIEW_RESCHEDULE,
    EventType.OFFER,
    EventType.REJECTION,
}
PENDING_STAGES = {Stage.APPLIED, Stage.SCREENING, Stage.INTERVIEWING}


def is_submitted(application: Application) -> bool:
    return application.stage in SUBMITTED_STAGES


def ever_interviewed(application: Application) -> bool:
    if application.stage in INTERVIEW_STAGES:
        return True
    if any(e.event_type in INTERVIEW_EVENT_TYPES for e in application.events.all()):
        return True
    return any(i.outcome != InterviewOutcome.CANCELLED for i in application.interviews.all())


def ever_offered(application: Application) -> bool:
    if application.stage in OFFER_STAGES:
        return True
    return any(e.event_type == EventType.OFFER for e in application.events.all())


@dataclass
class Rate:
    numerator: int
    denominator: int

    @property
    def pct(self) -> float | None:
        return (self.numerator / self.denominator * 100) if self.denominator else None


@dataclass
class Report:
    total: int = 0
    submitted: int = 0
    pending: int = 0
    stage_counts: list[tuple[str, int]] = field(default_factory=list)
    over_time: list[tuple[str, int]] = field(default_factory=list)
    interview_rate: Rate = field(default_factory=lambda: Rate(0, 0))
    offer_rate: Rate = field(default_factory=lambda: Rate(0, 0))
    response_median_days: float | None = None
    response_sample: int = 0
    response_pending: int = 0
    by_source: list[dict[str, Any]] = field(default_factory=list)
    by_resume: list[dict[str, Any]] = field(default_factory=list)


def _prep(queryset) -> list[Application]:
    return list(queryset.prefetch_related("events", "interviews", "documents").distinct())


def stage_distribution(applications: list[Application]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for application in applications:
        counts[application.stage] = counts.get(application.stage, 0) + 1
    order = [choice.value for choice in Stage]
    return [(Stage(value).label, counts[value]) for value in order if counts.get(value)]


def applications_over_time(applications: list[Application]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for application in applications:
        if not application.application_date:
            continue
        key = application.application_date.strftime("%Y-%m")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items())


def response_time(applications: list[Application]) -> tuple[float | None, int, int]:
    """Median days to first substantive response, plus sample and pending counts."""
    durations: list[int] = []
    pending = 0
    for application in applications:
        if not application.application_date:
            continue
        substantive = [
            event
            for event in application.events.all()
            if event.event_type in SUBSTANTIVE_EVENT_TYPES
        ]
        if substantive:
            first = min(substantive, key=lambda e: e.occurred_at)
            delta = (first.occurred_at.date() - application.application_date).days
            if delta >= 0:
                durations.append(delta)
        elif application.stage in PENDING_STAGES:
            pending += 1
    median = statistics.median(durations) if durations else None
    return median, len(durations), pending


def _group_counts(applications: list[Application], key_fn) -> list[dict[str, Any]]:
    groups: dict[str, list[Application]] = {}
    for application in applications:
        groups.setdefault(key_fn(application), []).append(application)
    rows = []
    for key, group in sorted(groups.items()):
        submitted = [a for a in group if is_submitted(a)]
        rows.append(
            {
                "key": key,
                "total": len(group),
                "submitted": len(submitted),
                "interviewed": len([a for a in submitted if ever_interviewed(a)]),
                "offered": len([a for a in submitted if ever_offered(a)]),
            }
        )
    return rows


def build_report(queryset=None) -> Report:
    if queryset is None:
        queryset = Application.objects.filter(is_archived=False)
    applications = _prep(queryset)
    submitted = [a for a in applications if is_submitted(a)]

    report = Report(
        total=len(applications),
        submitted=len(submitted),
        pending=len([a for a in applications if a.stage in PENDING_STAGES]),
        stage_counts=stage_distribution(applications),
        over_time=applications_over_time(applications),
    )
    report.interview_rate = Rate(len([a for a in submitted if ever_interviewed(a)]), len(submitted))
    report.offer_rate = Rate(len([a for a in submitted if ever_offered(a)]), len(submitted))
    median, sample, pending = response_time(submitted)
    report.response_median_days = median
    report.response_sample = sample
    report.response_pending = pending
    report.by_source = _group_counts(applications, lambda a: a.get_source_display())
    report.by_resume = _group_counts(
        applications,
        lambda a: (
            ", ".join(sorted(d.label for d in a.documents.all() if d.kind == "resume"))
            or "(no resume linked)"
        ),
    )
    return report
