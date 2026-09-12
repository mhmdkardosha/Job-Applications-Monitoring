from __future__ import annotations

import re

from django.core.management.base import BaseCommand
from django.db import transaction

from tracker.integrations.extract import normalize_company
from tracker.matching import STAGE_RANK
from tracker.models import (
    Application,
    Email,
    PostingSnapshot,
)
from tracker.services import record_event


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _identity(application: Application) -> tuple[str, str]:
    return (normalize_company(application.company), _norm(application.title))


def _score(application: Application) -> int:
    return (
        STAGE_RANK.get(application.stage, 0) * 10
        + application.events.count() * 3
        + application.emails.count() * 2
        + application.documents.count()
        + application.interviews.count()
    )


def _merge(keeper: Application, duplicate: Application) -> None:
    Email.objects.filter(application=duplicate).update(application=keeper)
    duplicate.events.all().update(application=keeper)
    duplicate.tasks.all().update(application=keeper)
    duplicate.interviews.all().update(application=keeper)
    for contact in duplicate.contacts.all():
        keeper.contacts.add(contact)
    for document in duplicate.documents.all():
        keeper.documents.add(document)
    duplicate.review_items.all().update(resolved_application=keeper)

    next_version = (
        keeper.snapshots.order_by("-version").values_list("version", flat=True).first() or 0
    ) + 1
    for snapshot in duplicate.snapshots.order_by("version"):
        PostingSnapshot.objects.filter(pk=snapshot.pk).update(
            application=keeper, version=next_version
        )
        next_version += 1

    record_event(
        keeper,
        event_type="note",
        summary=f"Merged duplicate application from {duplicate.company} — {duplicate.title}",
        details={"merged_application_id": duplicate.pk},
    )
    duplicate.delete()


class Command(BaseCommand):
    help = "Merge applications with the same normalized company + title. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Actually merge (otherwise dry-run)."
        )

    def handle(self, *args, **options):
        groups: dict[tuple[str, str], list[Application]] = {}
        for application in Application.objects.prefetch_related(
            "events", "emails", "documents", "interviews", "contacts"
        ):
            if not application.title.strip():
                continue
            groups.setdefault(_identity(application), []).append(application)

        duplicates = {k: v for k, v in groups.items() if len(v) > 1}
        if not duplicates:
            self.stdout.write(self.style.SUCCESS("No duplicate applications found."))
            return

        for (company, title), apps in duplicates.items():
            keeper = max(apps, key=_score)
            self.stdout.write(f"\n{company!r} / {title!r}: {len(apps)} records")
            for application in apps:
                marker = "KEEP" if application.pk == keeper.pk else "merge"
                self.stdout.write(
                    f"  [{marker}] #{application.pk} {application.company} — "
                    f"{application.title} ({application.stage})"
                )

        if not options["apply"]:
            self.stdout.write(self.style.WARNING("\nDry run only. Re-run with --apply to merge."))
            return

        merged = 0
        with transaction.atomic():
            for apps in duplicates.values():
                keeper = max(apps, key=_score)
                for application in apps:
                    if application.pk == keeper.pk:
                        continue
                    _merge(keeper, application)
                    merged += 1
        self.stdout.write(self.style.SUCCESS(f"\nMerged {merged} duplicate record(s)."))
