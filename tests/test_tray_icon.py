"""tray_icon pace math: verdict boundaries, the early-window and early-hit tolerances, window resets and glow keys.

All times come from a fixed clock (`now`), and resets are built as offsets from it, so the results never depend on the
wall clock."""
from datetime import datetime, timezone

import pytest

import tray_icon

NOW = 1_800_000_000.0
WEEK = 7 * 86400
MONTH = 30 * 86400


def reset_after(seconds):
    return datetime.fromtimestamp(NOW + seconds, timezone.utc).isoformat()


def week_at(elapsed_frac):
    """Reset time of a 7d window that is `elapsed_frac` through."""
    return reset_after(WEEK * (1 - elapsed_frac))


def pace_7d(usage, elapsed_frac, threshold=70):
    return tray_icon.pace(usage, week_at(elapsed_frac), "7d", threshold, now=NOW)


def test_no_usage_reading_or_no_threshold_gives_no_pace():
    assert tray_icon.pace(None, week_at(0.5), "7d", 70, now=NOW) is None
    assert tray_icon.pace(10, week_at(0.5), "7d", 0, now=NOW) is None


def test_unparseable_reset_gives_no_pace():
    assert tray_icon.pace(10, "soon", "7d", 70, now=NOW) is None


def test_window_not_running_with_no_usage_is_too_little_with_needle_at_zero():
    p = tray_icon.pace(0, None, "5h", 70, now=NOW)
    assert p["verdict"] == "too little" and p["ratio"] == 0.0 and p["left_min"] is None


def test_window_not_running_with_usage_has_no_pace():
    assert tray_icon.pace(12, None, "5h", 70, now=NOW) is None


@pytest.mark.parametrize(("usage", "verdict"), [
    (17.4, "too little"),   # ratio 0.497: just under half of the steady-burn line
    (17.5, "on plan"),      # ratio exactly 0.5 is on plan
    (35, "on plan"),        # ratio exactly 1.0 is on plan
    (35.1, "on plan"),      # ratio just over 1.0, but the limit is reached only ~29 min before the reset
    (52.5, "too much"),     # ratio 1.5: the limit would be reached 2 days early
    (70, "limit"),
    (80, "limit"),
])
def test_verdict_boundaries_at_half_of_a_week(usage, verdict):
    assert pace_7d(usage, elapsed_frac=0.5)["verdict"] == verdict


def test_projection_and_eta_of_a_steady_burn_that_ends_at_the_limit():
    p = pace_7d(35, elapsed_frac=0.5)
    assert p["ratio"] == pytest.approx(1.0)
    assert p["projected"] == pytest.approx(70)
    assert p["eta_min"] == pytest.approx(0.5 * WEEK / 60)  # reaches 70 exactly at the reset
    assert p["left_min"] == pytest.approx(0.5 * WEEK / 60)


def test_early_hit_within_four_hours_of_the_reset_still_counts_as_on_plan():
    # 80 % through the week: 57 % would reach the limit about 2.9 h before the reset (inside the 4 h tolerance)
    assert pace_7d(57, elapsed_frac=0.8)["verdict"] == "on plan"


def test_early_hit_more_than_four_hours_before_the_reset_is_too_much():
    # the same window with 60 % reaches the limit about 11 h before the reset
    p = pace_7d(60, elapsed_frac=0.8)
    assert p["verdict"] == "too much"
    assert p["eta_min"] == pytest.approx(0.8 * WEEK / 60 * (10 / 60))


@pytest.mark.parametrize("elapsed_frac", [0.0, 0.03, 0.049])
def test_first_five_percent_of_a_window_gives_an_early_needle_but_no_verdict(elapsed_frac):
    p = pace_7d(3, elapsed_frac=elapsed_frac)
    assert p["verdict"] == "early"
    assert p["ratio"] == pytest.approx(3 / (70 * 0.05))


def test_five_percent_in_the_verdict_starts():
    assert pace_7d(3, elapsed_frac=0.06)["verdict"] != "early"


def test_limit_beats_the_early_window():
    assert pace_7d(70, elapsed_frac=0.01)["verdict"] == "limit"


def test_monthly_window_uses_its_own_six_hour_early_period():
    reset = reset_after(MONTH * (1 - 0.005))  # 0.5 % through: inside 6 h of 30 days
    assert tray_icon.pace(5, reset, "cop", 70, now=NOW, span_s=MONTH, early_s=6 * 3600)["verdict"] == "early"
    reset = reset_after(MONTH * (1 - 0.02))  # 2 % through: past the early period
    assert tray_icon.pace(5, reset, "cop", 70, now=NOW, span_s=MONTH, early_s=6 * 3600)["verdict"] == "too much"


def test_reset_further_away_than_a_window_counts_as_a_fresh_window():
    p = tray_icon.pace(3, reset_after(10 * WEEK), "7d", 70, now=NOW)
    assert p["verdict"] == "early"
    assert p["left_min"] == pytest.approx(WEEK / 60)


@pytest.mark.parametrize(("usage", "elapsed_frac", "glow"), [
    (70, 0.01, "bad"),       # limit
    (3, 0.03, "muted"),      # early: no verdict yet
    (10, 0.5, "blue"),       # too little
    (30, 0.5, "good"),       # on plan
    (57, 0.8, "good"),       # too much only by the early-hit tolerance
    (60, 0.8, "warn"),       # too much, but the limit is hit only ~11 h early (under 10 % of the window)
    (60, 0.5, "bad"),        # too much, hit ~42 % of the window early
])
def test_glow_keys_follow_the_verdict(usage, elapsed_frac, glow):
    p = pace_7d(usage, elapsed_frac)
    assert tray_icon.window_verdict(p, usage, WEEK) == glow


def test_glow_is_muted_without_a_reading_or_pace():
    assert tray_icon.window_verdict(None, 10, WEEK) == "muted"
    assert tray_icon.window_verdict(pace_7d(10, 0.5), None, WEEK) == "muted"


@pytest.mark.parametrize(("ratio", "angle"), [
    (0.0, -90.0), (0.5, -45.0), (1.0, 0.0), (2.0, 90.0), (5.0, 90.0), (-3.0, -90.0),
])
def test_needle_sweeps_from_left_through_top_to_right_and_clamps(ratio, angle):
    assert tray_icon.needle_angle(ratio) == pytest.approx(angle)
