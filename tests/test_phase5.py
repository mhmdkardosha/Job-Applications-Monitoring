from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from tracker.forms import InterviewForm
from tracker.models import (
    Application,
    ApplicationEvent,
    AppSettings,
    Document,
    EventType,
    Interview,
    InterviewOutcome,
    Stage,
    Task,
    TaskStatus,
)
from tracker.reports import build_report

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_logged(client):
    user = get_user_model().objects.create_user(username="local", password="pw12345!")
    client.force_login(user)
    return client


def _event(application, event_type, when):
    return ApplicationEvent.objects.create(
        application=application, event_type=event_type, occurred_at=when
    )


def test_report_rates_and_response_time():
    interviewed = Application.objects.create(
        company="Acme",
        title="Engineer",
        stage=Stage.OFFER,
        application_date=dt.date(2026, 1, 1),
    )
    _event(
        interviewed,
        EventType.APPLICATION_CONFIRMATION,
        timezone.make_aware(dt.datetime(2026, 1, 1)),
    )
    _event(interviewed, EventType.OFFER, timezone.make_aware(dt.datetime(2026, 1, 11)))

    rejected = Application.objects.create(
        company="Globex",
        title="Engineer",
        stage=Stage.REJECTED,
        application_date=dt.date(2026, 1, 5),
    )
    _event(
        rejected, EventType.APPLICATION_CONFIRMATION, timezone.make_aware(dt.datetime(2026, 1, 5))
    )

    report = build_report()
    assert report.submitted == 2
    assert report.interview_rate.numerator == 1
    assert report.offer_rate.numerator == 1
    assert report.response_sample == 1
    assert report.response_median_days == 10


def test_applications_over_time():
    Application.objects.create(
        company="A", title="X", stage=Stage.APPLIED, application_date=dt.date(2026, 1, 3)
    )
    Application.objects.create(
        company="B", title="Y", stage=Stage.APPLIED, application_date=dt.date(2026, 1, 20)
    )
    report = build_report()
    assert report.over_time == [("2026-01", 2)]


def test_reports_page_renders(client_logged):
    Application.objects.create(company="A", title="X", stage=Stage.APPLIED)
    response = client_logged.get(reverse("tracker:reports"))
    assert response.status_code == 200
    assert b"Interviewed" in response.content


def test_task_create_snooze_delete(client_logged):
    application = Application.objects.create(company="A", title="X")
    create = client_logged.post(
        reverse("tracker:task_create_standalone"),
        {"title": "Follow up", "application": application.pk, "due_at": ""},
    )
    assert create.status_code == 302
    task = Task.objects.get()
    assert task.application_id == application.pk

    before = task.due_at
    client_logged.post(reverse("tracker:task_snooze", args=[task.pk]), {"days": "2"})
    task.refresh_from_db()
    assert task.status == TaskStatus.OPEN
    assert task.due_at > (before or timezone.now())

    client_logged.post(reverse("tracker:task_delete", args=[task.pk]))
    assert Task.objects.count() == 0


def test_invalid_standalone_task_renders_bound_errors(client_logged):
    response = client_logged.post(
        reverse("tracker:task_create_standalone"),
        {"title": "Follow up", "due_at": "not-a-date"},
    )
    assert response.status_code == 200
    assert b"Follow up" in response.content
    assert b"Enter a valid date/time." in response.content


def test_standalone_task_toggle_returns_to_task_list(client_logged):
    task = Task.objects.create(title="Standalone")
    response = client_logged.post(reverse("tracker:task_toggle", args=[task.pk]))
    assert response.status_code == 302
    assert response.url == reverse("tracker:task_list")


def test_follow_up_candidate_and_create(client_logged):
    config = AppSettings.load()
    config.follow_up_days = 7
    config.save()
    application = Application.objects.create(
        company="Acme",
        title="Engineer",
        stage=Stage.APPLIED,
        application_date=timezone.localdate() - dt.timedelta(days=30),
    )
    page = client_logged.get(reverse("tracker:task_list"))
    assert b"Suggested follow-ups" in page.content
    assert b"Acme" in page.content

    client_logged.post(
        reverse("tracker:follow_up_create", args=[application.pk]),
        {"next": reverse("tracker:task_list")},
    )
    assert application.tasks.filter(status=TaskStatus.OPEN).exists()

    unsafe_redirect = client_logged.post(
        reverse("tracker:follow_up_create", args=[application.pk]),
        {"next": "https://evil.example/redirect"},
    )
    assert unsafe_redirect.url == reverse("tracker:task_list")
    assert application.tasks.filter(status=TaskStatus.OPEN).count() == 1


def test_interview_ics_export(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    interview = Interview.objects.create(
        application=application,
        round=1,
        scheduled_start=timezone.make_aware(dt.datetime(2026, 9, 15, 15, 0)),
    )
    response = client_logged.get(reverse("tracker:interview_ics", args=[interview.pk]))
    assert response.status_code == 200
    assert "text/calendar" in response["Content-Type"]
    body = response.content.decode()
    assert "BEGIN:VEVENT" in body
    assert "DTSTART:20260915T150000Z" in body


def test_interview_ics_escapes_text(client_logged):
    application = Application.objects.create(company="Acme, Inc.", title="R&D; Lead")
    interview = Interview.objects.create(
        application=application,
        location="Room 1, HQ; East",
        prep_notes="Line one\nLine two",
    )
    body = client_logged.get(reverse("tracker:interview_ics", args=[interview.pk])).content.decode()
    assert "SUMMARY:R&D\\; Lead at Acme\\, Inc." in body
    assert "LOCATION:Room 1\\, HQ\\; East" in body
    assert "Line one\\nLine two" in body


def test_interview_form_validates_times_timezone_and_participants():
    form = InterviewForm(
        data={
            "round": 1,
            "kind": "video",
            "scheduled_start": "2026-09-20T11:00",
            "scheduled_end": "2026-09-20T10:00",
            "timezone": "Not/AZone",
            "participants": "Nora\nnora@example.com",
            "outcome": "scheduled",
        }
    )
    assert form.is_valid() is False
    assert "scheduled_end" in form.errors
    assert "timezone" in form.errors
    assert form.cleaned_data["participants"] == ["Nora", "nora@example.com"]


def test_interview_reschedule_records_event(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    interview = Interview.objects.create(
        application=application,
        round=1,
        kind="video",
        outcome=InterviewOutcome.SCHEDULED,
        scheduled_start=timezone.make_aware(dt.datetime(2026, 9, 15, 15, 0)),
    )
    response = client_logged.post(
        reverse("tracker:interview_update", args=[interview.pk]),
        {
            "round": 1,
            "kind": "video",
            "scheduled_start": "2026-09-16T16:00",
            "scheduled_end": "",
            "timezone": "Africa/Cairo",
            "location": "",
            "meeting_url": "",
            "prep_notes": "",
            "notes": "",
            "outcome": "scheduled",
        },
    )
    assert response.status_code == 302
    assert application.events.filter(event_type=EventType.INTERVIEW_RESCHEDULE).exists()


def test_document_link_and_unlink(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    document = Document.objects.create(
        file=SimpleUploadedFile("resume.pdf", b"pdf-bytes"),
        kind="resume",
        label="Resume v1",
        original_filename="resume.pdf",
        sha256="deadbeef",
    )
    response = client_logged.post(
        reverse("tracker:document_link", args=[application.pk]),
        {"document": document.pk},
    )
    assert response.status_code == 302
    assert document in application.documents.all()

    client_logged.post(reverse("tracker:document_unlink", args=[application.pk, document.pk]))
    assert document not in application.documents.all()


def test_documents_page_renders(client_logged):
    response = client_logged.get(reverse("tracker:document_list"))
    assert response.status_code == 200
    assert b"Documents" in response.content
