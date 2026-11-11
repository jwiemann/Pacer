"""Mini graphs of the sidebar tabs: how quota readings become the graph of a tab."""
import time
from datetime import datetime, timezone

import pytest

import dashboard_data


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def test_readings_fill_the_graph_width_and_height_follows_the_peak():
    now = time.time()
    span = 5 * 3600
    start = now - span / 2  # window is half over
    pts = [(start + 600, 10.0), (start + 3600, 30.0)]
    points, top = dashboard_data.quota_spark(pts, iso(start + span), span, 42.0, now)
    assert points[0][0] == 0 and points[-1] == pytest.approx((1.0, 42.0))  # stretched over the whole width
    assert top == pytest.approx(42.0)  # the peak: not 100, a small curve must stay visible


def test_flat_low_usage_gets_a_minimum_scale():
    now = time.time()
    pts = [(now - 3000, 0.0), (now - 1000, 0.0)]
    points, top = dashboard_data.quota_spark(pts, iso(now + 1000), 5 * 3600, 0.0, now)
    assert top == 5.0 and len(points) == 3


def test_readings_of_the_previous_window_are_dropped():
    now = time.time()
    span = 5 * 3600
    start = now - 1000
    pts = [(start - 3600, 99.0), (start + 100, 2.0)]  # first one belongs to the window that just reset
    points, _top = dashboard_data.quota_spark(pts, iso(start + span), span, 3.0, now)
    assert [v for _x, v in points] == [2.0, 3.0]


def test_no_window_or_no_history_no_graph():
    now = time.time()
    assert dashboard_data.quota_spark([], None, 18000, 10.0, now) is None
    assert dashboard_data.quota_spark([], iso(now + 100), 18000, None, now) is None
    assert dashboard_data.quota_spark([], "not a date", 18000, 10.0, now) is None
    assert dashboard_data.quota_spark([], iso(now + 100), None, 10.0, now) is None
    assert dashboard_data.quota_spark([], iso(now + 100), 18000, 10.0, now) is None  # a single point is no curve
