from __future__ import annotations

import datetime as dt
import time
from email.utils import parsedate_to_datetime

from django.core.management.base import BaseCommand

from tracker.integrations.gmail import get_service
from tracker.models import Application, Email


def _received_at(raw: dict) -> dt.datetime | None:
    internal = raw.get("internalDate")
    if internal:
        try:
            return dt.datetime.fromtimestamp(int(internal) / 1000, tz=dt.UTC)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    for header in raw.get("payload", {}).get("headers", []) or []:
        if header.get("name", "").lower() == "date":
            try:
                parsed = parsedate_to_datetime(header.get("value", ""))
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                return parsed
    return None


class Command(BaseCommand):
    help = (
        "Repair stored email dates from Gmail metadata, then fix application "
        "dates and event timestamps. Re-fetches metadata only, not new mail."
    )

    def add_arguments(self, parser):
        parser.add_argument("--account", default=None)
        parser.add_argument("--limit", type=int, default=None)
        parser.add_argument(
            "--all",
            action="store_true",
            help="Re-fetch even emails that already have a date.",
        )
        parser.add_argument(
            "--no-fetch",
            action="store_true",
            help="Only recompute events/application dates from stored data.",
        )

    def handle(self, *args, **options):
        query = Email.objects.select_related("account").order_by("id")
        if not options["all"]:
            query = query.filter(received_at__isnull=True)
        emails = list(query[: options["limit"]] if options["limit"] else query)

        fetched = 0
        if emails and not options["no_fetch"]:
            _account, service = get_service(options["account"])
            self.stdout.write(f"Fetching metadata for {len(emails)} email(s)…")
            for index, email in enumerate(emails, start=1):
                try:
                    raw = (
                        service.users()
                        .messages()
                        .get(
                            userId="me",
                            id=email.message_id,
                            format="metadata",
                            metadataHeaders=["Date"],
                        )
                        .execute()
                    )
                except Exception as exc:  # noqa: BLE001
                    self.stderr.write(f"  fetch failed {email.message_id}: {exc}")
                    continue
                received = _received_at(raw)
                if received:
                    email.received_at = received
                    email.save(update_fields=["received_at", "updated_at"])
                    fetched += 1
                if index % 50 == 0:
                    self.stdout.write(f"  {index}/{len(emails)}")
                time.sleep(0.05)

        # Fix events that are linked to an email.
        event_fixes = 0
        for email in Email.objects.exclude(received_at__isnull=True).prefetch_related("events"):
            for event in email.events.all():
                if event.occurred_at != email.received_at:
                    event.occurred_at = email.received_at
                    event.save(update_fields=["occurred_at"])
                    event_fixes += 1

        # Pair non-email events (e.g. stage changes) with the nearest email event.
        paired = 0
        for application in Application.objects.prefetch_related("events__email"):
            events = list(application.events.order_by("recorded_at", "id"))
            email_positions = [
                index
                for index, event in enumerate(events)
                if event.email_id and event.email and event.email.received_at
            ]
            if not email_positions:
                continue
            for index, event in enumerate(events):
                if event.email_id or event.source != "gmail":
                    continue
                nearest = min(email_positions, key=lambda pos: (abs(pos - index), pos))
                target = events[nearest].email.received_at
                if event.occurred_at != target:
                    event.occurred_at = target
                    event.save(update_fields=["occurred_at"])
                    paired += 1

        # Fill missing application dates from linked email dates (submitted only).
        app_fixes = 0
        for application in Application.objects.filter(
            source="gmail", is_archived=False
        ).prefetch_related("emails"):
            if not application.is_submitted:
                continue
            if "application_date" in (application.manual_override_fields or []):
                continue
            dates = [
                email.received_at.date() for email in application.emails.all() if email.received_at
            ]
            if not dates:
                continue
            new_date = min(dates)
            if application.application_date != new_date:
                application.application_date = new_date
                application.save(update_fields=["application_date", "updated_at"])
                app_fixes += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Dates fetched: {fetched} | events fixed: {event_fixes} | "
                f"stage events paired: {paired} | application dates set: {app_fixes}"
            )
        )
