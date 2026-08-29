"""Earthquakes and seismographs: what the services say, and what is made of it.

Nothing here touches the network. The two FDSN services return well-specified
shapes -- GeoJSON for events, pipe-separated text for channels -- so the useful
thing to test is the parsing and folding done on top of them, and the cases the
real services produce that are easy to get wrong: 204 for an empty match, a
station listed once per channel per instrument generation, a depth above sea
level, a magnitude that is absent altogether.
"""

from __future__ import annotations

import io

import pytest
import requests

from backend import seismic


BOX = (-122.5, 36.5, -121.0, 38.0)


@pytest.fixture(autouse=True)
def _empty_cache():
    """Every test starts with nothing held, or they contaminate each other."""
    seismic._cache.clear()
    yield
    seismic._cache.clear()


class FakeResponse:
    def __init__(self, *, status=200, payload=None, text="", content=b"x"):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = text
        self.content = content if status != 204 else b""
        self.reason = "OK"

    def json(self):
        return self._payload


def _answer(monkeypatch, response, captured=None):
    def fake_get(url, params=None, timeout=None):
        if captured is not None:
            captured.append((url, params))
        return response
    monkeypatch.setattr(seismic._session, "get", fake_get)


# ── Events ─────────────────────────────────────────────────────


def _feature(mag=4.2, depth=12.0, lon=-122.0, lat=37.5, **props):
    return {
        "id": "nc00001",
        "geometry": {"type": "Point", "coordinates": [lon, lat, depth]},
        "properties": {"mag": mag, "magType": "mw", "place": "12 km NE of Somewhere",
                       "time": 1700000000000, **props},
    }


def test_events_become_something_a_map_can_draw(monkeypatch):
    _answer(monkeypatch, FakeResponse(payload={"features": [_feature()]}))
    out = seismic.quakes(BOX)
    assert out["count"] == 1
    q = out["quakes"][0]
    assert q["magnitude"] == 4.2
    assert q["depth_km"] == 12.0
    assert q["lon"] == -122.0 and q["lat"] == 37.5
    assert q["time"].endswith("Z")


def test_a_quake_above_sea_level_keeps_its_negative_depth(monkeypatch):
    """Negative depth is real, not a glitch.

    A shallow event under high ground is located above the datum, and clamping
    it to zero would quietly invent a different earthquake.
    """
    _answer(monkeypatch, FakeResponse(payload={"features": [_feature(depth=-1.4)]}))
    assert seismic.quakes(BOX)["quakes"][0]["depth_km"] == -1.4


def test_a_quake_with_no_magnitude_survives(monkeypatch):
    """The USGS publishes events before the magnitude is settled."""
    _answer(monkeypatch, FakeResponse(payload={"features": [_feature(mag=None)]}))
    out = seismic.quakes(BOX)
    assert out["count"] == 1
    assert out["quakes"][0]["magnitude"] is None


def test_a_feature_with_no_position_is_dropped(monkeypatch):
    broken = _feature()
    broken["geometry"] = {"coordinates": []}
    _answer(monkeypatch, FakeResponse(payload={"features": [broken, _feature()]}))
    assert seismic.quakes(BOX)["count"] == 1


def test_no_matches_is_an_answer_not_a_failure(monkeypatch):
    """FDSN says 204 for a query that was fine and matched nothing.

    Treating that as an error would turn "the ground has been quiet" into "the
    service is down", which is the opposite of reassuring.
    """
    _answer(monkeypatch, FakeResponse(status=204))
    out = seismic.quakes(BOX)
    assert out["count"] == 0
    assert out["quakes"] == []


def test_a_real_failure_is_raised_with_what_the_service_said(monkeypatch):
    _answer(monkeypatch, FakeResponse(status=400, text="Bad Request: minmagnitude"))
    with pytest.raises(seismic.SeismicLookupError, match="minmagnitude"):
        seismic.quakes(BOX)


def test_the_service_is_asked_only_once_for_the_same_question(monkeypatch):
    seen = []
    _answer(monkeypatch, FakeResponse(payload={"features": [_feature()]}), captured=seen)
    seismic.quakes(BOX)
    seismic.quakes(BOX)
    assert len(seen) == 1
    # A different question is a different question.
    seismic.quakes(BOX, min_magnitude=5.0)
    assert len(seen) == 2


def test_a_stale_answer_is_not_served(monkeypatch):
    _answer(monkeypatch, FakeResponse(payload={"features": [_feature()]}))
    seismic.quakes(BOX)
    key = next(iter(seismic._cache))
    held, value = seismic._cache[key]
    seismic._cache[key] = (held - seismic.EVENTS_SECONDS - 1, value)
    assert seismic._cached(key, seismic.EVENTS_SECONDS) is None


def test_the_window_is_passed_through_as_a_start_time(monkeypatch):
    seen = []
    _answer(monkeypatch, FakeResponse(payload={"features": []}), captured=seen)
    seismic.quakes(BOX, hours=24, min_magnitude=1.5)
    _, params = seen[0]
    assert params["minmagnitude"] == 1.5
    assert params["maxlatitude"] == 38.0
    assert "starttime" in params


def test_an_unreachable_service_says_so(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(seismic._session, "get", boom)
    with pytest.raises(seismic.SeismicLookupError, match="could not be reached"):
        seismic.quakes(BOX)


# ── Stations ───────────────────────────────────────────────────

# The real service's text format: one row per channel per epoch.
CHANNELS = """\
#Network|Station|Location|Channel|Latitude|Longitude|Elevation|Depth|Azimuth|Dip|SensorDescription|Scale|ScaleFreq|ScaleUnits|SampleRate|StartTime|EndTime
BK|BKS|00|BHZ|37.8762|-122.2356|243.9|25.0|0.0|-90.0|Streckeisen STS-1|1.0|0.02|M/S|20.0|2000-01-01T00:00:00|2599-12-31T23:59:59
BK|BKS|00|HHZ|37.8762|-122.2356|243.9|25.0|0.0|-90.0|Streckeisen STS-1|1.0|0.02|M/S|100.0|2000-01-01T00:00:00|2599-12-31T23:59:59
BK|BKS|00|BHZ|37.8762|-122.2356|243.9|25.0|0.0|-90.0|Streckeisen STS-1|1.0|0.02|M/S|20.0|1990-01-01T00:00:00|1999-12-31T23:59:59
NC|CAD|--|EHZ|38.0430|-122.4720|180.0|0.0|0.0|-90.0|Mark Products L-4C|1.0|1.0|M/S|100.0|2010-01-01T00:00:00|2599-12-31T23:59:59
"""


def test_a_station_listed_many_times_is_still_one_dot(monkeypatch):
    """Four rows, two stations.

    The service lists every channel of every instrument generation. All of them
    sit at the same coordinates, so folding them onto the station is the
    difference between two markers and four stacked on top of each other.
    """
    _answer(monkeypatch, FakeResponse(text=CHANNELS))
    out = seismic.stations(BOX)
    assert out["count"] == 2
    assert [s["station"] for s in out["stations"]] == ["BKS", "CAD"]


def test_the_broadband_channel_wins_the_trace_button(monkeypatch):
    """BKS offers both BHZ and HHZ, and HHZ is the better first look.

    Whichever channel arrives first must not decide it, or the plot you get
    depends on the order the data centre happened to list its rows in.
    """
    _answer(monkeypatch, FakeResponse(text=CHANNELS))
    bks = seismic.stations(BOX)["stations"][0]
    assert bks["channel"] == "HHZ"
    assert set(bks["channels"]) == {"BHZ", "HHZ"}


def test_station_positions_and_elevation_are_read(monkeypatch):
    _answer(monkeypatch, FakeResponse(text=CHANNELS))
    bks = seismic.stations(BOX)["stations"][0]
    assert bks["lat"] == pytest.approx(37.8762)
    assert bks["lon"] == pytest.approx(-122.2356)
    assert bks["elevation_m"] == 244


def test_the_instrument_is_read_from_the_right_column(monkeypatch):
    """Counting back from the end of the row lands on the sample rate.

    Channel-level rows have no site-name column at all -- that one only exists
    at station level -- so the descriptive text has to come from the sensor
    description, by index from the front.
    """
    _answer(monkeypatch, FakeResponse(text=CHANNELS))
    bks = seismic.stations(BOX)["stations"][0]
    assert bks["instrument"] == "Streckeisen STS-1"


def test_a_malformed_row_does_not_lose_the_good_ones():
    out = seismic._parse_channels(
        CHANNELS + "GARBAGE\nXX|YY|--|BHZ|not-a-number|0|0|0|0|0|s|1|1|M/S|1|a|b\n"
    )
    assert out["count"] == 2


def test_an_empty_station_list_is_not_an_error(monkeypatch):
    _answer(monkeypatch, FakeResponse(status=204))
    out = seismic.stations(BOX)
    assert out["count"] == 0
    assert out["stations"] == []


def test_only_stations_still_recording_are_asked_for(monkeypatch):
    """A station decommissioned in 1998 has no live trace to plot."""
    seen = []
    _answer(monkeypatch, FakeResponse(text=CHANNELS), captured=seen)
    seismic.stations(BOX)
    _, params = seen[0]
    assert "endafter" in params
    assert params["includerestricted"] == "false"


# ── The trace ──────────────────────────────────────────────────


def test_a_blank_location_code_is_spelled_out(monkeypatch):
    """FDSN wants two dashes, not an empty string, for "any location"."""
    seen = []
    _answer(monkeypatch, FakeResponse(content=b"\x89PNG..."), captured=seen)
    seismic.trace("BK", "BKS", "HHZ", loc="")
    _, params = seen[0]
    assert params["loc"] == "--"
    assert params["output"] == "plot"


def test_a_station_with_nothing_to_give_says_so(monkeypatch):
    _answer(monkeypatch, FakeResponse(status=204))
    with pytest.raises(seismic.SeismicLookupError, match="no data"):
        seismic.trace("BK", "BKS", "HHZ", minutes=10)


def test_the_trace_comes_back_as_bytes(monkeypatch):
    _answer(monkeypatch, FakeResponse(content=b"\x89PNG\r\n\x1a\n"))
    assert seismic.trace("BK", "BKS", "HHZ").startswith(b"\x89PNG")


# ── Demo mode ──────────────────────────────────────────────────


def test_demo_quakes_land_inside_the_box_asked_for():
    out = seismic.demo_quakes(BOX)
    assert out["demo"] is True
    west, south, east, north = BOX
    for q in out["quakes"]:
        assert west <= q["lon"] <= east
        assert south <= q["lat"] <= north


def test_demo_quakes_have_more_small_ones_than_large():
    """Real seismicity is heavily skewed, and a flat spread would misrepresent
    what the layer shows once it is pointed at the world."""
    mags = [q["magnitude"] for q in seismic.demo_quakes(BOX)["quakes"]]
    small = sum(1 for m in mags if m < 4.0)
    large = sum(1 for m in mags if m >= 5.0)
    assert small > large


def test_the_demo_honours_the_magnitude_filter():
    """Offline, the slider is the only way to tell it is wired up at all.

    Generating the set from the threshold upwards would leave the count
    unchanged whatever the slider said, which reads as a broken control.
    """
    loose = seismic.demo_quakes(BOX, min_magnitude=1.0)["count"]
    tight = seismic.demo_quakes(BOX, min_magnitude=5.0)["count"]
    assert tight < loose
    assert all(q["magnitude"] >= 5.0 for q in seismic.demo_quakes(BOX, min_magnitude=5.0)["quakes"])


def test_demo_stations_carry_everything_the_trace_call_needs():
    for s in seismic.demo_stations(BOX)["stations"]:
        assert s["network"] and s["station"] and s["channel"]
        assert "lat" in s and "lon" in s


def test_the_demo_trace_is_a_real_png():
    from PIL import Image
    png = seismic.demo_trace("XX", "DM01", "BHZ", minutes=60)
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.size == (720, 240)


def test_the_demo_trace_is_the_same_every_time_for_one_station():
    """A redraw must not look like fresh data arriving."""
    a = seismic.demo_trace("XX", "DM01", "BHZ")
    b = seismic.demo_trace("XX", "DM01", "BHZ")
    assert a == b
    assert a != seismic.demo_trace("XX", "DM02", "BHZ")
