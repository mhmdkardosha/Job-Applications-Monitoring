from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tracker.backup import backup_all


class Command(BaseCommand):
    help = "Create a consistent database + media backup with rotation."

    def add_arguments(self, parser):
        parser.add_argument("--keep", type=int, default=7, help="Backups to retain")
        parser.add_argument("--out", default=None, help="Override backup directory")

    def handle(self, *args, **options):
        if options["keep"] < 1:
            raise CommandError("--keep must be at least 1")
        backup_dir = options["out"] or settings.BACKUP_DIR
        dest = backup_all(
            db_path=settings.DATABASES["default"]["NAME"],
            media_root=settings.MEDIA_ROOT,
            backup_dir=backup_dir,
            keep=options["keep"],
        )
        self.stdout.write(self.style.SUCCESS(f"Backup written to {dest}"))
