"""Validate extraction on a small sample of real Gmail messages.

Sends only job-related content (quoted history stripped) to DeepSeek and writes
a reviewable report. This is user-reviewed, not an accuracy benchmark.

    python -m tracker.integrations.validate_real --limit 5
    python -m tracker.integrations.validate_real --limit 8 --query "from:greenhouse.io"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .ai_client import DEFAULT_CEILING_USD, DeepSeekClient, UsageLedger
from .extract import InvalidExtraction, extract_email
from .gmail import JOB_SENDER_QUERY as JOB_SENDER_QUERY
from .gmail import get_service, iter_messages, list_message_ids

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracker.integrations.validate_real")
    parser.add_argument("--account", default=None)
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--query", default=JOB_SENDER_QUERY)
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--ceiling", type=float, default=DEFAULT_CEILING_USD)
    parser.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    account, service = get_service(args.account)
    ids = list_message_ids(service, days=args.days, query=args.query, limit=args.limit)
    print(f"{account}: reviewing {len(ids)} job-related message(s).")

    ledger = UsageLedger(DATA_DIR / "ai_usage", ceiling_usd=args.ceiling)
    client = DeepSeekClient(model=args.model, ledger=ledger)

    rows = []
    cost = 0.0
    for message in iter_messages(service, ids, account):
        subject = message.headers.get("Subject", "(no subject)")
        sender = message.headers.get("From", "")
        try:
            extraction = extract_email(client, message)
            cost += extraction.cost_usd
            rows.append(
                {
                    "id": message.id,
                    "subject": subject,
                    "from": sender,
                    "extraction": extraction.to_json(),
                }
            )
            print(
                f"\n[{message.id}] {subject[:70]}\n"
                f"  from={sender[:60]}\n"
                f"  event={extraction.event_type} company={extraction.company!r} "
                f"role={extraction.role!r} req={extraction.requisition_id!r}\n"
                f"  interview={extraction.interview_start} "
                f"confidence={extraction.confidence:.2f} "
                f"needs_review={extraction.needs_review} "
                f"action={extraction.recommended_action}"
            )
            for item in extraction.evidence[:3]:
                print(f"    evidence[{item['field']}]: {item['excerpt'][:80]!r}")
        except InvalidExtraction as exc:
            rows.append(
                {
                    "id": message.id,
                    "subject": subject,
                    "from": sender,
                    "error": str(exc),
                }
            )
            print(f"\n[{message.id}] {subject[:70]}\n  INVALID EXTRACTION: {exc}")

    out = (
        Path(args.out)
        if args.out
        else (DATA_DIR / "validation" / f"real-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "account": account,
                "when": dt.datetime.now(dt.UTC).isoformat(),
                "model": args.model,
                "count": len(ids),
                "cost_usd": round(cost, 6),
                "rows": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"\nCost this run: ${cost:.6f} | monthly spend: ${ledger.monthly_spend():.6f} "
        f"of ${args.ceiling:.2f}\nReport: {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
