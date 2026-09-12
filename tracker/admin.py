from __future__ import annotations

from django.contrib import admin

from .models import (
    Application,
    ApplicationEvent,
    AppSettings,
    Contact,
    Document,
    Email,
    GmailAccount,
    Interview,
    PostingSnapshot,
    ReviewItem,
    Tag,
    Task,
    WorkItem,
)


class ApplicationEventInline(admin.TabularInline):
    model = ApplicationEvent
    extra = 0
    fields = ("occurred_at", "event_type", "source", "actor", "summary", "confidence")
    readonly_fields = ("recorded_at",)


class InterviewInline(admin.TabularInline):
    model = Interview
    extra = 0


class TaskInline(admin.TabularInline):
    model = Task
    extra = 0


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "company",
        "title",
        "stage",
        "priority",
        "source",
        "application_date",
        "updated_at",
    )
    list_filter = ("stage", "priority", "source", "work_arrangement", "is_archived")
    search_fields = ("company", "title", "requisition_id", "notes")
    filter_horizontal = ("tags",)
    inlines = [ApplicationEventInline, InterviewInline, TaskInline]
    readonly_fields = ("manual_override_fields",)


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ("subject", "from_address", "account", "process_state", "received_at")
    list_filter = ("process_state", "account")
    search_fields = ("subject", "from_address", "body_text")


@admin.register(PostingSnapshot)
class PostingSnapshotAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "application", "version", "capture_state", "captured_at")
    list_filter = ("capture_state", "source")


@admin.register(GmailAccount)
class GmailAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "connection_state", "last_sync_at", "history_days")
    list_filter = ("connection_state",)


admin.site.register(ApplicationEvent)
admin.site.register(Task)
admin.site.register(Interview)
admin.site.register(Contact)
admin.site.register(Document)
admin.site.register(WorkItem)
admin.site.register(Tag)
admin.site.register(AppSettings)
admin.site.register(ReviewItem)
