from __future__ import annotations

import pytest

from tracker.integrations.extract import EmailExtraction
from tracker.matching import (
    Action,
    decide,
    find_matches,
    is_regression,
    target_stage,
    weak_candidates,
)
from tracker.models import (
    Application,
    Email,
    GmailAccount,
    Stage,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def account():
    return GmailAccount.objects.create(email="me@example.com")


@pytest.fixture
def email(account):
    return Email.objects.create(
        account=account,
        message_id="m1",
        thread_id="t1",
        subject="Thanks for applying",
        from_address="no-reply@greenhouse.io",
        body_text="Your application was received.",
    )


def make_extraction(**overrides) -> EmailExtraction:
    base = dict(
        event_type="application_confirmation",
        company="Acme Corp",
        role="Senior Backend Engineer",
        requisition_id="",
        confidence=0.95,
        needs_review=False,
    )
    base.update(overrides)
    return EmailExtraction(**base)


def test_target_stage_mapping():
    assert target_stage("interview_invitation") == "interviewing"
    assert target_stage("offer") == "offer"
    assert target_stage("rejection") == "rejected"
    assert target_stage("recruiter_contact") is None


def test_is_regression():
    assert is_regression(Stage.INTERVIEWING, Stage.APPLIED) is True
    assert is_regression(Stage.APPLIED, Stage.INTERVIEWING) is False


def test_find_match_by_requisition(email):
    app = Application.objects.create(company="Acme Corp", title="Backend", requisition_id="12345")
    matches = find_matches(make_extraction(requisition_id="12345"), email)
    assert [m.application.pk for m in matches] == [app.pk]
    assert "requisition" in matches[0].reason


def test_find_match_by_job_url(email):
    app = Application.objects.create(
        company="Acme Corp", title="Backend", job_url="https://jobs.example.com/123"
    )
    matches = find_matches(
        make_extraction(requisition_id="", links=["https://jobs.example.com/123/"]),
        email,
    )
    assert [m.application.pk for m in matches] == [app.pk]


def test_generic_candidate_login_url_does_not_match(email):
    Application.objects.create(
        company="Acme Corp",
        title="First role",
        job_url="https://acme.wd3.myworkdayjobs.com/Careers/login",
    )

    matches = find_matches(
        make_extraction(
            company="Acme Corp",
            role="Different role",
            links=["https://acme.wd3.myworkdayjobs.com/Careers/login"],
        ),
        email,
    )

    assert matches == []


def test_find_match_by_thread_continuity(account, email):
    app = Application.objects.create(company="Acme Corp", title="Backend")
    Email.objects.create(account=account, message_id="m0", thread_id="t1", application=app)
    matches = find_matches(make_extraction(requisition_id=""), email)
    assert [m.application.pk for m in matches] == [app.pk]
    assert matches[0].reason == "thread continuity"


def test_company_title_is_only_weak(email):
    app = Application.objects.create(company="Acme Corp", title="Senior Backend Engineer")
    matches = find_matches(make_extraction(requisition_id=""), email)
    assert matches == []
    weak = weak_candidates(make_extraction(requisition_id=""))
    assert [m.application.pk for m in weak] == [app.pk]


def test_weak_candidates_require_company_and_title(email):
    other_role = Application.objects.create(company="Acme Corp", title="Data Scientist")
    weak = weak_candidates(make_extraction(requisition_id=""))
    assert [m.application.pk for m in weak] == []
    assert other_role.pk not in [m.application.pk for m in weak]


def test_decide_unrelated_is_ignored(email):
    decision = decide(make_extraction(event_type="unrelated"), email)
    assert decision.action == Action.IGNORE


def test_decide_strong_match_auto_updates(email):
    app = Application.objects.create(company="Acme Corp", title="Backend", requisition_id="1")
    decision = decide(
        make_extraction(
            event_type="interview_invitation",
            requisition_id="1",
            role="Backend",
            interview_start="2026-09-20T10:00:00+02:00",
        ),
        email,
    )
    assert decision.action == Action.AUTO_UPDATE
    assert decision.application.pk == app.pk
    assert decision.target_stage == "interviewing"


def test_decide_multiple_strong_matches_review(email):
    Application.objects.create(company="A", title="X", requisition_id="1")
    Application.objects.create(company="B", title="Y", requisition_id="1")
    decision = decide(make_extraction(requisition_id="1"), email)
    assert decision.action == Action.REVIEW
    assert decision.review_kind == "ambiguous"


def test_decide_clear_confirmation_auto_creates(email):
    decision = decide(make_extraction(requisition_id=""), email)
    assert decision.action == Action.AUTO_CREATE


def test_decide_confirmation_for_identical_existing_links(email):
    app = Application.objects.create(company="Acme Corp", title="Senior Backend Engineer")
    decision = decide(make_extraction(requisition_id=""), email)
    assert decision.action == Action.AUTO_UPDATE
    assert decision.application.pk == app.pk
    assert decision.reason == "identical existing application"


def test_decide_confirmation_matches_captured_job_board_title(email):
    app = Application.objects.create(
        company="Perpay",
        title="Job Application for Data Science Internship, Summer 2027 at Perpay - Career's Page",
        job_url="https://job-boards.greenhouse.io/perpay/jobs/4076978007",
    )
    decision = decide(make_extraction(
        company="Perpay",
        role="Data Science Internship, Summer 2027",
        links=["https://perpay.com/company"],
    ), email)
    assert decision.action == Action.AUTO_UPDATE
    assert decision.application.pk == app.pk


def test_decide_confirmation_reviews_multiple_same_role_candidates(email):
    for _ in range(2):
        Application.objects.create(company="Perpay", title="Data Science Internship, Summer 2027")
    decision = decide(make_extraction(
        company="Perpay", role="Data Science Internship, Summer 2027"
    ), email)
    assert decision.action == Action.REVIEW
    assert decision.review_kind == "ambiguous"


def test_decide_confirmation_without_role_goes_to_review(email):
    decision = decide(make_extraction(requisition_id="", role=""), email)
    assert decision.action == Action.REVIEW


def test_decide_low_confidence_match_reviews(email):
    Application.objects.create(company="Acme Corp", title="Backend", requisition_id="1")
    decision = decide(
        make_extraction(event_type="offer", requisition_id="1", confidence=0.4),
        email,
    )
    assert decision.action == Action.REVIEW
    assert decision.review_kind == "needs_review"


def test_decide_honors_extraction_review_flag(email):
    Application.objects.create(company="Acme Corp", title="Backend", requisition_id="1")
    decision = decide(
        make_extraction(event_type="offer", requisition_id="1", needs_review=True),
        email,
    )
    assert decision.action == Action.REVIEW


def test_decide_reviews_uncertain_interview_schedule(email):
    Application.objects.create(company="Acme Corp", title="Backend", requisition_id="1")
    decision = decide(
        make_extraction(event_type="interview_invitation", requisition_id="1"),
        email,
    )
    assert decision.action == Action.REVIEW


def test_decide_respects_manual_stage_override(email):
    app = Application.objects.create(
        company="Acme Corp",
        title="Backend",
        requisition_id="1",
        manual_override_fields=["stage"],
    )
    decision = decide(make_extraction(event_type="rejection", requisition_id="1"), email)
    assert decision.action == Action.REVIEW
    assert decision.review_kind == "conflict"
    assert app.stage == Stage.SAVED


def test_decide_regression_keeps_history_only(email):
    app = Application.objects.create(
        company="Acme Corp", title="Backend", requisition_id="1", stage=Stage.OFFER
    )
    decision = decide(
        make_extraction(event_type="application_confirmation", requisition_id="1"), email
    )
    assert decision.action == Action.AUTO_UPDATE
    assert decision.apply_now is False
    assert app.stage == Stage.OFFER
