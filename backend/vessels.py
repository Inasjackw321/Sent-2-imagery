"""Ships, from AIS.

Every vessel over a certain size broadcasts its position, course and identity
continuously on VHF. Receivers ashore pick it up and some of them publish it.

Getting that feed for free and without an account is the hard part, and worth
being straight about: there is no open global AIS service. The one used here is
Digitraffic, run by the Finnish Transport Infrastructure Agency, which is
genuinely open -- no key, no signup, documented and stable -- and covers the
Baltic and the Gulf of Finland. Outside that box it returns nothing, and the
app says so rather than looking broken.

A global feed means an account somewhere. The front end can take an aisstream.io
key for that; this module is the part that needs nothing.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time

import requests

from . import config


class VesselLookupError(RuntimeError):
    pass


SOURCE = {
    "key": "digitraffic",
    "label": "Digitraffic (Finnish Transport Infrastructure Agency)",
    "locations": "https://meri.digitraffic.fi/api/ais/v1/locations",
    "vessels": "https://meri.digitraffic.fi/api/ais/v1/vessels",
    # Roughly what the network hears: the Baltic, the Gulf of Bothnia and the
    # Gulf of Finland. Checked before asking, so a map over the Pacific gets an
    # explanation instead of an empty answer.
    "bounds": (9.0, 53.0, 32.0, 66.5),
    "attribution": "Digitraffic / Fintraffic, CC BY 4.0",
}

# The feed updates every few seconds, but nothing is served from it faster than
# this: the map pans, and re-asking on every drag would be rude to a free
# service that asks for nothing in return.
CACHE_SECONDS = 20

# Vessel names change about never, so they are held far longer than positions
# and refreshed in the background rather than on the path of a request.
NAMES_SECONDS = 30 * 60

# AIS ship type is a two-digit code. The tens digit is the category, which is
# all anyone needs to tell a tanker from a tug on a map.
TYPES = {
    2: ("wig", "Wing-in-ground"),
    3: ("special", "Fishing, tug or dredger"),
    4: ("fast", "High-speed craft"),
    5: ("special", "Pilot, rescue or patrol"),
    6: ("passenger", "Passenger"),
    7: ("cargo", "Cargo"),
    8: ("tanker", "Tanker"),
    9: ("other", "Other"),
}

# What the navigational status field means. Under way is the ordinary case;
# the rest are worth showing because they explain a ship that is not moving.
NAV_STATUS = {
    0: "under way (engine)", 1: "at anchor", 2: "not under command",
    3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
    6: "aground", 7: "fishing", 8: "under way (sailing)",
    15: "undefined",
}

_lock = threading.Lock()
_positions: dict = {"at": 0.0, "data": None}
_names: dict = {"at": 0.0, "data": {}}

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT
# Digitraffic asks callers to identify themselves, and it is a reasonable
# thing to ask of a service that charges nothing.
_session.headers["Digitraffic-User"] = "EarthViewer/1.0"


def _covered(bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    w, s, e, n = SOURCE["bounds"]
    return not (east < w or west > e or north < s or south > n)


def _fetch(url: str) -> dict:
    try:
        resp = _session.get(url, timeout=25)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise VesselLookupError(f"{SOURCE['label']} could not be reached: {exc}") from exc
    return resp.json()


def _all_positions() -> dict:
    """Every position the service is publishing, cached briefly."""
    now = time.time()
    with _lock:
        if _positions["data"] is not None and now - _positions["at"] < CACHE_SECONDS:
            return _positions["data"]
    data = _fetch(SOURCE["locations"])
    with _lock:
        _positions.update(at=now, data=data)
    return data


def _all_names() -> dict[int, dict]:
    """Names and types by MMSI, cached for a long time."""
    now = time.time()
    with _lock:
        if _names["data"] and now - _names["at"] < NAMES_SECONDS:
            return _names["data"]
    try:
        rows = _fetch(SOURCE["vessels"])
    except VesselLookupError:
        # A ship with no name is still a ship. Losing the lookup should not
        # lose the positions with it.
        return _names["data"]
    table = {}
    for row in rows if isinstance(rows, list) else []:
        mmsi = row.get("mmsi")
        if mmsi is None:
            continue
        table[int(mmsi)] = {
            "name": (row.get("name") or "").strip() or None,
            "callsign": (row.get("callSign") or "").strip() or None,
            "type": row.get("shipType"),
            "destination": (row.get("destination") or "").strip() or None,
            "draught": row.get("draught"),
            "length": (row.get("referencePointA") or 0) + (row.get("referencePointB") or 0) or None,
        }
    with _lock:
        _names.update(at=now, data=table)
    return table


def _kind(ship_type) -> tuple[str, str]:
    """AIS ship type code to a category anyone can read off a map."""
    try:
        tens = int(ship_type) // 10
    except (TypeError, ValueError):
        return "other", "Unknown"
    return TYPES.get(tens, ("other", "Other"))


def _age(stamp) -> float | None:
    """Minutes since the position was broadcast."""
    try:
        return max(0.0, (time.time() * 1000 - float(stamp)) / 60000.0)
    except (TypeError, ValueError):
        return None


def vessels_in(bbox: tuple[float, float, float, float], limit: int = 900) -> dict:
    """Every vessel inside a rectangle, nearest the centre first."""
    west, south, east, north = bbox
    if not _covered(bbox):
        return {
            "vessels": [], "count": 0, "covered": False,
            "source": SOURCE["label"], "attribution": SOURCE["attribution"],
            "note": "This feed covers the Baltic and the Gulf of Finland. "
                    "There is no open global AIS service without an account.",
        }

    data = _all_positions()
    names = _all_names()

    centre_lon, centre_lat = (west + east) / 2, (south + north) / 2
    out = []
    for feature in data.get("features", []):
        lon, lat = (feature.get("geometry", {}).get("coordinates") or [None, None])[:2]
        if lon is None or not (west <= lon <= east and south <= lat <= north):
            continue
        props = feature.get("properties", {})
        mmsi = feature.get("mmsi") or props.get("mmsi")
        known = names.get(int(mmsi), {}) if mmsi is not None else {}
        category, type_label = _kind(known.get("type"))

        speed = props.get("sog")
        out.append({
            "mmsi": mmsi,
            "name": known.get("name"),
            "callsign": known.get("callsign"),
            "lon": lon, "lat": lat,
            # Speed over ground in knots; 102.3 is the "not available" value.
            "speed": None if speed is None or speed >= 102.0 else round(float(speed), 1),
            # Course over ground, and heading if the ship reports one. 511 is
            # the "no heading" value and must not be drawn as due north.
            "course": None if props.get("cog") is None or props["cog"] >= 360 else round(float(props["cog"]), 1),
            "heading": None if props.get("heading") is None or props["heading"] >= 511 else int(props["heading"]),
            "status": NAV_STATUS.get(props.get("navStat"), None),
            "category": category,
            "type": type_label,
            "destination": known.get("destination"),
            "draught": (known.get("draught") / 10.0) if known.get("draught") else None,
            "length": known.get("length"),
            "age_min": _age(props.get("timestampExternal") or props.get("timestamp")),
        })

    out.sort(key=lambda v: math.hypot(v["lon"] - centre_lon, v["lat"] - centre_lat))
    return {
        "vessels": out[:limit],
        "count": len(out),
        "covered": True,
        "source": SOURCE["label"],
        "attribution": SOURCE["attribution"],
        "fetched": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


# ── Synthetic vessels (DEMO_MODE) ──────────────────────────────


def demo_vessels(bbox: tuple[float, float, float, float], count: int = 40) -> dict:
    """Believable shipping for exploring offline.

    Laid along a couple of lanes rather than scattered, because scattered dots
    look nothing like traffic and would give a false impression of what the
    real layer shows.
    """
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    out = []
    for i in range(count):
        t = (i + 0.5) / count
        lane = i % 3
        lon = west + span_x * t
        lat = south + span_y * (0.25 + 0.22 * lane + 0.05 * math.sin(t * 9 + lane))
        category, label = list(TYPES.values())[i % len(TYPES)]
        heading = int((90 if lane % 2 == 0 else 270) + 12 * math.sin(t * 7))
        out.append({
            "mmsi": 200000000 + i * 7717,
            "name": f"DEMO {chr(65 + i % 26)}{i:02d}",
            "callsign": None,
            "lon": lon, "lat": lat,
            "speed": round(2 + 16 * abs(math.sin(t * 5 + lane)), 1),
            "course": float(heading % 360),
            "heading": heading % 360,
            "status": "under way (engine)",
            "category": category, "type": label,
            "destination": None, "draught": None, "length": None,
            "age_min": round(0.5 + 3 * t, 1),
        })
    return {
        "vessels": out, "count": len(out), "covered": True, "demo": True,
        "source": "Synthetic", "attribution": "Synthetic demo data",
        "fetched": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
