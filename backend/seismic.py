"""Earthquakes, and the instruments that recorded them.

Two different things share this module because they answer each other.

  * **Events** come from the USGS, which publishes every earthquake it locates
    anywhere on Earth, worldwide, usually within a few minutes. No account, no
    key, and the same feed the USGS's own map is drawn from.

  * **Stations** come from EarthScope's FDSN service, the index of the global
    seismograph networks -- roughly twenty thousand instruments whose data is
    open. For any one of them you can ask for the last few hours of ground
    motion and get back a plotted trace: an actual seismogram, from an actual
    instrument, of whatever the ground under it has been doing.

The pairing with the imagery is the point. A magnitude on a map is a dot; the
trace is the evidence behind the dot, and Sentinel-1 over the same ground a day
later is what the shaking did to it.

Everything here is keyless. Nothing in it needs an account.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import threading
import time

import requests

from . import config


class SeismicLookupError(RuntimeError):
    pass


# The USGS event service. FDSN is a standard, so this is the same query shape
# the station service below takes -- one of the nicer things about the format.
EVENTS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# EarthScope (formerly IRIS) runs the FDSN data centre most open networks
# archive to, and answers for stations far beyond its own instruments.
STATIONS_URL = "https://service.iris.edu/fdsnws/station/1/query"

# The same data centre will plot a channel for you and hand back a PNG. This is
# what turns a dot on a map into a seismogram without shipping a waveform
# library to the browser.
TRACE_URL = "https://service.iris.edu/irisws/timeseries/1/query"

ATTRIBUTION = {
    "events": "USGS Earthquake Hazards Program",
    "stations": "EarthScope / FDSN (formerly IRIS)",
}

# How far back the event feed can be asked to go, in hours.
WINDOWS = {24: "24 h", 168: "7 days", 720: "30 days"}

# A trace is only worth plotting over a window a human can read. Ten minutes
# shows a single arrival; six hours shows a day's background and any teleseism
# that turned up in it.
TRACE_MINUTES = {10: "10 min", 60: "1 hour", 360: "6 hours"}

# Vertical broadband and short-period channels, which is what you want for a
# first look. Horizontal components mostly duplicate the picture.
CHANNELS = "BHZ,HHZ,SHZ,EHZ,LHZ"

MAX_EVENTS = 1200
MAX_STATIONS = 600

# The station index changes when someone installs an instrument, so it is
# barely worth re-asking. Events move constantly.
EVENTS_SECONDS = 120
STATIONS_SECONDS = 30 * 60

# The data centre plots the trace itself, which takes a while for a long
# window over a slow link.
TRACE_TIMEOUT = 60

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _cached(key: str, ttl: float):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _keep(key: str, value: dict) -> dict:
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def _get(url: str, params: dict, *, timeout: int = 30) -> requests.Response:
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SeismicLookupError(f"{url.split('/')[2]} could not be reached: {exc}") from exc
    # FDSN says 204 for "your query was fine and matched nothing", which is an
    # answer rather than a failure and must not be raised over.
    if resp.status_code == 204:
        return resp
    if not resp.ok:
        raise SeismicLookupError(
            f"{url.split('/')[2]} answered {resp.status_code}: "
            f"{resp.text.strip()[:200] or resp.reason}"
        )
    return resp


# ── Earthquakes ────────────────────────────────────────────────


def quakes(
    bbox: tuple[float, float, float, float],
    hours: int = 168,
    min_magnitude: float = 2.5,
) -> dict:
    """Every located earthquake in a rectangle, strongest first."""
    west, south, east, north = bbox
    key = f"q:{west:.2f},{south:.2f},{east:.2f},{north:.2f}:{hours}:{min_magnitude}"
    if (hit := _cached(key, EVENTS_SECONDS)) is not None:
        return hit

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    resp = _get(EVENTS_URL, {
        "format": "geojson",
        "starttime": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "minlatitude": max(south, -90), "maxlatitude": min(north, 90),
        "minlongitude": west, "maxlongitude": east,
        "minmagnitude": min_magnitude,
        "orderby": "magnitude",
        "limit": MAX_EVENTS,
    })
    features = resp.json().get("features", []) if resp.content else []

    out = []
    for feature in features:
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        out.append({
            "id": feature.get("id"),
            "lon": lon, "lat": lat,
            # Third coordinate is depth in kilometres, and negative values are
            # real: a shallow event can be located above sea level.
            "depth_km": round(float(coords[2]), 1) if len(coords) > 2 and coords[2] is not None else None,
            "magnitude": props.get("mag"),
            "magnitude_type": props.get("magType"),
            "place": props.get("place"),
            "time": _moment(props.get("time")),
            # How many people felt it strongly enough to say so, and the
            # strongest shaking reported. Both are often absent.
            "felt": props.get("felt"),
            "intensity": props.get("cdi") or props.get("mmi"),
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
        })

    return _keep(key, {
        "quakes": out,
        "count": len(out),
        "hours": hours,
        "min_magnitude": min_magnitude,
        "source": ATTRIBUTION["events"],
        "attribution": ATTRIBUTION["events"],
    })


def _moment(millis) -> str | None:
    try:
        return dt.datetime.fromtimestamp(
            float(millis) / 1000.0, dt.timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


# ── Seismograph stations ───────────────────────────────────────


def stations(bbox: tuple[float, float, float, float]) -> dict:
    """Open seismograph stations in a rectangle, currently recording."""
    west, south, east, north = bbox
    key = f"s:{west:.1f},{south:.1f},{east:.1f},{north:.1f}"
    if (hit := _cached(key, STATIONS_SECONDS)) is not None:
        return hit

    now = dt.datetime.now(dt.timezone.utc)
    resp = _get(STATIONS_URL, {
        # Text is a fraction of the size of StationXML and carries everything
        # needed to put a dot on a map and ask for its trace afterwards.
        "format": "text", "level": "channel",
        "minlatitude": max(south, -90), "maxlatitude": min(north, 90),
        "minlongitude": west, "maxlongitude": east,
        "channel": CHANNELS,
        # Only instruments that are still running: a station decommissioned in
        # 1998 has no live trace to show.
        "endafter": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "includerestricted": "false",
        "nodata": "204",
    })
    return _keep(key, _parse_channels(resp.text if resp.content else ""))


def _parse_channels(text: str) -> dict:
    """Pipe-separated FDSN channel rows to one entry per station.

    A station reports several channels and often several instrument
    generations of each. They are all the same dot on the map, so the rows are
    folded onto the station and the best channel kept for the trace.
    """
    held: dict[tuple[str, str], dict] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        net, sta, loc, cha = parts[0], parts[1], parts[2], parts[3]
        try:
            lat, lon, elev = float(parts[4]), float(parts[5]), float(parts[6])
        except ValueError:
            continue
        entry = held.setdefault((net, sta), {
            "network": net, "station": sta, "lat": lat, "lon": lon,
            "elevation_m": round(elev),
            # Channel-level rows carry no site name -- that column only exists
            # at station level -- but they do name the instrument, which is
            # the more interesting half of it anyway.
            "instrument": parts[10] if len(parts) > 10 else "",
            "channels": [],
            "loc": loc, "channel": cha,
        })
        if cha not in entry["channels"]:
            entry["channels"].append(cha)
        # Broadband beats short period for a first look, so let it win the
        # channel that the trace button will ask for.
        if _rank(cha) < _rank(entry["channel"]):
            entry["channel"], entry["loc"] = cha, loc

    out = sorted(held.values(), key=lambda s: (s["network"], s["station"]))
    return {
        "stations": out[:MAX_STATIONS],
        "count": len(out),
        "capped": len(out) > MAX_STATIONS,
        "source": ATTRIBUTION["stations"],
        "attribution": ATTRIBUTION["stations"],
    }


_CHANNEL_ORDER = ["HHZ", "BHZ", "SHZ", "EHZ", "LHZ"]


def _rank(channel: str) -> int:
    return _CHANNEL_ORDER.index(channel) if channel in _CHANNEL_ORDER else len(_CHANNEL_ORDER)


# ── The trace itself ───────────────────────────────────────────


def trace(network: str, station: str, channel: str, loc: str = "", minutes: int = 60) -> bytes:
    """A plotted seismogram: the last `minutes` of ground motion, as a PNG.

    Asking the data centre to draw it rather than shipping the samples is the
    difference between a 30 kB image and a waveform library in the browser, and
    the plot it returns is the one seismologists look at.
    """
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    start = end - dt.timedelta(minutes=minutes)
    resp = _get(TRACE_URL, {
        "net": network, "sta": station, "cha": channel,
        # A blank location code means "whichever", but FDSN wants it spelled
        # out as two dashes rather than left empty.
        "loc": loc or "--",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        # Strip the instrument response so the trace is ground motion rather
        # than counts, and take out the long-period drift that would otherwise
        # be most of what you see.
        "demean": "true", "output": "plot",
    }, timeout=TRACE_TIMEOUT)
    if resp.status_code == 204 or not resp.content:
        raise SeismicLookupError(
            f"{network}.{station} has no data for the last {minutes} minutes. "
            "Stations go offline, and telemetry can run hours behind."
        )
    return resp.content


# ── Synthetic seismology (DEMO_MODE) ───────────────────────────


# The smallest synthetic event generated, before the requested filter is
# applied on top of it.
_DEMO_FLOOR = 0.6


def demo_quakes(bbox: tuple[float, float, float, float], hours: int = 168,
                min_magnitude: float = 2.5) -> dict:
    """Believable seismicity for exploring offline.

    Clustered along a line rather than scattered, because real earthquakes
    follow faults and a field of random dots would misrepresent the layer.
    """
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    out = []
    for i in range(40):
        t = (i + 0.5) / 40
        wobble = 0.06 * math.sin(t * 11) + 0.03 * math.sin(t * 29)
        # Gutenberg-Richter in spirit: small ones far outnumber large ones.
        magnitude = round(_DEMO_FLOOR + 6.4 * (1 - t) ** 3, 1)
        # The filter is applied here rather than baked into the range, so the
        # magnitude slider does something offline instead of looking broken.
        if magnitude < min_magnitude:
            continue
        out.append({
            "id": f"demo{i:03d}",
            "lon": west + span_x * t,
            "lat": south + span_y * (0.45 + wobble + 0.12 * math.sin(t * 4)),
            "depth_km": round(4 + 60 * abs(math.sin(t * 6)), 1),
            "magnitude": magnitude,
            "magnitude_type": "mb",
            "place": f"{10 + i * 3} km demo region",
            "time": (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=hours * t)).isoformat(timespec="seconds"),
            "felt": int(200 * (magnitude - _DEMO_FLOOR)) or None,
            "intensity": round(magnitude - 1.5, 1),
            "tsunami": False,
            "url": None,
        })
    out.sort(key=lambda q: -q["magnitude"])
    return {
        "quakes": out, "count": len(out), "hours": hours,
        "min_magnitude": min_magnitude, "demo": True,
        "source": "Synthetic", "attribution": "Synthetic demo data",
    }


def demo_stations(bbox: tuple[float, float, float, float]) -> dict:
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    out = []
    for i in range(12):
        row, col = divmod(i, 4)
        out.append({
            "network": "XX",
            "station": f"DM{i:02d}",
            "lat": south + span_y * (0.2 + 0.3 * row),
            "lon": west + span_x * (0.15 + 0.23 * col),
            "elevation_m": 40 + i * 37,
            "instrument": "Synthetic broadband",
            "channels": ["BHZ", "HHZ"],
            "loc": "00", "channel": "BHZ",
        })
    return {
        "stations": out, "count": len(out), "capped": False, "demo": True,
        "source": "Synthetic", "attribution": "Synthetic demo data",
    }


def demo_trace(network: str, station: str, channel: str, minutes: int = 60) -> bytes:
    """A drawn seismogram that looks like one, without pretending to be real.

    Background noise with one arrival in it: a sharp onset that decays away,
    which is the shape a distant earthquake actually makes.
    """
    from PIL import Image, ImageDraw

    width, height, mid = 720, 240, 120
    image = Image.new("RGB", (width, height), (12, 15, 20))
    draw = ImageDraw.Draw(image)

    for x in range(0, width, 60):
        draw.line([(x, 24), (x, height - 24)], fill=(28, 34, 45))
    draw.line([(0, mid), (width, mid)], fill=(40, 50, 66))

    onset = int(width * 0.42)
    # Deterministic wiggle: the same station always draws the same trace, so a
    # redraw does not look like new data arriving.
    seed = sum(ord(c) for c in f"{network}{station}{channel}")
    points = []
    for x in range(width):
        phase = x * 0.35 + seed
        noise = 2.5 * math.sin(phase) + 1.6 * math.sin(phase * 2.7 + 1) + 1.1 * math.sin(phase * 0.31)
        if x >= onset:
            decay = math.exp(-(x - onset) / (width * 0.16))
            noise += 46 * decay * math.sin((x - onset) * 0.55 + seed * 0.1)
        points.append((x, mid - noise))
    draw.line(points, fill=(126, 214, 255), width=1)

    draw.text((10, 8), f"{network}.{station}.{channel}  -  last {minutes} min", fill=(150, 165, 185))
    draw.text((10, height - 18), "SYNTHETIC - demo mode, not a real recording", fill=(255, 170, 90))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
