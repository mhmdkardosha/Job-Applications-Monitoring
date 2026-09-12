from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from tracker import sync as sync_mod
from tracker.models import GmailAccount


class Command(BaseCommand):
    help = "Sync Gmail now (backfill on first run, incremental afterwards)."

    def add_arguments(self, parser):
        parser.add_argument("--account", default=None, help="Gmail address")
        parser.add_argument(
            "--backfill",
            action="store_true",
            help="Re-scan the history window instead of incremental history sync",
        )
        parser.add_argument("--days", type=int, default=None, help="History window in days")
        parser.add_argument("--limit", type=int, default=None, help="Cap messages (testing)")
        parser.add_argument("--all", action="store_true", help="Sync every account")

    def _resolve_accounts(self, account_email: str | None) -> list[GmailAccount]:
        if account_email:
            return [sync_mod.ensure_account(account_email)]
        accounts = list(GmailAccount.objects.all())
        if accounts:
            return accounts
        known = sync_mod.known_account_emails()
        if len(known) == 1:
            return [sync_mod.ensure_account(known[0])]
        raise CommandError(
            "No Gmail account found. Run `python -m tracker.integrations.gmail auth` first, "
            "or pass --account."
        )

    def handle(self, *args, **options):
        accounts = (
            list(GmailAccount.objects.all())
            if options["all"]
            else self._resolve_accounts(options["account"])
        )
        if not accounts:
            self.stdout.write("No Gmail accounts configured; nothing to sync.")
            return

        failures = []
        for account in accounts:
            self.stdout.write(f"Syncing {account.email}…")
            try:
                report = sync_mod.sync_account(
                    account,
                    backfill=options["backfill"],
                    days=options["days"],
                    limit=options["limit"],
                    progress=self._progress,
                )
            except sync_mod.SyncBusy as exc:
                self.stdout.write(self.style.WARNING(f"  skipped: {exc}"))
                continue
            except Exception as exc:  # noqa: BLE001 - continue with other accounts
                failures.append(account.email)
                self.stderr.write(self.style.ERROR(f"  failed: {exc}"))
                continue
            self.stdout.write(self.style.SUCCESS(json.dumps(report.to_dict(), indent=2)))
        if failures:
            raise CommandError(f"Sync failed for {len(failures)} account(s).")

    def _progress(self, payload: dict | None) -> None:
        if payload:
            self.stderr.write(f"  {payload}")
