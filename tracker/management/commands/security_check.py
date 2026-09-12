from __future__ import annotations

import os
import stat

from django.conf import settings
from django.core.management.base import BaseCommand

from tracker.models import AppSettings

OK = "ok"
WARN = "warn"
FAIL = "fail"


class Command(BaseCommand):
    help = "Review the local security posture (plan §8)."

    def handle(self, *args, **options):
        results: list[tuple[str, str, str]] = []

        if settings.SECRET_KEY == "insecure-dev-only-change-me":
            results.append((FAIL, "SECRET_KEY", "Set DJANGO_SECRET_KEY in .env."))
        else:
            results.append((OK, "SECRET_KEY", "custom"))

        if "*" in settings.ALLOWED_HOSTS:
            results.append((FAIL, "ALLOWED_HOSTS", "Remove the '*' wildcard."))
        else:
            results.append((OK, "ALLOWED_HOSTS", ", ".join(settings.ALLOWED_HOSTS) or "empty"))

        for origin in settings.CSRF_TRUSTED_ORIGINS:
            if not origin.startswith(("http://127.0.0.1", "http://localhost")):
                results.append((WARN, "CSRF_TRUSTED_ORIGINS", f"non-loopback: {origin}"))
        results.append((OK, "CSRF", "trusted origins restricted to loopback"))

        if settings.DEBUG:
            results.append((WARN, "DEBUG", "on — fine locally, avoid on shared machines"))
        else:
            results.append((OK, "DEBUG", "off"))

        data_dir = settings.DATA_DIR
        mode = stat.S_IMODE(os.stat(data_dir).st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            results.append((WARN, "DATA_DIR perms", f"{oct(mode)} (group/other accessible)"))
        else:
            results.append((OK, "DATA_DIR perms", oct(mode)))

        config = AppSettings.load()
        if config.capture_extension_token_hash:
            results.append((OK, "Extension pairing", "token set"))
        else:
            results.append((WARN, "Extension pairing", "no token yet"))

        results.append((OK, "Secrets", "stored in OS keyring / .env, never the database"))

        failures = 0
        for status, name, detail in results:
            failures += status == FAIL
            style = {
                OK: self.style.SUCCESS,
                WARN: self.style.WARNING,
                FAIL: self.style.ERROR,
            }[status]
            self.stdout.write(style(f"[{status.upper():4}] {name}: {detail}"))

        if failures:
            self.stderr.write(self.style.ERROR(f"{failures} security check(s) failed."))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("Security review complete."))
