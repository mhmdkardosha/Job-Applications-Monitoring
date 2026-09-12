"""Activate the configured display timezone for each request.

Precedence: AppSettings.display_timezone (editable in the UI) then the
DISPLAY_TIMEZONE setting. Timestamps stay UTC in the database.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone


def _configured_timezone() -> str | None:
    try:
        from .models import AppSettings

        value = AppSettings.load().display_timezone
        if value:
            return value
    except Exception:  # noqa: BLE001 - DB not ready / unauthenticated request
        pass
    return getattr(settings, "DISPLAY_TIMEZONE", None)


class DisplayTimezoneMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tzname = _configured_timezone()
        if tzname:
            try:
                timezone.activate(ZoneInfo(tzname))
            except Exception:  # noqa: BLE001 - invalid tz falls back to UTC
                timezone.deactivate()
        else:
            timezone.deactivate()
        try:
            return self.get_response(request)
        finally:
            # Leave the thread in the default timezone so the setting does not
            # leak into other requests, management commands, or tests.
            timezone.deactivate()
