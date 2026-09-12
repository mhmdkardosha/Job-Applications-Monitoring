"""Posting archive service (plan §5).

Stores immutable snapshots from the browser extension or from fetching public
job links found in emails. JSON-LD structured fields take precedence over AI
enrichment; unknown values stay unknown.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from .integrations.capture import capture as fetch_capture
from .integrations.capture import is_probable_job_url
from .integrations.extract import extract_requirements
from .models import (
    Application,
    Email,
    EmploymentType,
    EventActor,
    EventType,
    PostingSnapshot,
    Source,
    Stage,
    WorkArrangement,
)
from .services import create_snapshot, record_event
from .sync import get_ai_client

logger = logging.getLogger("tracker.capture")

SCALAR_REQUIREMENT_FIELDS = (
    "experience",
    "education",
    "location",
    "work_arrangement",
    "employment_type",
    "compensation",
)
LIST_REQUIREMENT_FIELDS = (
    "responsibilities",
    "required_skills",
    "preferred_skills",
    "qualifications",
    "benefits",
)


def _requirements_from_structured(data: dict[str, Any]) -> dict[str, Any]:
    req = {key: data.get(key) or "" for key in SCALAR_REQUIREMENT_FIELDS}
    for key in LIST_REQUIREMENT_FIELDS:
        value = data.get(key)
        req[key] = value if isinstance(value, list) else [value] if isinstance(value, str) and value.strip() else []
    req["evidence"] = data.get("evidence") or []
    return req


def _merge_requirements(structured: dict, enrichment: dict, description: str) -> dict:
    merged = {**structured, **enrichment}
    for key in (*SCALAR_REQUIREMENT_FIELDS, *LIST_REQUIREMENT_FIELDS):
        if structured.get(key):
            merged[key] = structured[key]
    source = " ".join(description.casefold().split())
    for key in ("responsibilities", "required_skills"):
        items = list(structured.get(key) or [])
        seen = {" ".join(item.casefold().split()) for item in items}
        for item in enrichment.get(key) or []:
            normalized = " ".join(item.casefold().split())
            if normalized and normalized in source and normalized not in seen:
                items.append(item)
                seen.add(normalized)
        merged[key] = items
    return merged


def _map_work_arrangement(value: str) -> str:
    text = (value or "").lower()
    if "remote" in text:
        return WorkArrangement.REMOTE
    if "hybrid" in text:
        return WorkArrangement.HYBRID
    if "on-site" in text or "onsite" in text:
        return WorkArrangement.ONSITE
    return WorkArrangement.UNKNOWN


def _map_employment_type(value: str) -> str:
    text = (value or "").lower()
    if "full" in text:
        return EmploymentType.FULL_TIME
    if "part" in text:
        return EmploymentType.PART_TIME
    if "contract" in text:
        return EmploymentType.CONTRACT
    if "intern" in text:
        return EmploymentType.INTERNSHIP
    if "tempor" in text:
        return EmploymentType.TEMPORARY
    return EmploymentType.UNKNOWN


def _same_url(left: str, right: str) -> bool:
    return left.rstrip("/").lower() == right.rstrip("/").lower()


def find_application_for(url: str = "", requisition_id: str = "") -> Application | None:
    """Strict link target: exact job URL or requisition ID."""
    if url:
        for application in Application.objects.exclude(job_url="").only("id", "job_url"):
            if _same_url(application.job_url, url):
                return application
    if requisition_id:
        match = Application.objects.filter(requisition_id__iexact=requisition_id.strip()).first()
        if match:
            return match
    return None


@transaction.atomic
def _persist_capture(
    data: dict[str, Any],
    *,
    application: Application | None,
    create_if_new: bool,
    source: str,
    url: str,
    description: str,
    requirements: dict[str, Any],
) -> tuple[PostingSnapshot, Application | None, bool]:
    created_application = False
    if application is None:
        application = find_application_for(url, data.get("job_identifier", ""))
    if application is None and create_if_new:
        company = (data.get("company") or "").strip()
        title = (data.get("title") or "").strip()
        if company and title:
            application = Application.objects.create(
                company=company,
                title=title,
                job_url=url,
                requisition_id=(data.get("job_identifier") or "")[:120],
                location=(data.get("location") or "")[:200],
                work_arrangement=_map_work_arrangement(data.get("work_arrangement", "")),
                employment_type=_map_employment_type(data.get("employment_type", "")),
                source=source,
                stage=Stage.SAVED,
            )
            created_application = True

    snapshot = create_snapshot(
        application,
        {
            "url": data.get("url", url),
            "resolved_url": data.get("resolved_url", url),
            "source": source,
            "title": data.get("title", ""),
            "company": data.get("company", ""),
            "job_identifier": data.get("job_identifier", ""),
            "description": description,
            "requirements": requirements,
            "capture_state": data.get("capture_state", "text"),
            "unknown_fields": data.get("unknown_fields", []),
            "error": data.get("error", ""),
        },
    )
    if application is not None:
        record_event(
            application,
            event_type=EventType.CUSTOM,
            source=source,
            actor=EventActor.USER,
            summary=f"Posting archived (v{snapshot.version}): {snapshot.title or url}",
            details={"snapshot_id": snapshot.pk, "url": url},
        )
    return snapshot, application, created_application


def store_capture(
    data: dict[str, Any],
    *,
    application: Application | None = None,
    create_if_new: bool = False,
    source: str = Source.BROWSER,
    run_ai: bool = True,
    client=None,
) -> tuple[PostingSnapshot, Application | None, bool]:
    """Persist a snapshot; optionally create an application. Returns (snap, app, created)."""
    url = (data.get("resolved_url") or data.get("url") or "").strip()
    structured = _requirements_from_structured(data)
    description = (data.get("description") or "").strip()
    capture_state = data.get("capture_state", "text")
    usable = capture_state in ("structured", "text")

    if not usable:
        # Do not archive failed/partial pages or record events for them.
        return (
            PostingSnapshot(
                url=data.get("url", url),
                resolved_url=data.get("resolved_url", url),
                source=source,
                title=data.get("title", ""),
                company=data.get("company", ""),
                job_identifier=data.get("job_identifier", ""),
                description=description,
                requirements=structured,
                capture_state=capture_state,
                unknown_fields=data.get("unknown_fields", []),
                error=data.get("error", "") or "Page did not look like a job posting.",
            ),
            application,
            False,
        )

    requirements = structured
    if run_ai and description:
        try:
            client = client or get_ai_client()
            enrichment = extract_requirements(client, description)
            requirements = _merge_requirements(structured, enrichment.to_json(), description)
        except Exception as exc:  # noqa: BLE001 - keep the snapshot even if AI fails
            logger.warning("requirements extraction failed: %s", exc)

    return _persist_capture(
        data,
        application=application,
        create_if_new=create_if_new,
        source=source,
        url=url,
        description=description,
        requirements=requirements,
    )


def capture_url(
    url: str,
    *,
    application: Application | None = None,
    create_if_new: bool = False,
    source: str = "email-link",
    run_ai: bool = True,
    client=None,
) -> tuple[PostingSnapshot, Application | None, bool]:
    snap = fetch_capture(url, source=source)
    data = snap.to_json()
    data["requirements"] = _requirements_from_structured(data)
    return store_capture(
        data,
        application=application,
        create_if_new=create_if_new,
        source=source,
        run_ai=run_ai,
        client=client,
    )


def _stored_urls() -> set[str]:
    urls: set[str] = set()
    for url, resolved in PostingSnapshot.objects.values_list("url", "resolved_url"):
        if url:
            urls.add(url.rstrip("/").lower())
        if resolved:
            urls.add(resolved.rstrip("/").lower())
    return urls


def collect_email_job_links() -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    stored = _stored_urls()
    for extracted in Email.objects.exclude(extracted={}).values_list("extracted", flat=True):
        for link in (extracted or {}).get("links", []) or []:
            if not isinstance(link, str) or link in seen:
                continue
            if not is_probable_job_url(link):
                continue
            if link.rstrip("/").lower() in stored:
                continue
            seen.add(link)
            links.append(link)
    return links


def capture_links_from_emails(
    *,
    limit: int = 10,
    create_if_new: bool = True,
    run_ai: bool = True,
    client=None,
) -> dict[str, Any]:
    available_links = collect_email_job_links()
    links = available_links[:limit]
    report = {
        "found": len(available_links),
        "attempted": 0,
        "captured": 0,
        "created_applications": 0,
        "failed": 0,
        "partial": 0,
        "snapshots": [],
    }
    for link in links:
        report["attempted"] += 1
        try:
            snapshot, application, created = capture_url(
                link,
                create_if_new=create_if_new,
                source=Source.GMAIL,
                run_ai=run_ai,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001
            report["failed"] += 1
            logger.warning("capture failed for %s: %s", link, exc)
            continue
        if snapshot.capture_state == "failed":
            report["failed"] += 1
            continue
        if snapshot.capture_state == "partial":
            report["partial"] += 1
            continue
        report["captured"] += 1
        report["created_applications"] += int(created)
        report["snapshots"].append({"id": snapshot.pk, "title": snapshot.title, "url": link})
    return report
