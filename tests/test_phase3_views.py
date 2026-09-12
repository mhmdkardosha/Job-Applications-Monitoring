from __future__ import annotations

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse

from tracker.integrations.extract import EmailExtraction
from tracker.models import (
    Application,
    AppSettings,
    Contact,
    Email,
    EmailProcessState,
    GmailAccount,
    Interview,
    ReviewItem,
    ReviewStatus,
    Stage,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def client_logged(client):
    user = get_user_model().objects.create_user(username="local", password="pw12345!")
    client.force_login(user)
    return client


@pytest.fixture
def review_item():
    account = GmailAccount.objects.create(email="me@example.com")
    email = Email.objects.create(
        account=account,
        message_id="m-review",
        thread_id="t-review",
        subject="Interview invitation",
        from_address="recruiter@acmecorp.com",
        body_text="Would Tuesday work?",
        process_state=EmailProcessState.REVIEW,
    )
    extraction = EmailExtraction(
        event_type="interview_invitation",
        company="Acme Corp",
        role="Backend Engineer",
        confidence=0.8,
        needs_review=True,
    )
    return ReviewItem.objects.create(
        email=email,
        kind="needs_review",
        suggestion=extraction.to_json(),
        candidates=[],
        note="no unambiguous match",
    )


def test_settings_renders(client_logged):
    response = client_logged.get(reverse("tracker:settings"))
    assert response.status_code == 200
    assert b"Gmail connections" in response.content


def test_app_settings_initial_defaults_follow_environment(settings):
    settings.AI_MODEL = "test-model"
    settings.AI_MONTHLY_CEILING_USD = 3.5
    config = AppSettings.load()
    assert config.ai_model == "test-model"
    assert float(config.ai_monthly_ceiling_usd) == 3.5


def test_settings_save_rejects_invalid_timezone(client_logged):
    response = client_logged.post(
        reverse("tracker:settings_save"),
        {
            "gmail_history_days": "90",
            "follow_up_days": "7",
            "display_timezone": "Not/AZone",
            "ai_model": "deepseek-flash",
            "ai_monthly_ceiling_usd": "10",
        },
    )
    assert response.status_code == 302
    from tracker.models import AppSettings

    assert AppSettings.load().display_timezone == settings.DISPLAY_TIMEZONE


def test_review_inbox_renders(client_logged, review_item):
    response = client_logged.get(reverse("tracker:review_inbox"))
    assert response.status_code == 200
    assert b"Interview invitation" in response.content


def test_review_ignore_resolves(client_logged, review_item):
    response = client_logged.post(
        reverse("tracker:review_action", args=[review_item.pk]),
        {"action": "ignore"},
    )
    assert response.status_code == 302
    review_item.refresh_from_db()
    assert review_item.status == ReviewStatus.IGNORED
    assert review_item.email.process_state == EmailProcessState.IGNORED


def test_review_link_applies_stage_and_event(client_logged, review_item):
    application = Application.objects.create(company="Acme Corp", title="Backend Engineer")
    response = client_logged.post(
        reverse("tracker:review_action", args=[review_item.pk]),
        {"action": "link", "application": application.pk},
    )
    assert response.status_code == 302
    application.refresh_from_db()
    review_item.refresh_from_db()
    assert application.stage == Stage.INTERVIEWING
    assert review_item.status == ReviewStatus.LINKED
    assert review_item.email.application_id == application.pk
    assert application.events.count() == 2  # stage change + confirmed event
    assert "stage" not in application.manual_override_fields


def test_review_link_creates_extracted_contact_and_interview(client_logged, review_item):
    review_item.suggestion = EmailExtraction(
        event_type="interview_invitation",
        company="Acme Corp",
        role="Backend Engineer",
        interview_start="2026-09-20T10:00:00+02:00",
        contacts=[{"name": "Nora", "email": "nora@example.com", "role": "Recruiter"}],
        confidence=0.9,
    ).to_json()
    review_item.save(update_fields=["suggestion", "updated_at"])
    application = Application.objects.create(company="Acme Corp", title="Backend Engineer")

    response = client_logged.post(
        reverse("tracker:review_action", args=[review_item.pk]),
        {"action": "link", "application": application.pk},
    )

    assert response.status_code == 302
    assert Contact.objects.filter(email="nora@example.com", applications=application).exists()
    assert Interview.objects.filter(application=application).exists()


def test_review_create_requires_company_and_role(client_logged):
    account = GmailAccount.objects.create(email="x@example.com")
    email = Email.objects.create(account=account, message_id="m2", body_text="hi")
    item = ReviewItem.objects.create(
        email=email,
        kind="needs_review",
        suggestion=EmailExtraction(event_type="offer").to_json(),
    )
    response = client_logged.post(
        reverse("tracker:review_action", args=[item.pk]), {"action": "create"}
    )
    assert response.status_code == 302
    item.refresh_from_db()
    assert item.status == ReviewStatus.PENDING


def _make_item(account, message_id, event_type="offer", company="Acme", role="Eng"):
    email = Email.objects.create(
        account=account,
        message_id=message_id,
        body_text="body",
        process_state=EmailProcessState.REVIEW,
    )
    return ReviewItem.objects.create(
        email=email,
        kind="needs_review",
        suggestion=EmailExtraction(
            event_type=event_type, company=company, role=role, confidence=0.9
        ).to_json(),
    )


def test_review_bulk_ignore(client_logged, review_item):
    response = client_logged.post(
        reverse("tracker:review_bulk"),
        {"bulk_action": "bulk_ignore", "item_ids": [review_item.pk]},
    )
    assert response.status_code == 302
    review_item.refresh_from_db()
    assert review_item.status == ReviewStatus.IGNORED


def test_review_bulk_link(client_logged):
    account = GmailAccount.objects.create(email="bulk@example.com")
    application = Application.objects.create(company="Acme", title="Eng")
    first = _make_item(account, "b1")
    second = _make_item(account, "b2")
    response = client_logged.post(
        reverse("tracker:review_bulk"),
        {
            "bulk_action": "bulk_link",
            "application": application.pk,
            "item_ids": [first.pk, second.pk],
        },
    )
    assert response.status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == ReviewStatus.LINKED
    assert second.status == ReviewStatus.LINKED
    assert first.email.application_id == application.pk


def test_review_create_marks_email_classified(client_logged, review_item):
    response = client_logged.post(
        reverse("tracker:review_action", args=[review_item.pk]), {"action": "create"}
    )
    assert response.status_code == 302
    review_item.email.refresh_from_db()
    assert review_item.email.process_state == EmailProcessState.CLASSIFIED


def test_review_accept_refuses_ambiguous_strong_matches(client_logged, review_item):
    app1 = Application.objects.create(company="Acme Corp", title="Backend Engineer")
    app2 = Application.objects.create(company="Acme Corp", title="Backend Engineer II")
    review_item.candidates = [
        {"application_id": app1.pk, "strong": True, "label": str(app1)},
        {"application_id": app2.pk, "strong": True, "label": str(app2)},
    ]
    review_item.save(update_fields=["candidates", "updated_at"])
    response = client_logged.post(
        reverse("tracker:review_action", args=[review_item.pk]), {"action": "accept"}
    )
    assert response.status_code == 302
    review_item.refresh_from_db()
    assert review_item.status == ReviewStatus.PENDING
