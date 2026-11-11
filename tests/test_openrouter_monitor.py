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


MODELS_YML = """providers:
  other:
    apiKey: sk-or-v1-other
  openrouter:
    baseUrl: https://openrouter.ai/api/v1
    apiKey: sk-or-v1-omp-key
    api: openai-completions
"""


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """Key sources in a sandbox: a fake home with omp's models.yml, an empty key file path, no env key."""
    (tmp_path / ".omp" / "agent").mkdir(parents=True)
    (tmp_path / ".omp" / "agent" / "models.yml").write_text(MODELS_YML, encoding="utf-8")
    for var in ("USERPROFILE", "HOME"):
        monkeypatch.setenv(var, str(tmp_path))
    monkeypatch.setattr(om, "KEY_FILE", str(tmp_path / "openrouter.key"))
    monkeypatch.delenv(om.ENV_VAR, raising=False)
    monkeypatch.setattr(om.sys, "platform", "linux")  # no registry lookup
    return tmp_path


def test_the_key_omp_spends_with_beats_the_environment_key(keys, monkeypatch):
    monkeypatch.setenv(om.ENV_VAR, "sk-or-v1-env-key")
    assert om.read_key() == "sk-or-v1-omp-key"


def test_the_key_file_overrides_omp_and_the_environment(keys, monkeypatch):
    monkeypatch.setenv(om.ENV_VAR, "sk-or-v1-env-key")
    (keys / "openrouter.key").write_text("sk-or-v1-file-key\n", encoding="utf-8")
    assert om.read_key() == "sk-or-v1-file-key"


def test_without_omp_config_the_environment_key_is_used(keys, monkeypatch):
    (keys / ".omp" / "agent" / "models.yml").unlink()
    monkeypatch.setenv(om.ENV_VAR, "sk-or-v1-env-key")
    assert om.read_key() == "sk-or-v1-env-key"


def test_omp_entry_that_is_not_a_literal_key_is_ignored(keys):
    (keys / ".omp" / "agent" / "models.yml").write_text(MODELS_YML.replace("sk-or-v1-omp-key", "OPENROUTER_API_KEY"),
                                                        encoding="utf-8")
    assert om.read_key() is None


@pytest.mark.parametrize(("adapter", "model", "provider"), [
    ("opencode_local", "openrouter/anthropic/claude-sonnet-4.5", "openrouter"),
    ("pi_local", "openrouter/google/gemini-3-flash-preview", "openrouter"),
    ("pi_local", "github-copilot/gpt-5-mini", None), ("opencode_local", "github-copilot/gpt-5-mini", "copilot")])
def test_agent_provider_follows_adapter_and_model(adapter, model, provider):
    assert throttle.provider_of({"adapterType": adapter, "adapterConfig": {"model": model}}) == provider
