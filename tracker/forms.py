"""Forms for the tracker UI."""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django import forms

from .models import (
    BOARD_CARD_FIELD_CHOICES,
    Application,
    Contact,
    Document,
    DocumentKind,
    Interview,
    Priority,
    Source,
    Stage,
    Tag,
    Task,
    WorkArrangement,
)

DATE_INPUT = forms.DateInput


class StyledFormMixin:
    """Apply consistent widget classes without per-field boilerplate."""

    def _style(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check")
            elif isinstance(widget, forms.SelectMultiple):
                widget.attrs.setdefault("class", "form-multiselect")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select")
            elif isinstance(widget, forms.Textarea):
                widget.attrs.setdefault("class", "form-textarea")
                widget.attrs.setdefault("rows", 4)
            else:
                widget.attrs.setdefault("class", "form-input")
            if isinstance(widget, forms.DateInput):
                widget.input_type = "date"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class BoardCardFieldsForm(forms.Form):
    properties = forms.MultipleChoiceField(
        choices=BOARD_CARD_FIELD_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )


class ApplicationForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Application
        fields = [
            "company",
            "title",
            "job_url",
            "requisition_id",
            "source",
            "location",
            "work_arrangement",
            "employment_type",
            "salary_min",
            "salary_max",
            "salary_currency",
            "salary_period",
            "application_date",
            "closing_date",
            "priority",
            "stage",
            "tags",
            "notes",
        ]
        widgets = {
            "job_url": forms.URLInput(attrs={"placeholder": "https://…"}),
            "salary_currency": forms.TextInput(attrs={"placeholder": "USD", "maxlength": 3}),
            "notes": forms.Textarea(attrs={"rows": 5}),
            "application_date": DATE_INPUT(),
            "closing_date": DATE_INPUT(),
        }

    def clean(self):
        cleaned = super().clean()
        low = cleaned.get("salary_min")
        high = cleaned.get("salary_max")
        if low is not None and high is not None and low > high:
            self.add_error("salary_max", "Maximum must be greater than or equal to minimum.")
        return cleaned


class DocumentUploadForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Document
        fields = ["file", "kind", "label"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].initial = DocumentKind.RESUME


class ContactForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Contact
        fields = ["name", "email", "phone", "company", "role", "linkedin_url", "notes"]


class TaskForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "notes", "due_at"]
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}


class TaskStandaloneForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["application", "title", "notes", "due_at"]
        widgets = {"due_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["application"].required = False
        self.fields["application"].queryset = Application.objects.filter(
            is_archived=False
        ).order_by("company", "title")
        self.fields["application"].empty_label = "No application"


class InterviewForm(StyledFormMixin, forms.ModelForm):
    participants = forms.CharField(
        required=False,
        help_text="One name or email per line.",
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    class Meta:
        model = Interview
        fields = [
            "round",
            "kind",
            "scheduled_start",
            "scheduled_end",
            "timezone",
            "participants",
            "location",
            "meeting_url",
            "prep_notes",
            "notes",
            "outcome",
        ]
        widgets = {
            "scheduled_start": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}
            ),
            "scheduled_end": forms.DateTimeInput(
                format="%Y-%m-%dT%H:%M", attrs={"type": "datetime-local"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and not self.is_bound:
            self.initial["participants"] = "\n".join(self.instance.participants or [])

    def clean_participants(self):
        value = self.cleaned_data.get("participants", "")
        return [line.strip() for line in value.splitlines() if line.strip()]

    def clean_timezone(self):
        value = self.cleaned_data.get("timezone", "").strip()
        if value:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise forms.ValidationError(
                    "Enter a valid IANA timezone, such as Africa/Cairo."
                ) from exc
        return value

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("scheduled_start")
        end = cleaned.get("scheduled_end")
        if start and end and end <= start:
            self.add_error("scheduled_end", "End time must be after the start time.")
        return cleaned


class StageSelectForm(forms.Form):
    stage = forms.ChoiceField(choices=Stage.choices, widget=forms.Select)
    next = forms.CharField(widget=forms.HiddenInput, required=False)


class ApplicationFilterForm(StyledFormMixin, forms.Form):
    q = forms.CharField(required=False, label="Search")
    company = forms.CharField(required=False)
    role = forms.CharField(required=False)
    stage = forms.ChoiceField(required=False, choices=[("", "All stages"), *Stage.choices])
    priority = forms.ChoiceField(required=False, choices=[("", "Any priority"), *Priority.choices])
    source = forms.ChoiceField(required=False, choices=[("", "Any source"), *Source.choices])
    sort = forms.ChoiceField(
        required=False,
        choices=[
            ("", "Recently updated"),
            ("company", "Company A–Z"),
            ("role", "Role A–Z"),
            ("applied_new", "Applied newest"),
            ("applied_old", "Applied oldest"),
        ],
    )
    location = forms.CharField(required=False)
    work_arrangement = forms.ChoiceField(
        required=False,
        choices=[("", "Any arrangement"), *WorkArrangement.choices],
    )
    applied_after = forms.DateField(required=False, widget=DATE_INPUT())
    applied_before = forms.DateField(required=False, widget=DATE_INPUT())
    tag = forms.ModelChoiceField(
        required=False,
        queryset=Tag.objects.order_by("name"),
        empty_label="Any tag",
    )
    show_archived = forms.BooleanField(required=False, label="Show archived")
