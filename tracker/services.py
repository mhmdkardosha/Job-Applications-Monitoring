"""Domain services: stage/history recording, document storage, snapshots."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any

from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone

from .models import (
    Application,
    ApplicationEvent,
    Document,
    DocumentKind,
    EventActor,
    EventType,
    PostingSnapshot,
    Source,
    Stage,
)


def record_event(
    application: Application,
    *,
    event_type: str,
    source: str = Source.MANUAL,
    actor: str = EventActor.USER,
    email=None,
    summary: str = "",
    details: dict[str, Any] | None = None,
    confidence: float | None = None,
    is_correction: bool = False,
    dedupe_key: str | None = None,
    occurred_at=None,
) -> ApplicationEvent:
    """Create an event, honouring an idempotency key."""
    values = {
        "application": application,
        "event_type": event_type,
        "source": source,
        "actor": actor,
        "email": email,
        "summary": summary[:300],
        "details": details or {},
        "confidence": confidence,
        "is_correction": is_correction,
        "occurred_at": occurred_at or timezone.now(),
    }
    if dedupe_key:
        event, _ = ApplicationEvent.objects.get_or_create(
            dedupe_key=dedupe_key,
            defaults=values,
        )
        return event
    return ApplicationEvent.objects.create(**values)


@transaction.atomic
def set_stage(
    application: Application,
    new_stage: str,
    *,
    actor: str = EventActor.USER,
    source: str = Source.MANUAL,
    summary: str = "",
    occurred_at=None,
    dedupe_key: str | None = None,
) -> ApplicationEvent | None:
    """Change stage and record history. No-op (and no event) if unchanged."""
    old_stage = application.stage
    if old_stage == new_stage:
        return None
    application.stage = new_stage
    update_fields = ["stage", "updated_at"]
    if actor == EventActor.USER:
        overrides = set(application.manual_override_fields or [])
        overrides.add("stage")
        application.manual_override_fields = sorted(overrides)
        update_fields.append("manual_override_fields")
    application.save(update_fields=update_fields)
    event_type = EventType.STAGE_CHANGE
    if new_stage in (Stage.REJECTED,):
        event_type = EventType.REJECTION
    elif new_stage == Stage.WITHDRAWN:
        event_type = EventType.WITHDRAWAL
    elif new_stage == Stage.OFFER:
        event_type = EventType.OFFER
    return record_event(
        application,
        event_type=event_type,
        source=source,
        actor=actor,
        summary=summary or f"{old_stage} → {new_stage}",
        details={"from": old_stage, "to": new_stage},
        occurred_at=occurred_at,
        dedupe_key=dedupe_key,
    )


def _hash_file(upload: UploadedFile) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    for chunk in upload.chunks():
        digest.update(chunk)
        size += len(chunk)
    upload.seek(0)
    return digest.hexdigest(), size


@transaction.atomic
def store_document(
    upload: UploadedFile,
    *,
    kind: str = DocumentKind.OTHER,
    label: str = "",
    applications: Iterable[Application] | None = None,
) -> tuple[Document, bool]:
    """Store an uploaded file, deduplicating by SHA-256. Returns (doc, created)."""
    sha256, size = _hash_file(upload)
    existing = Document.objects.filter(sha256=sha256).first()
    if existing is not None:
        if applications:
            existing.applications.add(*applications)
        return existing, False

    document = Document.objects.create(
        file=upload,
        kind=kind,
        label=label or upload.name,
        original_filename=upload.name,
        sha256=sha256,
        size=size,
        mime_type=getattr(upload, "content_type", "") or "",
    )
    if applications:
        document.applications.add(*applications)
    return document, True


def create_snapshot(
    application: Application | None,
    data: dict[str, Any],
) -> PostingSnapshot:
    """Create the next immutable snapshot version from extracted data."""
    version = 1
    if application is not None:
        last = PostingSnapshot.objects.filter(application=application).order_by("-version").first()
        version = (last.version + 1) if last else 1
    requirements = data.get("requirements") or {}
    return PostingSnapshot.objects.create(
        application=application,
        version=version,
        url=data.get("url", ""),
        resolved_url=data.get("resolved_url", ""),
        source=data.get("source", ""),
        title=data.get("title", ""),
        company=data.get("company", ""),
        job_identifier=data.get("job_identifier", ""),
        description=data.get("description", ""),
        requirements=requirements,
        capture_state=data.get("capture_state", "text"),
        unknown_fields=data.get("unknown_fields", []),
        error=data.get("error", ""),
    )
