"""Weather at a point, from Open-Meteo.

Keyless, no account, and generous enough about it that a desktop app asking
once per right-click is well inside what they offer for free.

It earns its place next to the satellite passes rather than as a layer of its
own: the question the pass popup answers is "when will there be a picture of
this", and the question this answers is "will there be anything to see" --
Sentinel-2 is optical, so eight-eighths of cloud means the pass happens and
the picture is white. Together they are one answer.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import requests

from . import config


class WeatherError(RuntimeError):
    pass


FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Weather moves, but not in seconds, and the map fires a lot of right-clicks
# in the same neighbourhood.
CACHE_SECONDS = 600

# How many days of the outlook to keep. Cloud cover more than about three days
# out is not worth planning a satellite pass against.
DAYS = 4

# WMO weather codes, which is what Open-Meteo speaks. Grouped rather than
# listed one by one: the distinction between "slight" and "moderate" drizzle
# is not one this app has any use for.
CODES = {
    0: ("Clear", "☀"),
    1: ("Mainly clear", "🌤"), 2: ("Partly cloudy", "⛅"), 3: ("Overcast", "☁"),
    45: ("Fog", "🌫"), 48: ("Freezing fog", "🌫"),
    51: ("Drizzle", "🌦"), 53: ("Drizzle", "🌦"), 55: ("Heavy drizzle", "🌦"),
    56: ("Freezing drizzle", "🌧"), 57: ("Freezing drizzle", "🌧"),
    61: ("Light rain", "🌦"), 63: ("Rain", "🌧"), 65: ("Heavy rain", "🌧"),
    66: ("Freezing rain", "🌧"), 67: ("Freezing rain", "🌧"),
    71: ("Light snow", "🌨"), 73: ("Snow", "🌨"), 75: ("Heavy snow", "❄"),
    77: ("Snow grains", "🌨"),
    80: ("Showers", "🌦"), 81: ("Showers", "🌧"), 82: ("Violent showers", "⛈"),
    85: ("Snow showers", "🌨"), 86: ("Snow showers", "🌨"),
    95: ("Thunderstorm", "⛈"), 96: ("Thunderstorm, hail", "⛈"),
    99: ("Thunderstorm, hail", "⛈"),
}

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

_lock = threading.Lock()
_cache: dict[tuple[float, float], tuple[float, dict]] = {}


def _described(code) -> tuple[str, str]:
    try:
        return CODES.get(int(code), ("Unknown", "•"))
    except (TypeError, ValueError):
        return ("Unknown", "•")


def at(lon: float, lat: float) -> dict:
    """Now, and the next few days, at one point."""
    # Rounded to about a kilometre. Two right-clicks a few pixels apart are the
    # same weather, and rounding is the difference between a cache that works
    # and a cache that never hits.
    key = (round(lon, 2), round(lat, 2))
    with _lock:
        held = _cache.get(key)
    if held and time.time() - held[0] < CACHE_SECONDS:
        return held[1]

    try:
        resp = _session.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lon,
            "current": ",".join([
                "temperature_2m", "apparent_temperature", "relative_humidity_2m",
                "precipitation", "cloud_cover", "wind_speed_10m",
                "wind_direction_10m", "weather_code", "is_day", "visibility",
            ]),
            "daily": ",".join([
                "weather_code", "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "cloud_cover_mean",
            ]),
            "forecast_days": DAYS,
            "timezone": "auto",
            "wind_speed_unit": "kmh",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise WeatherError(f"Open-Meteo could not be reached: {exc}") from exc
    except ValueError as exc:
        raise WeatherError("Open-Meteo sent something unreadable") from exc

    out = _shape(data)
    with _lock:
        _cache[key] = (time.time(), out)
    return out


def _shape(data: dict) -> dict:
    now = data.get("current", {}) or {}
    label, glyph = _described(now.get("weather_code"))

    daily = data.get("daily", {}) or {}
    days = []
    for i, date in enumerate(daily.get("time", [])[:DAYS]):
        code = (daily.get("weather_code") or [None] * DAYS)[i]
        day_label, day_glyph = _described(code)
        days.append({
            "date": date,
            "label": day_label, "glyph": day_glyph,
            "high": _pick(daily, "temperature_2m_max", i),
            "low": _pick(daily, "temperature_2m_min", i),
            "rain_mm": _pick(daily, "precipitation_sum", i),
            "cloud": _pick(daily, "cloud_cover_mean", i),
        })

    cloud = now.get("cloud_cover")
    return {
        "now": {
            "label": label, "glyph": glyph,
            "temperature": now.get("temperature_2m"),
            "feels_like": now.get("apparent_temperature"),
            "humidity": now.get("relative_humidity_2m"),
            "precipitation": now.get("precipitation"),
            "cloud": cloud,
            "wind": now.get("wind_speed_10m"),
            "wind_from": now.get("wind_direction_10m"),
            # Metres, and often absent. Divided here so the front end never has
            # to know which unit it arrived in.
            "visibility_km": (now.get("visibility") / 1000.0
                              if isinstance(now.get("visibility"), (int, float)) else None),
            "daylight": bool(now.get("is_day")),
            "at": now.get("time"),
        },
        # The one line that connects this to the rest of the app.
        "optical_outlook": _optical(cloud, bool(now.get("is_day"))),
        "days": days,
        "timezone": data.get("timezone"),
        "elevation_m": data.get("elevation"),
        "source": "Open-Meteo",
        "attribution": "Open-Meteo, CC BY 4.0",
    }


def _pick(block: dict, name: str, index: int):
    values = block.get(name) or []
    return values[index] if index < len(values) else None


def _optical(cloud, daylight: bool) -> str:
    """What this weather means for an optical satellite pass.

    Sentinel-2 and Landsat see reflected sunlight, so cloud is not a nuisance
    to be corrected for -- it is the end of the picture. Sentinel-1 is radar
    and does not care, which is the useful half of saying so.
    """
    if cloud is None:
        return "No cloud reading here."
    if not daylight:
        return "Dark now — optical passes only happen in daylight. Sentinel-1 radar works regardless."
    if cloud < 15:
        return "Clear enough for a good optical pass."
    if cloud < 50:
        return "Broken cloud — an optical pass would be partly obscured."
    if cloud < 85:
        return "Mostly cloudy — expect little usable optical imagery."
    return "Overcast — an optical pass would show cloud. Sentinel-1 radar sees through it."


# ── Synthetic weather (DEMO_MODE) ──────────────────────────────


def demo(lon: float, lat: float) -> dict:
    """Believable weather for exploring offline, varying with where you ask."""
    seed = (abs(lon) * 7.3 + abs(lat) * 11.7)
    cloud = int(seed * 13) % 101
    code = [0, 2, 3, 61, 80][int(seed) % 5]
    label, glyph = _described(code)
    today = dt.date.today()
    return {
        "now": {
            "label": label, "glyph": glyph,
            "temperature": round(22 - abs(lat) * 0.35, 1),
            "feels_like": round(21 - abs(lat) * 0.35, 1),
            "humidity": 40 + (int(seed * 3) % 55),
            "precipitation": 0.0,
            "cloud": cloud,
            "wind": round(4 + (seed % 22), 1),
            "wind_from": int(seed * 37) % 360,
            "visibility_km": 24.0,
            "daylight": True,
            "at": dt.datetime.now().isoformat(timespec="minutes"),
        },
        "optical_outlook": _optical(cloud, True),
        "days": [{
            "date": (today + dt.timedelta(days=i)).isoformat(),
            "label": label, "glyph": glyph,
            "high": round(24 - abs(lat) * 0.3 + i, 1),
            "low": round(14 - abs(lat) * 0.3 + i, 1),
            "rain_mm": round((seed + i) % 6, 1),
            "cloud": (cloud + i * 17) % 101,
        } for i in range(DAYS)],
        "timezone": "UTC",
        "elevation_m": 0,
        "demo": True,
        "source": "Synthetic",
        "attribution": "Synthetic demo data",
    }
