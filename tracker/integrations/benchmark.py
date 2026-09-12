"""Phase 1 benchmark: extraction quality and cost on synthetic fixtures.

    python -m tracker.integrations.benchmark --dry-run          # no API key needed
    python -m tracker.integrations.benchmark                    # runs live DeepSeek extraction
    python -m tracker.integrations.benchmark --posting fixtures/postings/greenhouse.html

Reports per-field accuracy against fixtures/emails/expected.json, latency,
token usage and cost, and writes a JSON report under data/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import time
from pathlib import Path

from .ai_client import DEFAULT_CEILING_USD, DeepSeekClient, UsageLedger, estimate_cost
from .capture import build_snapshot
from .extract import EMAIL_SYSTEM, extract_email, extract_requirements, normalize_company
from .gmail import Message

BASE_DIR = Path(__file__).resolve().parents[2]
FIXTURES = BASE_DIR / "fixtures" / "emails"
POSTINGS = BASE_DIR / "fixtures" / "postings"
DATA_DIR = BASE_DIR / "data"

APPROX_CHARS_PER_TOKEN = 4.0
ASSUMED_OUTPUT_TOKENS = 450


def load_message(path: Path) -> Message:
    data = json.loads(path.read_text(encoding="utf-8"))
    return Message(**data)


def _norm(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def compare(expected: dict, actual) -> dict[str, bool]:
    checks = {
        "event_type": expected["event_type"] == actual.event_type,
        "company": normalize_company(expected["company"]) == normalize_company(actual.company),
        "role": _norm(expected["role"]) == _norm(actual.role),
        "requisition_id": _norm(expected["requisition_id"]) == _norm(actual.requisition_id),
        "interview_time": bool(expected["has_interview_time"]) == bool(actual.interview_start),
    }
    return checks


def dry_run(client_model: str, ceiling: float) -> int:
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    total_prompt_chars = 0
    rows = []
    for name in sorted(expected):
        message = load_message(FIXTURES / name)
        system_len = len(EMAIL_SYSTEM)
        prompt_len = system_len + len(message.headers.get("From", "")) + len(message.body_text)
        total_prompt_chars += prompt_len
        est_input = int(prompt_len / APPROX_CHARS_PER_TOKEN)
        est_cost = estimate_cost(_usage(est_input, ASSUMED_OUTPUT_TOKENS), client_model, peak=True)
        rows.append((name, est_input, est_cost))

    total_est = sum(r[2] for r in rows)
    print(f"DRY RUN — model={client_model} ceiling=${ceiling:.2f}/month")
    print(f"{'fixture':<34}{'est_input_tokens':>18}{'est_cost_usd':>14}")
    for name, tokens, cost in rows:
        print(f"{name:<34}{tokens:>18}{cost:>14.6f}")
    print(f"{'TOTAL':<34}{'':>18}{total_est:>14.6f}")
    print(
        f"\nApprox chars/token={APPROX_CHARS_PER_TOKEN}, assumed output="
        f"{ASSUMED_OUTPUT_TOKENS} tokens, peak pricing."
    )
    print(
        f"This fixture set would cost ~${total_est:.6f}; 1000 similar emails "
        f"~${total_est * (1000 / max(len(rows), 1)):.4f}."
    )
    return 0


def _usage(prompt_tokens: int, completion_tokens: int):
    from .ai_client import Usage

    return Usage(
        prompt_cache_hit_tokens=0,
        prompt_cache_miss_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def run_emails(client: DeepSeekClient) -> dict:
    expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
    field_names = ["event_type", "company", "role", "requisition_id", "interview_time"]
    totals = dict.fromkeys(field_names, 0)
    count = 0
    latencies: list[float] = []
    cost_total = 0.0
    mismatches = []
    for name in sorted(expected):
        message = load_message(FIXTURES / name)
        started = time.perf_counter()
        actual = extract_email(client, message)
        latencies.append(time.perf_counter() - started)
        cost_total += actual.cost_usd
        checks = compare(expected[name], actual)
        count += 1
        for field, ok in checks.items():
            totals[field] += int(ok)
            if not ok:
                mismatches.append(
                    {
                        "fixture": name,
                        "field": field,
                        "expected": expected[name].get(field),
                        "actual": getattr(actual, field, None),
                    }
                )
    return {
        "count": count,
        "field_accuracy": {f: (totals[f] / count if count else 0.0) for f in field_names},
        "cost_usd": round(cost_total, 6),
        "latency_ms_median": round(statistics.median(latencies) * 1000, 1) if latencies else 0,
        "mismatches": mismatches,
    }


def run_posting(client: DeepSeekClient | None, path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    snapshot = build_snapshot(
        url=f"https://example.com/jobs/{path.stem}",
        html=html,
        resolved_url=f"https://example.com/jobs/{path.stem}",
        source="fixture",
    )
    report = {
        "fixture": path.name,
        "capture_state": snapshot.capture_state,
        "title": snapshot.title,
        "company": snapshot.company,
        "description_chars": len(snapshot.description),
        "unknown_fields": snapshot.unknown_fields,
    }
    if client is not None and snapshot.description:
        req = extract_requirements(client, snapshot.description)
        report["requirements"] = req.to_json()
        report["cost_usd"] = req.cost_usd
    return report


def run_live(model: str, ceiling: float, posting_paths: list[Path]) -> int:
    ledger = UsageLedger(DATA_DIR / "ai_usage", ceiling_usd=ceiling)
    client = DeepSeekClient(model=model, ledger=ledger)
    report: dict = {
        "model": model,
        "ceiling_usd": ceiling,
        "when": dt.datetime.now(dt.UTC).isoformat(),
    }
    report["emails"] = run_emails(client)
    report["postings"] = [run_posting(client, p) for p in posting_paths]
    report["monthly_spend_after_usd"] = round(ledger.monthly_spend(), 6)

    out = DATA_DIR / "benchmark" / f"report-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Live benchmark — model={model}")
    for field, acc in report["emails"]["field_accuracy"].items():
        print(f"  {field:<16} {acc:6.1%}")
    print(
        f"  emails={report['emails']['count']} "
        f"cost=${report['emails']['cost_usd']:.6f} "
        f"median_latency={report['emails']['latency_ms_median']}ms"
    )
    for mismatch in report["emails"]["mismatches"]:
        print(f"  MISMATCH {mismatch}")
    for posting in report["postings"]:
        print(
            f"  posting {posting['fixture']}: state={posting['capture_state']} "
            f"title={posting['title']!r} unknown={posting['unknown_fields']}"
        )
    print(f"  monthly spend now: ${report['monthly_spend_after_usd']:.6f}")
    print(f"Report written to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracker.integrations.benchmark")
    parser.add_argument("--dry-run", action="store_true", help="Estimate only, no API calls")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--ceiling", type=float, default=DEFAULT_CEILING_USD)
    parser.add_argument(
        "--posting",
        action="append",
        default=None,
        help="Posting HTML fixture(s) to include (default: greenhouse.html)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        return dry_run(args.model, args.ceiling)
    posting_paths = (
        [Path(p) for p in args.posting]
        if args.posting
        else [POSTINGS / "greenhouse.html", POSTINGS / "plain.html"]
    )
    return run_live(args.model, args.ceiling, posting_paths)


if __name__ == "__main__":
    raise SystemExit(main())
