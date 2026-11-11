"""OpenRouter key reading: spend share of the limit and the period it resets in (midnight UTC; Monday; the 1st)."""
from datetime import datetime, timezone

import pytest

import openrouter_monitor as om
import throttle

WED = datetime(2026, 11, 11, 15, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(("kind", "resets", "span_days"), [
    ("daily", "2026-11-12T00:00:00+00:00", 1), ("weekly", "2026-11-16T00:00:00+00:00", 7), ("monthly", "2026-12-01T00:00:00+00:00", 30)])
def test_limit_period_ends_at_the_next_utc_reset(kind, resets, span_days):
    st = om.reading({"limit": 10, "limit_remaining": 2.5, "limit_reset": kind}, WED)
    assert st["resets"] == resets and st["span"] == span_days * 86400
    assert st["used"] == 75 and st["spent"] == 7.5 and st["unlimited"] is False


def test_limit_without_reset_has_no_period():
    st = om.reading({"limit": 20, "limit_remaining": 20, "limit_reset": None}, WED)
    assert st["used"] == 0 and "resets" not in st


@pytest.mark.parametrize("limit", [None, 0])
def test_key_without_limit_is_unlimited(limit):
    assert om.reading({"limit": limit, "usage": 3.2}, WED)["unlimited"] is True


def test_overspent_key_reads_at_most_100_percent():
    assert om.reading({"limit": 5, "limit_remaining": -1, "limit_reset": "daily"}, WED)["used"] == 100


@pytest.mark.parametrize(("adapter", "model", "provider"), [
    ("opencode_local", "openrouter/anthropic/claude-sonnet-4.5", "openrouter"),
    ("pi_local", "openrouter/google/gemini-3-flash-preview", "openrouter"),
    ("pi_local", "github-copilot/gpt-5-mini", None), ("opencode_local", "github-copilot/gpt-5-mini", "copilot")])
def test_agent_provider_follows_adapter_and_model(adapter, model, provider):
    assert throttle.provider_of({"adapterType": adapter, "adapterConfig": {"model": model}}) == provider
