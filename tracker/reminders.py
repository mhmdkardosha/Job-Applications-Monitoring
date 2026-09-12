"""Follow-up suggestions and copyable drafts (plan §6).

Reminder timing is a user setting, not a claim about employer expectations.
The tracker drafts text to copy; it never sends email.
"""

from __future__ import annotations

import datetime as dt

from django.utils import timezone

from .models import Application, Stage, Task, TaskStatus

FOLLOW_UP_STAGES = {Stage.APPLIED, Stage.SCREENING}


def follow_up_candidates(config, *, limit: int = 20) -> list[Application]:
    today = timezone.localdate()
    cutoff = today - dt.timedelta(days=config.follow_up_days)
    candidates = (
        Application.objects.filter(
            is_archived=False,
            stage__in=FOLLOW_UP_STAGES,
            application_date__isnull=False,
            application_date__lte=cutoff,
        )
        .exclude(tasks__status__in=[TaskStatus.OPEN, TaskStatus.SNOOZED])
        .distinct()
        .order_by("application_date")
    )
    return list(candidates[:limit])


def follow_up_draft(application: Application) -> str:
    applied = (
        application.application_date.strftime("%d %B %Y")
        if application.application_date
        else "recently"
    )
    contact = application.contacts.first()
    greeting = f"Hi {contact.name.split()[0]}," if contact and contact.name else "Hi,"
    return (
        f"Subject: Following up on my application - {application.title}\n\n"
        f"{greeting}\n\n"
        f"I applied for the {application.title} role at {application.company} "
        f"on {applied} and wanted to follow up on the status of my application. "
        f"I remain very interested in the position and would be glad to provide "
        f"any additional information.\n\n"
        f"Thank you for your time.\n\n"
        f"Best regards,\n"
    )


def create_follow_up_task(application: Application, config) -> Task:
    existing = application.tasks.filter(status__in=[TaskStatus.OPEN, TaskStatus.SNOOZED]).first()
    if existing:
        return existing
    due = timezone.now() + dt.timedelta(days=config.follow_up_days)
    return Task.objects.create(
        application=application,
        title=f"Follow up on {application.title} at {application.company}",
        due_at=due,
    )
