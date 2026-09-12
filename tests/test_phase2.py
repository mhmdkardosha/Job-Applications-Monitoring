from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from tracker.models import (
    Application,
    ApplicationEvent,
    Document,
    Email,
    EventType,
    GmailAccount,
    Stage,
    Task,
)
from tracker.services import create_snapshot, record_event, set_stage, store_document

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="local", password="pw12345!")


@pytest.fixture
def client_logged(client, user):
    client.force_login(user)
    return client


def test_login_required_redirects(client):
    response = client.get(reverse("tracker:today"))
    assert response.status_code == 302
    assert "/accounts/login/" in response.url


def test_create_application_persists_and_records_event(client_logged):
    response = client_logged.post(
        reverse("tracker:application_create"),
        {
            "company": "Acme Corp",
            "title": "Senior Backend Engineer",
            "requisition_id": "12345",
            "source": "manual",
            "stage": "applied",
            "priority": "high",
            "work_arrangement": "remote",
            "employment_type": "full_time",
        },
    )
    assert response.status_code == 302
    application = Application.objects.get()
    assert application.company == "Acme Corp"
    assert application.stage == Stage.APPLIED
    assert application.is_submitted is True
    assert ApplicationEvent.objects.filter(
        application=application, event_type=EventType.CUSTOM
    ).exists()


def test_stage_change_records_history(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    response = client_logged.post(
        reverse("tracker:application_set_stage", args=[application.pk]),
        {"stage": Stage.INTERVIEWING},
    )
    assert response.status_code == 302
    application.refresh_from_db()
    assert application.stage == Stage.INTERVIEWING
    event = ApplicationEvent.objects.get(application=application, event_type=EventType.STAGE_CHANGE)
    assert event.details == {"from": Stage.SAVED, "to": Stage.INTERVIEWING}
    application.refresh_from_db()
    assert "stage" in application.manual_override_fields


def test_board_cards_can_move_to_closed_stage_and_return_to_board(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    board_url = reverse("tracker:application_board")
    board = client_logged.get(board_url, {"q": "Acme"})
    content = board.content.decode()
    assert 'draggable="true"' in content
    assert 'data-stage="rejected"' in content
    assert 'name="next" value="/board/?q=Acme"' in content

    response = client_logged.post(
        reverse("tracker:application_set_stage", args=[application.pk]),
        {"stage": Stage.REJECTED, "next": f"{board_url}?q=Acme"},
    )

    assert response.status_code == 302
    assert response.url == f"{board_url}?q=Acme"
    application.refresh_from_db()
    assert application.stage == Stage.REJECTED
    assert "stage" in application.manual_override_fields
    assert application.events.filter(event_type=EventType.REJECTION).count() == 1
    assert b"Acme" in client_logged.get(board_url).content


def test_board_cards_show_application_date(client_logged):
    Application.objects.create(
        company="Acme",
        title="Engineer",
        application_date=dt.date(2026, 1, 5),
    )
    content = client_logged.get(reverse("tracker:application_board")).content.decode()
    assert "Applied 05 Jan" in content
    assert "No date" not in content


def test_stage_change_rejects_external_return_url(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    response = client_logged.post(
        reverse("tracker:application_set_stage", args=[application.pk]),
        {"stage": Stage.APPLIED, "next": "https://evil.example/"},
    )
    assert response.url == reverse("tracker:application_detail", args=[application.pk])


def test_invalid_task_stays_open_with_values_and_errors(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    response = client_logged.post(
        reverse("tracker:task_create", args=[application.pk]),
        {"title": "Call recruiter", "notes": "Ask about timeline", "due_at": "not-a-date"},
    )

    assert response.status_code == 200
    content = response.content.decode()
    assert "Call recruiter" in content
    assert "Enter a valid date/time." in content
    assert '<details class="disclosure" style="margin-top:10px" open>' in content
    assert Task.objects.count() == 0


def test_archive_action_toggles_and_restores(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer", is_archived=False)
    url = reverse("tracker:application_archive", args=[application.pk])
    client_logged.post(url)
    application.refresh_from_db()
    assert application.is_archived is True
    client_logged.post(url)
    application.refresh_from_db()
    assert application.is_archived is False


def test_stage_change_is_noop_when_unchanged():
    application = Application.objects.create(company="Acme", title="Engineer")
    event = set_stage(application, Stage.SAVED)
    assert event is None
    assert ApplicationEvent.objects.count() == 0


def test_document_upload_stores_and_dedupes(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    upload1 = SimpleUploadedFile("resume.pdf", b"%PDF-1.4 hello", content_type="application/pdf")
    document1, created1 = store_document(upload1, applications=[application])
    assert created1 is True
    assert document1.sha256

    upload2 = SimpleUploadedFile(
        "resume-copy.pdf", b"%PDF-1.4 hello", content_type="application/pdf"
    )
    document2, created2 = store_document(upload2, applications=[application])
    assert created2 is False
    assert document2.pk == document1.pk
    assert Document.objects.count() == 1

    response = client_logged.post(
        reverse("tracker:document_upload", args=[application.pk]),
        {
            "file": SimpleUploadedFile("cover.txt", b"cover letter body"),
            "kind": "cover_letter",
            "label": "Cover v1",
        },
    )
    assert response.status_code == 302
    assert application.documents.count() == 2
    assert ApplicationEvent.objects.filter(
        application=application, event_type=EventType.DOCUMENT_ADDED
    ).exists()


def test_snapshot_is_immutable_and_versioned():
    application = Application.objects.create(company="Acme", title="Engineer")
    first = create_snapshot(application, {"title": "Engineer", "description": "one"})
    second = create_snapshot(application, {"title": "Engineer", "description": "two"})
    assert (first.version, second.version) == (1, 2)
    first.description = "mutated"
    with pytest.raises(ValueError):
        first.save()


@pytest.mark.parametrize(
    "url_name",
    ["tracker:today", "tracker:application_list", "tracker:application_board"],
)
def test_pages_render(client_logged, url_name):
    response = client_logged.get(reverse(url_name))
    assert response.status_code == 200


def test_detail_page_renders_with_relations(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    create_snapshot(
        application,
        {
            "title": "Engineer",
            "description": "Build things",
            "requirements": {"required_skills": ["Python"], "responsibilities": ["Ship"]},
        },
    )
    response = client_logged.get(reverse("tracker:application_detail", args=[application.pk]))
    assert response.status_code == 200
    assert b"Posting" in response.content
    assert b"Python" in response.content


def test_detail_email_link_and_legacy_html_body_are_readable(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    account = GmailAccount.objects.create(email="me+jobs@example.com")
    email = Email.objects.create(
        account=account,
        application=application,
        message_id="message-1",
        thread_id="thread/1",
        subject="Interview invitation",
        body_text=(
            "<html><style>.hidden { display: none }</style><body>"
            "<p>Hello <b>candidate</b></p><script>alert('x')</script>"
            "</body></html>"
        ),
    )
    record_event(application, event_type=EventType.CUSTOM, email=email)

    content = client_logged.get(
        reverse("tracker:application_detail", args=[application.pk])
    ).content.decode()

    assert (
        content.count(
            'href="https://mail.google.com/mail/u/?authuser=me%2Bjobs%40example.com#all/thread%2F1"'
        )
        == 2
    )
    assert "Hello\ncandidate" in content
    assert "display: none" not in content
    assert "alert(&#x27;x&#x27;)" not in content
    assert "<summary>Message body</summary>" in content
    assert 'style="margin-top:8px" open' in content


def test_filter_by_search(client_logged):
    Application.objects.create(company="Acme", title="Backend Engineer")
    Application.objects.create(company="Globex", title="Data Scientist")
    response = client_logged.get(reverse("tracker:application_list"), {"q": "Acme"})
    assert response.status_code == 200
    assert b"Acme" in response.content
    assert b"Globex" not in response.content


def test_filter_searches_snapshot_requirements(client_logged):
    matching = Application.objects.create(company="Acme", title="Engineer")
    create_snapshot(
        matching,
        {"title": "Engineer", "requirements": {"required_skills": ["Terraform"]}},
    )
    Application.objects.create(company="Globex", title="Designer")

    response = client_logged.get(reverse("tracker:application_list"), {"q": "Terraform"})

    assert response.status_code == 200
    assert b"Acme" in response.content
    assert b"Globex" not in response.content


def test_today_displays_upcoming_tasks(client_logged):
    application = Application.objects.create(company="Acme", title="Engineer")
    Task.objects.create(
        application=application,
        title="Prepare portfolio",
        due_at=timezone.now() + dt.timedelta(days=3),
    )

    response = client_logged.get(reverse("tracker:today"))

    assert response.status_code == 200
    assert b"Upcoming tasks" in response.content
    assert b"Prepare portfolio" in response.content
