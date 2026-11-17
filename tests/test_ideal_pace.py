import throttle


def test_ideal_five_rate_follows_the_cost_of_a_five_hour_percent():
    now = 1_000_000_000
    # 10 five-hour percent cost 1 seven-day percent; the second step crosses a 5h reset (five drops: skipped)
    hist = [(now - 400, 0, 10), (now - 300, 10, 11), (now - 200, 20, 12), (now - 100, 5, 12), (now, 15, 13)]
    assert round(throttle.ideal_five_rate(hist, 100, now), 2) == round(100 / 168 / 0.1, 2)


def test_ideal_five_rate_needs_history():
    assert throttle.ideal_five_rate([(1, 0, 0), (2, 0.5, 0)], 100, 3) is None


def test_follow_the_7d_plan_pauses_above_the_cap_and_resumes_under_it():
    s = {**throttle.DEFAULTS["claude"], "fullBudget": True, "byWeek": True}
    win = lambda usage, cap: [{"usage": usage, "reset": None, "key": "5h", "span": 5 * 3600, "cap": cap}]
    assert throttle.decide("active", None, win(40, 30), s) == ("paused", "week")
    assert throttle.decide("paused", "week", win(40, 30), s) == ("paused", "week")
    assert throttle.decide("paused", "week", win(40, 45), s) == ("active", None)
    assert throttle.decide("active", None, win(40, 30), {**s, "byWeek": False}) == ("active", None)
