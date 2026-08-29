"""Weather at a point, and what it means for an optical satellite pass.

Open-Meteo is unreachable from here, so its answers are stubbed. The parts
worth testing are the ones this code decides rather than receives: which WMO
code becomes which description, how a missing field is handled, and the one
sentence that connects the weather to the rest of the app -- whether an
optical pass over this ground would show anything but cloud.
"""

from __future__ import annotations

import pytest
import requests

from backend import weather


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    weather._cache.clear()

    def refuse(*a, **kw):
        raise AssertionError("unstubbed network call")
    monkeypatch.setattr(weather._session, "get", refuse)
    yield
    weather._cache.clear()


def _answer(monkeypatch, payload, captured=None):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    def fake_get(url, params=None, timeout=None):
        if captured is not None:
            captured.append(params)
        return Reply()
    monkeypatch.setattr(weather._session, "get", fake_get)


def _payload(**now):
    current = {
        "temperature_2m": 12.0, "apparent_temperature": 10.5,
        "relative_humidity_2m": 70, "precipitation": 0.0,
        "cloud_cover": 20, "wind_speed_10m": 15.0, "wind_direction_10m": 180,
        "weather_code": 2, "is_day": 1, "visibility": 24000.0,
        "time": "2026-08-29T12:00",
    }
    current.update(now)
    return {
        "current": current,
        "daily": {
            "time": ["2026-08-29", "2026-08-30"],
            "weather_code": [3, 61],
            "temperature_2m_max": [18.0, 16.0],
            "temperature_2m_min": [9.0, 8.0],
            "precipitation_sum": [0.0, 4.2],
            "cloud_cover_mean": [80, 95],
        },
        "timezone": "Europe/Kyiv",
        "elevation": 150.0,
    }


def test_the_current_conditions_come_through(monkeypatch):
    _answer(monkeypatch, _payload())
    out = weather.at(30.5, 50.45)
    assert out["now"]["temperature"] == 12.0
    assert out["now"]["label"] == "Partly cloudy"
    assert out["now"]["cloud"] == 20


def test_visibility_is_converted_from_metres(monkeypatch):
    """Open-Meteo sends metres. Showing "24000 km" would be quite a view."""
    _answer(monkeypatch, _payload(visibility=24000.0))
    assert weather.at(0, 0)["now"]["visibility_km"] == 24.0


def test_a_missing_visibility_is_not_a_zero(monkeypatch):
    """Not every model publishes it, and 0 km means dense fog."""
    payload = _payload()
    del payload["current"]["visibility"]
    _answer(monkeypatch, payload)
    assert weather.at(0, 0)["now"]["visibility_km"] is None


def test_the_days_ahead_are_shaped_for_the_panel(monkeypatch):
    _answer(monkeypatch, _payload())
    days = weather.at(0, 0)["days"]
    assert len(days) == 2
    assert days[1]["label"] == "Light rain"
    assert days[1]["rain_mm"] == 4.2
    assert days[1]["cloud"] == 95


def test_a_day_with_fields_missing_does_not_break_the_row(monkeypatch):
    """A forecast can run out of one field before another, and a short list
    must not take the whole popup down."""
    payload = _payload()
    payload["daily"]["precipitation_sum"] = [0.0]
    _answer(monkeypatch, payload)
    days = weather.at(0, 0)["days"]
    assert days[1]["rain_mm"] is None
    assert days[1]["high"] == 16.0


@pytest.mark.parametrize("code, label", [
    (0, "Clear"), (3, "Overcast"), (45, "Fog"), (65, "Heavy rain"),
    (75, "Heavy snow"), (95, "Thunderstorm"),
])
def test_wmo_codes_become_words(code, label):
    assert weather._described(code)[0] == label


def test_an_unknown_code_is_not_a_crash():
    assert weather._described(1234)[0] == "Unknown"
    assert weather._described(None)[0] == "Unknown"


# ── The line that ties it to the satellites ────────────────────


@pytest.mark.parametrize("cloud, expect", [
    (0, "Clear enough"),
    (10, "Clear enough"),
    (30, "Broken cloud"),
    (70, "Mostly cloudy"),
    (95, "Overcast"),
])
def test_cloud_decides_whether_an_optical_pass_is_worth_waiting_for(cloud, expect):
    """Sentinel-2 and Landsat see reflected sunlight, so cloud is not a
    nuisance to correct for -- it is the end of the picture."""
    assert expect in weather._optical(cloud, daylight=True)


def test_darkness_is_reported_even_under_a_clear_sky():
    """A clear night is not a good optical pass; there is no light to reflect.
    Radar is the half of the answer that still works."""
    said = weather._optical(0, daylight=False)
    assert "Dark" in said
    assert "Sentinel-1" in said


def test_heavy_cloud_points_at_the_radar_instead():
    assert "Sentinel-1" in weather._optical(95, daylight=True)


def test_no_cloud_reading_is_not_reported_as_clear_sky():
    """Absent is not zero, and saying "clear" on the strength of a missing
    field is the kind of confident wrongness that wastes a pass."""
    assert weather._optical(None, daylight=True) == "No cloud reading here."


# ── Asking ─────────────────────────────────────────────────────


def test_two_clicks_in_the_same_place_ask_once(monkeypatch):
    """The map fires a lot of right-clicks in the same neighbourhood, and
    weather does not change between two of them."""
    asked = []
    _answer(monkeypatch, _payload(), captured=asked)
    weather.at(30.512, 50.451)
    weather.at(30.514, 50.449)     # a few hundred metres away
    assert len(asked) == 1


def test_somewhere_else_is_asked_for_separately(monkeypatch):
    asked = []
    _answer(monkeypatch, _payload(), captured=asked)
    weather.at(30.5, 50.4)
    weather.at(-3.2, 55.9)
    assert len(asked) == 2


def test_an_unreachable_service_says_so(monkeypatch):
    def boom(url, params=None, timeout=None):
        raise requests.ConnectionError("no route to host")
    monkeypatch.setattr(weather._session, "get", boom)
    with pytest.raises(weather.WeatherError, match="could not be reached"):
        weather.at(0, 0)


def test_a_reply_that_is_not_json_is_reported_as_such(monkeypatch):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(weather._session, "get", lambda *a, **kw: Reply())
    with pytest.raises(weather.WeatherError, match="unreadable"):
        weather.at(0, 0)


# ── Demo mode ──────────────────────────────────────────────────


def test_demo_weather_varies_with_where_you_ask():
    """One fixed forecast everywhere would make the layer look broken the
    moment somebody right-clicked twice."""
    a = weather.demo(30.5, 50.4)["now"]
    b = weather.demo(-120.0, 35.0)["now"]
    assert (a["cloud"], a["temperature"]) != (b["cloud"], b["temperature"])


def test_demo_weather_is_marked_as_synthetic():
    assert weather.demo(0, 0)["demo"] is True


def test_demo_weather_carries_the_same_shape_as_the_real_thing(monkeypatch):
    _answer(monkeypatch, _payload())
    real = weather.at(0, 0)
    fake = weather.demo(1, 1)
    assert set(fake["now"]) == set(real["now"])
    assert set(fake["days"][0]) == set(real["days"][0])
