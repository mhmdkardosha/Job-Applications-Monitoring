from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from tracker import sync as sync_mod


class Command(BaseCommand):
    help = "Re-run AI extraction on stored emails that previously failed. No Gmail access."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=50)

    def handle(self, *args, **options):
        report = sync_mod.retry_failed_emails(limit=options["limit"])
        self.stdout.write(json.dumps(report.to_dict(), indent=2))
