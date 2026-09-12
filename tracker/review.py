"""Resolve Review Inbox items: confirm, link, create, or ignore."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .integrations.extract import EmailExtraction
from .matching import is_regression, target_stage
from .models import (
    Application,
    EmailProcessState,
    EventActor,
    ReviewItem,
    ReviewStatus,
    Source,
)
from .services import record_event, set_stage
from .sync import _apply_related_records, _create_application_from_email


class ReviewError(ValueError):
    pass


def pending_items():
    return (
        ReviewItem.objects.filter(status=ReviewStatus.PENDING)
        .select_related("email", "email__account")
        .order_by("created_at")
    )


def extraction_from(item: ReviewItem) -> EmailExtraction:
    data = dict(item.suggestion or {})
    allowed = set(EmailExtraction.__dataclass_fields__)
    return EmailExtraction(**{k: v for k, v in data.items() if k in allowed})


def _link_and_apply(item: ReviewItem, application: Application, extraction: EmailExtraction):
    email = item.email
    email.application = application
    email.process_state = EmailProcessState.CLASSIFIED
    email.save(update_fields=["application", "process_state", "updated_at"])

    target = target_stage(extraction.event_type)
    if (
        target
        and "stage" not in (application.manual_override_fields or [])
        and not is_regression(application.stage, target)
    ):
        set_stage(
            application,
            target,
            actor=EventActor.SYSTEM,
            source=Source.GMAIL,
            summary=f"Confirmed from review: {extraction.event_type}",
            occurred_at=email.received_at,
            dedupe_key=f"review-stage:{email.pk}",
        )
    record_event(
        application,
        event_type=extraction.event_type,
        source=Source.GMAIL,
        actor=EventActor.USER,
        email=email,
        summary=f"Confirmed from review inbox ({item.get_kind_display()})",
        details={"review_item": item.pk, "reason": item.note},
        confidence=extraction.confidence,
        is_correction=True,
        dedupe_key=f"review:{email.pk}",
        occurred_at=email.received_at,
    )
    _apply_related_records(application, extraction)
    return application


@transaction.atomic
def resolve(
    item: ReviewItem,
    *,
    action: str,
    application: Application | None = None,
) -> Application | None:
    if item.status != ReviewStatus.PENDING:
        raise ReviewError("This item was already resolved.")
    extraction = extraction_from(item)

    if action == "ignore":
        item.email.process_state = EmailProcessState.IGNORED
        item.email.save(update_fields=["process_state", "updated_at"])
        item.status = ReviewStatus.IGNORED
        item.resolved_action = "ignored"
        item.resolved_at = timezone.now()
        item.save()
        return None

    if action == "create":
        if not extraction.company or not extraction.role:
            raise ReviewError("Company and role are required to create an application.")
        linked = _create_application_from_email(item.email, extraction)
        item.email.process_state = EmailProcessState.CLASSIFIED
        item.email.save(update_fields=["process_state", "updated_at"])
        item.status = ReviewStatus.CREATED
        item.resolved_action = "created"
        item.resolved_application = linked
        item.resolved_at = timezone.now()
        item.save()
        return linked

    if action == "accept":
        strong_ids = [
            c["application_id"]
            for c in item.candidates
            if c.get("strong") and c.get("application_id")
        ]
        if len(strong_ids) != 1:
            raise ReviewError("Choose one application explicitly; this item is ambiguous.")
        application = Application.objects.filter(pk=strong_ids[0]).first()
        if application is None:
            raise ReviewError("The suggested application no longer exists; choose another.")
        action = "link"

    if action == "link":
        if application is None:
            raise ReviewError("Choose an application to link.")
        _link_and_apply(item, application, extraction)
        item.status = ReviewStatus.LINKED
        item.resolved_action = "linked"
        item.resolved_application = application
        item.resolved_at = timezone.now()
        item.save()
        return application

    raise ReviewError(f"Unknown action: {action}")
