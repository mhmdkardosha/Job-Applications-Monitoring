"""OS credential-store helpers (keyring). Secrets never touch the database.

Usage:
    python -m tracker.integrations.secrets set-deepseek-key       # prompts, hidden input
    python -m tracker.integrations.secrets status
    python -m tracker.integrations.secrets set-gmail-token EMAIL  # reads token JSON on stdin
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

import keyring

SERVICE = "job-application-monitor"
DEEPSEEK_USERNAME = "deepseek"
GMAIL_PREFIX = "gmail:"


def set_secret(username: str, value: str) -> None:
    keyring.set_password(SERVICE, username, value)


def get_secret(username: str) -> str | None:
    return keyring.get_password(SERVICE, username)


def delete_secret(username: str) -> None:
    try:
        keyring.delete_password(SERVICE, username)
    except keyring.errors.PasswordDeleteError:
        pass


def gmail_token_username(account: str) -> str:
    return GMAIL_PREFIX + account


def has_gmail_token(account: str) -> bool:
    return get_secret(gmail_token_username(account)) is not None


def _status() -> int:
    from .ai_client import MissingApiKey, resolve_api_key

    try:
        key = resolve_api_key()
        print(f"DeepSeek key: present (…{key[-4:]})")
    except MissingApiKey:
        print("DeepSeek key: MISSING")

    accounts = known_gmail_accounts()
    if accounts:
        for account in accounts:
            state = "present" if has_gmail_token(account) else "MISSING"
            print(f"Gmail token [{account}]: {state}")
    else:
        print("Gmail tokens: none registered")
    return 0


def registry_path():
    default_data_dir = Path(__file__).resolve().parents[2] / "data"
    data_dir = Path(os.environ.get("JOBMON_DATA_DIR", default_data_dir))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "gmail_accounts.json"


def known_gmail_accounts() -> list[str]:
    path = registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return [str(a) for a in data if isinstance(a, str)]


def register_gmail_account(account: str) -> None:
    accounts = known_gmail_accounts()
    if account not in accounts:
        accounts.append(account)
        registry_path().write_text(json.dumps(accounts, indent=2), encoding="utf-8")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "set-deepseek-key":
        value = getpass.getpass("Paste DEEPSEEK_API_KEY (hidden): ").strip()
        if not value:
            print("No value provided.", file=sys.stderr)
            return 1
        set_secret(DEEPSEEK_USERNAME, value)
        print("Stored DeepSeek key in the OS credential store.")
        return 0
    if cmd == "set-gmail-token":
        if len(argv) < 2:
            print("Account email required.", file=sys.stderr)
            return 2
        payload = sys.stdin.read().strip()
        json.loads(payload)  # validate
        set_secret(GMAIL_PREFIX + argv[1], payload)
        print(f"Stored Gmail token for {argv[1]}.")
        return 0
    if cmd == "delete":
        if len(argv) < 2:
            print("Username required.", file=sys.stderr)
            return 2
        delete_secret(argv[1])
        print(f"Deleted {argv[1]}.")
        return 0
    if cmd == "status":
        return _status()
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
