"""Governor tick: which agents get paused and resumed, against a fake Paperclip and a fake usage endpoint.

The Anthropic usage URL is routed to the fake server (the only outside host the governor calls); WSL is never run."""
import json, os, subprocess, sys, time
from datetime import datetime, timezone

import pytest

import copilot_monitor
import openrouter_monitor
import fsutil
import governor
import http_util
import pacer_config
from fake_paperclip import FakePaperclip, point_pacer_at

ANTHROPIC = "https://api.anthropic.com"
CLAUDE, OPENCODE = "claude_local", "opencode_local"
COPILOT_MODEL = "github-copilot/claude-haiku-5.5"
LOG = os.path.join(pacer_config.DIR, "claude-governor.log")


@pytest.fixture
def fake(monkeypatch):
    server = FakePaperclip()
    point_pacer_at(server.url)
    for path in (governor.STATE, governor.SETTINGS, governor.STATUS, governor.LOCK, governor.LOCK + ".guard", copilot_monitor.STATUS, openrouter_monitor.STATUS):
        if os.path.exists(path):
            os.remove(path)
    os.makedirs(os.path.dirname(governor.CREDS), exist_ok=True)
    with open(governor.CREDS, "w", encoding="utf-8") as f:  # token far from expiry: no `claude -p` refresh
        json.dump({"claudeAiOauth": {"accessToken": "test-token", "refreshToken": "test-refresh",
                                     "expiresAt": int((time.time() + 3 * 3600) * 1000)}}, f)
    real_request_json = http_util.request_json
    monkeypatch.setattr(http_util, "request_json",
                        lambda url, **kw: real_request_json(url.replace(ANTHROPIC, server.url, 1), **kw))
    monkeypatch.setattr(pacer_config, "wsl_run", lambda cfg, cmd, *a, **k: pacer_config.WslResult(code=0))
    yield server
    server.close()


def set_settings(paperclip=True, copilot=None, **claude):
    """Settings with Claude's throttle settings as keywords; copilot: its throttle settings, None = Copilot hidden."""
    with open(governor.SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"paperclip": paperclip, "copilot": copilot is not None,
                   "providers": {"claude": claude, "copilot": copilot or {}}}, f)


def write_state(**kv):
    with open(governor.STATE, "w", encoding="utf-8") as f:
        json.dump({"mode": "active", "usageFailures": 0, **kv}, f)


def state():
    with open(governor.STATE, encoding="utf-8") as f:
        return json.load(f)


def tick_ok():
    assert governor.run() is True


def sub(name="claude"):
    """Sub-state of one provider."""
    return state()["providers"][name]


def set_copilot(used, elapsed_frac, **kv):
    """Copilot monitor reading: `used` percent of the monthly quota with `elapsed_frac` of the month gone."""
    reset = datetime.fromtimestamp(time.time() + 30 * 86400 * (1 - elapsed_frac), timezone.utc).isoformat()
    fsutil.write_json_atomic(copilot_monitor.STATUS, {"state": "ok", "unlimited": False, "used": used, "resets": reset,
                                                      "at": time.time(), **kv})


def log_size():
    return os.path.getsize(LOG) if os.path.exists(LOG) else 0


def log_since(offset):
    with open(LOG, "rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8")


def test_pauses_only_claude_agents_at_the_limit(fake):
    set_settings()
    sentinel = fake.add_agent("Sentinel", CLAUDE)
    atlas = fake.add_agent("Atlas", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE)
    fake.set_usage(five=75, seven=20)
    tick_ok()
    assert fake.posted("pause") == [sentinel, atlas]
    assert fake.posted("resume") == []
    assert fake.status(eli) == "active"
    assert sub()["mode"] == "paused"
    assert sub()["pausedAgents"] == [sentinel, atlas]
    with open(governor.STATUS, encoding="utf-8") as f:
        status = json.load(f)
    assert status["mode"] == "paused" and status["usage"]["five"] == 75


@pytest.mark.parametrize(("five", "seven", "paused"),
                         [(69.9, 10, False), (70, 10, True), (10, 70, True), (10, 69.9, False)])
def test_pause_limit_is_inclusive_and_either_window_triggers_it(fake, five, seven, paused):
    set_settings()
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(five, seven)
    tick_ok()
    assert fake.posted("pause") == ([agent] if paused else [])


def test_limits_come_from_settings(fake):
    set_settings(pauseAt=50, resumeBelow=40)
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(49, 10)
    tick_ok()
    assert fake.posted("pause") == []
    fake.set_usage(50, 10)
    tick_ok()
    assert fake.posted("pause") == [agent]
    fake.set_usage(45, 10)  # above the custom resume limit of 40: stays paused
    tick_ok()
    assert fake.posted("resume") == []
    fake.set_usage(39, 10)
    tick_ok()
    assert fake.posted("resume") == [agent]


def test_resumes_after_reset_only_when_both_windows_are_below_the_resume_limit(fake):
    set_settings()
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(75, 20)
    tick_ok()
    fake.set_usage(five=10, seven=65)  # the 7d window is still above 60
    tick_ok()
    assert fake.posted("resume") == []
    fake.set_usage(five=10, seven=59)  # both windows have reset below 60
    tick_ok()
    assert fake.posted("resume") == [agent]
    assert fake.status(agent) == "active"
    assert sub()["mode"] == "active" and sub()["pausedAgents"] == []


def test_agents_paused_by_hand_are_never_resumed_by_the_governor(fake):
    set_settings()
    auto = fake.add_agent("Sentinel", CLAUDE)
    manual = fake.add_agent("Atlas", CLAUDE, status="paused")
    fake.set_usage(75, 20)
    tick_ok()
    assert fake.posted("pause") == [auto]
    fake.set_usage(10, 10)
    tick_ok()
    assert fake.posted("resume") == [auto]
    assert fake.status(manual) == "paused"


def test_non_claude_agent_paused_by_an_earlier_governor_is_resumed_at_once(fake):
    set_settings()
    eli = fake.add_agent("Eli", OPENCODE, status="paused")
    sentinel = fake.add_agent("Sentinel", CLAUDE)
    write_state(mode="paused", pausedAgents=[eli])
    fake.set_usage(75, 20)  # still over the limit: Eli runs on Copilot, outside the Claude budget
    tick_ok()
    assert fake.posted("resume") == [eli]
    assert fake.posted("pause") == [sentinel]
    assert fake.status(eli) == "active"
    assert sub()["pausedAgents"] == [sentinel]


def test_non_claude_agent_paused_by_hand_is_left_alone(fake):
    set_settings()
    fake.add_agent("Sentinel", CLAUDE)
    fake.add_agent("Eli", OPENCODE, status="paused")
    fake.set_usage(10, 10)
    tick_ok()
    assert fake.posted("resume") == []


def test_claude_agent_with_a_live_run_drains_before_it_is_paused(fake):
    set_settings()
    running = fake.add_agent("Sentinel", CLAUDE)
    idle = fake.add_agent("Atlas", CLAUDE)
    fake.live_agent_ids = {running}
    fake.set_usage(75, 10)
    tick_ok()
    assert fake.posted("pause") == [idle]
    assert sub()["mode"] == "draining"
    fake.live_agent_ids = set()
    fake.set_usage(75, 10)
    tick_ok()
    assert fake.posted("pause") == [idle, running]
    assert sub()["mode"] == "paused"


@pytest.mark.parametrize(("five", "expected_mode", "cancelled"), [(89, "draining", False), (90, "paused", True)])
def test_hard_limit_cancels_a_live_run(fake, five, expected_mode, cancelled):
    set_settings()
    running = fake.add_agent("Sentinel", CLAUDE)
    fake.live_agent_ids = {running}
    fake.set_usage(five, 10)
    tick_ok()
    assert fake.posted("pause") == ([running] if cancelled else [])
    assert sub()["mode"] == expected_mode


def test_full_budget_pauses_only_at_100_percent(fake):
    set_settings(fullBudget=True)
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(95, 95)
    tick_ok()
    assert fake.posted("pause") == []
    assert sub()["mode"] == "active"
    fake.set_usage(100, 10)
    tick_ok()
    assert fake.posted("pause") == [agent]


def test_without_paperclip_nothing_is_paused_but_usage_is_reported(fake):
    set_settings(paperclip=False)
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(95, 95)
    tick_ok()
    assert fake.posted("pause") == []
    assert fake.status(agent) == "active"
    with open(governor.STATUS, encoding="utf-8") as f:
        assert json.load(f)["usage"]["five"] == 95


def test_unhealthy_paperclip_skips_the_tick(fake):
    set_settings()
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.health = (503, {"error": "down"})
    fake.set_usage(95, 95)
    tick_ok()
    assert fake.posted("pause") == []
    assert fake.status(agent) == "active"
    assert not os.path.exists(governor.STATUS)  # no usage reading, so no status written


def test_tick_fails_without_a_claude_agent_and_pauses_nothing(fake):
    set_settings()
    fake.add_agent("Eli", OPENCODE)
    fake.set_usage(95, 95)
    assert governor.run() is False
    assert fake.posted("pause") == []


BROKEN_USAGE = [
    pytest.param((500, {"error": "boom"}), id="http-500"),
    pytest.param((429, {"error": "slow down"}), id="http-429"),
    pytest.param((200, b"<html>login</html>"), id="not-json"),
]


@pytest.mark.parametrize("broken", BROKEN_USAGE)
def test_three_failed_usage_polls_in_a_row_pause_claude_agents(fake, broken):
    set_settings()
    claude = fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE)
    fake.usage = broken
    tick_ok()
    assert state()["usageFailures"] == 1 and fake.posted("pause") == []
    tick_ok()
    assert state()["usageFailures"] == 2 and fake.posted("pause") == []
    tick_ok()
    assert state()["usageFailures"] == 3
    assert fake.posted("pause") == [claude]
    assert fake.status(eli) == "active"


def test_a_good_reading_resets_the_failure_count(fake):
    set_settings()
    fake.add_agent("Sentinel", CLAUDE)
    fake.usage = (500, {"error": "boom"})
    tick_ok()
    tick_ok()
    fake.set_usage(10, 10)
    tick_ok()
    assert state()["usageFailures"] == 0
    fake.usage = (500, {"error": "boom"})
    tick_ok()
    tick_ok()
    assert fake.posted("pause") == []


SCHEMA_CHANGES = [
    pytest.param({}, id="no-windows"),
    pytest.param({"five_hour": {"utilization": "80"}, "seven_day": {"utilization": 10}}, id="string-utilization"),
    pytest.param({"five_hour": {"utilization": True}, "seven_day": {"utilization": 10}}, id="bool-utilization"),
    pytest.param({"five_hour": {"resets_at": "2026-11-09T12:00:00Z"}, "seven_day": {"utilization": 10}},
                 id="missing-utilization"),
    pytest.param([], id="not-an-object"),
]


@pytest.mark.parametrize("body", SCHEMA_CHANGES)
def test_changed_usage_schema_is_logged_and_counts_as_a_failed_poll(fake, body):
    set_settings()
    claude = fake.add_agent("Sentinel", CLAUDE)
    fake.usage = (200, body)
    offset = log_size()
    tick_ok()
    assert "ERROR usage endpoint schema changed" in log_since(offset)
    assert state()["usageFailures"] == 1
    assert fake.posted("pause") == []
    tick_ok()
    tick_ok()
    assert fake.posted("pause") == [claude]


# ---- providers have their own settings and sub-state

def test_copilot_burning_too_fast_pauses_only_copilot_agents(fake):
    set_settings(copilot={})
    sentinel = fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, model=COPILOT_MODEL)
    fake.set_usage(30, 20)
    set_copilot(used=60, elapsed_frac=0.33)  # would hit 100 % around day 17 of the month
    tick_ok()
    assert fake.posted("pause") == [eli]
    assert fake.status(sentinel) == "active"
    assert sub("copilot") == {"mode": "paused", "pausedAgents": [eli], "reason": "pace"}
    assert sub("claude")["mode"] == "active"
    with open(governor.STATUS, encoding="utf-8") as f:
        status = json.load(f)
    assert status["mode"] == "paused"  # overall = the worst sub-state
    assert status["providers"]["claude"]["mode"] == "active"
    assert status["providers"]["copilot"]["mode"] == "paused" and status["providers"]["copilot"]["reason"] == "pace"


def test_copilot_agents_resume_once_usage_is_back_on_the_plan_line(fake):
    set_settings(copilot={})
    fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, model=COPILOT_MODEL)
    fake.set_usage(30, 20)
    set_copilot(used=60, elapsed_frac=0.33)
    tick_ok()
    set_copilot(used=60, elapsed_frac=0.55)  # still ahead of the plan, only less: stays paused
    tick_ok()
    assert fake.posted("resume") == []
    set_copilot(used=60, elapsed_frac=0.70)
    tick_ok()
    assert fake.posted("resume") == [eli]
    assert sub("copilot")["mode"] == "active"


def test_without_the_pace_rule_copilot_pauses_only_at_its_limit(fake):
    set_settings(copilot={"byPace": False, "fullBudget": False, "pauseAt": 80, "resumeBelow": 70})
    fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, model=COPILOT_MODEL)
    fake.set_usage(30, 20)
    set_copilot(used=60, elapsed_frac=0.33)
    tick_ok()
    assert fake.posted("pause") == []
    set_copilot(used=80, elapsed_frac=0.40)
    tick_ok()
    assert fake.posted("pause") == [eli]
    assert sub("copilot")["reason"] == "limit"


def test_claude_limit_leaves_copilot_agents_running(fake):
    set_settings(copilot={})
    sentinel = fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, model=COPILOT_MODEL)
    fake.set_usage(75, 20)
    set_copilot(used=20, elapsed_frac=0.50)
    tick_ok()
    assert fake.posted("pause") == [sentinel]
    assert fake.status(eli) == "active"
    assert sub("copilot")["mode"] == "active"


@pytest.mark.parametrize("reading", [
    pytest.param(None, id="no-status-file"),
    pytest.param({"state": "signin"}, id="signed-out"),
    pytest.param({"at": time.time() - 3600}, id="stale"),
])
def test_without_a_usable_copilot_reading_its_agents_stay_as_they_are(fake, reading):
    set_settings(copilot={})
    fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, status="paused", model=COPILOT_MODEL)
    write_state(providers={"copilot": {"mode": "paused", "pausedAgents": [eli], "reason": "pace"}})
    fake.set_usage(30, 20)
    if reading is not None:
        set_copilot(used=10, elapsed_frac=0.50, **reading)
    tick_ok()
    assert fake.posted("resume") == [] and fake.posted("pause") == []
    assert sub("copilot")["mode"] == "paused"


def test_hidden_copilot_is_not_throttled_and_its_paused_agents_resume(fake):
    set_settings()  # copilot: false
    fake.add_agent("Sentinel", CLAUDE)
    eli = fake.add_agent("Eli", OPENCODE, status="paused", model=COPILOT_MODEL)
    write_state(providers={"copilot": {"mode": "paused", "pausedAgents": [eli], "reason": "pace"}})
    fake.set_usage(30, 20)
    set_copilot(used=60, elapsed_frac=0.33)
    tick_ok()
    assert fake.posted("resume") == [eli]
    assert sub("copilot")["mode"] == "active"


def test_opencode_agent_on_another_provider_is_never_paused(fake):
    set_settings(copilot={})
    fake.add_agent("Sentinel", CLAUDE)
    other = fake.add_agent("Nova", OPENCODE, model="openrouter/some-model")
    fake.set_usage(30, 20)
    set_copilot(used=60, elapsed_frac=0.33)
    tick_ok()
    assert fake.posted("pause") == []
    assert fake.status(other) == "active"
    assert sub("copilot")["mode"] == "active"  # nobody to pause: it does not hold the overall state


def test_claude_pace_rule_pauses_a_window_that_burns_too_fast(fake):
    set_settings(byPace=True)
    agent = fake.add_agent("Sentinel", CLAUDE)
    in_4h = datetime.fromtimestamp(time.time() + 4 * 3600, timezone.utc).isoformat()
    in_3h = datetime.fromtimestamp(time.time() + 3 * 3600, timezone.utc).isoformat()
    fake.set_usage(40, 5, resets5=in_4h)  # 40 % after 1 of 5 hours, limit 70 %: reached after 1h45m
    tick_ok()
    assert fake.posted("pause") == [agent]
    assert sub()["reason"] == "pace"
    fake.set_usage(40, 5, resets5=in_3h)  # plan at 2 of 5 hours is 28 %: still ahead
    tick_ok()
    assert fake.posted("resume") == []
    in_2h = datetime.fromtimestamp(time.time() + 2 * 3600, timezone.utc).isoformat()
    fake.set_usage(40, 5, resets5=in_2h)  # plan at 3 of 5 hours is 42 %
    tick_ok()
    assert fake.posted("resume") == [agent]


def test_tick_is_skipped_while_another_live_tick_holds_the_lock(fake):
    set_settings()
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(95, 95)
    fsutil.pid_lock_write(governor.LOCK)  # held by this live test process
    assert governor.run() is True
    assert fake.requests == []
    assert fake.status(agent) == "active"


def test_lock_left_behind_by_a_dead_tick_is_taken_over(fake):
    set_settings()
    agent = fake.add_agent("Sentinel", CLAUDE)
    fake.set_usage(75, 10)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    with open(governor.LOCK, "w", encoding="utf-8") as f:
        json.dump({"pid": dead.pid, "start": None}, f)
    tick_ok()
    assert fake.posted("pause") == [agent]
    assert not os.path.exists(governor.LOCK)


def set_openrouter(used, **kv):
    fsutil.write_json_atomic(openrouter_monitor.STATUS, {"state": "ok", "unlimited": False, "used": used, "kind": None,
                                                         "at": time.time(), **kv})


def test_openrouter_key_limit_reached_pauses_only_openrouter_agents(fake):
    set_settings(copilot={})
    sentinel = fake.add_agent("Sentinel", CLAUDE)
    router = fake.add_agent("Router", OPENCODE, model="openrouter/anthropic/claude-sonnet-4.5")
    fake.set_usage(30, 20)
    set_openrouter(used=100)
    tick_ok()
    assert fake.posted("pause") == [router]
    assert fake.status(sentinel) == "active"
    assert sub("openrouter") == {"mode": "paused", "pausedAgents": [router], "reason": "limit"}
    assert sub("claude")["mode"] == "active"


def test_openrouter_below_the_limit_and_keys_without_limit_pause_nothing(fake):
    set_settings(copilot={})
    fake.add_agent("Sentinel", CLAUDE)
    fake.add_agent("Router", OPENCODE, model="openrouter/anthropic/claude-sonnet-4.5")
    fake.set_usage(30, 20)
    set_openrouter(used=80)  # full budget by default: only the real limit pauses
    tick_ok()
    set_openrouter(used=None, unlimited=True)
    tick_ok()
    assert fake.posted("pause") == []
