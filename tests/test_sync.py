from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from googleapiclient.errors import HttpError

from tracker import sync as sync_mod
from tracker.integrations.extract import EmailExtraction
from tracker.integrations.gmail import Message
from tracker.models import (
    Application,
    Email,
    EmailProcessState,
    GmailAccount,
    ReviewItem,
    Stage,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return GmailAccount.objects.create(email="me@example.com", history_id="111")


@pytest.fixture
def stored_email(account):
    return Email.objects.create(
        account=account,
        message_id="m1",
        thread_id="t1",
        subject="Thanks for applying",
        from_address="no-reply@greenhouse.io",
        body_text="We received your application.",
        received_at=dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC),
    )


def extraction(**overrides):
    base = dict(
        event_type="application_confirmation",
        company="Acme Corp",
        role="Senior Backend Engineer",
        requisition_id="",
        confidence=0.95,
        needs_review=False,
    )
    base.update(overrides)
    return EmailExtraction(**base)


def test_candidate_reason_detects_job_sender():
    message = Message(
        id="m",
        thread_id="t",
        account="me@example.com",
        headers={"From": "no-reply@greenhouse.io", "Subject": "Hello"},
        snippet="",
        body_text="",
    )
    assert sync_mod.candidate_reason(message) == "job-platform sender"


def test_candidate_reason_rejects_unrelated():
    message = Message(
        id="m",
        thread_id="t",
        account="me@example.com",
        headers={"From": "friend@example.com", "Subject": "Lunch?"},
        snippet="Want to grab food?",
        body_text="Want to grab food?",
    )
    assert sync_mod.candidate_reason(message) is None


def test_store_email_is_idempotent(account):
    message = Message(
        id="m9",
        thread_id="t9",
        account=account.email,
        headers={"From": "a@b.com", "Subject": "S"},
        snippet="s",
        body_text="b",
    )
    first, created_first = sync_mod.store_email(account, message)
    second, created_second = sync_mod.store_email(account, message)
    assert created_first is True
    assert created_second is False
    assert first.pk == second.pk
    assert Email.objects.count() == 1


def test_store_email_uses_gmail_internal_date(account):
    message = Message(
        id="dated",
        thread_id="dated-thread",
        account=account.email,
        headers={"From": "a@b.com", "Subject": "S"},
        snippet="s",
        body_text="b",
        internal_date="1756728000000",
    )
    email, created = sync_mod.store_email(account, message)
    assert created is True
    assert email.received_at == dt.datetime(2025, 9, 1, 12, 0, tzinfo=dt.UTC)


def test_process_email_auto_creates_and_does_not_duplicate(stored_email, monkeypatch):
    monkeypatch.setattr(sync_mod, "extract_email", lambda client, msg: extraction())
    report = sync_mod.SyncReport(account=stored_email.account.email)
    sync_mod.process_email(stored_email, object(), report)
    stored_email.refresh_from_db()
    assert Application.objects.count() == 1
    assert stored_email.process_state == EmailProcessState.CLASSIFIED
    assert report.created_applications == 1

    sync_mod.process_email(stored_email, object(), report)
    assert Application.objects.count() == 1


def test_process_email_reuses_versioned_content_cache(account, stored_email, monkeypatch):
    calls = 0

    def fake_extract(client, message):
        nonlocal calls
        calls += 1
        return extraction()

    monkeypatch.setattr(sync_mod, "extract_email", fake_extract)
    sync_mod.process_email(stored_email, object(), sync_mod.SyncReport(account=account.email))
    duplicate_content = Email.objects.create(
        account=account,
        message_id="m-cache",
        thread_id="another-thread",
        subject=stored_email.subject,
        from_address=stored_email.from_address,
        body_text=stored_email.body_text,
        received_at=stored_email.received_at,
    )
    sync_mod.process_email(
        duplicate_content,
        object(),
        sync_mod.SyncReport(account=account.email),
    )
    duplicate_content.refresh_from_db()
    assert calls == 1
    assert duplicate_content.content_hash == stored_email.content_hash


def test_process_email_routes_ambiguity_to_review(stored_email, monkeypatch):
    monkeypatch.setattr(
        sync_mod,
        "extract_email",
        lambda client, msg: extraction(
            event_type="interview_invitation", requisition_id="", role=""
        ),
    )
    report = sync_mod.SyncReport(account=stored_email.account.email)
    sync_mod.process_email(stored_email, object(), report)
    stored_email.refresh_from_db()
    assert ReviewItem.objects.count() == 1
    assert stored_email.process_state == EmailProcessState.REVIEW
    assert report.review_items == 1


def test_process_email_never_regresses_stage(account, monkeypatch):
    application = Application.objects.create(
        company="Acme Corp", title="Backend", requisition_id="1", stage=Stage.OFFER
    )
    email = Email.objects.create(
        account=account,
        message_id="m2",
        thread_id="t2",
        from_address="no-reply@greenhouse.io",
        body_text="Your application",
        received_at=dt.datetime(2026, 9, 1, tzinfo=dt.UTC),
    )
    from django.utils import timezone

    sync_mod.record_event(
        application,
        event_type="offer",
        summary="offer",
        occurred_at=timezone.now(),
    )
    monkeypatch.setattr(
        sync_mod,
        "extract_email",
        lambda client, msg: extraction(requisition_id="1", role="Backend"),
    )
    report = sync_mod.SyncReport(account=account.email)
    sync_mod.process_email(email, object(), report)
    application.refresh_from_db()
    assert application.stage == Stage.OFFER
    assert report.updated_applications == 0


class _Exec:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return self._data


class _Users:
    def getProfile(self, **kwargs):
        return _Exec({"historyId": "999"})


class _Service:
    def users(self):
        return _Users()


def test_history_expiry_triggers_recovery(account, monkeypatch):
    def raise_404(service, history_id):
        raise HttpError(SimpleNamespace(status=404, reason="Not Found"), b"{}")

    monkeypatch.setattr(sync_mod, "_history_message_ids", raise_404)
    monkeypatch.setattr(sync_mod, "get_service", lambda email: (email, _Service()))
    monkeypatch.setattr(sync_mod, "list_message_ids", lambda service, **kw: ["m3"])
    monkeypatch.setattr(
        sync_mod,
        "get_message",
        lambda service, message_id, account_email: Message(
            id=message_id,
            thread_id="t3",
            account=account_email,
            headers={"From": "no-reply@greenhouse.io", "Subject": "Your application"},
            snippet="",
            body_text="We received your application for Backend Engineer.",
        ),
    )
    monkeypatch.setattr(sync_mod, "extract_email", lambda client, msg: extraction())

    report = sync_mod.sync_account(account, client=object(), limit=10)
    account.refresh_from_db()
    assert report.history_expired is True
    assert report.mode == "recovery"
    assert account.history_id == "999"


def test_sync_lock_prevents_concurrent_runs(account, monkeypatch):
    from django.utils import timezone

    account.sync_started_at = timezone.now()
    account.save(update_fields=["sync_started_at"])
    with pytest.raises(sync_mod.SyncBusy):
        sync_mod.sync_account(account, client=object())


def test_sync_releases_lock_when_ai_client_setup_fails(account, monkeypatch):
    monkeypatch.setattr(
        sync_mod,
        "get_ai_client",
        lambda: (_ for _ in ()).throw(RuntimeError("missing key")),
    )
    with pytest.raises(RuntimeError, match="missing key"):
        sync_mod.sync_account(account)
    account.refresh_from_db()
    assert account.sync_started_at is None


def test_stale_worker_cannot_release_or_renew_newer_lease(account):
    old_lease = sync_mod._acquire_lock(account)
    assert old_lease is not None
    new_lease = old_lease + dt.timedelta(seconds=1)
    GmailAccount.objects.filter(pk=account.pk).update(sync_started_at=new_lease)

    sync_mod._release_lock(account, old_lease)
    account.refresh_from_db()
    assert account.sync_started_at == new_lease
    with pytest.raises(sync_mod.SyncBusy, match="lock was lost"):
        sync_mod._heartbeat_lock(account, old_lease)


def test_sync_does_not_advance_history_after_fetch_failure(account, monkeypatch):
    monkeypatch.setattr(sync_mod, "get_service", lambda email: (email, object()))
    monkeypatch.setattr(
        sync_mod,
        "_history_message_ids",
        lambda service, history_id: (["m-fail"], "222"),
    )
    monkeypatch.setattr(
        sync_mod,
        "get_message",
        lambda service, message_id, account_email: (_ for _ in ()).throw(
            RuntimeError("temporary fetch failure")
        ),
    )
    with pytest.raises(RuntimeError, match="Could not fetch Gmail"):
        sync_mod.sync_account(account, client=object())
    account.refresh_from_db()
    assert account.history_id == "111"
    assert account.sync_started_at is None


def test_limited_backfill_resumes_without_skipping_messages(account, monkeypatch):
    monkeypatch.setattr(sync_mod, "get_service", lambda email: (email, _Service()))
    monkeypatch.setattr(sync_mod, "list_message_ids", lambda service, **kw: ["m1", "m2"])
    monkeypatch.setattr(
        sync_mod,
        "get_message",
        lambda service, message_id, account_email: Message(
            id=message_id,
            thread_id=message_id,
            account=account_email,
            headers={"From": "no-reply@greenhouse.io", "Subject": "Your application"},
            snippet="",
            body_text="We received your application.",
        ),
    )
    monkeypatch.setattr(sync_mod, "extract_email", lambda client, message: extraction())

    sync_mod.sync_account(account, backfill=True, limit=1, client=object())
    account.refresh_from_db()
    assert account.history_id == ""
    assert account.backfill_completed is False
    assert Email.objects.count() == 1

    sync_mod.sync_account(account, limit=1, client=object())
    account.refresh_from_db()
    assert account.history_id == "999"
    assert account.backfill_completed is True
    assert Email.objects.count() == 2


def test_retry_failed_emails_reprocesses(account, monkeypatch):
    email = Email.objects.create(
        account=account,
        message_id="err1",
        thread_id="t-err",
        from_address="no-reply@greenhouse.io",
        body_text="Your application",
        process_state=EmailProcessState.ERROR,
        processing_error="Model did not return a JSON object.",
    )
    monkeypatch.setattr(sync_mod, "extract_email", lambda client, msg: extraction())
    report = sync_mod.retry_failed_emails(client=object(), limit=10)
    email.refresh_from_db()
    assert email.process_state == EmailProcessState.CLASSIFIED
    assert Application.objects.count() == 1
    assert report.listed == 1
    assert report.errors == 0


def test_merge_duplicate_applications():
    from django.core.management import call_command
    from django.utils import timezone

    Application.objects.create(company="Acme Corp.", title="Backend Engineer")
    duplicate = Application.objects.create(company="Acme, Inc", title="backend engineer")
    Email.objects.create(
        account=GmailAccount.objects.create(email="m@example.com"),
        message_id="d1",
        application=duplicate,
    )
    duplicate.events.create(event_type="offer", occurred_at=timezone.now(), summary="offer")

    call_command("merge_duplicate_applications", "--apply")
    assert Application.objects.count() == 1
    surviving = Application.objects.get()
    assert surviving.emails.count() == 1
    assert surviving.events.filter(event_type="offer").count() == 1


def test_store_email_parses_internal_date(account):
    message = Message(
        id="dated-1",
        thread_id="t",
        account=account.email,
        headers={"From": "a@b.com", "Subject": "S"},
        snippet="",
        body_text="",
        internal_date="1756717920000",
    )
    email, _created = sync_mod.store_email(account, message)
    assert email.received_at is not None
    assert email.received_at.date().isoformat() == "2025-09-01"


def test_create_application_falls_back_to_email_date(stored_email):
    application = sync_mod._create_application_from_email(stored_email, extraction(event_date=None))
    assert application.application_date.isoformat() == "2026-09-01"


def test_create_application_does_not_use_outcome_email_date(stored_email):
    application = sync_mod._create_application_from_email(
        stored_email,
        extraction(event_type="rejection", event_date=None),
    )
    assert application.application_date is None


def test_repair_received_at_from_metadata():
    from tracker.management.commands.repair_email_dates import _received_at

    raw = {"internalDate": "1756717920000", "payload": {"headers": []}}
    parsed = _received_at(raw)
    assert parsed is not None and parsed.date().isoformat() == "2025-09-01"
    assert (
        _received_at(
            {"payload": {"headers": [{"name": "Date", "value": "Mon, 01 Sep 2026 09:12:00 +0000"}]}}
        )
        .date()
        .isoformat()
        == "2026-09-01"
    )


def test_repair_command_overrides_gmail_application_date(account):
    from django.core.management import call_command

    application = Application.objects.create(
        company="Acme Corp",
        title="Engineer",
        source="gmail",
        stage=Stage.APPLIED,
        application_date=dt.date(2026, 1, 1),
    )
    Email.objects.create(
        account=account,
        message_id="repair-1",
        application=application,
        received_at=dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC),
    )
    call_command("repair_email_dates", "--no-fetch")
    application.refresh_from_db()
    assert application.application_date == dt.date(2026, 9, 1)
