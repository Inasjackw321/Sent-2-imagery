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


# ── Radar interference ─────────────────────────────────────────


def _radar_scene(rng, h=260, w=260, floor=-22.0):
    """Calm water at the noise floor, with speckle, in decibels."""
    import numpy as np
    return (np.full((h, w), floor, dtype="float32")
            + rng.normal(0, 1.6, (h, w)).astype("float32"))


def _add_streak(scene, angle_deg, width=2.0, boost=9.0):
    import numpy as np
    h, w = scene.shape
    yy, xx = np.mgrid[0:h, 0:w]
    t = np.deg2rad(angle_deg)
    across = np.abs((xx - w / 2) * np.sin(t) - (yy - h / 2) * np.cos(t))
    return scene + boost * np.exp(-(across / width) ** 2)


def _score(image):
    import numpy as np

    from backend.composite import interference
    masked = np.ma.masked_array(image, mask=np.zeros_like(image, dtype=bool))
    return float(interference(masked).max())


@pytest.mark.parametrize("angle", [0, 30, 45, 70, 90, 135])
def test_interference_is_found_at_any_angle(angle):
    """Interference crosses the swath at whatever angle the pass was flown."""
    import numpy as np
    rng = np.random.default_rng(11)
    assert _score(_add_streak(_radar_scene(rng), angle)) > 4.0


def test_bright_things_that_are_not_streaks_are_not_reported():
    """The hard part is what it refuses.

    A ship is a bright pixel; so is a city; so is a coastline, which is even
    beautifully straight. None of them is a radar transmitting at the
    satellite, and an earlier version of this reported the bright field as
    though it were -- oriented is not the same as being a ridge.
    """
    import numpy as np
    rng = np.random.default_rng(11)
    water = _radar_scene(rng)
    h, w = water.shape
    yy, xx = np.mgrid[0:h, 0:w]

    ship = water + 9.0 * np.exp(-(np.hypot(xx - w / 2, yy - h / 2) / 6.0) ** 2)
    town = water.copy()
    town[h // 4:3 * h // 4, w // 4:3 * w // 4] += 9.0
    coast = water.copy()
    coast[:, w // 2:] += 9.0

    streak = _score(_add_streak(water, 35))
    for label, image in (("plain water", water), ("a ship", ship),
                         ("a town", town), ("a coastline", coast)):
        assert _score(image) < streak, f"{label} scored as high as a streak"

    # Not merely lower than a streak: nowhere near one. The gap is what makes
    # the number readable as a quantity instead of needing a threshold tuned
    # per scene.
    assert streak > 3.0
    for label, image in (("plain water", water), ("a ship", ship),
                         ("a town", town), ("a coastline", coast)):
        assert _score(image) < streak / 3.0, f"{label} is too close to a streak"


def test_the_interference_index_is_radar_only():
    from backend import config
    assert config.INDICES["rfi"]["sat"] == ["sentinel-1"]
    assert config.INDICES["rfi"]["bands"] == ["vh", "vv"]


def test_the_global_feed_will_not_be_asked_more_than_once_in_five_minutes():
    """The floor is the backend's, not the page's.

    A rule the front end keeps is a rule a reload can break, so this one lives
    where the request is actually made.
    """
    import time

    from backend import aisstream

    assert aisstream.MIN_INTERVAL_SECONDS == 300

    aisstream.set_key("pretend-key")
    try:
        # Pretend a collection just happened.
        aisstream._cache.update(at=time.time(), box=(0, 0, 1, 1),
                                data={"vessels": [], "count": 0, "covered": True})
        out = aisstream.vessels_in((0, 0, 1, 1))
        assert out["cached"] is True
        assert 0 < out["next_in"] <= 300
        assert aisstream.seconds_until_next() > 290
    finally:
        aisstream._cache.update(at=0.0, box=None, data=None)
        aisstream.set_key(None)


def test_no_key_means_a_clear_refusal_rather_than_an_empty_map():
    from backend import aisstream

    aisstream.set_key(None)
    with pytest.raises(aisstream.StreamError, match="API key"):
        aisstream.vessels_in((0, 0, 1, 1))


def test_changing_the_key_discards_what_the_old_one_collected():
    import time

    from backend import aisstream

    aisstream.set_key("one")
    aisstream._cache.update(at=time.time(), box=(0, 0, 1, 1), data={"vessels": []})
    aisstream.set_key("two")
    try:
        assert aisstream._cache["data"] is None
        assert aisstream.seconds_until_next() == 0
    finally:
        aisstream.set_key(None)


def test_a_later_position_report_replaces_an_earlier_one():
    """Within one listening window a ship reports several times.

    Merging the new report *under* what was already known -- which is how this
    was first written -- freezes every vessel at the first position it sent
    and silently ignores the rest of the window. The ships would be real and
    in the wrong place, which is worse than no ships at all.
    """
    import datetime as dt

    from backend import aisstream

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + " +0000 UTC"

    def report(lat, lon, speed):
        return {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": 123, "ShipName": "ALPHA", "time_utc": now},
            "Message": {"PositionReport": {
                "Latitude": lat, "Longitude": lon, "Sog": speed,
                "Cog": 90.0, "TrueHeading": 91}},
        }

    seen, static = {}, {}
    aisstream._absorb(report(60.0, 22.0, 5.0), seen, static)
    aisstream._absorb(report(60.5, 22.5, 9.0), seen, static)

    assert seen[123]["lat"] == 60.5
    assert seen[123]["lon"] == 22.5
    assert seen[123]["speed"] == 9.0
    assert seen[123]["age_min"] is not None


def test_static_data_names_a_ship_without_moving_it():
    """Names and types arrive on their own message type, usually later."""
    from backend import aisstream

    seen = {123: {"mmsi": 123, "lat": 60.5, "lon": 22.5, "name": None,
                  "category": "other", "type": "Unknown"}}
    static = {}
    aisstream._absorb({
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 123},
        "Message": {"ShipStaticData": {
            "Name": "ALPHA MARU", "Type": 80, "CallSign": "AB12",
            "Destination": "HELSINKI", "Dimension": {"A": 100, "B": 50}}},
    }, seen, static)

    for mmsi, extra in static.items():
        seen[mmsi].update({k: v for k, v in extra.items() if v is not None})

    assert seen[123]["name"] == "ALPHA MARU"
    assert seen[123]["category"] == "tanker"
    assert seen[123]["length"] == 150
    assert seen[123]["lat"] == 60.5, "naming a ship must not move it"


def test_the_interference_view_is_a_picture_of_the_whole_swath():
    """The detector answers "how much"; this answers "show me".

    VV to red and blue, VH to green: ordinary ground has almost no
    cross-polarised return and comes out violet, and interference lifts the
    green until the band blazes. The green window has to sit above what
    vegetation returns or every field saturates and the streaks vanish into a
    uniformly green scene -- which is what a first attempt at 0.045 did.
    """
    from backend import config

    preset = config.COMPOSITES["radar_interference"]
    assert preset["sat"] == ["sentinel-1"]
    assert preset["bands"] == ["vv", "vh", "vv"]
    assert preset["from_db"] is True

    red, green, blue = preset["windows"]
    # Forest returns about -13 dB in VH, which is 0.045 in power.
    assert green[1] > 0.045 * 2, "green saturates on vegetation"
    # And interference, nearer -10 dB, must reach the top of it.
    assert green[1] <= 10 ** (-8.0 / 10.0)
    assert red[1] > green[1] and blue[1] > green[1], "the base would not read violet"


def _terrain(seed=0, n=420, with_streak=False):
    """A radar scene with the things real land actually has in it.

    Water alone was too easy a test, and passing it is what let a detector
    ship that reported every hedgerow. Fields have boundaries, roads and
    railways run dead straight for miles and bounce brilliantly, towns are
    bright and busy -- and all of those are the bright *lines* that a streak
    detector has to not report.
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n]

    vv = np.full((n, n), -13.0, dtype="float32")
    vh = np.full((n, n), -20.0, dtype="float32")

    river = np.abs(yy - (n * 0.62 + 26 * np.sin(xx / 48.0))) < 4
    vv[river], vh[river] = -22.0, -29.0

    for k in range(7):                       # hedges and fences, both channels
        c = int(n * (k + 1) / 8.0)
        vv[:, c - 1:c + 1] += 6.0
        vh[:, c - 1:c + 1] += 6.0
        vv[c - 1:c + 1, :] += 5.0
        vh[c - 1:c + 1, :] += 5.0

    for off, boost in ((-40, 8.0), (70, 6.0)):   # railway and road
        line = np.abs(xx - yy + off) < 2
        vv[line] += boost
        vh[line] += boost

    town = (np.abs(xx - n * 0.28) < n * 0.10) & (np.abs(yy - n * 0.30) < n * 0.09)
    vv[town] += 11.0
    vh[town] += 10.0

    vv = vv + rng.normal(0, 1.5, (n, n)).astype("float32")
    vh = vh + rng.normal(0, 1.5, (n, n)).astype("float32")

    if with_streak:
        # Interference lifts VH far more than VV: cross-polarised return is
        # about ten decibels weaker to begin with, so the same injected power
        # stands much prouder of it.
        t = np.deg2rad(28.0)
        across = (xx - n / 2) * np.sin(t) - (yy - n / 2) * np.cos(t)
        bands = np.zeros((n, n), dtype="float32")
        for k in range(-3, 4):
            bands += np.exp(-((across - k * 62) / 2.2) ** 2).astype("float32")
        vh = vh + bands * 11.0
        vv = vv + bands * 4.0

    def masked(a):
        import numpy as np
        return np.ma.masked_array(a, mask=np.zeros_like(a, dtype=bool))

    return masked(vv), masked(vh)


@pytest.mark.parametrize("seed", [0, 1, 2, 3])
def test_ordinary_land_does_not_read_as_interference(seed):
    """The test that matters, and the one that was missing.

    Water plus a streak is easy and everything passes it. On land, before VV
    was brought in, hedgerows and a railway scored 7.0 dB against streaks at
    9.2 -- no separation worth the name, and the layer reported field
    boundaries with total confidence.
    """
    from backend.composite import interference

    vv, vh = _terrain(seed, with_streak=False)
    quiet = float(interference(vh, vv).max())

    vv, vh = _terrain(seed, with_streak=True)
    loud = float(interference(vh, vv).max())

    assert loud > 4.0, "interference over land must still be found"
    assert quiet < 1.5, f"terrain alone scored {quiet:.2f} dB"
    assert loud > quiet * 4, "the two must not be close"


def test_the_co_polarised_channel_is_what_rejects_ground_features():
    """A hedge brightens both polarisations; interference brightens one.

    Handing the same scene over without VV should measurably lose that, which
    is what makes this worth asserting rather than assuming.
    """
    from backend.composite import interference

    vv, vh = _terrain(0, with_streak=False)
    with_vv = float(interference(vh, vv).max())
    without = float(interference(vh).max())
    assert without > with_vv * 3, "VV is doing the work it is supposed to do"
