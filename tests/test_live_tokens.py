"""live_tokens: model families and the token rate series (per provider and split by model family)."""
import pytest

import live_tokens
from live_tokens import LiveTokens, family


@pytest.mark.parametrize(("model", "name"), [
    ("claude-sonnet-5-5", "Sonnet"), ("github-copilot/claude-sonnet-5.5-1m", "Sonnet"), ("claude-opus-5-5", "Opus"),
    ("github-copilot/claude-haiku-5.5", "Haiku"), ("claude-fable-5-2", "Fable"), ("github-copilot/gpt-5-mini", "Gpt"),
    (None, "other"), ("", "other"),
])
def test_model_names_are_reduced_to_their_family(model, name):
    assert family(model) == name


@pytest.mark.parametrize(("adapter", "model", "provider"), [
    ("claude_local", "claude-sonnet-5-5", "claude"),
    ("opencode_local", "github-copilot/gpt-5-mini", "copilot"),
    ("opencode_local", "openrouter/z-ai/glm-5.3", "openrouter"),
    ("pi_local", "openrouter/anthropic/claude-sonnet-4.5", "openrouter"),
    ("pi_local", "z-ai/glm-5.3", None),
])
def test_agent_models_map_to_their_provider(adapter, model, provider):
    assert live_tokens.provider_of({"adapterType": adapter, "adapterConfig": {"model": model}}) == provider


def test_openrouter_transcript_lines_count_as_openrouter():
    rec = {"type": "message", "id": "m1", "timestamp": "2026-11-11T08:00:00Z",
           "message": {"role": "assistant", "provider": "openrouter", "model": "z-ai/glm-5.3",
                       "usage": {"input": 100, "output": 20, "cacheWrite": 30, "cacheRead": 900}}}
    assert live_tokens.transcript_tokens("omp", rec, {}) == [("2026-11-11T08:00:00Z", "openrouter", 150, 900, 130, "Glm")]


def test_transcript_line_carries_the_model_family():
    rec = {"type": "message", "id": "m1", "timestamp": "2026-11-11T08:00:00Z",
           "message": {"role": "assistant", "provider": "github-copilot", "model": "claude-haiku-5.5",
                       "usage": {"input": 100, "output": 20, "cacheWrite": 30, "cacheRead": 900}}}
    assert live_tokens.transcript_tokens("omp", rec, {}) == [("2026-11-11T08:00:00Z", "copilot", 150, 900, 130, "Haiku")]


def test_provider_curve_is_the_sum_of_its_model_families():
    live, now = LiveTokens(), 1_000_000.0
    live.events.extend([(now - 100, "claude", 6000, 0, 0, "Sonnet"), (now - 90, "claude", 3000, 0, 0, "Opus"),
                        (now - 80, "copilot", 1000, 0, 0, "Sonnet")])
    s = live.series(window_s=300, step_s=5, avg_s=30, now=now)
    assert sorted(s["models"]["claude"]) == ["Opus", "Sonnet"] and list(s["models"]["copilot"]) == ["Sonnet"]
    assert s["claude"] == pytest.approx([a + b for a, b in zip(s["models"]["claude"]["Opus"], s["models"]["claude"]["Sonnet"])])
    assert max(s["models"]["claude"]["Sonnet"]) == pytest.approx(6000 * 60 / 30)  # the peak is the plain 30 s average
    assert s["claude"][-1] == 0 and s["copilot"][0] == 0  # the burst is over / has not started at the window edges


def test_a_burst_ramps_in_and_out_instead_of_stepping():
    live, now = LiveTokens(), 1_000_000.0
    live.events.append((now - 100, "claude", 6000, 0, 0, "Sonnet"))
    values = live.series(window_s=300, step_s=1, avg_s=30, now=now)["claude"]
    steps = [abs(b - a) for a, b in zip(values, values[1:])]
    assert max(steps) <= max(values) / 6 + 1e-6  # RAMP_STEPS sub-steps: never the whole height at once
