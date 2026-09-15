"""NOTAMs -- the notices that say which airspace is shut, and when.

A NOTAM is how a state tells pilots that something about its airspace has
changed: a corridor closed for firing practice, a whole FIR shut to civil
traffic, a navaid out of service, a drone operation at a named point. They are
the formal record of where aircraft may not go, published by the state that
owns the airspace, and they sit naturally beside this app's other layer: one
shows what somebody said is flying, the other shows what the authorities have
closed.

Where they come from
--------------------

The FAA's NOTAM API, which serves ICAO NOTAMs worldwide rather than only
American ones, and is the only free source of the whole set that does not
require a device-code OAuth dance. It wants a client id and secret, both free
to obtain, and both read from the environment and held in memory -- never
written anywhere, the same rule the rest of this app's keys follow.

With no key the layer says so and draws nothing. That is deliberate: a NOTAM
layer that quietly shows an empty map is indistinguishable from a sky with
nothing closed in it, and those are very different facts.

What this module is careful about
---------------------------------

  The coordinates. A NOTAM's Q-line carries its position as a compact
  "4915N02330E" -- degrees and minutes, no separator, hemisphere as a letter
  -- and its radius in NAUTICAL MILES. Reading those as decimal degrees and
  kilometres puts a closure in the wrong country at a fifth of its size. Both
  forms are parsed here and both are tested against the spelling in the
  documentation.

  The times. A NOTAM is only true between its start and end, and "PERM" is a
  real end value meaning permanent. One that has expired is not drawn.

  Saying nothing rather than guessing. A record whose position cannot be read
  is counted and listed, not placed -- the same rule the air tracker follows
  for a report it cannot geocode.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import re
import threading
import time
from typing import Any

import requests

from . import config

BASE = "https://external-api.faa.gov/notamapi/v1/notams"

CLIENT_ID = os.environ.get("FAA_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("FAA_CLIENT_SECRET", "").strip()

# One nautical mile. The Q-line radius is in these, and reading it as
# kilometres shrinks every closure to a bit over half its size.
NM_KM = 1.852

# Their page size, and how many pages are worth walking. A busy FIR runs to
# hundreds of notices and nobody reads the four hundredth; this is enough to
# cover a country's active set without turning a map pan into a minute of
# paging.
PER_PAGE = 250
MOST_PAGES = 4

TIMEOUT = 20.0

# How long an answer is kept. NOTAMs are issued hours ahead and change slowly,
# so re-asking on every pan would spend somebody's quota to be told the same
# thing.
KEEP_SECONDS = 600

# The radius a point NOTAM with no stated one is drawn at. Their own default
# for a Q-line with no radius is 5 NM, which is what this is.
DEFAULT_NM = 5.0

# And a ceiling, because a NOTAM covering a whole FIR carries a radius of
# hundreds of miles and drawing it as a circle would cover half of Europe in
# one wash. Past this it is listed and marked as area-wide rather than drawn
# as a disc pretending to be a boundary.
MOST_KM = 400.0


class NotamError(RuntimeError):
    pass


_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


def configured() -> bool:
    """Whether there is a key to ask with."""
    return bool(CLIENT_ID and CLIENT_SECRET)


# "4915N02330E" and "491530N0233045E": degrees, minutes, optionally seconds,
# with the hemisphere as a trailing letter and no separators anywhere.
QCOORD = re.compile(
    r"^(\d{2})(\d{2})(\d{2})?([NS])(\d{3})(\d{2})(\d{2})?([EW])$", re.I)


def read_coord(raw: Any) -> tuple[float, float] | None:
    """A Q-line coordinate as decimal degrees, or None.

    "4915N02330E" is 49 degrees 15 minutes north, 23 degrees 30 minutes east.
    Read as a number it is forty-nine million, and read as decimal degrees it
    is 4915 -- either way the mark leaves the planet, so this refuses anything
    that is not exactly the documented shape.
    """
    text = " ".join(str(raw or "").split()).replace(" ", "").upper()
    hit = QCOORD.match(text)
    if not hit:
        return None
    lat = int(hit.group(1)) + int(hit.group(2)) / 60 + int(hit.group(3) or 0) / 3600
    lon = int(hit.group(5)) + int(hit.group(6)) / 60 + int(hit.group(7) or 0) / 3600
    if hit.group(4) == "S":
        lat = -lat
    if hit.group(8) == "W":
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return round(lat, 5), round(lon, 5)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _text(value: Any, limit: int = 2000) -> str | None:
    out = " ".join(str(value or "").split())[:limit]
    return out or None


def _moment(value: Any) -> float | None:
    """A NOTAM time as epoch seconds, or None.

    None covers two different things and they want the same answer here.
    "PERM" and "UFN" are real end values meaning the notice does not expire,
    and in_force() reads no-end as "still true" rather than as "ended long
    ago". Nonsense is also None, because a time this cannot read is a time it
    must not act on.

    They were listed explicitly for a while. That list did nothing: "PERM" is
    not a timestamp, so it comes back None from the parse below anyway, and a
    branch nothing can reach is one nobody would notice breaking.
    """
    text = _text(value, 40)
    if not text:
        return None
    try:
        when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when.timestamp()


def read_notam(raw: Any) -> dict[str, Any] | None:
    """One of their records, as something this map can draw.

    Their shape, from the documentation: each item carries
    properties.coreNOTAMData.notam, and the fields worth having are on it.
    """
    if not isinstance(raw, dict):
        return None
    props = raw.get("properties")
    props = props if isinstance(props, dict) else {}
    core = props.get("coreNOTAMData")
    core = core if isinstance(core, dict) else {}
    notam = core.get("notam")
    notam = notam if isinstance(notam, dict) else raw
    if not isinstance(notam, dict):
        return None

    ident = _text(notam.get("number") or notam.get("id"), 40)
    body = _text(notam.get("text") or notam.get("icaoMessage"), 2000)
    if not ident or not body:
        return None

    # Position, from whichever of the two forms is there. The decimal pair is
    # preferred when present because it needs no interpretation; the Q-line
    # string is the fallback and is what most records actually carry.
    lat = _number(notam.get("latitude"))
    lon = _number(notam.get("longitude"))
    if lat is None or lon is None:
        spot = read_coord(notam.get("coordinates"))
        if spot:
            lat, lon = spot
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None

    # The radius, in nautical miles on the wire.
    nm = _number(notam.get("radius"))
    km = (nm if nm and nm > 0 else DEFAULT_NM) * NM_KM

    return {
        "id": ident,
        "location": _text(notam.get("location") or notam.get("icaoLocation"), 12),
        "text": body,
        "classification": _text(notam.get("classification"), 20),
        "kind": _text(notam.get("type"), 20),
        "lat": lat,
        "lon": lon,
        "radius_km": round(km, 2),
        # Bigger than a circle can honestly say. Listed rather than drawn as a
        # disc; a FIR-wide closure is a boundary, not a compass circle.
        "wide": km > MOST_KM,
        "from": _moment(notam.get("effectiveStart")),
        "to": _moment(notam.get("effectiveEnd")),
        "placed": lat is not None,
        "why_unplaced": None if lat is not None else
                        "the notice carries no position this can read",
    }


def in_force(notam: dict[str, Any], now: float) -> bool:
    """Whether this notice applies at this moment.

    A NOTAM issued for next Tuesday is real and is not a closure now, and one
    that ended this morning is neither. "PERM" arrives as no end at all, which
    is not the same as an end in the past.
    """
    began = notam.get("from")
    ends = notam.get("to")
    if began is not None and began > now:
        return False
    return not (ends is not None and ends < now)


def _ask(params: dict[str, Any]) -> dict[str, Any]:
    if not configured():
        raise NotamError("no FAA key is set, so NOTAMs cannot be fetched")
    try:
        resp = _session.get(BASE, params=params, timeout=TIMEOUT, headers={
            "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "Accept": "application/json",
        })
    except requests.RequestException as exc:
        raise NotamError(f"the NOTAM service could not be reached: {exc}") from exc
    if resp.status_code in (401, 403):
        raise NotamError("the NOTAM service refused the key")
    if resp.status_code == 429:
        raise NotamError("the NOTAM service is rate limiting")
    if not resp.ok:
        raise NotamError(f"the NOTAM service answered {resp.status_code}")
    try:
        got = resp.json()
    except ValueError as exc:
        raise NotamError("the NOTAM service answered something that is not JSON") from exc
    return got if isinstance(got, dict) else {}


def around(lat: float, lon: float, radius_km: float) -> dict[str, Any]:
    """Every notice in force within a radius of a point.

    A circle rather than the map's rectangle, because that is the query their
    API takes. The caller passes the circle that covers what is on screen,
    which is a little more than the screen and never less.
    """
    radius_nm = max(1.0, min(500.0, radius_km / NM_KM))
    key = f"{round(lat, 2)}/{round(lon, 2)}/{round(radius_nm)}"
    now = time.time()
    with _lock:
        held = _cache.get(key)
        if held and now - held[0] < KEEP_SECONDS:
            return held[1]

    found: list[dict[str, Any]] = []
    total = 0
    for page in range(1, MOST_PAGES + 1):
        got = _ask({
            "locationLongitude": round(lon, 4),
            "locationLatitude": round(lat, 4),
            "locationRadius": round(radius_nm),
            "pageSize": PER_PAGE,
            "pageNum": page,
        })
        items = got.get("items")
        if not isinstance(items, list):
            break
        for item in items:
            one = read_notam(item)
            if one:
                found.append(one)
        total = int(_number(got.get("totalCount")) or 0) or total
        if len(items) < PER_PAGE:
            break

    live = [n for n in found if in_force(n, now)]
    answer = {
        "notams": [n for n in live if n["placed"]],
        "unplaced": [n for n in live if not n["placed"]],
        "count": len(live),
        # How many their service says there are, so a page cap that bites is
        # visible rather than looking like a quiet sky.
        "total": total or len(found),
        "capped": total > len(found) if total else False,
        "source": "FAA NOTAM API",
    }
    with _lock:
        _cache[key] = (now, answer)
        if len(_cache) > 64:
            _cache.clear()
    return answer


def forget() -> None:
    """Drop what is cached. For tests and for starting over."""
    with _lock:
        _cache.clear()


def status() -> dict[str, Any]:
    return {"configured": configured(), "source": "FAA NOTAM API",
            "cached": len(_cache)}


def _apart(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """How far apart two points are, in kilometres. Haversine."""
    a, b = math.radians(lat), math.radians(to_lat)
    dlat = b - a
    dlon = math.radians(to_lon - lon)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def bounds_circle(west: float, south: float, east: float,
                  north: float) -> tuple[float, float, float]:
    """The centre and radius of a circle covering a rectangle, in km.

    Their API asks for a circle and a map shows a rectangle, so one has to
    become the other. Bigger rather than smaller: a circle that misses the
    corners misses closures that are on screen, which is the one error worth
    avoiding here.

    The radius is the real distance to the furthest corner rather than a flat
    approximation. The first version scaled the east-west half-width by the
    cosine of the CENTRE latitude, which is not where the box is widest -- the
    edge nearer the equator is -- so it came up twenty-one kilometres short on
    a box the size of Ukraine and the south-west corner fell outside.
    """
    lat = (south + north) / 2
    lon = (west + east) / 2
    corners = ((south, west), (south, east), (north, west), (north, east))
    return lat, lon, max(_apart(lat, lon, *corner) for corner in corners)


# Invented notices for the build with no network and no key.
#
# In their documented shape, read by the same read_notam() the live path uses,
# so the offline build exercises the reader rather than a copy of it. The text
# says plainly that they are invented: a convincing fake airspace closure is
# among the worst things this app could put on a screen.
DEMO = (
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0001/26", "location": "UKBV", "type": "N",
        "classification": "INTL",
        "coordinates": "5020N03030E", "radius": 30,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. AIRSPACE CLOSED TO ALL CIVIL "
                "TRAFFIC. NOT A REAL NOTAM.",
    }}}},
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0002/26", "location": "UKLV", "type": "N",
        "classification": "INTL",
        "coordinates": "4950N02400E", "radius": 12,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. TEMPORARY RESERVED AREA ACTIVE "
                "SFC-FL200. NOT A REAL NOTAM.",
    }}}},
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0003/26", "location": "UKOO", "type": "N",
        "classification": "INTL",
        "coordinates": "4630N03045E", "radius": 240,
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. FIR-WIDE RESTRICTION, LISTED "
                "RATHER THAN DRAWN AS A CIRCLE. NOT A REAL NOTAM.",
    }}}},
    # One with no readable position, so the offline build shows what an
    # unplaceable notice looks like rather than only the happy path.
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0004/26", "location": "UKFV", "type": "N",
        "classification": "INTL", "coordinates": "NOT A COORDINATE",
        "effectiveStart": "2026-01-01T00:00:00Z", "effectiveEnd": "PERM",
        "text": "DEMO — INVENTED NOTICE. NO POSITION THIS CAN READ. "
                "NOT A REAL NOTAM.",
    }}}},
    # One that has already ended, so "in force" is exercised rather than
    # asserted. It must not appear.
    {"properties": {"coreNOTAMData": {"notam": {
        "number": "A0005/26", "location": "UKBV", "type": "N",
        "classification": "INTL", "coordinates": "5100N03100E", "radius": 20,
        "effectiveStart": "2020-01-01T00:00:00Z",
        "effectiveEnd": "2020-01-02T00:00:00Z",
        "text": "DEMO — INVENTED NOTICE, ALREADY EXPIRED. NOT A REAL NOTAM.",
    }}}},
)


def demo() -> dict[str, Any]:
    """The invented set, through the real reader."""
    now = time.time()
    read = [n for n in (read_notam(raw) for raw in DEMO) if n]
    live = [n for n in read if in_force(n, now)]
    return {
        "notams": [n for n in live if n["placed"]],
        "unplaced": [n for n in live if not n["placed"]],
        "count": len(live),
        "total": len(live),
        "capped": False,
        "source": "demo — invented notices",
    }
