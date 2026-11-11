"""quota_moved: a provider's tabs are shown only while its usage moved within the last 24 h."""
import pytest

from dashboard_data import quota_moved

NOW = 1_000_000.0
H = 3600


@pytest.mark.parametrize(("pts", "usage", "moved"), [
    ([(NOW - 2 * H, 10), (NOW - H, 10)], 10, False),            # flat
    ([(NOW - 2 * H, 10)], 12, True),                           # grew since the last reading
    ([(NOW - 30 * H, 5), (NOW - 25 * H, 5)], 40, True),        # the newest reading before the window is the starting value
    ([(NOW - 30 * H, 5), (NOW - 25 * H, 20)], 20, False),      # changes older than 24 h only
    ([], None, False),                                         # no reading at all
])
def test_usage_counts_as_moving_only_within_the_window(pts, usage, moved):
    assert quota_moved(pts, usage, NOW) is moved
