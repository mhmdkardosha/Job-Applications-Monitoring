from __future__ import annotations

import csv
import sys

from django.core.management.base import BaseCommand

from tracker.backup import csv_rows
from tracker.models import Application


class Command(BaseCommand):
    help = "Export an applications summary as CSV (not a full backup)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="-", help="File path or - for stdout")
        parser.add_argument("--include-archived", action="store_true")

    def handle(self, *args, **options):
        queryset = Application.objects.all().order_by("-application_date")
        if not options["include_archived"]:
            queryset = queryset.filter(is_archived=False)
        rows = csv_rows(queryset)

        if options["out"] == "-":
            writer = csv.writer(sys.stdout)
            writer.writerows(rows)
            return
        with open(options["out"], "w", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerows(rows)
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows) - 1} row(s) to {options['out']}"))
