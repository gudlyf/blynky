"""The zone days are split in, as the server names it to the browser."""

import time

import pytest

from blynky import config, daily, library

pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs tzset")


@pytest.fixture
def zone(monkeypatch):
    """Switch this process's local time to a TZ, and back again after."""

    def use(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def test_names_the_zone_tz_sets(zone):
    zone("America/Los_Angeles")
    assert config._time_zone() == "America/Los_Angeles"


@pytest.mark.parametrize("tz", ["EST5EDT,M3.2.0,M11.1.0", "Not/A_Zone", ""])
def test_no_name_unless_tz_names_the_clock_kept(zone, tz):
    # A POSIX rule is no IANA name, a zone that does not exist leaves the C
    # library on UTC, and no TZ at all leaves the browser to its own clock.
    zone(tz)
    assert config._time_zone() is None


def test_days_split_at_local_midnight(zone):
    zone("America/Los_Angeles")
    # 03:30 UTC on the 2nd is still the evening of the 1st in Los Angeles.
    record = {
        "id": "porch/1",
        "slug": "porch",
        "kind": library.MOTION,
        "stamp": "2026-11-02T03:30:00+00:00",
    }
    grouped = daily.Daily()._group([record], "porch")
    assert list(grouped) == [("porch", "2026-11-01")]
