"""Posting capture: fetch a job URL and extract structured JobPosting data.

Fallback chain (see IMPLEMENTATION_PLAN.md §5):
  1. schema.org JobPosting JSON-LD
  2. readable page text (trafilatura)
  3. explicit partial/failed state — never silently store a bare URL

Network safety: only http(s), only recognized job-posting URLs, block
private/loopback/link-local destinations on every redirect hop, cap body size
and time, and never fetch unsubscribe/tracking/action links.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

MAX_BYTES = 3_000_000
MAX_REDIRECTS = 5
TIMEOUT = (5, 15)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 JobMonitor/0.1"
)

JOB_HOST_HINTS = (
    "greenhouse.io",
    "lever.co",
    "workday.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "icims.com",
    "jobvite.com",
    "workable.com",
    "recruitee.com",
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "wellfound.com",
    "angel.co",
    "bamboohr.com",
)
JOB_PATH_HINTS = ("/job", "/jobs", "/career", "/careers", "/positions", "/opening")
BLOCKED_PATH_HINTS = (
    "unsubscribe",
    "/track",
    "/click",
    "/redirect",
    "/optout",
    "/preferences",
    "/comm/",
    "/notification",
    "/search-appearances",
    "/signin",
    "/sign-in",
    "/login",
    "/log-in",
    "/auth/",
    "/account",
    "/password",
    "/settings",
)
BLOCKED_QUERY_KEYS = (
    "lipi",
    "trk",
    "midsig",
    "midtoken",
    "trkemail",
    "eid",
    "otptoken",
    "trackingid",
    "gclid",
    "fbclid",
)
LOGIN_MARKERS = (
    "sign in to continue",
    "log in to continue",
    "create an account",
    "join now",
    "authwall",
    "verify you are human",
    "enable javascript",
)


class UnsafeUrl(ValueError):
    """Raised when a URL fails the fetch-safety policy."""


class FetchFailed(RuntimeError):
    """Raised when the page cannot be retrieved."""


@dataclass
class PostingSnapshot:
    url: str
    resolved_url: str
    source: str
    captured_at: str
    title: str = ""
    company: str = ""
    job_identifier: str = ""
    description: str = ""
    requirements: str = ""
    responsibilities: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    qualifications: list[str] = field(default_factory=list)
    experience: str = ""
    education: str = ""
    location: str = ""
    work_arrangement: str = ""
    employment_type: str = ""
    compensation: str = ""
    benefits: list[str] = field(default_factory=list)
    date_posted: str = ""
    valid_through: str = ""
    capture_state: str = "failed"
    unknown_fields: list[str] = field(default_factory=list)
    error: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _host_is_safe(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"Cannot resolve host {host!r}") from exc
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise UnsafeUrl(f"Blocked non-public address {address} for host {host!r}")
    return True


def is_probable_job_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    path = parsed.path.lower()
    if any(hint in path for hint in BLOCKED_PATH_HINTS):
        return False
    query_keys = {part.split("=", 1)[0].lower() for part in (parsed.query or "").split("&") if part}
    if query_keys & set(BLOCKED_QUERY_KEYS):
        return False
    host = (parsed.hostname or "").lower()
    if any(host == hint or host.endswith(f".{hint}") for hint in JOB_HOST_HINTS):
        return True
    return any(hint in path for hint in JOB_PATH_HINTS)


def _stream_get(session: requests.Session, url: str) -> tuple[str, bytes, str]:
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        _host_is_safe(urlparse(current).hostname or "")
        response = session.get(
            current,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        )
        if response.is_redirect:
            location = response.headers.get("Location", "")
            response.close()
            if not location:
                raise FetchFailed(f"Redirect without Location from {current}")
            current = urljoin(current, location)
            if urlparse(current).scheme not in ("http", "https"):
                raise UnsafeUrl(f"Unsafe redirect scheme: {current}")
            if not is_probable_job_url(current):
                raise UnsafeUrl(f"Redirected to an unrecognized job URL: {current}")
            continue
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            response.close()
            raise FetchFailed(str(exc)) from exc
        content_type = response.headers.get("Content-Type", "")
        chunks: list[bytes] = []
        size = 0
        too_large = False
        for chunk in response.iter_content(chunk_size=65536):
            size += len(chunk)
            if size > MAX_BYTES:
                too_large = True
                break
            chunks.append(chunk)
        response.close()
        if too_large:
            raise FetchFailed(f"Response exceeds {MAX_BYTES} bytes")
        return current, b"".join(chunks), content_type
    raise FetchFailed(f"Too many redirects (>{MAX_REDIRECTS})")


def fetch(url: str, *, session: requests.Session | None = None) -> tuple[str, str]:
    """Return (resolved_url, html). Enforces the safety policy."""
    if not is_probable_job_url(url):
        raise UnsafeUrl(f"Not a recognized job-posting URL: {url}")
    parsed = urlparse(url)
    _host_is_safe(parsed.hostname or "")
    owns_session = session is None
    session = session or requests.Session()
    session.trust_env = False
    try:
        final_url, body, content_type = _stream_get(session, url)
    except requests.RequestException as exc:
        raise FetchFailed(str(exc)) from exc
    finally:
        if owns_session:
            session.close()
    if "html" not in content_type and content_type:
        raise FetchFailed(f"Unsupported content type: {content_type}")
    return final_url, body.decode("utf-8", "replace")


def _walk_json_ld(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        types = node.get("@type")
        if types == "JobPosting" or (isinstance(types, list) and "JobPosting" in types):
            found.append(node)
        for value in node.values():
            found.extend(_walk_json_ld(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_json_ld(item))
    return found


def extract_jobposting(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    results: list[dict[str, Any]] = []
    for script in soup.find_all("script", type="application/ld+json"):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        results.extend(_walk_json_ld(data))
    return results


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(filter(None, (_flatten(v) for v in value)))
    if isinstance(value, dict):
        return "\n".join(filter(None, (_flatten(v) for v in value.values())))
    return str(value)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [s for s in (_flatten(v).strip() for v in value) if s]
    flattened = _flatten(value).strip()
    return [flattened] if flattened else []


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "lxml")
    return soup.get_text("\n", strip=True)


def _location_from(posting: dict[str, Any]) -> tuple[str, str]:
    location = ""
    work_arrangement = ""
    job_location = posting.get("jobLocation")
    if isinstance(job_location, list):
        job_location = job_location[0] if job_location else None
    if isinstance(job_location, dict):
        address = job_location.get("address", job_location)
        parts = [
            _flatten(address.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
            if isinstance(address, dict)
        ]
        location = ", ".join(p for p in parts if p)
    if _flatten(posting.get("jobLocationType")).lower().find("telecommute") >= 0:
        work_arrangement = "Remote"
    return location, work_arrangement


def _compensation_from(posting: dict[str, Any]) -> str:
    salary = posting.get("baseSalary")
    if not isinstance(salary, dict):
        return ""
    currency = _flatten(salary.get("currency"))
    value = salary.get("value", {})
    if isinstance(value, dict):
        unit = _flatten(value.get("unitText"))
        min_v = _flatten(value.get("minValue"))
        max_v = _flatten(value.get("maxValue"))
        single = _flatten(value.get("value"))
        amounts = " - ".join(x for x in (min_v, max_v) if x) or single
        return " ".join(x for x in (currency, amounts, unit) if x).strip()
    return " ".join(x for x in (currency, _flatten(value)) if x).strip()


def build_snapshot(url: str, html: str, resolved_url: str, source: str) -> PostingSnapshot:
    captured = dt.datetime.now(dt.UTC).isoformat()
    postings = extract_jobposting(html)
    if postings:
        posting = max(postings, key=lambda p: len(_flatten(p.get("description"))))
        location, arrangement = _location_from(posting)
        description_html = _flatten(posting.get("description"))
        description = (
            _html_to_text(description_html) if "<" in description_html else description_html
        )
        snapshot = PostingSnapshot(
            url=url,
            resolved_url=resolved_url,
            source=source,
            captured_at=captured,
            title=_flatten(posting.get("title")),
            company=_flatten(
                (posting.get("hiringOrganization") or {}).get("name")
                if isinstance(posting.get("hiringOrganization"), dict)
                else posting.get("hiringOrganization")
            ),
            job_identifier=_flatten(posting.get("identifier")),
            description=description,
            responsibilities=_as_list(posting.get("responsibilities")),
            required_skills=_as_list(posting.get("skills")),
            qualifications=_as_list(posting.get("qualifications")),
            experience=_flatten(posting.get("experienceRequirements")),
            education=_flatten(posting.get("educationRequirements")),
            location=location,
            work_arrangement=arrangement,
            employment_type=_flatten(posting.get("employmentType")),
            compensation=_compensation_from(posting),
            benefits=_as_list(posting.get("jobBenefits")),
            date_posted=_flatten(posting.get("datePosted")),
            valid_through=_flatten(posting.get("validThrough")),
            capture_state="structured",
        )
    else:
        try:
            import trafilatura

            text = trafilatura.extract(html) or ""
        except Exception:  # noqa: BLE001
            text = ""
        if not text:
            text = _html_to_text(html)
        soup = BeautifulSoup(html, "lxml")
        title = soup.title.get_text(strip=True) if soup.title else ""
        probe = f"{title}\n{text[:800]}".lower()
        looks_blocked = any(marker in probe for marker in LOGIN_MARKERS)
        if not text.strip():
            state = "failed"
            error = "No readable text was extracted."
        elif looks_blocked or len(text.strip()) < 300:
            state = "partial"
            error = "Page did not contain a readable job posting (possibly login-protected)."
        else:
            state = "text"
            error = ""
        snapshot = PostingSnapshot(
            url=url,
            resolved_url=resolved_url,
            source=source,
            captured_at=captured,
            title=title,
            description=text,
            capture_state=state,
            error=error,
        )

    required = [
        "title",
        "company",
        "description",
        "location",
        "compensation",
        "employment_type",
    ]
    snapshot.unknown_fields = [f for f in required if not getattr(snapshot, f)]
    return snapshot


def capture(
    url: str, *, source: str = "manual", session: requests.Session | None = None
) -> PostingSnapshot:
    try:
        resolved_url, html = fetch(url, session=session)
    except (UnsafeUrl, FetchFailed) as exc:
        return PostingSnapshot(
            url=url,
            resolved_url=url,
            source=source,
            captured_at=dt.datetime.now(dt.UTC).isoformat(),
            capture_state="failed",
            error=str(exc),
        )
    return build_snapshot(url, html, resolved_url, source)
