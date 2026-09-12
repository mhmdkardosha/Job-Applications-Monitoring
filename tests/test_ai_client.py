from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from tracker.integrations.ai_client import (
    BudgetExceeded,
    Usage,
    UsageLedger,
    _is_peak,
    estimate_cost,
)


def test_cost_zero_for_empty_usage():
    assert estimate_cost(Usage(), peak=True) == 0.0


def test_cost_scales_with_tokens():
    usage = Usage(prompt_cache_miss_tokens=1_000_000, completion_tokens=1_000_000)
    peak = estimate_cost(usage, peak=True)
    off = estimate_cost(usage, peak=False)
    assert peak == pytest.approx(0.30 + 1.20)
    assert off == pytest.approx(peak / 2)


def test_cache_hit_cheaper():
    hit = estimate_cost(Usage(prompt_cache_hit_tokens=1_000_000), peak=True)
    miss = estimate_cost(Usage(prompt_cache_miss_tokens=1_000_000), peak=True)
    assert hit < miss


@pytest.mark.parametrize(
    "when,expected",
    [
        (dt.datetime(2026, 9, 7, 2, 0, tzinfo=dt.UTC), True),  # Monday 02:00
        (dt.datetime(2026, 9, 7, 5, 0, tzinfo=dt.UTC), False),  # Monday 05:00
        (dt.datetime(2026, 9, 5, 2, 0, tzinfo=dt.UTC), False),  # Saturday
        (dt.datetime(2026, 9, 7, 9, 59, tzinfo=dt.UTC), True),  # Monday 09:59
    ],
)
def test_peak_windows(when, expected):
    assert _is_peak(when) is expected


def test_ledger_enforces_ceiling(tmp_path):
    ledger = UsageLedger(tmp_path, ceiling_usd=0.001)
    ledger.check_budget()  # under ceiling
    ledger.record({"cost_usd": 0.002, "model": "deepseek-flash"})
    assert ledger.monthly_spend() == pytest.approx(0.002)
    with pytest.raises(BudgetExceeded):
        ledger.check_budget()


def test_ledger_ignores_corrupt_lines(tmp_path):
    ledger = UsageLedger(tmp_path, ceiling_usd=10)
    path = ledger.directory / f"{ledger._month()}.jsonl"
    path.write_text('{"cost_usd": 0.5}\nnot json\n{"cost_usd": 0.25}\n', encoding="utf-8")
    assert ledger.monthly_spend() == pytest.approx(0.75)


class _FakeCompletions:
    def __init__(self, content: str):
        self.calls: list[dict] = []
        self._content = content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        usage = SimpleNamespace(
            prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=12, completion_tokens=8
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class _FakeClient:
    def __init__(self, content: str):
        self.completions = _FakeCompletions(content)
        self.chat = SimpleNamespace(completions=self.completions)


def test_complete_disables_thinking_and_sends_temperature():
    from tracker.integrations.ai_client import DeepSeekClient

    fake = _FakeClient('{"ok": true}')
    client = DeepSeekClient(client=fake, model="deepseek-flash")
    result = client.complete("sys", "user", json_mode=True)
    call = fake.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "temperature" in call
    assert "reasoning_effort" not in call
    assert result.parsed == {"ok": True}


def test_complete_enables_thinking_when_requested():
    from tracker.integrations.ai_client import DeepSeekClient

    fake = _FakeClient('{"ok": true}')
    client = DeepSeekClient(client=fake, model="deepseek-flash")
    client.complete("sys", "user", thinking=True)
    call = fake.completions.calls[0]
    assert call["extra_body"] == {"thinking": {"type": "enabled"}}
    assert call["reasoning_effort"] == "high"
    assert "temperature" not in call


def test_parse_json_falls_back_to_balanced_object():
    from tracker.integrations.ai_client import DeepSeekClient

    text = 'Here is the result:\n{"event_type": "offer", "confidence": 0.9}\nThanks!'
    assert DeepSeekClient._parse_json(text) == {
        "event_type": "offer",
        "confidence": 0.9,
    }


def test_parse_json_handles_fenced_block():
    from tracker.integrations.ai_client import DeepSeekClient

    text = '```json\n{"a": 1}\n```'
    assert DeepSeekClient._parse_json(text) == {"a": 1}
