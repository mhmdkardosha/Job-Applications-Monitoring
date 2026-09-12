from __future__ import annotations

import base64

import pytest

from tracker.integrations.gmail import (
    JOB_SENDER_QUERY,
    AuthError,
    _extract_body,
    get_service,
    list_message_ids,
)


class _Exec:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return self.data


class _Messages:
    def __init__(self):
        self.query = None

    def list(self, **kwargs):
        self.query = kwargs["q"]
        return _Exec({"messages": [{"id": "m1"}]})


class _Users:
    def __init__(self):
        self.messages_api = _Messages()

    def messages(self):
        return self.messages_api


class _Service:
    def __init__(self):
        self.users_api = _Users()

    def users(self):
        return self.users_api


def test_list_message_ids_keeps_date_filter_with_job_query():
    service = _Service()
    assert list_message_ids(service, days=7, query=JOB_SENDER_QUERY) == ["m1"]
    query = service.users_api.messages_api.query
    assert "after:" in query
    assert "greenhouse.io" in query
    assert "in:archive" not in query


def test_list_message_ids_folder_scope_keeps_inbox_when_archive_is_disabled():
    service = _Service()
    list_message_ids(service, include_archived=False, include_sent=True)
    query = service.users_api.messages_api.query
    assert "in:inbox OR in:sent" in query


def test_extract_body_prefers_plain_text_and_accepts_unpadded_base64():
    plain = base64.urlsafe_b64encode(b"plain body").decode().rstrip("=")
    html = base64.urlsafe_b64encode(b"<p>html body</p>").decode().rstrip("=")
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": html}},
            {"mimeType": "text/plain", "body": {"data": plain}},
        ],
    }
    assert _extract_body(payload) == "plain body"


def test_extract_body_converts_html_to_readable_text_without_css_or_scripts():
    html = base64.urlsafe_b64encode(
        b"<style>.hidden { display: none; }</style><p>Hello <b>candidate</b></p>"
        b"<script>alert('x')</script>"
    ).decode()
    payload = {"mimeType": "text/html", "body": {"data": html}}

    assert _extract_body(payload) == "Hello\ncandidate"


def test_get_service_does_not_authorize_a_different_account(monkeypatch):
    monkeypatch.setattr("tracker.integrations.gmail.load_credentials", lambda account: None)
    with pytest.raises(AuthError, match="me@example.com"):
        get_service("me@example.com")
