from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import serve
from django.contrib import admin
from django.urls import include, path, re_path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("tracker.urls")),
    # Loopback-only single-user app: serve managed media regardless of DEBUG
    # (static assets are served by runserver --insecure in the launcher).
    re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
]
