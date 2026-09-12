from __future__ import annotations

from django.core.management.base import BaseCommand

from tracker.integrations.gmail import authorize
from tracker.sync import ensure_account


class Command(BaseCommand):
    help = "Connect a Gmail account via OAuth (opens a browser) and store the token."

    def handle(self, *args, **options):
        account_email, _creds = authorize()
        ensure_account(account_email)
        self.stdout.write(self.style.SUCCESS(f"Connected {account_email}."))
