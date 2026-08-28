"""AIS: what the feed reaches, and what it does with what it hears."""

from __future__ import annotations

import pytest

from backend import vessels

BALTIC = (20.0, 59.0, 25.0, 61.0)
PACIFIC = (-160.0, 10.0, -150.0, 20.0)


def test_it_says_where_the_feed_reaches():
    """A blank map must be explained, not left looking broken.

    There is no open global AIS service, so most of the world has no coverage
    here. Asking outside the box answers immediately and says why, rather than
    troubling the service for an empty list.
    """
    out = vessels.vessels_in(PACIFIC)
    assert out["covered"] is False
    assert out["vessels"] == []
    assert "global" in out["note"].lower()


def test_the_baltic_is_covered():
    assert vessels._covered(BALTIC)
    assert not vessels._covered(PACIFIC)
    # A box straddling the edge still counts: part of it can be seen.
    assert vessels._covered((28.0, 60.0, 40.0, 66.0))


@pytest.mark.parametrize("code, expected", [
    (70, "cargo"), (79, "cargo"),
    (80, "tanker"), (89, "tanker"),
    (60, "passenger"), (30, "special"),
    (None, "other"), ("", "other"), (0, "other"), (99, "other"),
])
def test_ship_type_codes_become_something_readable(code, expected):
    category, label = vessels._kind(code)
    assert category == expected
    assert label


def test_the_offline_fleet_looks_like_traffic():
    """Synthetic ships are laid along lanes, not scattered.

    Scattered dots look nothing like shipping and would give a false
    impression of what the real layer shows.
    """
    out = vessels.demo_vessels(BALTIC, count=30)
    assert out["count"] == 30
    assert out["demo"] is True

    for ship in out["vessels"]:
        assert BALTIC[0] <= ship["lon"] <= BALTIC[2]
        assert BALTIC[1] <= ship["lat"] <= BALTIC[3]
        assert 0 <= ship["course"] < 360
        assert ship["category"] in {c for c, _ in vessels.TYPES.values()} | {"other"}
        assert ship["mmsi"]

    # Laid in lanes: far fewer distinct latitudes than ships.
    lanes = {round(s["lat"], 1) for s in out["vessels"]}
    assert len(lanes) < len(out["vessels"])


def test_a_ship_that_reports_no_heading_is_not_pointed_north():
    """511 is the AIS value for "no heading", and 360+ for no course.

    Drawing either as zero would point a whole harbour due north and look
    like a fact rather than a missing field.
    """
    feature = {
        "geometry": {"coordinates": [22.0, 60.0]},
        "mmsi": 123456789,
        "properties": {"sog": 102.3, "cog": 360.0, "heading": 511, "navStat": 5,
                       "timestampExternal": 0},
    }
    vessels._positions.update(at=9e18, data={"features": [feature]})
    vessels._names.update(at=9e18, data={})
    try:
        out = vessels.vessels_in(BALTIC)
    finally:
        vessels._positions.update(at=0.0, data=None)
        vessels._names.update(at=0.0, data={})

    ship = out["vessels"][0]
    assert ship["heading"] is None
    assert ship["course"] is None
    # 102.3 knots is the "speed not available" value, not a speed.
    assert ship["speed"] is None
    assert ship["status"] == "moored"
