from __future__ import annotations

from pathlib import Path

import pytest
import requests

from tracker.integrations.capture import (
    FetchFailed,
    UnsafeUrl,
    build_snapshot,
    extract_jobposting,
    is_probable_job_url,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "postings"


def test_greenhouse_structured_snapshot():
    html = (FIXTURES / "greenhouse.html").read_text(encoding="utf-8")
    snapshot = build_snapshot(
        url="https://boards.greenhouse.io/acme/jobs/12345",
        html=html,
        resolved_url="https://boards.greenhouse.io/acme/jobs/12345",
        source="fixture",
    )
    assert snapshot.capture_state == "structured"
    assert snapshot.title == "Senior Backend Engineer"
    assert snapshot.company == "Acme Corp"
    assert "Python" in snapshot.description
    assert snapshot.compensation == "USD 90000 - 130000 YEAR"
    assert snapshot.location == "Cairo, EG"
    assert snapshot.date_posted == "2026-08-20"


def test_plain_posting_falls_back_to_text():
    html = (FIXTURES / "plain.html").read_text(encoding="utf-8")
    snapshot = build_snapshot(
        url="https://example.com/jobs/platform",
        html=html,
        resolved_url="https://example.com/jobs/platform",
        source="fixture",
    )
    assert snapshot.capture_state == "text"
    assert "Platform Engineer" in snapshot.description
    assert "compensation" in snapshot.unknown_fields


def test_jobposting_jsonld_found():
    html = (FIXTURES / "greenhouse.html").read_text(encoding="utf-8")
    postings = extract_jobposting(html)
    assert len(postings) == 1
    assert postings[0]["title"] == "Senior Backend Engineer"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme/jobs/12345", True),
        ("https://example.com/careers/backend", True),
        ("https://greenhouse.io.evil.example/roles/12345", False),
        ("https://hiringweekly.com/unsubscribe?u=1", False),
        ("ftp://example.com/jobs/1", False),
        ("javascript:alert(1)", False),
    ],
)
def test_is_probable_job_url(url, expected):
    assert is_probable_job_url(url) is expected


def test_private_host_blocked():
    from tracker.integrations.capture import _host_is_safe

    with pytest.raises(UnsafeUrl):
        _host_is_safe("localhost")


def test_fetch_rejects_non_job_url():
    from tracker.integrations.capture import fetch

    with pytest.raises(UnsafeUrl):
        fetch("https://evil.example.com/unsubscribe")


class _Response:
    is_redirect = False
    headers = {"Content-Type": "text/html"}

    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise requests.HTTPError(self.error)

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        pass


class _Session:
    def __init__(self, response):
        self.response = response
        self.trust_env = True

    def get(self, *args, **kwargs):
        return self.response


def test_fetch_rejects_http_errors(monkeypatch):
    monkeypatch.setattr("tracker.integrations.capture._host_is_safe", lambda host: True)
    with pytest.raises(FetchFailed):
        from tracker.integrations.capture import fetch

        fetch(
            "https://example.com/jobs/platform",
            session=_Session(_Response([], error="404")),
        )


def test_fetch_rejects_oversized_body(monkeypatch):
    monkeypatch.setattr("tracker.integrations.capture._host_is_safe", lambda host: True)
    monkeypatch.setattr("tracker.integrations.capture.MAX_BYTES", 3)
    with pytest.raises(FetchFailed, match="exceeds"):
        from tracker.integrations.capture import fetch

        fetch("https://example.com/jobs/platform", session=_Session(_Response([b"1234"])))


def test_is_probable_job_url_rejects_tracking_links():
    assert (
        is_probable_job_url(
            "https://www.linkedin.com/comm/me/search-appearances/?lipi=abc&trk=email"
        )
        is False
    )
    assert is_probable_job_url("https://x.com/jobs/1?gclid=123") is False


def test_login_page_is_marked_partial():
    html = "<html><head><title>Sign in</title></head><body><p>Sign in to continue</p></body></html>"
    snapshot = build_snapshot(
        "https://www.linkedin.com/jobs/view/1",
        html,
        "https://www.linkedin.com/jobs/view/1",
        "test",
    )
    assert snapshot.capture_state == "partial"
    assert snapshot.error
