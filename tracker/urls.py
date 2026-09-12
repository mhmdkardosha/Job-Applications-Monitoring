from __future__ import annotations

from django.urls import path

from . import views

app_name = "tracker"

urlpatterns = [
    path("", views.today, name="today"),
    path("applications/", views.application_list, name="application_list"),
    path("board/", views.application_board, name="application_board"),
    path("applications/new/", views.application_create, name="application_create"),
    path("applications/<int:pk>/", views.application_detail, name="application_detail"),
    path("applications/<int:pk>/edit/", views.application_update, name="application_update"),
    path(
        "applications/<int:pk>/stage/",
        views.application_set_stage,
        name="application_set_stage",
    ),
    path(
        "applications/<int:pk>/archive/",
        views.application_archive,
        name="application_archive",
    ),
    path("applications/<int:pk>/documents/", views.document_upload, name="document_upload"),
    path("applications/<int:pk>/tasks/", views.task_create, name="task_create"),
    path("applications/<int:pk>/interviews/", views.interview_create, name="interview_create"),
    path("applications/<int:pk>/contacts/", views.contact_create, name="contact_create"),
    path("tasks/<int:pk>/toggle/", views.task_toggle, name="task_toggle"),
    path("tasks/", views.task_list, name="task_list"),
    path("tasks/new/", views.task_create_standalone, name="task_create_standalone"),
    path("tasks/<int:pk>/snooze/", views.task_snooze, name="task_snooze"),
    path("tasks/<int:pk>/delete/", views.task_delete, name="task_delete"),
    path(
        "applications/<int:pk>/follow-up/",
        views.follow_up_create,
        name="follow_up_create",
    ),
    path(
        "applications/<int:pk>/follow-up-draft/",
        views.follow_up_draft_view,
        name="follow_up_draft",
    ),
    path("interviews/<int:pk>/ics/", views.interview_ics, name="interview_ics"),
    path("interviews/<int:pk>/edit/", views.interview_update, name="interview_update"),
    path("interviews/<int:pk>/delete/", views.interview_delete, name="interview_delete"),
    path("documents/", views.document_list, name="document_list"),
    path(
        "applications/<int:pk>/documents/link/",
        views.document_link,
        name="document_link",
    ),
    path(
        "applications/<int:pk>/documents/<int:document_id>/unlink/",
        views.document_unlink,
        name="document_unlink",
    ),
    path("reports/", views.reports_view, name="reports"),
    path("review/", views.review_inbox, name="review_inbox"),
    path("review/bulk/", views.review_bulk, name="review_bulk"),
    path("review/<int:pk>/action/", views.review_action, name="review_action"),
    path("settings/", views.settings_view, name="settings"),
    path("settings/export.csv", views.export_csv_view, name="export_csv"),
    path("settings/backup/", views.backup_now, name="backup_now"),
    path("settings/save/", views.settings_save, name="settings_save"),
    path("settings/gmail/sync/", views.gmail_sync_now, name="gmail_sync_now"),
    path("settings/gmail/preview/", views.gmail_preview, name="gmail_preview"),
    path("settings/gmail/retry/", views.retry_failed_view, name="retry_failed"),
    path(
        "settings/postings/capture/",
        views.capture_email_links_view,
        name="capture_email_links",
    ),
    path(
        "settings/extension/token/",
        views.generate_capture_token,
        name="generate_capture_token",
    ),
    path(
        "settings/extension/revoke/",
        views.revoke_capture_token,
        name="revoke_capture_token",
    ),
    path(
        "applications/<int:pk>/capture/",
        views.capture_from_url,
        name="capture_from_url",
    ),
    path(
        "applications/<int:pk>/capture-text/",
        views.capture_from_text,
        name="capture_from_text",
    ),
    path("snapshots/<int:pk>/print/", views.snapshot_print, name="snapshot_print"),
    path("api/capture/", views.api_capture, name="api_capture"),
]
