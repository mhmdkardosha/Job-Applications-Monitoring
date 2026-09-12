from __future__ import annotations

import json
from pathlib import Path

import pytest

from tracker.integrations.ai_client import AiResult, Usage
from tracker.integrations.extract import (
    InvalidExtraction,
    normalize_company,
    parse_email_result,
    strip_quoted_reply,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "emails"


def _result(payload: dict) -> AiResult:
    return AiResult(
        text=json.dumps(payload),
        parsed=payload,
        model="deepseek-flash",
        usage=Usage(prompt_cache_miss_tokens=100, completion_tokens=50),
        cost_usd=0.0001,
    )


def test_parse_valid_email_result():
    result = _result(
        {
            "event_type": "interview_invitation",
            "company": "Acme Corp",
            "role": "Senior Backend Engineer",
            "requisition_id": "12345",
            "interview_start": "2026-09-15T15:00:00+02:00",
            "confidence": 0.92,
            "needs_review": False,
            "evidence": [{"field": "company", "excerpt": "Acme Corp"}],
        }
    )
    extraction = parse_email_result(result)
    assert extraction.event_type == "interview_invitation"
    assert extraction.interview_start == "2026-09-15T15:00:00+02:00"
    assert extraction.confidence == pytest.approx(0.92)
    assert extraction.needs_review is False


def test_parse_rejects_unknown_event_type():
    with pytest.raises(InvalidExtraction):
        parse_email_result(_result({"event_type": "something_else"}))


def test_parse_rejects_non_json():
    result = AiResult(
        text="not json",
        parsed=None,
        model="deepseek-flash",
        usage=Usage(),
        cost_usd=0.0,
    )
    with pytest.raises(InvalidExtraction):
        parse_email_result(result)


def test_invalid_date_becomes_none():
    extraction = parse_email_result(_result({"event_type": "offer", "event_date": "not-a-date"}))
    assert extraction.event_date is None


def test_confidence_clamped():
    extraction = parse_email_result(_result({"event_type": "offer", "confidence": 5}))
    assert extraction.confidence == 1.0


def test_fixture_expected_event_types_are_valid():
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    from tracker.integrations.extract import EVENT_TYPES

    for spec in expected.values():
        assert spec["event_type"] in EVENT_TYPES


def test_normalize_company():
    assert normalize_company("Acme Corp.") == normalize_company("Acme, Inc")


def test_strip_quoted_reply_cuts_history():
    body = (
        "Thanks, see you then.\n\n"
        "On Tue, 8 Sep 2026 at 10:00, Jordan Lee wrote:\n"
        "> Would you like to interview?\n"
    )
    cleaned = strip_quoted_reply(body)
    assert "Thanks, see you then." in cleaned
    assert "Jordan Lee wrote" not in cleaned
    assert "Would you like to interview?" not in cleaned


def test_strip_quoted_reply_removes_inline_quotes():
    cleaned = strip_quoted_reply("Hi\n> quoted line\nBye")
    assert cleaned == "Hi\nBye"
