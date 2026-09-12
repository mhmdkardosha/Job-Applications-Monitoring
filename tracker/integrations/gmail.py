"""Gmail OAuth (read-only) and message sampling — Phase 1 feasibility gate.

Credentials: an OAuth *Desktop app* client JSON from Google Cloud, placed at
GOOGLE_OAUTH_CLIENT_SECRETS (default: ./credentials.json). Refresh tokens are
stored in the OS credential store via keyring, never on disk in the repo.

    python -m tracker.integrations.gmail auth
    python -m tracker.integrations.gmail list --days 90 --limit 20
    python -m tracker.integrations.gmail show <message-id>
    python -m tracker.integrations.gmail export --days 90 --out fixtures/sample_messages.jsonl

NOTE: External OAuth apps left in "Testing" issue refresh tokens that expire
after 7 days. Unattended sync must tolerate reconnection. See plan §4.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .secrets import (
    get_secret,
    gmail_token_username,
    known_gmail_accounts,
    register_gmail_account,
    set_secret,
)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_CLIENT_SECRETS = Path(
    os.environ.get("GOOGLE_OAUTH_CLIENT_SECRETS", BASE_DIR / "credentials.json")
)

# Cheap first-pass filter used for backfill listing and candidate detection.
JOB_SENDER_QUERY = (
    "from:greenhouse.io OR from:greenhouse-mail.io OR from:ashbyhq.com "
    "OR from:lever.co OR from:hire.lever.co OR from:workablemail.com "
    "OR from:smartrecruiters.com OR from:myworkdayjobs.com OR from:icims.com "
    "OR from:jobvite.com OR from:workday.com OR from:linkedin.com "
    "OR subject:(application OR interview OR offer OR assessment OR recruiter "
    'OR "next steps" OR "your candidacy")'
)

_HEADER_KEYS = (
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "Message-ID",
    "References",
    "In-Reply-To",
    "List-Unsubscribe",
)


class AuthError(RuntimeError):
    pass


@dataclass
class Message:
    id: str
    thread_id: str
    account: str
    headers: dict[str, str]
    snippet: str
    body_text: str
    label_ids: list[str] = field(default_factory=list)
    internal_date: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _client_config() -> dict[str, Any]:
    if not DEFAULT_CLIENT_SECRETS.exists():
        raise AuthError(
            f"OAuth client file not found at {DEFAULT_CLIENT_SECRETS}. "
            "See README -> Gmail OAuth setup."
        )
    return json.loads(DEFAULT_CLIENT_SECRETS.read_text(encoding="utf-8"))


def load_credentials(account: str | None = None) -> Credentials | None:
    if not account:
        return None
    raw = get_secret(gmail_token_username(account))
    if not raw:
        return None
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            set_secret(gmail_token_username(account), creds.to_json())
        except Exception as exc:  # noqa: BLE001
            raise AuthError(
                f"Refresh token for {account} is no longer valid ({exc}). "
                "Re-run `python -m tracker.integrations.gmail auth`."
            ) from exc
    return creds


def _build_service(creds: Credentials):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def authorize(interactive: bool = True) -> tuple[str, Credentials]:
    """Run the installed-app OAuth flow and persist the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not interactive:
        raise AuthError("No stored credentials and interactive auth disabled.")
    flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message="Opening browser for Gmail read-only access…",
    )
    service = _build_service(creds)
    profile = service.users().getProfile(userId="me").execute()
    account = profile["emailAddress"]
    set_secret(gmail_token_username(account), creds.to_json())
    register_gmail_account(account)
    return account, creds


def get_service(account: str | None = None):
    if account is None:
        accounts = known_gmail_accounts()
        if len(accounts) == 1:
            account = accounts[0]
    creds = load_credentials(account)
    if creds is None:
        if account:
            raise AuthError(
                f"No stored Gmail credentials for {account}. "
                "Run `python -m tracker.integrations.gmail auth` first."
            )
        account, creds = authorize()
    return account, _build_service(creds)


def _decode_part(data: str) -> str:
    try:
        padded = data + "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", "replace")
    except (binascii.Error, UnicodeEncodeError):
        return ""


def _extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to text/html; else concatenate leaf text."""
    if payload.get("filename"):
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data") if isinstance(body, dict) else None
    if data and mime == "text/plain":
        return _decode_part(data)
    html_text = ""
    if data and mime == "text/html":
        soup = BeautifulSoup(_decode_part(data), "lxml")
        for tag in soup(["script", "style"]):
            tag.decompose()
        html_text = soup.get_text("\n", strip=True)
    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            part_mime = part.get("mimeType", "")
            (plain_parts if part_mime == "text/plain" else html_parts).append(text)
    return "\n".join(plain_parts or html_parts) or html_text


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in payload.get("headers", []) or []:
        name = item.get("name", "")
        if name in _HEADER_KEYS:
            result[name] = item.get("value", "")
    return result


def get_message(service, message_id: str, account: str) -> Message:
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = raw.get("payload", {})
    return Message(
        id=raw["id"],
        thread_id=raw.get("threadId", ""),
        account=account,
        headers=_headers(payload),
        snippet=raw.get("snippet", ""),
        body_text=_extract_body(payload),
        label_ids=raw.get("labelIds", []) or [],
        internal_date=raw.get("internalDate"),
    )


def _query_for_days(days: int) -> str:
    after = dt.date.today() - dt.timedelta(days=days)
    return f"after:{after:%Y/%m/%d}"


def list_message_ids(
    service,
    *,
    days: int = 90,
    query: str | None = None,
    include_archived: bool = True,
    include_sent: bool = True,
    limit: int | None = None,
) -> list[str]:
    date_query = _query_for_days(days)
    base = f"({date_query}) ({query})" if query else date_query
    if include_archived and include_sent:
        q = base  # Gmail searches inbox, archived mail, and sent mail by default.
    elif include_archived:
        q = f"({base}) -in:sent"
    elif include_sent:
        q = f"({base}) (in:inbox OR in:sent)"
    else:
        q = f"({base}) in:inbox"
    ids: list[str] = []
    page_token: str | None = None
    while True:
        response = (
            service.users()
            .messages()
            .list(userId="me", q=q, pageToken=page_token, maxResults=100)
            .execute()
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token or (limit and len(ids) >= limit):
            break
    return ids[:limit] if limit else ids


def iter_messages(service, message_ids: Iterable[str], account: str) -> Iterable[Message]:
    for message_id in message_ids:
        yield get_message(service, message_id, account)


def _cmd_auth(_: argparse.Namespace) -> int:
    account, _creds = authorize()
    print(f"Authorized {account}. Refresh token stored in the OS credential store.")
    print(
        "Warning: if this OAuth app is in 'Testing', the refresh token expires "
        "after 7 days; reconnect when prompted."
    )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    account, service = get_service(args.account)
    ids = list_message_ids(service, days=args.days, query=args.query, limit=args.limit)
    print(f"{account}: {len(ids)} message(s) matched.")
    for msg in iter_messages(service, ids, account):
        subject = msg.headers.get("Subject", "(no subject)")
        sender = msg.headers.get("From", "(unknown)")
        print(f"  {msg.id}  {sender[:40]:<40}  {subject[:60]}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    account, service = get_service(args.account)
    msg = get_message(service, args.message_id, account)
    print(json.dumps(msg.to_json(), indent=2))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    account, service = get_service(args.account)
    ids = list_message_ids(service, days=args.days, query=args.query, limit=args.limit)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out.open("w", encoding="utf-8") as fh:
        for msg in iter_messages(service, ids, account):
            fh.write(json.dumps(msg.to_json()) + "\n")
            count += 1
    print(f"Exported {count} message(s) to {out}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracker.integrations.gmail")
    parser.add_argument("--account", default=None, help="Gmail address (default: prompt/auth)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("auth", help="Run OAuth and store the refresh token").set_defaults(
        func=_cmd_auth
    )

    p_list = sub.add_parser("list", help="List matching messages")
    p_list.add_argument("--days", type=int, default=90)
    p_list.add_argument("--query", default=None)
    p_list.add_argument("--limit", type=int, default=25)
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="Show one message as JSON")
    p_show.add_argument("message_id")
    p_show.set_defaults(func=_cmd_show)

    p_export = sub.add_parser("export", help="Export messages to JSONL")
    p_export.add_argument("--days", type=int, default=90)
    p_export.add_argument("--query", default=None)
    p_export.add_argument("--limit", type=int, default=None)
    p_export.add_argument("--out", required=True)
    p_export.set_defaults(func=_cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except AuthError as exc:
        print(f"Auth error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
