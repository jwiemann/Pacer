"""USD -> EUR rate behind the spend line: fetched from the ECB reference rate, cached in the data dir, never trusted blindly."""
import json

import pytest

import dashboard_data as dd
import http_util


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(dd, "FX_FILE", str(tmp_path / "fx.json"))
    monkeypatch.setattr(dd, "_fx", {"rate": None, "at": 0.0, "tried": 0.0, "busy": False})


def answer(monkeypatch, reply):
    def fake(url, **kw):
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(http_util, "request_json", fake)


def test_fetched_rate_is_used_and_cached_for_the_next_start(monkeypatch, tmp_path):
    answer(monkeypatch, {"base": "USD", "rates": {"EUR": 0.89871}})
    assert dd.fx_fetch() is True
    assert dd._fx["rate"] == 0.89871
    assert json.loads((tmp_path / "fx.json").read_text())["rate"] == 0.89871


@pytest.mark.parametrize("reply", [{"rates": {}}, {"rates": {"EUR": 0}}, {"rates": {"EUR": 900}}, {"rates": {"EUR": "x"}}, None,
                                   http_util.HttpError("down", "https://x")])
def test_unusable_answers_keep_the_current_rate(monkeypatch, reply):
    dd._fx["rate"] = 0.9
    answer(monkeypatch, reply)
    assert dd.fx_fetch() is False
    assert dd._fx["rate"] == 0.9


def test_a_fresh_cache_is_used_without_asking_the_network(monkeypatch, tmp_path):
    import time
    (tmp_path / "fx.json").write_text(json.dumps({"rate": 0.91, "at": time.time()}))
    answer(monkeypatch, RuntimeError("must not be called"))
    assert dd.usd_eur() == 0.91


def test_without_cache_the_fallback_serves_until_a_fetch_succeeded(monkeypatch):
    started = []
    monkeypatch.setattr(dd.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda s: started.append(target)})())
    assert dd.usd_eur() == dd.FX_FALLBACK
    assert len(started) == 1  # a background fetch was kicked off
    assert dd.usd_eur() == dd.FX_FALLBACK and len(started) == 1  # ...and is not started twice while busy or within the retry pause


@pytest.mark.parametrize(("cents", "text"), [
    (0, ""), (-1e-9, ""), (0.2, ""), (0.49, ""),                           # rounds to zero: hidden
    (0.51, "1 ct/min"), (1.26, "1 ct/min"), (44.09, "44 ct/min"), (55.6, "56 ct/min"), (99.49, "99 ct/min"),
    (99.6, "1.00 \u20ac/min"), (100, "1.00 \u20ac/min"), (125.4, "1.25 \u20ac/min"), (1234.5, "12.35 \u20ac/min")])  # from 1 EUR/min: euros
def test_spend_rate_label(cents, text):
    assert dd.fmt_spend(cents) == text
