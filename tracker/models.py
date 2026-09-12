"""Domain models for the job application monitor (plan §7).

The current stage is a field on Application; every change is also recorded as
an ApplicationEvent so history is preserved. PostingSnapshots are immutable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import quote

from django.conf import settings
from django.db import models
from django.utils import timezone


class Stage(models.TextChoices):
    SAVED = "saved", "Saved"
    PREPARING = "preparing", "Preparing"
    APPLIED = "applied", "Applied"
    SCREENING = "screening", "Screening"
    INTERVIEWING = "interviewing", "Interviewing"
    OFFER = "offer", "Offer"
    ACCEPTED = "accepted", "Accepted"
    REJECTED = "rejected", "Rejected"
    WITHDRAWN = "withdrawn", "Withdrawn"


# Board shows the live pipeline; Rejected/Withdrawn are terminal side states.
PIPELINE_STAGES = [
    Stage.SAVED,
    Stage.PREPARING,
    Stage.APPLIED,
    Stage.SCREENING,
    Stage.INTERVIEWING,
    Stage.OFFER,
    Stage.ACCEPTED,
]
CLOSED_STAGES = [Stage.REJECTED, Stage.WITHDRAWN]


class Source(models.TextChoices):
    MANUAL = "manual", "Manual"
    GMAIL = "gmail", "Gmail"
    BROWSER = "browser", "Browser capture"
    IMPORT = "import", "Imported"


class WorkArrangement(models.TextChoices):
    ONSITE = "onsite", "On-site"
    HYBRID = "hybrid", "Hybrid"
    REMOTE = "remote", "Remote"
    UNKNOWN = "unknown", "Unknown"


class EmploymentType(models.TextChoices):
    FULL_TIME = "full_time", "Full-time"
    PART_TIME = "part_time", "Part-time"
    CONTRACT = "contract", "Contract"
    INTERNSHIP = "internship", "Internship"
    TEMPORARY = "temporary", "Temporary"
    UNKNOWN = "unknown", "Unknown"


class Priority(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    URGENT = "urgent", "Urgent"


class SalaryPeriod(models.TextChoices):
    HOUR = "hour", "per hour"
    DAY = "day", "per day"
    MONTH = "month", "per month"
    YEAR = "year", "per year"


class EventType(models.TextChoices):
    APPLICATION_CONFIRMATION = "application_confirmation", "Application confirmation"
    ACKNOWLEDGMENT = "acknowledgment", "Acknowledgment"
    RECRUITER_CONTACT = "recruiter_contact", "Recruiter contact"
    ASSESSMENT = "assessment", "Assessment"
    INTERVIEW_INVITATION = "interview_invitation", "Interview invitation"
    INTERVIEW_RESCHEDULE = "interview_reschedule", "Interview reschedule"
    INTERVIEW_CANCELLATION = "interview_cancellation", "Interview cancellation"
    OFFER = "offer", "Offer"
    REJECTION = "rejection", "Rejection"
    WITHDRAWAL = "withdrawal", "Withdrawal"
    STAGE_CHANGE = "stage_change", "Stage change"
    NOTE = "note", "Note"
    DOCUMENT_ADDED = "document_added", "Document added"
    CUSTOM = "custom", "Custom"


class EventActor(models.TextChoices):
    SYSTEM = "system", "System"
    USER = "user", "User"


class GmailConnectionState(models.TextChoices):
    CONNECTED = "connected", "Connected"
    NEEDS_RECONNECT = "needs_reconnect", "Needs reconnect"
    ERROR = "error", "Error"
    DISCONNECTED = "disconnected", "Disconnected"


class EmailProcessState(models.TextChoices):
    PENDING = "pending", "Pending"
    REVIEW = "review", "In review"
    CLASSIFIED = "classified", "Classified"
    IGNORED = "ignored", "Ignored"
    ERROR = "error", "Error"


class CaptureState(models.TextChoices):
    STRUCTURED = "structured", "Structured"
    TEXT = "text", "Readable text"
    PARTIAL = "partial", "Partial"
    FAILED = "failed", "Failed"


class DocumentKind(models.TextChoices):
    RESUME = "resume", "Resume"
    COVER_LETTER = "cover_letter", "Cover letter"
    OTHER = "other", "Other"


class TaskStatus(models.TextChoices):
    OPEN = "open", "Open"
    DONE = "done", "Done"
    SNOOZED = "snoozed", "Snoozed"
    CANCELLED = "cancelled", "Cancelled"


class InterviewKind(models.TextChoices):
    PHONE = "phone", "Phone screen"
    VIDEO = "video", "Video call"
    TECHNICAL = "technical", "Technical"
    ONSITE = "onsite", "On-site"
    HR = "hr", "HR / culture"
    OTHER = "other", "Other"


class InterviewOutcome(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"
    NO_SHOW = "no_show", "No show"
    PASSED = "passed", "Passed"
    FAILED = "failed", "Failed"


class WorkItemStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    DONE = "done", "Done"
    FAILED = "failed", "Failed"


class ReviewStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    LINKED = "linked", "Linked"
    CREATED = "created", "Created"
    IGNORED = "ignored", "Ignored"


class ReviewKind(models.TextChoices):
    UNMATCHED = "unmatched", "No match found"
    AMBIGUOUS = "ambiguous", "Ambiguous match"
    NEEDS_REVIEW = "needs_review", "Needs review"
    CONFLICT = "conflict", "Conflicts with your correction"


BOARD_CARD_FIELD_CHOICES = (
    ("priority", "Priority"),
    ("application_date", "Application date"),
    ("location", "Location"),
    ("work_arrangement", "Work arrangement"),
    ("employment_type", "Employment type"),
    ("salary", "Salary"),
    ("source", "Source"),
    ("tags", "Tags"),
    ("closing_date", "Closing date"),
)


def default_board_card_fields() -> list[str]:
    return ["priority", "application_date"]


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Tag(models.Model):
    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=60, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class GmailAccount(TimeStampedModel):
    email = models.EmailField(unique=True)
    credential_ref = models.CharField(max_length=120, blank=True)
    history_id = models.CharField(max_length=64, blank=True)
    connection_state = models.CharField(
        max_length=20,
        choices=GmailConnectionState.choices,
        default=GmailConnectionState.CONNECTED,
    )
    include_archived = models.BooleanField(default=True)
    history_days = models.PositiveIntegerField(default=90)
    backfill_completed = models.BooleanField(default=False)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    sync_started_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["email"]

    def __str__(self) -> str:
        return self.email


class Application(TimeStampedModel):
    company = models.CharField(max_length=200)
    title = models.CharField(max_length=200)
    job_url = models.URLField(max_length=1000, blank=True)
    requisition_id = models.CharField(max_length=120, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    location = models.CharField(max_length=200, blank=True)
    work_arrangement = models.CharField(
        max_length=20,
        choices=WorkArrangement.choices,
        default=WorkArrangement.UNKNOWN,
    )
    employment_type = models.CharField(
        max_length=20,
        choices=EmploymentType.choices,
        default=EmploymentType.UNKNOWN,
    )
    salary_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_currency = models.CharField(max_length=3, blank=True)
    salary_period = models.CharField(max_length=10, choices=SalaryPeriod.choices, blank=True)
    application_date = models.DateField(null=True, blank=True)
    closing_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    stage = models.CharField(max_length=20, choices=Stage.choices, default=Stage.SAVED)
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="applications")
    is_archived = models.BooleanField(default=False)
    # Fields the user edited by hand; automation must not overwrite these (plan §4.7).
    manual_override_fields = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["stage"]),
            models.Index(fields=["company"]),
            models.Index(fields=["application_date"]),
        ]

    def __str__(self) -> str:
        return f"{self.company} — {self.title}"

    @property
    def is_submitted(self) -> bool:
        return self.stage not in (Stage.SAVED, Stage.PREPARING)

    @property
    def salary_display(self) -> str:
        if self.salary_min is None and self.salary_max is None:
            return ""
        low = f"{self.salary_min:,.0f}" if self.salary_min is not None else ""
        high = f"{self.salary_max:,.0f}" if self.salary_max is not None else ""
        amount = f"{low}–{high}" if low and high else (low or high)
        parts = [self.salary_currency, amount, self.get_salary_period_display()]
        return " ".join(p for p in parts if p).strip()


class Email(TimeStampedModel):
    account = models.ForeignKey(GmailAccount, on_delete=models.CASCADE, related_name="emails")
    message_id = models.CharField(max_length=255)
    thread_id = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    from_address = models.CharField(max_length=320, blank=True)
    to_addresses = models.CharField(max_length=500, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    snippet = models.TextField(blank=True)
    body_text = models.TextField(blank=True)
    label_ids = models.JSONField(default=list, blank=True)
    content_hash = models.CharField(max_length=64, blank=True)
    extracted = models.JSONField(default=dict, blank=True)
    process_state = models.CharField(
        max_length=20,
        choices=EmailProcessState.choices,
        default=EmailProcessState.PENDING,
    )
    processing_error = models.TextField(blank=True)
    application = models.ForeignKey(
        Application,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emails",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account", "message_id"], name="uniq_email_account_message"
            )
        ]
        ordering = ["-received_at", "-id"]
        indexes = [models.Index(fields=["thread_id"])]

    def __str__(self) -> str:
        return self.subject or self.message_id

    @property
    def gmail_url(self) -> str:
        if not self.thread_id:
            return ""
        account = quote(self.account.email, safe="")
        thread = quote(self.thread_id, safe="")
        return f"https://mail.google.com/mail/u/?authuser={account}#all/{thread}"

    @property
    def display_body(self) -> str:
        text = self.body_text or self.snippet
        lowered = text.lower()
        if not any(
            tag in lowered for tag in ("<html", "<body", "<div", "<table", "<style", "<script")
        ):
            return text
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(text, "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text("\n", strip=True)


class ApplicationEvent(models.Model):
    application = models.ForeignKey(Application, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=40, choices=EventType.choices)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    actor = models.CharField(max_length=10, choices=EventActor.choices, default=EventActor.SYSTEM)
    email = models.ForeignKey(
        Email, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    summary = models.CharField(max_length=300, blank=True)
    details = models.JSONField(default=dict, blank=True)
    confidence = models.FloatField(null=True, blank=True)
    is_correction = models.BooleanField(default=False)
    # Deterministic key so replayed imports never create duplicate events (plan §4.8).
    dedupe_key = models.CharField(max_length=255, unique=True, null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]

    def __str__(self) -> str:
        return f"{self.get_event_type_display()} · {self.application_id}"


class ReviewItem(TimeStampedModel):
    email = models.ForeignKey(Email, on_delete=models.CASCADE, related_name="review_items")
    kind = models.CharField(max_length=20, choices=ReviewKind.choices)
    status = models.CharField(
        max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.PENDING
    )
    suggestion = models.JSONField(default=dict, blank=True)
    candidates = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_action = models.CharField(max_length=40, blank=True)
    resolved_application = models.ForeignKey(
        Application,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="review_items",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["email"], name="uniq_review_email")]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.email_id}"

    @property
    def can_accept(self) -> bool:
        return (
            sum(
                1
                for candidate in self.candidates
                if isinstance(candidate, dict) and candidate.get("strong")
            )
            == 1
        )


class PostingSnapshot(models.Model):
    application = models.ForeignKey(
        Application,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="snapshots",
    )
    version = models.PositiveIntegerField(default=1)
    url = models.URLField(max_length=1000)
    resolved_url = models.URLField(max_length=1000, blank=True)
    source = models.CharField(max_length=40, blank=True)
    captured_at = models.DateTimeField(default=timezone.now)
    title = models.CharField(max_length=300, blank=True)
    company = models.CharField(max_length=200, blank=True)
    job_identifier = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    requirements = models.JSONField(default=dict, blank=True)
    raw_html = models.TextField(blank=True)
    capture_state = models.CharField(
        max_length=20,
        choices=CaptureState.choices,
        default=CaptureState.TEXT,
    )
    unknown_fields = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    content_hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["-captured_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["application", "version"], name="uniq_snapshot_app_version"
            )
        ]

    def __str__(self) -> str:
        return f"Snapshot v{self.version} · {self.title or self.url}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("PostingSnapshot is immutable; create a new version.")
        if self.description and not self.content_hash:
            self.content_hash = hashlib.sha256(self.description.encode("utf-8")).hexdigest()
        super().save(*args, **kwargs)


class Task(TimeStampedModel):
    application = models.ForeignKey(
        Application,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="tasks",
    )
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=TaskStatus.choices, default=TaskStatus.OPEN)
    snoozed_until = models.DateTimeField(null=True, blank=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["due_at", "id"]
        indexes = [models.Index(fields=["status", "due_at"])]

    def __str__(self) -> str:
        return self.title


class Interview(TimeStampedModel):
    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, related_name="interviews"
    )
    round = models.PositiveIntegerField(default=1)
    kind = models.CharField(
        max_length=20, choices=InterviewKind.choices, default=InterviewKind.VIDEO
    )
    scheduled_start = models.DateTimeField(null=True, blank=True)
    scheduled_end = models.DateTimeField(null=True, blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    participants = models.JSONField(default=list, blank=True)
    location = models.CharField(max_length=300, blank=True)
    meeting_url = models.URLField(max_length=1000, blank=True)
    prep_notes = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    outcome = models.CharField(
        max_length=20,
        choices=InterviewOutcome.choices,
        default=InterviewOutcome.SCHEDULED,
    )

    class Meta:
        ordering = ["scheduled_start", "round", "id"]

    def __str__(self) -> str:
        return f"Round {self.round} · {self.application_id}"


class Contact(TimeStampedModel):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    company = models.CharField(max_length=200, blank=True)
    role = models.CharField(max_length=200, blank=True)
    linkedin_url = models.URLField(max_length=1000, blank=True)
    notes = models.TextField(blank=True)
    applications = models.ManyToManyField(Application, blank=True, related_name="contacts")

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


def document_upload_path(instance: Document, filename: str) -> str:
    return f"documents/{timezone.now():%Y/%m}/{Path(filename).name}"


class Document(TimeStampedModel):
    file = models.FileField(upload_to=document_upload_path)
    kind = models.CharField(max_length=20, choices=DocumentKind.choices, default=DocumentKind.OTHER)
    label = models.CharField(max_length=200, blank=True)
    original_filename = models.CharField(max_length=300, blank=True)
    sha256 = models.CharField(max_length=64, unique=True)
    size = models.PositiveBigIntegerField(default=0)
    mime_type = models.CharField(max_length=120, blank=True)
    applications = models.ManyToManyField(Application, blank=True, related_name="documents")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.label or self.original_filename or self.file.name


class WorkItem(TimeStampedModel):
    kind = models.CharField(max_length=60)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=12, choices=WorkItemStatus.choices, default=WorkItemStatus.PENDING
    )
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    run_after = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["run_after", "id"]
        indexes = [models.Index(fields=["status", "run_after"])]

    def __str__(self) -> str:
        return f"{self.kind} ({self.status})"


class AppSettings(models.Model):
    """Singleton configuration (plan §3, Settings screen)."""

    display_timezone = models.CharField(max_length=64, default="Africa/Cairo")
    gmail_history_days = models.PositiveIntegerField(default=90)
    follow_up_days = models.PositiveIntegerField(default=7)
    ai_model = models.CharField(max_length=60, default="deepseek-flash")
    ai_monthly_ceiling_usd = models.DecimalField(max_digits=8, decimal_places=2, default=10)
    capture_extension_token_hash = models.CharField(max_length=128, blank=True)
    board_card_fields = models.JSONField(default=default_board_card_fields, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "App settings"

    def __str__(self) -> str:
        return "Application settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> AppSettings:
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "display_timezone": getattr(settings, "DISPLAY_TIMEZONE", "Africa/Cairo"),
                "gmail_history_days": getattr(settings, "GMAIL_DEFAULT_HISTORY_DAYS", 90),
                "ai_model": getattr(settings, "AI_MODEL", "deepseek-flash"),
                "ai_monthly_ceiling_usd": getattr(settings, "AI_MONTHLY_CEILING_USD", 10),
            },
        )
        return obj
