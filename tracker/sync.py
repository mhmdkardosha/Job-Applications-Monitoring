"""Gmail sync engine (plan §4).

Manual/on-demand sync: backfill over a configurable window, history-based
incremental sync, retry via WorkItem, and routing to the Review Inbox. Strict,
forward-only application updates; ambiguous results are never applied silently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from googleapiclient.errors import HttpError

from .integrations.ai_client import DeepSeekClient, UsageLedger
from .integrations.extract import EVENT_TYPES, EmailExtraction, InvalidExtraction, extract_email
from .integrations.gmail import (
    JOB_SENDER_QUERY,
    AuthError,
    Message,
    get_message,
    get_service,
    list_message_ids,
)
from .integrations.secrets import known_gmail_accounts
from .matching import Action, decide, target_stage
from .models import (
    Application,
    AppSettings,
    Contact,
    Email,
    EmailProcessState,
    EventActor,
    EventType,
    GmailAccount,
    GmailConnectionState,
    Interview,
    ReviewItem,
    Source,
    Stage,
    WorkItem,
    WorkItemStatus,
)
from .services import record_event, set_stage

logger = logging.getLogger("tracker.sync")

SYNC_LOCK_TIMEOUT = dt.timedelta(minutes=15)
BACKOFF_BASE_SECONDS = 60
EMAIL_EXTRACTION_VERSION = 1

ProgressFn = Callable[[dict[str, Any]], None]


@dataclass
class SyncReport:
    account: str
    mode: str = "incremental"
    listed: int = 0
    candidates: int = 0
    new_emails: int = 0
    created_applications: int = 0
    updated_applications: int = 0
    events: int = 0
    review_items: int = 0
    ignored: int = 0
    errors: int = 0
    cost_usd: float = 0.0
    history_expired: bool = False
    skipped_reason: str = ""
    messages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost_usd"] = round(self.cost_usd, 6)
        return data


class SyncBusy(RuntimeError):
    pass


def get_ai_client() -> DeepSeekClient:
    config = AppSettings.load()
    ledger = UsageLedger(
        Path(settings.DATA_DIR) / "ai_usage",
        ceiling_usd=float(config.ai_monthly_ceiling_usd),
    )
    return DeepSeekClient(model=config.ai_model, base_url=settings.AI_BASE_URL, ledger=ledger)


def ensure_account(account_email: str) -> GmailAccount:
    account_email = (account_email or "").strip()
    if not account_email:
        raise ValueError("An account email is required.")
    account, _ = GmailAccount.objects.get_or_create(
        email=account_email,
        defaults={"history_days": AppSettings.load().gmail_history_days},
    )
    return account


def candidate_reason(message: Message) -> str | None:
    """Cheap second-pass filter after the Gmail query."""
    headers = {k.lower(): v for k, v in message.headers.items()}
    haystack = " ".join(
        [headers.get("subject", ""), message.snippet, message.body_text[:2000]]
    ).lower()
    sender = headers.get("from", "").lower()
    domain_hits = (
        "greenhouse",
        "ashbyhq",
        "lever.co",
        "workable",
        "smartrecruiters",
        "myworkdayjobs",
        "icims",
        "jobvite",
        "linkedin",
        "workday",
    )
    if any(domain in sender for domain in domain_hits):
        return "job-platform sender"
    keywords = (
        "your application",
        "thank you for applying",
        "interview",
        "next steps",
        "offer",
        "not moving forward",
        "recruiter",
        "candidacy",
    )
    for keyword in keywords:
        if keyword in haystack:
            return f"keyword:{keyword}"
    return None


def _message_from_email(email: Email) -> Message:
    headers = {
        "From": email.from_address,
        "To": email.to_addresses,
        "Subject": email.subject,
    }
    if email.received_at:
        headers["Date"] = email.received_at.isoformat()
    return Message(
        id=email.message_id,
        thread_id=email.thread_id,
        account=email.account.email,
        headers=headers,
        snippet=email.snippet,
        body_text=email.body_text,
        label_ids=email.label_ids,
    )


def _content_hash(message: Message) -> str:
    parts = [
        f"email-extraction-v{EMAIL_EXTRACTION_VERSION}",
        message.headers.get("From", ""),
        message.headers.get("To", ""),
        message.headers.get("Subject", ""),
        message.headers.get("Date", ""),
        message.body_text,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _cached_extraction(email: Email, content_hash: str):
    cached = (
        Email.objects.filter(content_hash=content_hash)
        .exclude(pk=email.pk)
        .exclude(extracted={})
        .values_list("extracted", flat=True)
        .first()
    )
    if not isinstance(cached, dict):
        return None
    allowed = set(EmailExtraction.__dataclass_fields__)
    try:
        extraction = EmailExtraction(
            **{key: value for key, value in cached.items() if key in allowed}
        )
    except (TypeError, ValueError):
        return None
    if extraction.event_type not in EVENT_TYPES:
        return None
    extraction.cost_usd = 0.0
    return extraction


def store_email(account: GmailAccount, message: Message) -> tuple[Email, bool]:
    received_at = None
    if message.internal_date:
        try:
            received_at = dt.datetime.fromtimestamp(int(message.internal_date) / 1000, tz=dt.UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            received_at = None
    email, created = Email.objects.get_or_create(
        account=account,
        message_id=message.id,
        defaults={
            "thread_id": message.thread_id,
            "subject": message.headers.get("Subject", ""),
            "from_address": message.headers.get("From", ""),
            "to_addresses": message.headers.get("To", ""),
            "snippet": message.snippet,
            "body_text": message.body_text,
            "label_ids": message.label_ids,
            "received_at": received_at,
        },
    )
    return email, created


def _has_newer_history(application: Application, received_at) -> bool:
    if received_at is None:
        return False
    latest = (
        application.events.order_by("-occurred_at").values_list("occurred_at", flat=True).first()
    )
    return bool(latest and received_at < latest)


def _event_dedupe_key(email: Email) -> str:
    return f"email:{email.pk}"


def _record_extraction_event(
    application: Application,
    email: Email,
    extraction,
    *,
    summary: str,
    is_correction: bool = False,
):
    return record_event(
        application,
        event_type=extraction.event_type,
        source=Source.GMAIL,
        actor=EventActor.SYSTEM,
        email=email,
        summary=summary,
        details={
            "company": extraction.company,
            "role": extraction.role,
            "requisition_id": extraction.requisition_id,
            "interview_start": extraction.interview_start,
            "recommended_action": extraction.recommended_action,
        },
        confidence=extraction.confidence,
        is_correction=is_correction,
        dedupe_key=_event_dedupe_key(email),
        occurred_at=email.received_at,
    )


def _create_application_from_email(email: Email, extraction):
    initial_stage = target_stage(extraction.event_type) or Stage.SAVED
    link = next((u for u in extraction.links if u.startswith("http")), "")
    application_date = (
        dt.date.fromisoformat(extraction.event_date) if extraction.event_date else None
    )
    if (
        application_date is None
        and email.received_at
        and extraction.event_type == EventType.APPLICATION_CONFIRMATION
    ):
        application_date = email.received_at.date()
    application = Application.objects.create(
        company=extraction.company.strip(),
        title=extraction.role.strip(),
        requisition_id=extraction.requisition_id.strip(),
        job_url=link,
        location=extraction.location.strip(),
        source=Source.GMAIL,
        stage=initial_stage,
        application_date=application_date,
    )
    email.application = application
    email.save(update_fields=["application", "updated_at"])
    _record_extraction_event(
        application,
        email,
        extraction,
        summary=f"Created from email: {extraction.event_type}",
    )
    _apply_related_records(application, extraction)
    return application


def _apply_related_records(application: Application, extraction) -> None:
    for item in extraction.contacts:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        address = str(item.get("email", "")).strip()
        role = str(item.get("role", "")).strip()
        if not name and not address:
            continue
        contact = Contact.objects.filter(email__iexact=address).first() if address else None
        if contact is None:
            contact = Contact.objects.create(
                name=name or address,
                email=address,
                role=role,
                company=application.company,
            )
        contact.applications.add(application)

    if extraction.event_type != EventType.INTERVIEW_INVITATION or not extraction.interview_start:
        return
    try:
        start = dt.datetime.fromisoformat(extraction.interview_start)
    except ValueError:
        return
    if start.tzinfo is None:
        return
    participants = [
        str(item.get("name") or item.get("email") or "").strip()
        for item in extraction.contacts
        if isinstance(item, dict) and (item.get("name") or item.get("email"))
    ]
    next_round = (application.interviews.aggregate(value=Max("round"))["value"] or 0) + 1
    Interview.objects.get_or_create(
        application=application,
        scheduled_start=start,
        defaults={"round": next_round, "participants": participants},
    )


_REVIEW_KIND = {
    "ambiguous": "ambiguous",
    "needs_review": "needs_review",
    "conflict": "conflict",
}


def _route_to_review(email: Email, extraction, decision) -> ReviewItem:
    candidates = [
        {
            "application_id": match.application_id,
            "reason": match.reason,
            "strong": match.strong,
            "label": str(match.application),
        }
        for match in [*decision.matches, *decision.weak_candidates]
    ]
    item, _ = ReviewItem.objects.update_or_create(
        email=email,
        defaults={
            "kind": _REVIEW_KIND.get(decision.review_kind, "needs_review"),
            "status": "pending",
            "suggestion": extraction.to_json(),
            "candidates": candidates,
            "note": decision.reason,
        },
    )
    email.process_state = EmailProcessState.REVIEW
    email.save(update_fields=["process_state", "updated_at"])
    return item


def apply_decision(email: Email, extraction, decision, report: SyncReport) -> None:
    application = decision.application

    if decision.action == Action.IGNORE:
        email.process_state = EmailProcessState.IGNORED
        email.save(update_fields=["process_state", "updated_at"])
        report.ignored += 1
        return

    if decision.action == Action.REVIEW:
        _route_to_review(email, extraction, decision)
        report.review_items += 1
        return

    if decision.action == Action.AUTO_CREATE:
        _create_application_from_email(email, extraction)
        report.created_applications += 1
        report.events += 1
        email.process_state = EmailProcessState.CLASSIFIED
        email.save(update_fields=["process_state", "updated_at"])
        return

    # AUTO_UPDATE
    if application is None:
        _route_to_review(email, extraction, decision)
        report.review_items += 1
        return
    email.application = application
    email.save(update_fields=["application", "updated_at"])

    apply_stage = (
        decision.target_stage
        and decision.apply_now
        and not _has_newer_history(application, email.received_at)
    )
    if apply_stage:
        set_stage(
            application,
            decision.target_stage,
            actor=EventActor.SYSTEM,
            source=Source.GMAIL,
            summary=f"From email: {extraction.event_type}",
            occurred_at=email.received_at,
            dedupe_key=f"stage:{_event_dedupe_key(email)}",
        )
        report.updated_applications += 1
    _record_extraction_event(
        application,
        email,
        extraction,
        summary=decision.reason,
    )
    _apply_related_records(application, extraction)
    report.events += 1
    email.process_state = EmailProcessState.CLASSIFIED
    email.save(update_fields=["process_state", "updated_at"])


def process_email(email: Email, client: DeepSeekClient, report: SyncReport) -> None:
    if email.process_state in (
        EmailProcessState.CLASSIFIED,
        EmailProcessState.IGNORED,
        EmailProcessState.REVIEW,
    ):
        return
    message = _message_from_email(email)
    content_hash = _content_hash(message)
    extraction = _cached_extraction(email, content_hash) or extract_email(client, message)
    report.cost_usd += extraction.cost_usd
    with transaction.atomic():
        email.extracted = extraction.to_json()
        email.content_hash = content_hash
        email.save(update_fields=["extracted", "content_hash", "updated_at"])
        decision = decide(extraction, email)
        apply_decision(email, extraction, decision, report)


def _enqueue_retry(email: Email) -> None:
    item, _ = WorkItem.objects.get_or_create(
        kind="process_email",
        payload={"email_id": email.pk},
        status=WorkItemStatus.PENDING,
    )
    item.attempts += 1
    item.last_error = email.processing_error
    if item.attempts >= item.max_attempts:
        item.status = WorkItemStatus.FAILED
    else:
        item.run_after = timezone.now() + dt.timedelta(
            seconds=BACKOFF_BASE_SECONDS * (2 ** (item.attempts - 1))
        )
    item.save()


def process_due_work(
    client: DeepSeekClient,
    report: SyncReport,
    *,
    account: GmailAccount | None = None,
    lease: dt.datetime | None = None,
    limit: int = 20,
) -> dt.datetime | None:
    due = WorkItem.objects.filter(status=WorkItemStatus.PENDING, run_after__lte=timezone.now())
    if account is not None:
        email_ids = Email.objects.filter(account=account).values_list("pk", flat=True)
        due = due.filter(payload__email_id__in=list(email_ids))
    due = due[:limit]
    for item in due:
        try:
            if item.kind != "process_email":
                continue
            email = Email.objects.filter(pk=item.payload.get("email_id")).first()
            if email is None:
                item.status = WorkItemStatus.DONE
                item.save(update_fields=["status", "updated_at"])
                continue
            try:
                process_email(email, client, report)
            except Exception as exc:  # noqa: BLE001
                email.process_state = EmailProcessState.ERROR
                email.processing_error = str(exc)[:2000]
                email.save(update_fields=["process_state", "processing_error", "updated_at"])
                _enqueue_retry(email)
                report.errors += 1
                continue
            item.status = WorkItemStatus.DONE
            item.save(update_fields=["status", "updated_at"])
        finally:
            if account is not None and lease is not None:
                lease = _heartbeat_lock(account, lease)
    return lease


def _ingest_ids(account, service, ids, client, report, progress=None, *, lease=None):
    total = len(ids)
    report.listed = total
    fetch_failures: list[str] = []
    for index, message_id in enumerate(ids, start=1):
        try:
            if Email.objects.filter(account=account, message_id=message_id).exists():
                continue
            try:
                message = get_message(service, message_id, account.email)
            except HttpError as exc:
                if getattr(exc.resp, "status", None) == 404:
                    report.errors += 1
                    logger.info("message %s disappeared before fetch", message_id)
                else:
                    fetch_failures.append(message_id)
                    report.errors += 1
                    logger.warning("fetch failed for %s: %s", message_id, exc)
                continue
            except Exception as exc:  # noqa: BLE001
                fetch_failures.append(message_id)
                report.errors += 1
                logger.warning("fetch failed for %s: %s", message_id, exc)
                continue
            reason = candidate_reason(message)
            email, created = store_email(account, message)
            if not reason:
                if created:
                    email.process_state = EmailProcessState.IGNORED
                    email.save(update_fields=["process_state", "updated_at"])
                report.ignored += 1
                continue
            report.candidates += 1
            if not created:
                continue
            report.new_emails += 1
            report.messages.append(message_id)
            try:
                process_email(email, client, report)
            except InvalidExtraction as exc:
                email.process_state = EmailProcessState.ERROR
                email.processing_error = str(exc)
                email.save(update_fields=["process_state", "processing_error", "updated_at"])
                _enqueue_retry(email)
                report.errors += 1
            except Exception as exc:  # noqa: BLE001
                email.process_state = EmailProcessState.ERROR
                email.processing_error = str(exc)[:2000]
                email.save(update_fields=["process_state", "processing_error", "updated_at"])
                _enqueue_retry(email)
                report.errors += 1
        finally:
            if progress:
                progress({"phase": "ingest", "done": index, "total": total})
            if lease is not None:
                lease = _heartbeat_lock(account, lease)
    if fetch_failures:
        raise RuntimeError("Could not fetch Gmail message(s): " + ", ".join(fetch_failures[:10]))
    return lease


def _acquire_lock(account: GmailAccount) -> dt.datetime | None:
    now = timezone.now()
    stale = now - SYNC_LOCK_TIMEOUT
    updated = (
        GmailAccount.objects.filter(pk=account.pk)
        .filter(Q(sync_started_at__isnull=True) | Q(sync_started_at__lt=stale))
        .update(sync_started_at=now)
    )
    if updated == 1:
        account.sync_started_at = now
        return now
    return None


def _heartbeat_lock(account: GmailAccount, lease: dt.datetime) -> dt.datetime:
    renewed = timezone.now()
    if not GmailAccount.objects.filter(pk=account.pk, sync_started_at=lease).update(
        sync_started_at=renewed
    ):
        raise SyncBusy("sync lock was lost")
    account.sync_started_at = renewed
    return renewed


def _release_lock(
    account: GmailAccount,
    lease: dt.datetime,
    *,
    error: str = "",
    connection_state: str | None = None,
) -> None:
    values = {
        "sync_started_at": None,
        "last_error": error,
        "connection_state": connection_state
        or (GmailConnectionState.ERROR if error else GmailConnectionState.CONNECTED),
        "updated_at": timezone.now(),
    }
    if not error:
        values["last_sync_at"] = timezone.now()
    GmailAccount.objects.filter(pk=account.pk, sync_started_at=lease).update(**values)


def _current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return str(profile.get("historyId", ""))


def _unseen_message_ids(
    account: GmailAccount,
    message_ids: list[str],
    limit: int | None,
) -> tuple[list[str], bool]:
    existing = set(Email.objects.filter(account=account).values_list("message_id", flat=True))
    unseen = [message_id for message_id in message_ids if message_id not in existing]
    return (unseen[:limit] if limit else unseen), bool(limit and len(unseen) > limit)


def sync_account(
    account: GmailAccount | None = None,
    *,
    account_email: str | None = None,
    backfill: bool = False,
    days: int | None = None,
    limit: int | None = None,
    client: DeepSeekClient | None = None,
    progress: ProgressFn | None = None,
) -> SyncReport:
    account = account or ensure_account(account_email or "")
    if not account.email:
        raise ValueError("An account email is required.")
    if days is not None and days < 1:
        raise ValueError("days must be at least 1.")
    if limit is not None and limit < 1:
        raise ValueError("limit must be at least 1.")

    report = SyncReport(account=account.email)
    lease = _acquire_lock(account)
    if lease is None:
        report.skipped_reason = "sync already running"
        raise SyncBusy(report.skipped_reason)

    try:
        client = client or get_ai_client()
        window_days = days if days is not None else account.history_days
        if days is not None:
            account.history_days = days
        account_email_resolved, service = get_service(account.email)
        if account_email_resolved.casefold() != account.email.casefold():
            raise AuthError(
                f"Authorized Gmail account {account_email_resolved} does not match {account.email}."
            )
        lease = process_due_work(client, report, account=account, lease=lease)

        if backfill or not account.history_id:
            report.mode = "backfill"
            checkpoint = _current_history_id(service)
            listed_ids = list_message_ids(
                service,
                days=window_days,
                query=JOB_SENDER_QUERY,
                include_archived=account.include_archived,
            )
            ids, has_more = _unseen_message_ids(account, listed_ids, limit)
            report.listed = len(ids)
            lease = _ingest_ids(account, service, ids, client, report, progress, lease=lease)
            account.history_id = "" if has_more else checkpoint
            account.backfill_completed = not has_more
        else:
            report.mode = "incremental"
            try:
                ids, checkpoint = _history_message_ids(service, account.history_id)
                report.listed = len(ids)
                lease = _ingest_ids(account, service, ids, client, report, progress, lease=lease)
                account.history_id = checkpoint
            except HttpError as exc:
                if getattr(exc.resp, "status", None) == 404:
                    report.history_expired = True
                    report.mode = "recovery"
                    report.errors += 1
                    since = account.last_sync_at or (
                        timezone.now() - dt.timedelta(days=window_days)
                    )
                    recovery_days = max((timezone.now() - since).days, 1)
                    checkpoint = _current_history_id(service)
                    listed_ids = list_message_ids(
                        service,
                        days=recovery_days,
                        query=JOB_SENDER_QUERY,
                        include_archived=account.include_archived,
                    )
                    ids, has_more = _unseen_message_ids(account, listed_ids, limit)
                    report.listed = len(ids)
                    lease = _ingest_ids(
                        account, service, ids, client, report, progress, lease=lease
                    )
                    account.history_id = "" if has_more else checkpoint
                    account.backfill_completed = not has_more
                else:
                    raise

        lease = _heartbeat_lock(account, lease)
        account.save(
            update_fields=["history_id", "backfill_completed", "history_days", "updated_at"]
        )
        _release_lock(account, lease)
    except SyncBusy:
        raise
    except AuthError as exc:
        _release_lock(
            account,
            account.sync_started_at or lease,
            error=str(exc),
            connection_state=GmailConnectionState.NEEDS_RECONNECT,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _release_lock(account, account.sync_started_at or lease, error=str(exc)[:2000])
        raise
    return report


def retry_failed_emails(*, client: DeepSeekClient | None = None, limit: int = 50) -> SyncReport:
    """Reprocess already-stored emails that failed extraction. No Gmail access."""
    client = client or get_ai_client()
    report = SyncReport(account="(stored emails)")
    emails = list(
        Email.objects.filter(process_state=EmailProcessState.ERROR)
        .select_related("account")
        .order_by("id")[:limit]
    )
    report.listed = len(emails)
    report.new_emails = len(emails)
    for email in emails:
        email.process_state = EmailProcessState.PENDING
        email.processing_error = ""
        email.save(update_fields=["process_state", "processing_error", "updated_at"])
        try:
            process_email(email, client, report)
        except Exception as exc:  # noqa: BLE001
            email.process_state = EmailProcessState.ERROR
            email.processing_error = str(exc)[:2000]
            email.save(update_fields=["process_state", "processing_error", "updated_at"])
            report.errors += 1
            continue
        WorkItem.objects.filter(kind="process_email", payload__email_id=email.pk).update(
            status=WorkItemStatus.DONE
        )
    return report


def _history_message_ids(service, start_history_id: str) -> tuple[list[str], str]:
    ids: list[str] = []
    checkpoint = start_history_id
    page_token: str | None = None
    while True:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
                maxResults=100,
            )
            .execute()
        )
        for record in response.get("history", []) or []:
            for added in record.get("messagesAdded", []) or []:
                message_id = added.get("message", {}).get("id")
                if message_id:
                    ids.append(message_id)
        checkpoint = str(response.get("historyId") or checkpoint)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return list(dict.fromkeys(ids)), checkpoint


def preview_backfill(account: GmailAccount, days: int | None = None) -> dict[str, Any]:
    """Scope estimate before importing: how many messages match the window."""
    _, service = get_service(account.email)
    ids = list_message_ids(
        service,
        days=days or account.history_days,
        query=JOB_SENDER_QUERY,
        include_archived=account.include_archived,
    )
    return {
        "account": account.email,
        "days": days or account.history_days,
        "matched_messages": len(ids),
    }


def sync_all(*, backfill: bool = False, client: DeepSeekClient | None = None) -> list[SyncReport]:
    reports: list[SyncReport] = []
    for account in GmailAccount.objects.all():
        try:
            reports.append(sync_account(account, backfill=backfill, client=client))
        except SyncBusy:
            continue
    return reports


def known_account_emails() -> list[str]:
    emails = list(GmailAccount.objects.values_list("email", flat=True))
    for email in known_gmail_accounts():
        if email not in emails:
            emails.append(email)
    return emails
