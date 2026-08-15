"""Active fire detections from NASA FIRMS.

FIRMS is NASA's Fire Information for Resource Management System: every few
hours it publishes every thermal anomaly the polar-orbiting satellites have
spotted, worldwide, within about three hours of the overpass. A detection is
not a fire boundary -- it is one satellite pixel, a few hundred metres across,
that came back hot. What it tells you is *where something was burning and
when*, which is exactly the question a satellite image of a fire raises.

Two ways in, and the app uses whichever is available:

  * The public archive files, which need no account at all. One global file per
    sensor per time window, republished continuously. Free, but the whole world
    arrives each time, so it is fetched once and held.
  * The area API, if a free MAP_KEY is in the environment. That returns only
    the rectangle asked for, so it is far smaller and fresher.

The pairing with the imagery is the point: Sentinel-2 shows the smoke, and
Sentinel-1 sees the ground through it, so the detections say which part of what
you are looking at was alight and at what hour.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import math
import os
import threading
from typing import Any

import requests

from . import config

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

FIRMS_ROOT = "https://firms.modaps.eosdis.nasa.gov"
MAP_KEY = os.environ.get("FIRMS_MAP_KEY", "").strip()

# The sensors worth asking. VIIRS resolves 375 m and catches much smaller fires
# than MODIS's 1 km, so it leads; MODIS is kept because its record is longer
# and it fills gaps between the VIIRS overpasses.
SENSORS = {
    "viirs-snpp": {
        "label": "VIIRS (Suomi-NPP)", "resolution_m": 375,
        "archive": "suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_Global_{window}.csv",
        "api": "VIIRS_SNPP_NRT",
    },
    "viirs-noaa20": {
        "label": "VIIRS (NOAA-20)", "resolution_m": 375,
        "archive": "noaa-20-viirs-c2/csv/J1_VIIRS_C2_Global_{window}.csv",
        "api": "VIIRS_NOAA20_NRT",
    },
    "modis": {
        "label": "MODIS (Terra/Aqua)", "resolution_m": 1000,
        "archive": "modis-c6.1/csv/MODIS_C6_1_Global_{window}.csv",
        "api": "MODIS_NRT",
    },
}

WINDOWS = {24: "24h", 48: "48h", 168: "7d"}

# The archive files cover the whole planet, so they are fetched once and held
# rather than pulled down per map pan. FIRMS republishes roughly every 15
# minutes and the files are large, so this is both kinder and quicker.
CACHE_SECONDS = 900
MAX_RETURNED = 4000

_cache: dict[tuple, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


class FireLookupError(RuntimeError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _window(hours: int) -> tuple[int, str]:
    """Which published file covers the hours asked for.

    FIRMS publishes three windows and the app may be asked for anything, so it
    takes the shortest file that *covers* the request and trims the result to
    the exact hours afterwards. Snapping to the nearest instead would quietly
    change the answer -- ask for 36 hours, be given 24 and never told that half
    a day of fires was dropped.
    """
    hours = max(1, int(hours))
    for published in sorted(WINDOWS):
        if hours <= published:
            return published, WINDOWS[published]
    longest = max(WINDOWS)
    return longest, WINDOWS[longest]


# ── Parsing ────────────────────────────────────────────────────


def _confidence(raw: str, sensor: str) -> tuple[float, str]:
    """One scale out of two.

    VIIRS reports low/nominal/high, MODIS a percentage. Both end up as a
    fraction plus the word, so the map can size and label a detection without
    knowing which satellite found it.
    """
    text = (raw or "").strip().lower()
    if text in ("l", "low"):
        return 0.25, "low"
    if text in ("n", "nominal"):
        return 0.65, "nominal"
    if text in ("h", "high"):
        return 0.95, "high"
    try:
        pct = float(text) / 100.0
    except ValueError:
        return 0.65, "nominal"
    return pct, "low" if pct < 0.3 else "nominal" if pct < 0.8 else "high"


def _acquired(date: str, time: str) -> str:
    """FIRMS splits the timestamp: '2024-08-15' plus '0742', both UTC."""
    stamp = (time or "0000").zfill(4)
    try:
        return f"{date}T{stamp[:2]}:{stamp[2:4]}:00Z"
    except (TypeError, IndexError):
        return f"{date}T00:00:00Z"


def _detection(row: dict, sensor: str) -> dict | None:
    try:
        lat = float(row["latitude"])
        lon = float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None

    level, label = _confidence(row.get("confidence", ""), sensor)
    # VIIRS names its channels bright_ti4/ti5, MODIS just brightness.
    brightness = row.get("bright_ti4") or row.get("brightness") or ""
    return {
        "lat": round(lat, 5),
        "lon": round(lon, 5),
        # Fire radiative power, in megawatts: how much energy the fire is
        # actually putting out, which is the closest thing to "how bad".
        "frp": _number(row.get("frp")),
        "brightness_k": _number(brightness),
        "confidence": round(level, 2),
        "confidence_label": label,
        "acquired": _acquired(row.get("acq_date", ""), row.get("acq_time", "")),
        "sensor": sensor,
        "resolution_m": SENSORS[sensor]["resolution_m"],
        "day": (row.get("daynight") or "").upper() == "D",
    }


def _number(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _parse(text: str, sensor: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [d for d in (_detection(row, sensor) for row in reader) if d]


# ── Fetching ───────────────────────────────────────────────────


def _fetch(url: str, what: str) -> str:
    try:
        resp = _session.get(url, timeout=120)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise FireLookupError(f"Could not reach NASA FIRMS for {what}: {exc}") from exc
    if resp.text.lstrip().lower().startswith("invalid"):
        raise FireLookupError(f"NASA FIRMS rejected the request for {what}")
    return resp.text


def _archive(sensor: str, window: str) -> list[dict]:
    """The global file for a sensor and window, held between requests."""
    key = (sensor, window)
    with _lock:
        hit = _cache.get(key)
        if hit and (_now().timestamp() - hit[0]) < CACHE_SECONDS:
            return hit[1]

    path = SENSORS[sensor]["archive"].format(window=window)
    rows = _parse(_fetch(f"{FIRMS_ROOT}/data/active_fire/{path}",
                         f"{sensor} {window}"), sensor)
    with _lock:
        _cache[(sensor, window)] = (_now().timestamp(), rows)
    return rows


def _area(sensor: str, bbox: tuple[float, float, float, float], days: int) -> list[dict]:
    """Just the rectangle, when a free MAP_KEY is available."""
    west, south, east, north = bbox
    url = (f"{FIRMS_ROOT}/api/area/csv/{MAP_KEY}/{SENSORS[sensor]['api']}/"
           f"{west:.4f},{south:.4f},{east:.4f},{north:.4f}/{days}")
    return _parse(_fetch(url, f"{sensor} area"), sensor)


# ── The question the app asks ──────────────────────────────────


def active_fires(bbox: tuple[float, float, float, float], hours: int = 24,
                 sensors=None, demo: bool | None = None) -> dict[str, Any]:
    """Every detection inside a rectangle in the last so many hours."""
    west, south, east, north = _sane_bbox(bbox)
    hours = max(1, min(int(hours), max(WINDOWS)))
    covering, window = _window(hours)
    wanted = [s for s in (sensors or SENSORS) if s in SENSORS]

    if demo if demo is not None else config.DEMO_MODE:
        found = _demo_fires((west, south, east, north), hours)
        keyed = True
    else:
        found = []
        keyed = bool(MAP_KEY)
        for sensor in wanted:
            rows = (_area(sensor, (west, south, east, north),
                          max(1, math.ceil(covering / 24)))
                    if keyed else _archive(sensor, window))
            found.extend(rows)

    cutoff = (_now() - dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    inside = [f for f in found
              if south <= f["lat"] <= north and west <= f["lon"] <= east
              and f["acquired"] >= cutoff]

    # A continent's worth of detections would bury the map and the browser, so
    # when there are too many the fiercest are kept -- losing the smallest
    # detections rather than an arbitrary corner of the map.
    total = len(inside)
    inside.sort(key=lambda f: (f["frp"] or 0.0), reverse=True)
    trimmed = inside[:MAX_RETURNED]
    trimmed.sort(key=lambda f: f["acquired"], reverse=True)

    return {
        "fires": trimmed,
        "count": len(trimmed),
        "total": total,
        "capped": total > len(trimmed),
        "hours": hours,
        "bbox": [west, south, east, north],
        "keyed": keyed,
        "attribution": "NASA FIRMS (LANCE / MODIS and VIIRS near real-time)",
        "sensors": {k: SENSORS[k]["label"] for k in wanted},
        "asked_at": _now().isoformat().replace("+00:00", "Z"),
    }


def _sane_bbox(bbox) -> tuple[float, float, float, float]:
    west, south, east, north = (float(v) for v in bbox)
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    return (max(west, -180.0), max(south, -90.0),
            min(east, 180.0), min(north, 90.0))


# ── Synthetic detections for demo mode ─────────────────────────


def _demo_fires(bbox, hours: int) -> list[dict]:
    """A believable scatter of fires, so the layer can be seen offline.

    Real detections come in clusters along a fire front rather than sprinkled
    evenly, so these do too: a few seats of fire, each with a tail of weaker
    detections downwind of it.
    """
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    now = _now()
    out: list[dict] = []

    for seat in range(4):
        angle = seat * 2.399
        cx = west + span_x * (0.5 + 0.32 * math.cos(angle * 1.7))
        cy = south + span_y * (0.5 + 0.32 * math.sin(angle * 2.3))
        for i in range(9):
            drift = i / 9.0
            out.append({
                "lat": round(cy + span_y * 0.06 * drift * math.sin(angle), 5),
                "lon": round(cx + span_x * 0.06 * drift * math.cos(angle), 5),
                "frp": round(4.0 + 90.0 * (1 - drift) * abs(math.cos(seat + i)), 2),
                "brightness_k": round(320 + 60 * (1 - drift), 2),
                "confidence": 0.95 if drift < 0.3 else 0.65 if drift < 0.7 else 0.25,
                "confidence_label": "high" if drift < 0.3 else "nominal" if drift < 0.7 else "low",
                "acquired": (now - dt.timedelta(hours=drift * hours * 0.9))
                            .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "sensor": "viirs-snpp",
                "resolution_m": 375,
                "day": i % 2 == 0,
                "demo": True,
            })
    return out
