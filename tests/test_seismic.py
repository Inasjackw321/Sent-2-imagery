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
from PIL import Image
import requests

from backend import seismic
from tests.test_miniseed import _record, _steim2_of


BOX = (-122.5, 36.5, -121.0, 38.0)


@pytest.fixture(autouse=True)
def _empty_cache():
    """Every test starts with nothing held, or they contaminate each other."""
    seismic._cache.clear()
    yield
    seismic._cache.clear()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Nothing reaches the network by accident.

    A trace now walks a list of data centres, so a test that stubs too few
    responses would otherwise fall through to a real one and hang.
    """
    def refuse(*a, **kw):
        raise AssertionError("unstubbed network call")
    monkeypatch.setattr(seismic._session, "get", refuse)
    monkeypatch.setattr(seismic._session, "post", refuse)


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
    """Answer every GET with one response, and record what was asked."""
    replies = response if isinstance(response, list) else None

    def fake_get(url, params=None, timeout=None, **kw):
        if captured is not None:
            captured.append((url, params))
        if replies is not None:
            return replies.pop(0) if replies else FakeResponse(status=204)
        return response
    monkeypatch.setattr(seismic._session, "get", fake_get)


def _by_url(monkeypatch, answer, captured=None):
    """Answer each GET according to the URL it went to."""
    def fake_get(url, params=None, timeout=None, **kw):
        if captured is not None:
            captured.append((url, params))
        return answer(url, params)
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


# One row, from a node that only this one knows about.
GREEK_ONLY = """\
HL|ATH|--|HHZ|37.9724|23.7176|110.0|0.0|0.0|-90.0|Guralp CMG-3ESP|1.0|1.0|M/S|100.0|2005-01-01T00:00:00|2599-12-31T23:59:59
"""

# A strong-motion station, which the old channel list would have dropped.
ACCELEROMETER = """\
IV|MILN|00|HNZ|45.4780|9.2300|130.0|0.0|0.0|-90.0|Kinemetrics EpiSensor|1.0|1.0|M/S**2|200.0|2012-01-01T00:00:00|2599-12-31T23:59:59
"""


def _url_of(name):
    return dict((n, u) for n, u in seismic.STATION_SERVICES)[name]


def test_every_index_is_asked_and_the_answers_merged(monkeypatch):
    """No single index knows about every instrument.

    A European node lists stations that never reach an American one, and the
    reverse is true too. Asking one and calling it the world is how a map of
    twenty thousand open seismographs shows four hundred.
    """
    def answer(url, params):
        if url == _url_of("NOA"):
            return FakeResponse(text=GREEK_ONLY)
        if url == _url_of("EarthScope"):
            return FakeResponse(text=CHANNELS)
        return FakeResponse(status=204)

    _by_url(monkeypatch, answer)
    out = seismic.stations(BOX)
    names = {s["station"] for s in out["stations"]}
    assert names == {"BKS", "CAD", "ATH"}
    assert "NOA" in out["services"] and "EarthScope" in out["services"]


def test_a_station_two_indexes_both_know_about_is_one_dot(monkeypatch):
    """Overlap between indexes is the normal case, not the exception."""
    _by_url(monkeypatch, lambda url, params: FakeResponse(text=CHANNELS))
    out = seismic.stations(BOX)
    assert out["count"] == 2


def test_one_index_failing_does_not_lose_the_others(monkeypatch):
    def answer(url, params):
        if url == _url_of("EarthScope"):
            raise requests.ConnectionError("down")
        return FakeResponse(text=GREEK_ONLY)

    _by_url(monkeypatch, answer)
    out = seismic.stations(BOX)
    assert out["count"] == 1
    assert "EarthScope" in out["missing"]


def test_every_index_failing_is_reported_rather_than_shown_as_empty_ground(monkeypatch):
    """An empty map because nothing answered looks exactly like an empty map
    because there is nothing there, and they mean opposite things."""
    def answer(url, params):
        raise requests.ConnectionError("down")

    _by_url(monkeypatch, answer)
    with pytest.raises(seismic.SeismicLookupError, match="No station index answered"):
        seismic.stations(BOX)


def test_the_legacy_host_is_only_asked_when_nothing_else_answered(monkeypatch):
    """It is the same index under an old name, so asking it alongside the one
    that replaced it is one request for nothing every time."""
    asked = []
    _by_url(monkeypatch, lambda url, params: FakeResponse(text=CHANNELS), captured=asked)
    seismic.stations(BOX)
    assert _url_of("EarthScope (legacy)") not in [u for u, _ in asked]


def test_the_legacy_host_is_asked_when_everything_else_is_silent(monkeypatch):
    def answer(url, params):
        if url == _url_of("EarthScope (legacy)"):
            return FakeResponse(text=CHANNELS)
        return FakeResponse(status=204)

    _by_url(monkeypatch, answer)
    assert seismic.stations(BOX)["count"] == 2


def test_accelerometers_are_asked_for_too(monkeypatch):
    """Most of the dense urban networks are strong-motion instruments. Asking
    only for seismometer channels drops every one of them."""
    seen = []
    _by_url(monkeypatch, lambda url, params: FakeResponse(status=204), captured=seen)
    # Every index answering 204 is "asked fine, nothing there", not a failure.
    assert seismic.stations(BOX)["count"] == 0
    channels = seen[0][1]["channel"].split(",")
    assert "HNZ" in channels and "ENZ" in channels
    assert "HHZ" in channels


def test_a_station_with_only_an_accelerometer_is_still_plotted(monkeypatch):
    _by_url(monkeypatch, lambda url, params: FakeResponse(text=ACCELEROMETER))
    out = seismic.stations(BOX)
    assert out["count"] == 1
    assert out["stations"][0]["channel"] == "HNZ"


def test_a_seismometer_beats_an_accelerometer_for_the_trace(monkeypatch):
    """An accelerometer is deaf to small distant events by design, which is
    what makes it useful in a city and useless as a first look."""
    _by_url(monkeypatch, lambda url, params: FakeResponse(
        text=CHANNELS + ACCELEROMETER.replace("IV|MILN", "BK|BKS")))
    bks = next(s for s in seismic.stations(BOX)["stations"] if s["station"] == "BKS")
    assert bks["channel"] == "HHZ"


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


def test_the_nearest_stations_are_the_ones_kept(monkeypatch):
    """A wide view holds more stations than can be drawn, so the list is cut.

    It used to be sorted by network name before cutting, which meant a wide
    view returned whichever networks sorted early in the alphabet and dropped
    everything else -- while the interface called it "nearest shown".
    """
    monkeypatch.setattr(seismic, "MAX_STATIONS", 1)
    _answer(monkeypatch, FakeResponse(text=CHANNELS))
    # A box centred on CAD, which sorts second alphabetically.
    out = seismic.stations((-122.5, 37.9, -122.4, 38.1))
    assert [s["station"] for s in out["stations"]] == ["CAD"]
    assert out["capped"] is True


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


# ── Fetching and drawing a trace ───────────────────────────────

# Real miniSEED, built by the decoder's own test helpers rather than described
# in prose: if the reader and this agree, the round trip is genuinely closed.
WAVEFORM = _record(_steim2_of([100, 104, 99, 103, 108, 101, 97, 102]), 8)

EARTHSCOPE = seismic.DATA_CENTRES[0][1]
ORFEUS = seismic.DATA_CENTRES[2][1]


def test_a_trace_is_fetched_decoded_and_drawn(monkeypatch):
    _answer(monkeypatch, FakeResponse(content=WAVEFORM))
    png = seismic.trace("BK", "BKS", "HHZ", loc="00", minutes=10)
    assert png.startswith(b"\x89PNG")
    assert Image.open(io.BytesIO(png)).size == seismic.PLOT_SIZE


def test_a_station_held_at_another_data_centre_is_still_plotted(monkeypatch):
    """The whole reason for walking the list.

    KO.DKL is Turkish. Its description is federated to EarthScope but its
    recordings have never been there, so asking one archive and stopping was
    always going to fail for most of the world.
    """
    asked = []

    def answer(url, params):
        return FakeResponse(content=WAVEFORM) if url == ORFEUS else FakeResponse(status=204)

    _by_url(monkeypatch, answer, captured=asked)
    assert seismic.trace("KO", "DKL", "HHZ", loc="", minutes=10).startswith(b"\x89PNG")
    assert asked[0][0] == EARTHSCOPE
    assert ORFEUS in [url for url, _ in asked]


def test_a_retired_endpoint_does_not_stop_the_search(monkeypatch):
    """The failure that started this: 410 Gone, with a page of HTML.

    One archive going away must cost the reader nothing as long as another
    holds the recording.
    """
    def answer(url, params):
        if url == EARTHSCOPE:
            return FakeResponse(status=410, text="<!doctype html><title>Service Unavailable</title>")
        return FakeResponse(content=WAVEFORM) if url == ORFEUS else FakeResponse(status=204)

    _by_url(monkeypatch, answer)
    assert seismic.trace("KO", "DKL", "HHZ", minutes=10).startswith(b"\x89PNG")


def test_when_nothing_has_it_the_reasons_are_reported_without_markup(monkeypatch):
    """An HTML error page pasted into the interface buries the one useful
    sentence in tags, which is exactly how it looked to the reader."""
    _answer(monkeypatch, FakeResponse(
        status=410, text="<!doctype html>\n<html><head><title>NGF: Service Unavailable</title>"))
    with pytest.raises(seismic.SeismicLookupError) as raised:
        seismic.trace("KO", "DKL", "HHZ", minutes=10)
    said = str(raised.value)
    assert "<" not in said and ">" not in said
    assert "NGF: Service Unavailable" in said
    assert "longer window" in said


def test_a_location_code_mismatch_is_retried_with_any(monkeypatch):
    """Metadata and archive disagree about location codes more often than they
    should, and a mismatch returns nothing rather than an explanation."""
    seen = []

    def answer(url, params):
        if url != EARTHSCOPE:
            return FakeResponse(status=204)
        return FakeResponse(content=WAVEFORM) if params["loc"] == "*" else FakeResponse(status=204)

    _by_url(monkeypatch, answer, captured=seen)
    assert seismic.trace("BK", "BKS", "HHZ", loc="00", minutes=10).startswith(b"\x89PNG")
    assert [p["loc"] for _, p in seen][:2] == ["00", "*"]


def test_a_trace_that_works_first_time_is_not_asked_for_twice(monkeypatch):
    seen = []
    _answer(monkeypatch, FakeResponse(content=WAVEFORM), captured=seen)
    seismic.trace("BK", "BKS", "HHZ", loc="00", minutes=10)
    assert len(seen) == 1


def test_undecodable_bytes_do_not_pass_for_a_recording(monkeypatch):
    """A data centre answering 200 with something that is not miniSEED must
    not become a picture of nothing."""
    _answer(monkeypatch, FakeResponse(content=b"<html>maintenance</html>"))
    with pytest.raises(seismic.SeismicLookupError):
        seismic.trace("BK", "BKS", "HHZ", minutes=10)


def test_the_window_ends_behind_now(monkeypatch):
    """Asking up to the present returns an empty plot from a healthy station:
    even good telemetry takes minutes to reach the archive."""
    import datetime as dt
    seen = []
    _answer(monkeypatch, FakeResponse(content=WAVEFORM), captured=seen)
    seismic.trace("BK", "BKS", "HHZ", minutes=60)
    _, params = seen[0]
    end = dt.datetime.strptime(params["endtime"], "%Y-%m-%dT%H:%M:%S")
    behind = (dt.datetime.utcnow() - end).total_seconds() / 60
    assert behind >= seismic.TRACE_LAG_MINUTES - 1


def test_the_plot_keeps_the_peaks(monkeypatch):
    """Down-sampling by taking every nth sample would drop the spikes, which
    are the whole point of a seismogram: an earthquake would draw as a quiet
    afternoon. Columns are drawn from the highest and lowest sample instead."""
    import numpy as np
    quiet = np.zeros(40000)
    quiet[19999] = 5000.0            # one spike, between two sampled positions
    png = seismic.plot({"samples": quiet, "rate": 100.0, "start": None,
                        "channel": "HHZ"}, "XX.TEST.HHZ", "test", 10)
    pixels = np.asarray(Image.open(io.BytesIO(png)).convert("L"), dtype=float)
    # The spike has to reach the top half of the plot area somewhere.
    assert pixels[30:60, :].max() > 100


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
    assert image.size == seismic.PLOT_SIZE


def test_the_demo_trace_is_the_same_every_time_for_one_station():
    """A redraw must not look like fresh data arriving."""
    a = seismic.demo_trace("XX", "DM01", "BHZ")
    b = seismic.demo_trace("XX", "DM01", "BHZ")
    assert a == b
    assert a != seismic.demo_trace("XX", "DM02", "BHZ")
