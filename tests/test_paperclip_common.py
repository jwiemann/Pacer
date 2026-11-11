"""paperclip_common: duration and pace text, the tray/dashboard status model (evaluate), and the shared settings file."""
import json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import fsutil
import logutil
import paperclip_common as pc
import throttle

ROOT = Path(__file__).resolve().parents[1]
WEEK = 7 * 86400


@pytest.fixture
def settings_file():
    """The shared settings file, empty at the start of each test."""
    if os.path.exists(pc.SETTINGS):
        os.remove(pc.SETTINGS)
    return pc.SETTINGS


def set_settings(paperclip=True, **claude):
    with open(pc.SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"paperclip": paperclip, "providers": {"claude": claude}}, f)


def write_status(usage=None, mode="active", at=None, full=False, reason=None, copilot="active"):
    data = {"at": logutil.iso_now() if at is None else at, "mode": throttle.overall([mode, copilot]),
            "providers": {"claude": {"mode": mode, "reason": reason, "fullBudget": full}, "copilot": {"mode": copilot}},
            "usage": usage}
    fsutil.write_json_atomic(pc.STATUS, data, indent=2)


def reset_in(seconds):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def no_status_file():
    if os.path.exists(pc.STATUS):
        os.remove(pc.STATUS)


# ---- fmt_min / pace_text

@pytest.mark.parametrize(("minutes", "text"), [
    (None, "-"), (0, "0m"), (59, "59m"), (60, "1h 00m"), (61, "1h 01m"), (1439, "23h 59m"),
    (1440, "1d 0h"), (1500, "1d 1h"), (90.7, "1h 30m"), (3 * 1440 + 150, "3d 2h"),
])
def test_fmt_min_shows_the_largest_two_units(minutes, text):
    assert pc.fmt_min(minutes) == text


def test_pace_text_without_pace_is_none():
    assert pc.pace_text(None) is None


@pytest.mark.parametrize(("pace", "text"), [
    ({"verdict": "early"}, "pace: too early to tell (tentative)"),
    ({"verdict": "limit"}, "pace: limit reached"),
    ({"verdict": "too much", "eta_min": 125.4}, "pace: TOO MUCH - limit in ~2h 05m"),
    ({"verdict": "too little", "projected": 12.4}, "pace: too little - ends ~12%"),
    ({"verdict": "on plan", "projected": 83.6}, "pace: on plan - ends ~84%"),
])
def test_pace_text_states_the_verdict(pace, text):
    assert pc.pace_text(pace) == text


# ---- evaluate: tray icon model and tooltip

def test_without_a_status_file_the_tray_reports_no_status_and_a_fault(settings_file):
    set_settings()
    model, tip = pc.evaluate()
    assert model["fault"] is True
    assert model["pct"] is None
    assert tip == "Claude: no status yet"


def test_running_below_the_limit_shows_the_limiting_window_and_pause_point(settings_file):
    set_settings()
    write_status(usage={"five": 75, "seven": 20})
    model, tip = pc.evaluate()
    assert model["fault"] is False and model["paused"] is False
    assert model["window"] == "5h" and model["pct"] == 75 and model["threshold"] == 70
    assert tip.splitlines()[0] == "5h usage 75% - running"
    assert tip.splitlines()[-1] == "pauses at 70%"


def test_paused_shows_the_resume_point(settings_file):
    set_settings()
    write_status(usage={"five": 75, "seven": 20}, mode="paused")
    model, tip = pc.evaluate()
    assert model["paused"] is True
    assert tip.splitlines()[0] == "5h usage 75% - paused"
    assert tip.splitlines()[-1] == "resumes below 60%"


def test_draining_is_shown_as_its_own_state(settings_file):
    set_settings()
    write_status(usage={"five": 72, "seven": 20}, mode="draining")
    _, tip = pc.evaluate()
    assert tip.splitlines()[0] == "5h usage 72% - draining"


def test_full_budget_moves_the_pause_point_to_100_percent(settings_file):
    set_settings(fullBudget=True)
    write_status(usage={"five": 75, "seven": 20}, full=True)
    model, tip = pc.evaluate()
    assert model["threshold"] == 100 and model["full_budget"] is True
    assert tip.splitlines()[-1] == "full budget: pause at 100%"


def test_full_budget_not_yet_applied_to_the_governor_is_marked(settings_file):
    set_settings(fullBudget=True)
    write_status(usage={"five": 75, "seven": 20}, full=False)
    _, tip = pc.evaluate()
    assert tip.splitlines()[-1].endswith("(applying...)")


def test_paused_by_pace_names_the_plan_as_the_resume_point(settings_file):
    set_settings(byPace=True)
    write_status(usage={"five": 40, "seven": 20}, mode="paused", reason="pace")
    _, tip = pc.evaluate()
    assert tip.splitlines()[-1] == "resumes when back on plan"


def test_paused_copilot_makes_the_overall_state_paused_while_claude_runs(settings_file):
    set_settings()
    write_status(usage={"five": 30, "seven": 20}, copilot="paused")
    model, tip = pc.evaluate()
    assert model["paused"] is True
    assert model["providers"] == {"claude": "active", "copilot": "paused", "openrouter": "active"}
    assert tip.splitlines()[0] == "5h usage 30% - running"
    assert tip.splitlines()[-1] == "Copilot paused"


def test_without_paperclip_the_server_and_watchdog_are_not_watched(settings_file):
    set_settings(paperclip=False)
    write_status(usage={"five": 75, "seven": 20}, full=True)
    model, tip = pc.evaluate(gov_ok=True, wd_ok=False, server_ok=False)
    assert model["fault"] is False
    assert "SERVER DOWN" not in tip and "watchdog" not in tip


@pytest.mark.parametrize(("kwargs", "problem"), [
    ({"gov_ok": False}, "governor"),
    ({"wd_ok": False}, "watchdog"),
    ({"server_ok": False}, "SERVER DOWN"),
])
def test_a_failed_part_is_a_fault_and_named_in_the_tooltip(settings_file, kwargs, problem):
    set_settings()
    write_status(usage={"five": 10, "seven": 10})
    model, tip = pc.evaluate(**kwargs)
    assert model["fault"] is True
    assert problem in tip


@pytest.mark.parametrize(("age_min", "fault"), [(9, False), (11, True)])
def test_status_older_than_ten_minutes_is_a_fault(settings_file, age_min, fault):
    set_settings()
    write_status(usage={"five": 10, "seven": 10}, at=logutil.iso_now(time.time() - age_min * 60))
    model, tip = pc.evaluate()
    assert model["fault"] is fault
    assert (f"status {age_min}m old" in tip) is fault


@pytest.mark.parametrize(("seven", "elapsed_frac", "glow"), [
    (20, 0.5, "good"),
    (5, 0.5, "blue"),
    (60, 0.8, "warn"),
    (70, 0.5, "bad"),
])
def test_glow_is_the_seven_day_pace(settings_file, seven, elapsed_frac, glow):
    set_settings()
    write_status(usage={"five": 10, "seven": seven, "resets7": reset_in(WEEK * (1 - elapsed_frac))})
    model, _ = pc.evaluate()
    assert model["glow"] == glow


def test_tooltip_fits_the_tray_limit_with_every_problem_listed(settings_file):
    set_settings()
    write_status(usage={"five": 80, "seven": 80, "resets5": reset_in(3600), "resets7": reset_in(WEEK / 2)},
                 at=logutil.iso_now(time.time() - 3600))
    _, tip = pc.evaluate(gov_ok=False, wd_ok=False, server_ok=False)
    assert len(tip) <= 127
    assert "SERVER DOWN" in tip and "status 60m old" in tip


# ---- settings file

def test_writes_by_different_callers_keep_each_others_keys(settings_file):
    pc.write_settings(paperclip=True)
    pc.write_settings(keepAwake=True)
    assert pc.read_settings() == {"paperclip": True, "keepAwake": True}


def test_later_write_replaces_the_same_key(settings_file):
    pc.write_settings(lang="de")
    pc.write_settings(lang="en")
    assert pc.read_settings() == {"lang": "en"}


WRITER = """
import sys
from paperclip_common import write_settings
tag = sys.argv[1]
for i in range(15):
    write_settings(**{f"{tag}{i}": i})
"""


def test_concurrent_writers_from_several_processes_lose_no_keys(settings_file):
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, tag], cwd=ROOT) for tag in "abcd"]
    for w in writers:
        assert w.wait(timeout=120) == 0
    keys = pc.read_settings()
    assert len(keys) == 60
    assert all(keys[f"{tag}{i}"] == i for tag in "abcd" for i in range(15))


@pytest.mark.parametrize(("setting", "enabled"), [(False, False), (True, True)])
def test_paperclip_setting_decides_whether_paperclip_is_part_of_the_pacer(settings_file, setting, enabled):
    set_settings(paperclip=setting)
    assert pc.paperclip_enabled() is enabled


def test_without_paperclip_the_only_limit_is_the_real_one(settings_file):
    set_settings(paperclip=False, fullBudget=False)
    assert pc.full_budget() is True


def test_with_paperclip_full_budget_follows_its_setting(settings_file):
    set_settings(fullBudget=False)
    assert pc.full_budget() is False
    set_settings(fullBudget=True)
    assert pc.full_budget() is True


def test_limits_from_before_the_split_by_provider_move_to_claude(settings_file):
    with open(pc.SETTINGS, "w", encoding="utf-8") as f:
        json.dump({"paperclip": True, "lang": "de", "fullBudget": True, "pauseAt": 80, "resumeBelow": 50}, f)
    pc.migrate_settings()
    assert pc.read_settings() == {"paperclip": True, "lang": "de",
                                  "providers": {"claude": {"fullBudget": True, "pauseAt": 80, "resumeBelow": 50}}}
    assert pc.provider_settings("claude") == {"fullBudget": True, "pauseAt": 80, "resumeBelow": 50, "byPace": False}
    assert pc.provider_settings("copilot")["byPace"] is True  # untouched: its defaults


@pytest.mark.parametrize("bad", [{"pauseAt": 50, "resumeBelow": 60}, {"pauseAt": 0, "resumeBelow": 0},
                                 {"pauseAt": "80", "resumeBelow": 50}, {"pauseAt": True, "resumeBelow": True}])
def test_invalid_limits_fall_back_to_the_defaults(settings_file, bad):
    set_settings(**bad)
    s = pc.provider_settings("claude")
    assert (s["pauseAt"], s["resumeBelow"]) == (70, 60)
