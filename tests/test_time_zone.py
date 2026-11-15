"""The zone days are split in, as the server names it to the browser."""

import time

import pytest

from blynky import config, daily, library, monitor

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


# ------------------------------------------------------ zones per location


def motion(slug, stamp, summary="a fox"):
    return {
        "id": f"{slug}/{stamp}",
        "slug": slug,
        "camera": slug.title(),
        "kind": library.MOTION,
        "stamp": stamp,
        "summary": summary,
    }


@pytest.fixture
def two_places(monkeypatch, zone):
    """Put the server on New York time, with a cabin in Denver."""
    zone("America/New_York")
    monkeypatch.setattr(config, "TIME_ZONE", "America/New_York")
    monkeypatch.setenv(
        "LOCATION_TZ", "The Cabin = America/Denver, Home=America/New_York"
    )
    monkeypatch.setattr(config, "LOCATION_TZ", config._location_zones())
    summaries = daily.Daily(locate=lambda: {"deck": "The Cabin", "porch": "Home"})
    summaries.scopes = list(daily.SCOPES)
    return summaries


def test_location_tz_parses_and_drops_the_default(two_places):
    # Home is the server's zone already, so only the cabin is listed.
    assert config.LOCATION_TZ == {"the-cabin": "America/Denver"}


@pytest.mark.parametrize(
    "value", ["Cabin", "Cabin=", "=America/Denver", "Cabin=Not/A_Zone"]
)
def test_location_tz_refuses_what_it_cannot_read(monkeypatch, value):
    monkeypatch.setenv("LOCATION_TZ", value)
    with pytest.raises(SystemExit):
        config._location_zones()


def test_each_summary_splits_days_in_its_own_zone(two_places):
    # 04:30 UTC on the 2nd: 00:30 on the 2nd in New York (EDT, before the
    # change), 22:30 on the 1st in Denver.
    stamp = "2026-10-02T04:30:00+00:00"
    grouped = two_places._group([motion("deck", stamp), motion("porch", stamp)])
    assert ("deck", "2026-10-01") in grouped
    assert ("_location-the-cabin", "2026-10-01") in grouped
    assert ("porch", "2026-10-02") in grouped
    assert ("_location-home", "2026-10-02") in grouped
    # Every camera together keeps the server's days.
    assert len(grouped[(daily.ALL, "2026-10-02")]) == 2


def test_the_model_sees_the_cameras_own_clock(two_places):
    record = motion("deck", "2026-10-02T04:30:00+00:00")
    where = daily.zone(two_places.zone_name("deck"))
    assert daily.lines_for([record], where=where) == ["22:30 a fox"]


def test_zones_and_groups_name_the_cabin(two_places):
    assert two_places.zones() == {"deck": "America/Denver"}
    zones = {group["key"]: group["time_zone"] for group in two_places.groups()}
    assert zones == {
        daily.ALL: None,
        "_location-home": None,
        "_location-the-cabin": "America/Denver",
    }


def test_nothing_pending_until_the_cameras_are_known(monkeypatch, two_places):
    monkeypatch.setattr(two_places, "_locate", dict)
    record = motion("deck", "2020-01-01T12:00:00+00:00")
    assert two_places.pending([record]) == []


async def test_each_zone_gets_its_own_night(monkeypatch, two_places):
    monkeypatch.setattr(config, "DAILY_HOUR", 0)
    asked = []

    class Stub:
        daily = two_places
        _daily_auto = None
        _daily_covered = {}

        def _camera_locations(self):
            return {"deck": "The Cabin"}

        async def _run_daily_auto(self, due):
            asked.append(due)

    stub = Stub()
    two_places.enabled = True
    monitor.Monitor._daily_tick(stub)
    await stub._daily_auto
    assert asked == [
        {
            None: daily.yesterday(),
            "America/Denver": daily.yesterday(daily.zone("America/Denver")),
        }
    ]

    # Once a zone's night is covered, the tick leaves it alone.
    stub._daily_covered = dict(asked[0])
    stub._daily_auto = None
    monitor.Monitor._daily_tick(stub)
    assert stub._daily_auto is None


def test_a_summary_from_another_zone_can_be_written_again(two_places):
    record = motion("deck", "2026-10-02T04:30:00+00:00")
    entry = {"summary": "quiet", "described": 1}
    # Written before LOCATION_TZ: on the server's days, so out of date now.
    two_places._entries[("deck", "2026-10-01")] = entry
    assert two_places.day([record], "deck", "2026-10-01")["stale"]
    entry["time_zone"] = "America/Denver"
    assert not two_places.day([record], "deck", "2026-10-01")["stale"]
    # A camera at home is unaffected by a summary that says no zone.
    two_places._entries[("porch", "2026-10-02")] = {"summary": "quiet", "described": 1}
    porch = motion("porch", "2026-10-02T04:30:00+00:00")
    assert not two_places.day([porch], "porch", "2026-10-02")["stale"]


def test_warns_once_about_a_location_blink_does_not_have(
    monkeypatch, two_places, caplog
):
    monkeypatch.setattr(config, "LOCATION_TZ", {"the-cabn": "America/Denver"})
    two_places.zones()
    two_places.zones()
    warned = [r for r in caplog.records if "LOCATION_TZ" in r.getMessage()]
    assert len(warned) == 1
    assert "the-cabn" in warned[0].getMessage()
