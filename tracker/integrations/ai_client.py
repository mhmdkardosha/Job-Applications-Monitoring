"""DeepSeek (OpenAI-compatible) client with cost tracking and a spend ceiling.

Used throughout the app for email classification, field extraction and
posting/requirements extraction. Keeps an append-only JSON usage ledger so
requests stop once recorded monthly spend reaches the ceiling and usage can be
shown in Settings. See IMPLEMENTATION_PLAN.md §4 and §8.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openai import OpenAI

DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CEILING_USD = 10.0
KEYRING_SERVICE = "job-application-monitor"
KEYRING_USERNAME = "deepseek"

# USD per 1M tokens for DeepSeek-V4.1-Flash (peak). Off-peak is half.
# https://api-docs.deepseek.com/quick_start/pricing
PRICE_INPUT_CACHE_HIT = 0.006
PRICE_INPUT_CACHE_MISS = 0.30
PRICE_OUTPUT = 1.20
PEAK_WINDOWS_UTC = ((1, 4), (6, 10))


def _is_peak(when: dt.datetime) -> bool:
    """Peak pricing: 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    when = when.astimezone(dt.UTC)
    if when.weekday() >= 5:
        return False
    hour = when.hour
    return any(start <= hour < end for start, end in PEAK_WINDOWS_UTC)


def _price_for(model: str) -> tuple[float, float, float]:
    return PRICE_INPUT_CACHE_HIT, PRICE_INPUT_CACHE_MISS, PRICE_OUTPUT


class BudgetExceeded(RuntimeError):
    """Raised before a request when recorded spend has reached the ceiling."""


class MissingApiKey(RuntimeError):
    """Raised when no DeepSeek API key can be found."""


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        return env_key.strip()
    try:
        import keyring

        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
        if stored:
            return stored
    except Exception:  # keyring backend may be unavailable
        pass
    raise MissingApiKey(
        "No DeepSeek API key found. Set DEEPSEEK_API_KEY or run "
        "`python -m tracker.integrations.secrets set-deepseek-key`."
    )


@dataclass
class Usage:
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens + self.completion_tokens

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_cache_hit_tokens": self.prompt_cache_hit_tokens,
            "prompt_cache_miss_tokens": self.prompt_cache_miss_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


def estimate_cost(usage: Usage, model: str = DEFAULT_MODEL, *, peak: bool = True) -> float:
    hit, miss, out = _price_for(model)
    factor = 1.0 if peak else 0.5
    return (
        factor
        * (
            usage.prompt_cache_hit_tokens * hit
            + usage.prompt_cache_miss_tokens * miss
            + usage.completion_tokens * out
        )
        / 1_000_000
    )


@dataclass
class AiResult:
    text: str
    parsed: dict[str, Any] | None
    model: str
    usage: Usage
    cost_usd: float
    raw: dict[str, Any] = field(default_factory=dict)


class UsageLedger:
    """Append-only JSON ledger, one file per month: data/ai_usage/YYYY-MM.jsonl."""

    def __init__(self, directory: Path, ceiling_usd: float = DEFAULT_CEILING_USD):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.ceiling_usd = ceiling_usd

    def _path(self, month: str) -> Path:
        return self.directory / f"{month}.jsonl"

    @staticmethod
    def _month(when: dt.datetime | None = None) -> str:
        when = when or dt.datetime.now(dt.UTC)
        return when.astimezone(dt.UTC).strftime("%Y-%m")

    def monthly_spend(self, month: str | None = None) -> float:
        path = self._path(month or self._month())
        if not path.exists():
            return 0.0
        total = 0.0
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                total += float(json.loads(line).get("cost_usd", 0.0))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return total

    def remaining(self) -> float:
        return max(self.ceiling_usd - self.monthly_spend(), 0.0)

    def check_budget(self) -> None:
        spent = self.monthly_spend()
        if spent >= self.ceiling_usd:
            raise BudgetExceeded(
                f"Monthly AI ceiling reached: ${spent:.4f} of ${self.ceiling_usd:.2f}."
            )

    def record(self, entry: dict[str, Any]) -> None:
        month = self._month()
        record = {"ts": dt.datetime.now(dt.UTC).isoformat(), **entry}
        with self._path(month).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class DeepSeekClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        ledger: UsageLedger | None = None,
        client: OpenAI | None = None,
    ):
        self.model = model
        self.ledger = ledger
        self._client = client or OpenAI(api_key=resolve_api_key(api_key), base_url=base_url)

    def complete(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking: bool = False,
    ) -> AiResult:
        if self.ledger is not None:
            self.ledger.check_budget()

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            # DeepSeek V4.1 enables thinking by default; extraction is a
            # deterministic JSON task, so disable it unless asked otherwise.
            "extra_body": {"thinking": {"type": "enabled" if thinking else "disabled"}},
        }
        if thinking:
            kwargs["reasoning_effort"] = "high"
        else:
            kwargs["temperature"] = temperature
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = self._client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content or ""

        usage = Usage()
        raw_usage = getattr(response, "usage", None)
        if raw_usage is not None:
            usage.prompt_cache_hit_tokens = getattr(raw_usage, "prompt_cache_hit_tokens", 0) or 0
            usage.prompt_cache_miss_tokens = getattr(raw_usage, "prompt_cache_miss_tokens", 0) or 0
            if not usage.prompt_cache_miss_tokens:
                prompt = getattr(raw_usage, "prompt_tokens", 0) or 0
                usage.prompt_cache_miss_tokens = max(prompt - usage.prompt_cache_hit_tokens, 0)
            usage.completion_tokens = getattr(raw_usage, "completion_tokens", 0) or 0

        peak = _is_peak(dt.datetime.now(dt.UTC))
        cost = estimate_cost(usage, self.model, peak=peak)

        parsed: dict[str, Any] | None = None
        if json_mode and text:
            parsed = self._parse_json(text)

        if self.ledger is not None:
            self.ledger.record(
                {
                    "model": self.model,
                    "peak": peak,
                    "cost_usd": round(cost, 6),
                    "usage": usage.to_dict(),
                }
            )

        return AiResult(
            text=text,
            parsed=parsed,
            model=self.model,
            usage=usage,
            cost_usd=cost,
            raw={"peak": peak},
        )

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            if candidate.lower().startswith("json"):
                candidate = candidate[4:]
        parsed = DeepSeekClient._try_load(candidate)
        if parsed is not None:
            return parsed
        # Fallback: first balanced {...} object (handles stray prose/fences).
        start = candidate.find("{")
        while start != -1:
            depth = 0
            in_string = False
            escaped = False
            for index in range(start, len(candidate)):
                char = candidate[index]
                if in_string:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_string = False
                elif char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        parsed = DeepSeekClient._try_load(candidate[start : index + 1])
                        if parsed is not None:
                            return parsed
                        break
            start = candidate.find("{", start + 1)
        return None

    @staticmethod
    def _try_load(value: str) -> dict[str, Any] | None:
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None
