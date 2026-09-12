from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from tracker.backup import RestoreError, restore_backup


class Command(BaseCommand):
    help = "Restore a backup over the local database and media. Stop the app first."

    def add_arguments(self, parser):
        parser.add_argument("source", help="Backup directory (jobmon-…)")
        parser.add_argument("--yes", action="store_true", help="Skip confirmation")

    def handle(self, *args, **options):
        if not options["yes"]:
            answer = input(f"Restore {options['source']} over the current database? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                self.stdout.write("Aborted.")
                return
        connections.close_all()
        try:
            manifest = restore_backup(
                options["source"],
                db_path=settings.DATABASES["default"]["NAME"],
                media_root=settings.MEDIA_ROOT,
            )
        except RestoreError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Restored backup from {manifest.get('created_at', 'unknown')} "
                f"({manifest.get('applications', '?')} applications)."
            )
        )
        self.stdout.write(
            "Credentials are not restored: reconnect Gmail in Settings and re-enter "
            "the AI key if needed."
        )
