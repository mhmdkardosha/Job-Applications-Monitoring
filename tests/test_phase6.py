from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from tracker import sync as sync_mod
from tracker.backup import (
    DB_NAME,
    MANIFEST_NAME,
    RestoreError,
    backup_all,
    csv_rows,
    restore_backup,
    verify_backup,
)
from tracker.integrations.extract import EmailExtraction
from tracker.integrations.gmail import Message
from tracker.integrations.secrets import register_gmail_account, registry_path
from tracker.models import (
    Application,
    Email,
    EmailProcessState,
    GmailAccount,
    ReviewItem,
)

pytestmark = pytest.mark.django_db


def _make_sqlite(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t (x integer)")
    connection.execute("INSERT INTO t VALUES (1)")
    connection.commit()
    connection.close()


def test_backup_and_restore_roundtrip(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _make_sqlite(db_path)

    media = tmp_path / "media"
    media.mkdir()
    (media / "resume.txt").write_text("hello", encoding="utf-8")

    dest = backup_all(
        db_path=db_path,
        media_root=media,
        backup_dir=tmp_path / "backups",
        keep=3,
    )
    assert (dest / DB_NAME).exists()
    assert (dest / MANIFEST_NAME).exists()

    connection = sqlite3.connect(db_path)
    connection.execute("DELETE FROM t")
    connection.commit()
    connection.close()

    manifest = restore_backup(dest, db_path=db_path, media_root=media)
    assert manifest["applications"] >= 0
    connection = sqlite3.connect(db_path)
    assert connection.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
    connection.close()
    assert (media / "resume.txt").read_text(encoding="utf-8") == "hello"


def test_restore_clears_stale_media_when_backup_has_none(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _make_sqlite(db_path)
    dest = backup_all(
        db_path=db_path,
        media_root=tmp_path / "missing-media",
        backup_dir=tmp_path / "backups",
        keep=1,
    )
    media = tmp_path / "live-media"
    media.mkdir()
    (media / "stale.txt").write_text("stale", encoding="utf-8")

    restore_backup(dest, db_path=db_path, media_root=media)

    assert media.is_dir()
    assert list(media.iterdir()) == []


def test_backup_rotation(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _make_sqlite(db_path)
    backups = tmp_path / "backups"
    for day in range(3):
        backup_all(
            db_path=db_path,
            media_root=tmp_path / "media",
            backup_dir=backups,
            keep=2,
            when=dt.datetime(2026, 1, day + 1, tzinfo=dt.UTC),
        )
    remaining = sorted(p.name for p in backups.glob("jobmon-*"))
    assert len(remaining) == 2
    assert "jobmon-20260101" not in " ".join(remaining)


def test_verify_backup_detects_tampering(tmp_path):
    db_path = tmp_path / "db.sqlite3"
    _make_sqlite(db_path)
    dest = backup_all(
        db_path=db_path,
        media_root=tmp_path / "media",
        backup_dir=tmp_path / "backups",
        keep=1,
    )
    (dest / DB_NAME).write_bytes(b"corrupted")
    with pytest.raises(RestoreError):
        verify_backup(dest)


def test_csv_export_rows():
    application = Application.objects.create(
        company="Acme",
        title="Engineer",
        stage="applied",
        application_date=dt.date(2026, 1, 5),
    )
    rows = csv_rows(Application.objects.filter(pk=application.pk))
    assert rows[0][0] == "company"
    assert rows[1][0] == "Acme"
    assert rows[1][2] == "Applied"


def test_gmail_account_registry_uses_configured_data_directory(tmp_path, monkeypatch):
    data_dir = tmp_path / "private-data"
    monkeypatch.setenv("JOBMON_DATA_DIR", str(data_dir))
    register_gmail_account("me@example.com")
    assert registry_path() == data_dir / "gmail_accounts.json"
    assert "me@example.com" in registry_path().read_text(encoding="utf-8")


def test_security_check_passes_with_hardened_settings(settings):
    settings.SECRET_KEY = "x" * 50
    settings.ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
    settings.DEBUG = False
    call_command("security_check")  # should not raise SystemExit


def test_security_check_fails_with_insecure_settings(settings):
    settings.SECRET_KEY = "insecure-dev-only-change-me"
    settings.ALLOWED_HOSTS = ["*"]
    with pytest.raises(SystemExit):
        call_command("security_check")


def test_ingest_continues_after_one_message_failure(account, monkeypatch):
    def fake_get_message(service, message_id, account_email):
        if message_id == "bad":
            raise RuntimeError("boom")
        return Message(
            id=message_id,
            thread_id="t",
            account=account_email,
            headers={"From": "no-reply@greenhouse.io", "Subject": "Your application"},
            snippet="",
            body_text="We received your application.",
        )

    monkeypatch.setattr(sync_mod, "get_message", fake_get_message)
    monkeypatch.setattr(
        sync_mod,
        "extract_email",
        lambda client, msg: EmailExtraction(
            event_type="application_confirmation",
            company="Acme",
            role="Engineer",
            confidence=0.95,
        ),
    )
    report = sync_mod.SyncReport(account=account.email)
    with pytest.raises(RuntimeError, match="Could not fetch Gmail message"):
        sync_mod._ingest_ids(account, object(), ["bad", "good"], object(), report)
    assert report.errors == 1
    assert report.new_emails == 1
    assert Email.objects.count() == 1


def test_ingest_reports_progress_and_renews_lease_on_early_exits(account, monkeypatch):
    Email.objects.create(account=account, message_id="existing")

    def fake_get_message(service, message_id, account_email):
        if message_id == "failed":
            raise RuntimeError("boom")
        return Message(
            id=message_id,
            thread_id="t",
            account=account_email,
            headers={"From": "friend@example.com", "Subject": "Lunch?"},
            snippet="Want to grab food?",
            body_text="Want to grab food?",
        )

    heartbeats = []
    progress = []
    lease = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    monkeypatch.setattr(sync_mod, "get_message", fake_get_message)
    monkeypatch.setattr(
        sync_mod,
        "_heartbeat_lock",
        lambda account, current: heartbeats.append(current) or current,
    )

    report = sync_mod.SyncReport(account=account.email)
    with pytest.raises(RuntimeError, match="Could not fetch Gmail message"):
        sync_mod._ingest_ids(
            account,
            object(),
            ["existing", "ignored", "failed"],
            object(),
            report,
            progress.append,
            lease=lease,
        )

    assert len(heartbeats) == 3
    assert [item["done"] for item in progress] == [1, 2, 3]


def test_email_content_is_escaped_in_review_inbox(client, settings):
    user = get_user_model().objects.create_user(username="local", password="pw12345!")
    client.force_login(user)
    account = GmailAccount.objects.create(email="me@example.com")
    email = Email.objects.create(
        account=account,
        message_id="xss",
        subject="<script>alert(1)</script>",
        body_text="<img src=x onerror=alert(2)>",
        process_state=EmailProcessState.REVIEW,
    )
    ReviewItem.objects.create(
        email=email,
        kind="needs_review",
        suggestion={"event_type": "offer", "company": "Acme"},
    )
    content = client.get(reverse("tracker:review_inbox")).content.decode()
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


@pytest.fixture
def account():
    return GmailAccount.objects.create(email="me@example.com")
