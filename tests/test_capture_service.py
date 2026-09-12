from __future__ import annotations

import hashlib
import json

import pytest
from django.urls import reverse

from tracker import capture_service
from tracker.integrations.capture import PostingSnapshot as CapturedPosting
from tracker.models import (
    Application,
    AppSettings,
    Email,
    GmailAccount,
    PostingSnapshot,
    Source,
    Stage,
    WorkArrangement,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return GmailAccount.objects.create(email="me@example.com")


def _data(**overrides):
    base = {
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "resolved_url": "https://boards.greenhouse.io/acme/jobs/1",
        "title": "Senior Backend Engineer",
        "company": "Acme Corp",
        "job_identifier": "12345",
        "description": "Build reliable services with Python.",
        "capture_state": "structured",
        "location": "Cairo",
        "work_arrangement": "Remote",
        "employment_type": "Full-time",
        "responsibilities": ["Ship services"],
        "required_skills": ["Python"],
        "unknown_fields": [],
    }
    base.update(overrides)
    return base


def test_store_capture_creates_application_and_snapshot():
    snapshot, application, created = capture_service.store_capture(
        _data(), create_if_new=True, source=Source.BROWSER, run_ai=False
    )
    assert created is True
    assert application.source == Source.BROWSER
    assert application.stage == Stage.SAVED
    assert application.work_arrangement == WorkArrangement.REMOTE
    assert snapshot.application_id == application.pk
    assert snapshot.requirements["required_skills"] == ["Python"]


def test_capture_adds_only_source_grounded_skills_and_duties(monkeypatch):
    class FakeRequirements:
        def to_json(self):
            return {
                "responsibilities": ["Design reliable APIs", "Manage a team"],
                "required_skills": ["PostgreSQL", "Kubernetes"],
            }

    monkeypatch.setattr(capture_service, "extract_requirements", lambda client, text: FakeRequirements())
    snapshot, _, _ = capture_service.store_capture(
        _data(description="Design reliable APIs using Python and PostgreSQL."),
        client=object(),
    )
    assert snapshot.requirements["responsibilities"] == ["Ship services", "Design reliable APIs"]
    assert snapshot.requirements["required_skills"] == ["Python", "PostgreSQL"]


def test_store_capture_links_existing_by_url():
    existing = Application.objects.create(
        company="Acme Corp", title="Backend", job_url="https://boards.greenhouse.io/acme/jobs/1"
    )
    snapshot, application, created = capture_service.store_capture(
        _data(), create_if_new=True, source=Source.BROWSER, run_ai=False
    )
    assert created is False
    assert application.pk == existing.pk
    assert PostingSnapshot.objects.filter(application=existing).count() == 1


def test_store_capture_ignores_unusable_pages():
    snapshot, application, created = capture_service.store_capture(
        _data(capture_state="partial", description="Sign in to continue"),
        create_if_new=True,
        source=Source.BROWSER,
        run_ai=False,
    )
    assert created is False
    assert application is None
    assert snapshot.pk is None
    assert PostingSnapshot.objects.count() == 0


def test_capture_url_uses_fetch_and_stores(monkeypatch):
    captured = CapturedPosting(
        url="https://example.com/jobs/9",
        resolved_url="https://example.com/jobs/9",
        source="email-link",
        captured_at="2026-09-11T00:00:00+00:00",
        title="Platform Engineer",
        company="Beta Labs",
        description="Run Kubernetes.",
        capture_state="structured",
        required_skills=["Terraform"],
    )
    monkeypatch.setattr(capture_service, "fetch_capture", lambda url, source: captured)
    snapshot, application, created = capture_service.capture_url(
        "https://example.com/jobs/9", create_if_new=True, run_ai=False
    )
    assert snapshot.title == "Platform Engineer"
    assert application.company == "Beta Labs"
    assert snapshot.requirements["required_skills"] == ["Terraform"]


def test_collect_email_job_links_filters_and_dedupes(account):
    Email.objects.create(
        account=account,
        message_id="m1",
        extracted={
            "links": [
                "https://boards.greenhouse.io/acme/jobs/1",
                "https://evil.example.com/unsubscribe?u=1",
                "https://www.linkedin.com/jobs/view/2",
            ]
        },
    )
    links = capture_service.collect_email_job_links()
    assert "https://boards.greenhouse.io/acme/jobs/1" in links
    assert "https://www.linkedin.com/jobs/view/2" in links
    assert all("unsubscribe" not in link for link in links)

    capture_service.store_capture(
        _data(url="https://boards.greenhouse.io/acme/jobs/1"),
        run_ai=False,
    )
    remaining = capture_service.collect_email_job_links()
    assert "https://boards.greenhouse.io/acme/jobs/1" not in remaining


def test_capture_links_from_emails(account, monkeypatch):
    Email.objects.create(
        account=account,
        message_id="m1",
        extracted={"links": ["https://boards.greenhouse.io/acme/jobs/7"]},
    )

    def fake_capture(url, **kwargs):
        snapshot, application, created = capture_service.store_capture(
            _data(url=url, resolved_url=url), create_if_new=False, run_ai=False
        )
        return snapshot, application, created

    monkeypatch.setattr(capture_service, "capture_url", fake_capture)
    report = capture_service.capture_links_from_emails(limit=5)
    assert report["captured"] == 1
    assert PostingSnapshot.objects.count() == 1


def _pair_token() -> str:
    config = AppSettings.load()
    token = "test-token-abc"
    config.capture_extension_token_hash = hashlib.sha256(token.encode()).hexdigest()
    config.save()
    return token


def test_api_capture_requires_token(client):
    response = client.post(
        reverse("tracker:api_capture"),
        data=json.dumps(_data()),
        content_type="application/json",
    )
    assert response.status_code == 401


def test_api_capture_with_token(client, monkeypatch):
    token = _pair_token()

    class FakeRequirements:
        def to_json(self):
            return {
                "responsibilities": [],
                "required_skills": ["Python", "Postgres"],
                "preferred_skills": [],
                "qualifications": [],
                "experience": "",
                "education": "",
                "location": "",
                "work_arrangement": "",
                "employment_type": "",
                "compensation": "",
                "benefits": [],
                "evidence": [],
            }

    monkeypatch.setattr(capture_service, "get_ai_client", lambda: object())
    monkeypatch.setattr(
        capture_service, "extract_requirements", lambda client, text: FakeRequirements()
    )

    response = client.post(
        reverse("tracker:api_capture"),
        data=json.dumps(_data()),
        content_type="application/json",
        HTTP_X_CAPTURE_TOKEN=token,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created_application"] is True
    assert body["application_id"]
    snapshot = PostingSnapshot.objects.get(pk=body["snapshot_id"])
    # Structured JSON-LD skills win over AI enrichment.
    assert snapshot.requirements["required_skills"] == ["Python"]


def test_api_capture_rejects_wrong_token(client):
    _pair_token()
    response = client.post(
        reverse("tracker:api_capture"),
        data=json.dumps(_data()),
        content_type="application/json",
        HTTP_X_CAPTURE_TOKEN="wrong",
    )
    assert response.status_code == 401


def test_api_capture_rejects_string_boolean(client):
    token = _pair_token()
    response = client.post(
        reverse("tracker:api_capture"),
        data=json.dumps(_data(create_if_new="false")),
        content_type="application/json",
        HTTP_X_CAPTURE_TOKEN=token,
    )
    assert response.status_code == 400


def test_api_capture_reports_unusable_page_without_fake_snapshot_id(client):
    token = _pair_token()
    response = client.post(
        reverse("tracker:api_capture"),
        data=json.dumps(_data(capture_state="partial", error="Sign-in page")),
        content_type="application/json",
        HTTP_X_CAPTURE_TOKEN=token,
    )
    assert response.status_code == 422
    assert response.json()["error"] == "Sign-in page"
    assert PostingSnapshot.objects.count() == 0
