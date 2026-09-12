"""Views for the tracker UI (server-rendered, HTMX-enhanced)."""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import hmac
import json
import secrets
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import reminders as reminders_mod
from . import reports as reports_mod
from . import review as review_mod
from .backup import backup_all, csv_rows
from .capture_service import (
    capture_links_from_emails,
    capture_url,
    store_capture,
)
from .forms import (
    ApplicationFilterForm,
    ApplicationForm,
    BoardCardFieldsForm,
    ContactForm,
    DocumentUploadForm,
    InterviewForm,
    TaskForm,
    TaskStandaloneForm,
)
from .integrations.ai_client import BudgetExceeded, UsageLedger
from .models import (
    BOARD_CARD_FIELD_CHOICES,
    CLOSED_STAGES,
    PIPELINE_STAGES,
    Application,
    ApplicationEvent,
    AppSettings,
    Document,
    Email,
    EventActor,
    EventType,
    GmailAccount,
    Interview,
    InterviewOutcome,
    PostingSnapshot,
    ReviewItem,
    ReviewStatus,
    Source,
    Stage,
    Task,
    TaskStatus,
    default_board_card_fields,
)
from .services import record_event, set_stage, store_document
from .sync import SyncBusy, preview_backfill, retry_failed_emails, sync_account


def _filter_applications(request):
    form = ApplicationFilterForm(request.GET or None)
    qs = Application.objects.all()
    if form.is_valid():
        data = form.cleaned_data
        if data.get("q"):
            term = data["q"]
            qs = qs.filter(
                Q(company__icontains=term)
                | Q(title__icontains=term)
                | Q(requisition_id__icontains=term)
                | Q(location__icontains=term)
                | Q(notes__icontains=term)
                | Q(snapshots__description__icontains=term)
                | Q(snapshots__requirements__icontains=term)
                | Q(snapshots__title__icontains=term)
            ).distinct()
        if data.get("stage"):
            qs = qs.filter(stage=data["stage"])
        if data.get("company"):
            qs = qs.filter(company__icontains=data["company"])
        if data.get("role"):
            qs = qs.filter(title__icontains=data["role"])
        if data.get("priority"):
            qs = qs.filter(priority=data["priority"])
        if data.get("source"):
            qs = qs.filter(source=data["source"])
        if data.get("location"):
            qs = qs.filter(location__icontains=data["location"])
        if data.get("work_arrangement"):
            qs = qs.filter(work_arrangement=data["work_arrangement"])
        if data.get("applied_after"):
            qs = qs.filter(application_date__gte=data["applied_after"])
        if data.get("applied_before"):
            qs = qs.filter(application_date__lte=data["applied_before"])
        if data.get("tag"):
            qs = qs.filter(tags=data["tag"])
        if not data.get("show_archived"):
            qs = qs.filter(is_archived=False)
        order = {
            "company": ("company", "title"),
            "role": ("title", "company"),
            "applied_new": ("-application_date", "company"),
            "applied_old": ("application_date", "company"),
        }.get(data.get("sort"), ("-updated_at",))
        qs = qs.order_by(*order)
    else:
        qs = qs.filter(is_archived=False)
    return form, qs


def today(request):
    now = timezone.now()
    horizon = now + dt.timedelta(days=14)
    week = now + dt.timedelta(days=7)

    overdue_count = Task.objects.filter(status=TaskStatus.OPEN, due_at__lt=now).count()
    due_soon_count = Task.objects.filter(
        status=TaskStatus.OPEN, due_at__gte=now, due_at__lte=week
    ).count()
    interview_qs = Interview.objects.filter(
        scheduled_start__gte=now,
        scheduled_start__lte=horizon,
        outcome=InterviewOutcome.SCHEDULED,
    )
    interview_count = interview_qs.count()
    active_count = Application.objects.filter(is_archived=False, stage__in=PIPELINE_STAGES).count()

    due_tasks = (
        Task.objects.filter(status=TaskStatus.OPEN, due_at__lte=now)
        .select_related("application")
        .order_by("due_at")[:20]
    )
    upcoming_tasks = (
        Task.objects.filter(status=TaskStatus.OPEN, due_at__gt=now, due_at__lte=horizon)
        .select_related("application")
        .order_by("due_at")[:10]
    )
    upcoming_interviews = interview_qs.select_related("application").order_by("scheduled_start")[
        :10
    ]
    recent_events = (
        ApplicationEvent.objects.filter(recorded_at__gte=now - dt.timedelta(days=7))
        .select_related("application")
        .order_by("-recorded_at")[:20]
    )
    missing_descriptions = (
        Application.objects.filter(snapshots__isnull=True, is_archived=False)
        .exclude(stage=Stage.SAVED)
        .distinct()[:10]
    )
    gmail_accounts = GmailAccount.objects.all()
    config = AppSettings.load()
    notifications = [
        {
            "id": task.pk,
            "title": task.title,
            "due": task.due_at.isoformat() if task.due_at else "",
        }
        for task in due_tasks
    ]
    context = {
        "active_nav": "today",
        "now": now,
        "overdue_count": overdue_count,
        "due_soon_count": due_soon_count,
        "interview_count": interview_count,
        "active_count": active_count,
        "due_tasks": due_tasks,
        "upcoming_tasks": upcoming_tasks,
        "upcoming_interviews": upcoming_interviews,
        "recent_events": recent_events,
        "missing_descriptions": missing_descriptions,
        "gmail_accounts": gmail_accounts,
        "pipeline_stages": PIPELINE_STAGES,
        "follow_up_candidates": reminders_mod.follow_up_candidates(config),
        "notifications": notifications,
    }
    return render(request, "tracker/today.html", context)


def application_list(request):
    form, qs = _filter_applications(request)
    template = (
        "tracker/partials/application_rows.html"
        if request.headers.get("HX-Request")
        else "tracker/application_list.html"
    )
    context = {"form": form, "applications": qs, "active_nav": "applications"}
    return render(request, template, context)


def application_board(request):
    config = AppSettings.load()
    allowed_fields = {key for key, _ in BOARD_CARD_FIELD_CHOICES}
    stored_fields = config.board_card_fields
    if not isinstance(stored_fields, list):
        stored_fields = default_board_card_fields()
    visible_fields = {
        field for field in stored_fields if isinstance(field, str) and field in allowed_fields
    }
    board_stages = [*PIPELINE_STAGES, *CLOSED_STAGES]
    groups = {stage: [] for stage in board_stages}
    form, qs = _filter_applications(request)
    if "tags" in visible_fields:
        qs = qs.prefetch_related("tags")
    for app in qs.filter(stage__in=board_stages):
        groups[app.stage].append(app)
    columns = [
        {"stage": stage, "label": Stage(stage).label, "applications": groups[stage]}
        for stage in board_stages
    ]
    return render(
        request,
        "tracker/application_board.html",
        {
            "columns": columns,
            "form": form,
            "stages": Stage.choices,
            "active_nav": "board",
            "visible_fields": visible_fields,
            "card_fields_form": BoardCardFieldsForm(
                initial={
                    "properties": [key for key, _ in BOARD_CARD_FIELD_CHOICES if key in visible_fields]
                }
            ),
        },
    )


@require_POST
def board_card_fields_save(request):
    form = BoardCardFieldsForm(request.POST)
    if not form.is_valid():
        return HttpResponseBadRequest("Unknown board card property")
    selected = set(form.cleaned_data["properties"])
    config = AppSettings.load()
    config.board_card_fields = [key for key, _ in BOARD_CARD_FIELD_CHOICES if key in selected]
    config.save(update_fields=["board_card_fields", "updated_at"])
    messages.success(request, "Board card properties saved.")
    next_url = request.POST.get("next", "")
    if (
        next_url
        and urlsplit(next_url).path == reverse("tracker:application_board")
        and url_has_allowed_host_and_scheme(
            next_url,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return redirect(next_url)
    return redirect("tracker:application_board")


def _render_application_detail(request, pk: int, **extra_context):
    application = get_object_or_404(
        Application.objects.prefetch_related(
            "tags",
            "interviews",
            "tasks",
            "contacts",
            "documents",
            "snapshots",
        ),
        pk=pk,
    )
    context = {
        "active_nav": "applications",
        "application": application,
        "snapshots": application.snapshots.all(),
        "latest_snapshot": application.snapshots.first(),
        "events": application.events.select_related("email__account")[:50],
        "emails": application.emails.select_related("account")[:20],
        "interviews": application.interviews.all(),
        "tasks": application.tasks.all(),
        "contacts": application.contacts.all(),
        "documents": application.documents.all(),
        "stages": Stage.choices,
        "document_form": DocumentUploadForm(),
        "task_form": TaskForm(),
        "interview_form": InterviewForm(),
        "contact_form": ContactForm(),
        "all_documents": Document.objects.order_by("-created_at"),
    }
    context.update(extra_context)
    return render(request, "tracker/application_detail.html", context)


def application_detail(request, pk: int):
    return _render_application_detail(request, pk)


def application_create(request):
    if request.method == "POST":
        form = ApplicationForm(request.POST)
        if form.is_valid():
            application = form.save()
            record_event(
                application,
                event_type=EventType.CUSTOM,
                actor=EventActor.USER,
                summary="Application created",
                details={"stage": application.stage},
            )
            messages.success(request, f"Created {application}.")
            return redirect("tracker:application_detail", pk=application.pk)
    else:
        form = ApplicationForm()
    return render(
        request,
        "tracker/application_form.html",
        {"form": form, "active_nav": "applications", "mode": "create"},
    )


def application_update(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    old_stage = application.stage
    if request.method == "POST":
        form = ApplicationForm(request.POST, instance=application)
        if form.is_valid():
            changed = list(form.changed_data)
            if not changed:
                messages.info(request, "No changes to save.")
                return redirect("tracker:application_detail", pk=application.pk)
            application = form.save(commit=False)
            overrides = set(application.manual_override_fields or [])
            overrides.update(changed)
            application.manual_override_fields = sorted(overrides)
            application.save()
            form.save_m2m()
            if old_stage != application.stage:
                record_event(
                    application,
                    event_type=EventType.STAGE_CHANGE,
                    actor=EventActor.USER,
                    summary=f"{old_stage} → {application.stage}",
                    details={"from": old_stage, "to": application.stage},
                    is_correction=True,
                )
            else:
                record_event(
                    application,
                    event_type=EventType.CUSTOM,
                    actor=EventActor.USER,
                    summary="Details edited",
                    details={"changed": changed},
                    is_correction=True,
                )
            messages.success(request, "Saved changes.")
            return redirect("tracker:application_detail", pk=application.pk)
    else:
        form = ApplicationForm(instance=application)
    return render(
        request,
        "tracker/application_form.html",
        {"form": form, "application": application, "active_nav": "applications", "mode": "edit"},
    )


@require_POST
def application_set_stage(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    stage = request.POST.get("stage", "")
    if stage not in Stage.values:
        return HttpResponseBadRequest("Unknown stage")
    changed = application.stage != stage
    set_stage(application, stage, actor=EventActor.USER, source=Source.MANUAL)
    if request.headers.get("HX-Request"):
        return render(
            request,
            "tracker/partials/stage_control.html",
            {"application": application, "stages": Stage.choices},
        )
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        if changed:
            messages.success(request, f"Moved {application} to {application.get_stage_display()}.")
        return redirect(next_url)
    return redirect("tracker:application_detail", pk=application.pk)


@require_POST
def application_archive(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    application.is_archived = not application.is_archived
    application.save(update_fields=["is_archived", "updated_at"])
    messages.success(request, "Restored." if not application.is_archived else "Archived.")
    return redirect("tracker:application_list")


@require_POST
def document_upload(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    form = DocumentUploadForm(request.POST, request.FILES)
    if form.is_valid():
        document, created = store_document(
            form.cleaned_data["file"],
            kind=form.cleaned_data["kind"],
            label=form.cleaned_data["label"],
            applications=[application],
        )
        record_event(
            application,
            event_type=EventType.DOCUMENT_ADDED,
            actor=EventActor.USER,
            summary=f"{'Stored' if created else 'Linked'} {document.label}",
            details={"document_id": document.pk, "sha256": document.sha256},
        )
        messages.success(request, "Document saved.")
        return redirect("tracker:application_detail", pk=application.pk)
    return _render_application_detail(
        request,
        pk,
        document_form=form,
        open_form="document",
    )


@require_POST
def task_create(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    form = TaskForm(request.POST)
    if form.is_valid():
        task = form.save(commit=False)
        task.application = application
        task.save()
        messages.success(request, "Task added.")
        return redirect("tracker:application_detail", pk=application.pk)
    return _render_application_detail(request, pk, task_form=form, open_form="task")


@require_POST
def task_toggle(request, pk: int):
    task = get_object_or_404(Task, pk=pk)
    if task.status == TaskStatus.DONE:
        task.status = TaskStatus.OPEN
        task.completed_at = None
    else:
        task.status = TaskStatus.DONE
        task.completed_at = timezone.now()
    task.save(update_fields=["status", "completed_at", "updated_at"])
    if request.headers.get("HX-Request"):
        return render(request, "tracker/partials/task_row.html", {"task": task})
    if task.application_id:
        return redirect("tracker:application_detail", pk=task.application_id)
    return redirect("tracker:task_list")


@require_POST
def interview_create(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    form = InterviewForm(request.POST)
    if form.is_valid():
        interview = form.save(commit=False)
        interview.application = application
        if not interview.round:
            interview.round = application.interviews.count() + 1
        interview.save()
        record_event(
            application,
            event_type=EventType.INTERVIEW_INVITATION,
            actor=EventActor.USER,
            summary=f"Interview round {interview.round} added",
            details={"interview_id": interview.pk},
        )
        messages.success(request, "Interview added.")
        return redirect("tracker:application_detail", pk=application.pk)
    return _render_application_detail(
        request,
        pk,
        interview_form=form,
        open_form="interview",
    )


@require_POST
def contact_create(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    form = ContactForm(request.POST)
    if form.is_valid():
        contact = form.save()
        contact.applications.add(application)
        messages.success(request, "Contact added.")
        return redirect("tracker:application_detail", pk=application.pk)
    return _render_application_detail(request, pk, contact_form=form, open_form="contact")


def review_inbox(request):
    items = review_mod.pending_items()
    applications = Application.objects.filter(is_archived=False).order_by("company", "title")
    return render(
        request,
        "tracker/review_inbox.html",
        {
            "active_nav": "review",
            "items": items,
            "applications": applications,
            "pending_review_count": items.count(),
        },
    )


@require_POST
def review_action(request, pk: int):
    item = get_object_or_404(ReviewItem, pk=pk)
    action = request.POST.get("action", "accept")
    application_id = request.POST.get("application") or None
    application = Application.objects.filter(pk=application_id).first() if application_id else None
    try:
        resolved = review_mod.resolve(item, action=action, application=application)
    except review_mod.ReviewError as exc:
        messages.error(request, str(exc))
    else:
        if action == "ignore":
            messages.success(request, "Marked as not job-related.")
        else:
            label = str(resolved) if resolved else "application"
            messages.success(request, f"Resolved: {label}.")
    return redirect("tracker:review_inbox")


@require_POST
def gmail_sync_now(request):
    account_email = request.POST.get("account") or None
    backfill = request.POST.get("backfill") == "on"
    days_raw = request.POST.get("days", "")
    limit_raw = request.POST.get("limit", "")
    try:
        days = int(days_raw) if days_raw else None
        limit = int(limit_raw) if limit_raw else 50
    except ValueError:
        messages.error(request, "History window and message limit must be whole numbers.")
        return redirect("tracker:settings")

    account = (
        GmailAccount.objects.filter(email=account_email).first()
        if account_email
        else GmailAccount.objects.first()
    )
    if account is None:
        messages.error(request, "No Gmail account connected. Run `gmail_connect` first.")
        return redirect("tracker:settings")
    if days is not None and days < 1:
        messages.error(request, "History window must be at least 1 day.")
        return redirect("tracker:settings")
    if limit < 1:
        messages.error(request, "Message limit must be at least 1.")
        return redirect("tracker:settings")

    try:
        report = sync_account(account, backfill=backfill, days=days, limit=limit)
    except SyncBusy:
        messages.warning(request, "A sync is already running for that account.")
    except BudgetExceeded as exc:
        messages.error(request, f"AI budget reached: {exc}")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Sync failed: {exc}")
    else:
        messages.success(
            request,
            f"{report.mode.title()} sync: {report.new_emails} new email(s), "
            f"{report.created_applications} created, {report.updated_applications} updated, "
            f"{report.review_items} to review, {report.errors} error(s).",
        )
    return redirect("tracker:settings")


@require_POST
def gmail_preview(request):
    account = get_object_or_404(GmailAccount, email=request.POST.get("account", ""))
    days_raw = request.POST.get("days", "")
    try:
        days = int(days_raw) if days_raw else account.history_days
        if days < 1:
            raise ValueError
        preview = preview_backfill(account, days)
    except ValueError:
        messages.error(request, "History window must be a positive whole number.")
    except Exception as exc:  # noqa: BLE001 - surface OAuth/network failures in Settings
        messages.error(request, f"Preview failed: {exc}")
    else:
        messages.info(
            request,
            f"{preview['matched_messages']} Gmail message(s) match the {days}-day import scope.",
        )
    return redirect("tracker:settings")


def _settings_context(**extra):
    config = AppSettings.load()
    ledger = UsageLedger(
        settings.DATA_DIR / "ai_usage",
        ceiling_usd=float(config.ai_monthly_ceiling_usd),
    )
    context = {
        "active_nav": "settings",
        "config": config,
        "accounts": GmailAccount.objects.all(),
        "pending_reviews": ReviewItem.objects.filter(status=ReviewStatus.PENDING).count(),
        "failed_emails": Email.objects.filter(process_state="error").count(),
        "ai_spend": ledger.monthly_spend(),
        "ai_remaining": ledger.remaining(),
        "extension_token_set": bool(config.capture_extension_token_hash),
    }
    context.update(extra)
    return context


def settings_view(request):
    return render(request, "tracker/settings.html", _settings_context())


def export_csv_view(request):
    applications = Application.objects.all().order_by("-application_date", "company", "title")
    if request.GET.get("include_archived") != "1":
        applications = applications.filter(is_archived=False)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="job-applications.csv"'
    csv.writer(response).writerows(csv_rows(applications))
    return response


@require_POST
def backup_now(request):
    try:
        destination = backup_all(
            db_path=settings.DATABASES["default"]["NAME"],
            media_root=settings.MEDIA_ROOT,
            backup_dir=settings.BACKUP_DIR,
            keep=7,
        )
    except Exception as exc:  # noqa: BLE001 - keep this user-triggered operation recoverable
        messages.error(request, f"Backup failed: {exc}")
    else:
        messages.success(request, f"Backup created: {destination.name}")
    return redirect("tracker:settings")


@require_POST
def generate_capture_token(request):
    config = AppSettings.load()
    token = secrets.token_urlsafe(32)
    config.capture_extension_token_hash = hashlib.sha256(token.encode()).hexdigest()
    config.save(update_fields=["capture_extension_token_hash", "updated_at"])
    return render(
        request,
        "tracker/settings.html",
        _settings_context(new_capture_token=token),
    )


@require_POST
def revoke_capture_token(request):
    config = AppSettings.load()
    config.capture_extension_token_hash = ""
    config.save(update_fields=["capture_extension_token_hash", "updated_at"])
    messages.success(request, "Extension pairing revoked.")
    return redirect("tracker:settings")


@require_POST
def capture_email_links_view(request):
    limit_raw = request.POST.get("limit", "10")
    try:
        limit = max(int(limit_raw), 1)
    except ValueError:
        limit = 10
    try:
        report = capture_links_from_emails(limit=limit, create_if_new=True)
    except BudgetExceeded as exc:
        messages.error(request, f"AI budget reached: {exc}")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Capture failed: {exc}")
    else:
        messages.success(
            request,
            f"Email links: found {report['found']}, captured {report['captured']}, "
            f"created {report['created_applications']}, failed {report['failed']}.",
        )
    return redirect("tracker:settings")


@require_POST
def capture_from_url(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    url = (request.POST.get("url") or "").strip()
    if not url:
        messages.error(request, "Enter a job posting URL.")
        return redirect("tracker:application_detail", pk=pk)
    try:
        snapshot, _application, _created = capture_url(
            url, application=application, source=Source.MANUAL, run_ai=True
        )
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Could not capture posting: {exc}")
    else:
        if snapshot.pk:
            messages.success(
                request,
                f"Saved posting v{snapshot.version} ({snapshot.get_capture_state_display()}).",
            )
        else:
            messages.warning(request, snapshot.error or "The posting could not be archived.")
    return redirect("tracker:application_detail", pk=pk)


@require_POST
def capture_from_text(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    description = (request.POST.get("description") or "").strip()
    if not description:
        messages.error(request, "Paste a job description to archive.")
        return redirect("tracker:application_detail", pk=pk)
    try:
        snapshot, _application, _created = store_capture(
            {
                "url": application.job_url,
                "title": application.title,
                "company": application.company,
                "description": description,
                "capture_state": "text",
            },
            application=application,
            source=Source.MANUAL,
            run_ai=True,
        )
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Could not save the description: {exc}")
    else:
        messages.success(request, f"Saved pasted description as posting v{snapshot.version}.")
    return redirect("tracker:application_detail", pk=pk)


def snapshot_print(request, pk: int):
    snapshot = get_object_or_404(PostingSnapshot.objects.select_related("application"), pk=pk)
    return render(
        request,
        "tracker/snapshot_print.html",
        {"snapshot": snapshot, "application": snapshot.application},
    )


@login_not_required
@csrf_exempt
@require_POST
def api_capture(request):
    config = AppSettings.load()
    supplied = request.headers.get("X-Capture-Token", "")
    expected = config.capture_extension_token_hash
    if not expected or not supplied:
        return JsonResponse({"error": "unauthorized"}, status=401)
    if not hmac.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(), expected):
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"error": "invalid payload"}, status=400)

    url = (payload.get("url") or "").strip()
    description = (payload.get("description") or "").strip()
    create_if_new = payload.get("create_if_new", True)
    if not isinstance(create_if_new, bool):
        return JsonResponse({"error": "create_if_new must be a boolean"}, status=400)
    application_id = payload.get("application_id")
    if application_id is not None:
        try:
            application = Application.objects.get(pk=application_id)
        except (Application.DoesNotExist, TypeError, ValueError):
            return JsonResponse({"error": "unknown application_id"}, status=400)
    else:
        application = None

    try:
        if not description and url:
            snapshot, application, created = capture_url(
                url,
                application=application,
                create_if_new=create_if_new,
                source=Source.BROWSER,
                run_ai=True,
            )
        else:
            snapshot, application, created = store_capture(
                payload,
                application=application,
                create_if_new=create_if_new,
                source=Source.BROWSER,
                run_ai=True,
            )
    except BudgetExceeded as exc:
        return JsonResponse({"error": str(exc)}, status=402)
    except Exception as exc:  # noqa: BLE001
        return JsonResponse({"error": str(exc)}, status=500)

    if snapshot.pk is None:
        return JsonResponse(
            {"error": snapshot.error or "The page could not be archived."},
            status=422,
        )

    return JsonResponse(
        {
            "snapshot_id": snapshot.pk,
            "version": snapshot.version,
            "application_id": application.pk if application else None,
            "application_label": str(application) if application else None,
            "created_application": created,
            "capture_state": snapshot.capture_state,
            "unknown_fields": snapshot.unknown_fields,
            "error": snapshot.error,
        }
    )


@require_POST
def settings_save(request):
    config = AppSettings.load()
    days_raw = request.POST.get("gmail_history_days", "")
    if days_raw:
        try:
            days = int(days_raw)
        except ValueError:
            messages.error(request, "History window must be a whole number.")
            return redirect("tracker:settings")
        if days < 1:
            messages.error(request, "History window must be at least 1 day.")
            return redirect("tracker:settings")
        config.gmail_history_days = days
    follow_raw = request.POST.get("follow_up_days", "")
    if follow_raw:
        try:
            follow_up_days = int(follow_raw)
        except ValueError:
            messages.error(request, "Follow-up days must be a whole number.")
            return redirect("tracker:settings")
        if follow_up_days < 1:
            messages.error(request, "Follow-up days must be at least 1.")
            return redirect("tracker:settings")
        config.follow_up_days = follow_up_days
    ceiling_raw = request.POST.get("ai_monthly_ceiling_usd", "")
    if ceiling_raw:
        try:
            ceiling = Decimal(ceiling_raw)
        except InvalidOperation:
            messages.error(request, "Ceiling must be a number.")
            return redirect("tracker:settings")
        if not ceiling.is_finite() or ceiling < 0:
            messages.error(request, "Ceiling must be zero or greater.")
            return redirect("tracker:settings")
        config.ai_monthly_ceiling_usd = ceiling
    model = request.POST.get("ai_model", "").strip()
    if model:
        config.ai_model = model
    timezone_name = request.POST.get("display_timezone", "").strip()
    if timezone_name:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            messages.error(request, "Enter a valid IANA timezone, such as Africa/Cairo.")
            return redirect("tracker:settings")
        config.display_timezone = timezone_name
    config.save()
    messages.success(request, "Settings saved.")
    return redirect("tracker:settings")


@require_POST
def review_bulk(request):
    action = request.POST.get("bulk_action", "")
    item_ids = request.POST.getlist("item_ids")
    application = None
    if action in ("bulk_accept", "bulk_ignore"):
        per_item_action = "accept" if action == "bulk_accept" else "ignore"
    elif action == "bulk_link":
        per_item_action = "link"
        application_id = request.POST.get("application") or None
        application = (
            Application.objects.filter(pk=application_id).first() if application_id else None
        )
        if application is None:
            messages.error(request, "Choose an application to link to.")
            return redirect("tracker:review_inbox")
    else:
        messages.error(request, "Unknown bulk action.")
        return redirect("tracker:review_inbox")

    items = ReviewItem.objects.filter(pk__in=item_ids, status=ReviewStatus.PENDING)
    resolved = 0
    skipped = 0
    for item in items:
        try:
            review_mod.resolve(item, action=per_item_action, application=application)
            resolved += 1
        except review_mod.ReviewError:
            skipped += 1
    if resolved:
        messages.success(request, f"Resolved {resolved} item(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} item(s) that needed a match.")
    return redirect("tracker:review_inbox")


@require_POST
def retry_failed_view(request):
    limit_raw = request.POST.get("limit", "50")
    try:
        limit = max(int(limit_raw), 1)
    except ValueError:
        limit = 50
    try:
        report = retry_failed_emails(limit=limit)
    except BudgetExceeded as exc:
        messages.error(request, f"AI budget reached: {exc}")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, f"Retry failed: {exc}")
    else:
        messages.success(
            request,
            f"Retried {report.listed} email(s): "
            f"{report.created_applications} created, {report.review_items} to review, "
            f"{report.errors} still failing.",
        )
    return redirect("tracker:settings")


def reports_view(request):
    queryset = Application.objects.filter(is_archived=False)
    months_raw = request.GET.get("months", "")
    months = int(months_raw) if months_raw.isdigit() else None
    if months:
        since = timezone.localdate() - dt.timedelta(days=30 * months)
        queryset = queryset.filter(application_date__gte=since)
    report = reports_mod.build_report(queryset)
    return render(
        request,
        "tracker/reports.html",
        {"active_nav": "reports", "report": report, "months": months},
    )


def task_list(request, form=None):
    now = timezone.now()
    open_tasks = list(
        Task.objects.filter(status__in=[TaskStatus.OPEN, TaskStatus.SNOOZED])
        .select_related("application")
        .order_by("due_at")
    )
    overdue = [t for t in open_tasks if t.due_at and t.due_at < now]
    due_soon = [t for t in open_tasks if t.due_at and now <= t.due_at <= now + dt.timedelta(days=7)]
    later = [t for t in open_tasks if t not in overdue and t not in due_soon]
    done_tasks = (
        Task.objects.filter(status=TaskStatus.DONE)
        .select_related("application")
        .order_by("-completed_at")[:50]
    )
    config = AppSettings.load()
    return render(
        request,
        "tracker/task_list.html",
        {
            "active_nav": "tasks",
            "overdue": overdue,
            "due_soon": due_soon,
            "later": later,
            "done_tasks": done_tasks,
            "form": form or TaskStandaloneForm(),
            "follow_up_candidates": reminders_mod.follow_up_candidates(config),
        },
    )


@require_POST
def task_create_standalone(request):
    form = TaskStandaloneForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, "Task added.")
        return redirect("tracker:task_list")
    messages.error(request, "Could not add task. Check the fields and try again.")
    return task_list(request, form)


@require_POST
def task_snooze(request, pk: int):
    task = get_object_or_404(Task, pk=pk)
    days_raw = request.POST.get("days", "1")
    days = int(days_raw) if days_raw.isdigit() else 1
    base = task.due_at or timezone.now()
    if base < timezone.now():
        base = timezone.now()
    task.due_at = base + dt.timedelta(days=days)
    task.snoozed_until = task.due_at
    task.status = TaskStatus.OPEN
    task.save(update_fields=["due_at", "snoozed_until", "status", "updated_at"])
    if request.headers.get("HX-Request"):
        return render(request, "tracker/partials/task_row.html", {"task": task})
    return redirect("tracker:task_list")


@require_POST
def task_delete(request, pk: int):
    task = get_object_or_404(Task, pk=pk)
    task.delete()
    messages.success(request, "Task deleted.")
    return redirect("tracker:task_list")


@require_POST
def follow_up_create(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    config = AppSettings.load()
    reminders_mod.create_follow_up_task(application, config)
    messages.success(request, "Follow-up task created.")
    next_url = request.POST.get("next", "")
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect("tracker:task_list")


def follow_up_draft_view(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    return render(
        request,
        "tracker/follow_up_draft.html",
        {
            "active_nav": "tasks",
            "application": application,
            "draft": reminders_mod.follow_up_draft(application),
        },
    )


def _ics_datetime(value) -> str:
    return value.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def interview_ics(request, pk: int):
    interview = get_object_or_404(Interview.objects.select_related("application"), pk=pk)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Job Monitor//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:interview-{interview.pk}@job-monitor",
        f"DTSTAMP:{timezone.now().astimezone(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}",
        f"SUMMARY:{_ics_escape(f'{interview.application.title} at {interview.application.company}')}",
    ]
    if interview.scheduled_start:
        lines.append(f"DTSTART:{_ics_datetime(interview.scheduled_start)}")
    if interview.scheduled_end:
        lines.append(f"DTEND:{_ics_datetime(interview.scheduled_end)}")
    if interview.location:
        lines.append(f"LOCATION:{_ics_escape(interview.location)}")
    if interview.meeting_url:
        lines.append(f"URL:{interview.meeting_url}")
    description = "\n".join(filter(None, [interview.get_kind_display(), interview.prep_notes]))
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    response = HttpResponse("\r\n".join(lines), content_type="text/calendar")
    response["Content-Disposition"] = f'attachment; filename="interview-{interview.pk}.ics"'
    return response


def interview_update(request, pk: int):
    interview = get_object_or_404(Interview, pk=pk)
    old_start = interview.scheduled_start
    old_outcome = interview.outcome
    if request.method == "POST":
        form = InterviewForm(request.POST, instance=interview)
        if form.is_valid():
            interview = form.save()
            if old_start != interview.scheduled_start:
                event_type = EventType.INTERVIEW_RESCHEDULE
            elif (
                old_outcome != interview.outcome and interview.outcome == InterviewOutcome.CANCELLED
            ):
                event_type = EventType.INTERVIEW_CANCELLATION
            else:
                event_type = None
            if event_type:
                record_event(
                    interview.application,
                    event_type=event_type,
                    actor=EventActor.USER,
                    summary=f"Interview round {interview.round} {event_type.replace('_', ' ')}",
                    details={"interview_id": interview.pk},
                )
            messages.success(request, "Interview updated.")
            return redirect("tracker:application_detail", pk=interview.application_id)
    else:
        form = InterviewForm(instance=interview)
    return render(
        request,
        "tracker/interview_form.html",
        {
            "active_nav": "applications",
            "form": form,
            "interview": interview,
            "application": interview.application,
        },
    )


@require_POST
def interview_delete(request, pk: int):
    interview = get_object_or_404(Interview, pk=pk)
    application_id = interview.application_id
    interview.delete()
    messages.success(request, "Interview removed.")
    return redirect("tracker:application_detail", pk=application_id)


def document_list(request):
    documents = Document.objects.prefetch_related("applications").order_by("-created_at")
    return render(
        request,
        "tracker/document_list.html",
        {"active_nav": "documents", "documents": documents},
    )


@require_POST
def document_link(request, pk: int):
    application = get_object_or_404(Application, pk=pk)
    document = get_object_or_404(Document, pk=request.POST.get("document"))
    document.applications.add(application)
    record_event(
        application,
        event_type=EventType.DOCUMENT_ADDED,
        actor=EventActor.USER,
        summary=f"Linked document: {document.label or document.original_filename}",
        details={"document_id": document.pk},
    )
    messages.success(request, "Document linked.")
    return redirect("tracker:application_detail", pk=pk)


@require_POST
def document_unlink(request, pk: int, document_id: int):
    application = get_object_or_404(Application, pk=pk)
    document = get_object_or_404(Document, pk=document_id)
    document.applications.remove(application)
    messages.success(request, "Document unlinked.")
    return redirect("tracker:application_detail", pk=pk)
