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

# The furthest their radius query will go, in nautical miles.
#
# This is why the layer showed nothing. A map of Ukraine is an 850-kilometre
# circle -- four hundred and sixty nautical miles -- and their API refuses a
# locationRadius past a hundred, so every request at the zoom anybody
# actually uses this map at was rejected before it was read. The layer was
# doing the right thing with the answer it got, and the answer was "no".
MOST_NM = 100.0

# The flight information regions this app's subject is in, as rough boxes.
#
# Asking by FIR rather than by radius is both the fix for the cap above and
# the better query: a country-sized view is one or two of these instead of a
# grid of circles, and a notice that closes a whole FIR is FILED against that
# FIR rather than against a point in it.
#
# The boxes are approximate and only ever used to decide which FIR to ask
# about. Nothing is drawn from them, so a box that is too generous costs one
# extra request and a box that is too mean costs a missed region -- never a
# mark in the wrong place. Same for the codes: a wrong one comes back empty,
# which the panel says, rather than coming back wrong.
FIRS: tuple[tuple[str, str, float, float, float, float], ...] = (
    # code,  name,          south, west,  north, east
    ("UKBV", "Kyiv",         48.3,  28.0,  52.4,  35.6),
    ("UKLV", "Lviv",         47.7,  22.1,  51.6,  28.6),
    ("UKOV", "Odesa",        44.9,  28.0,  48.8,  33.6),
    ("UKDV", "Dnipro",       46.4,  33.0,  50.6,  40.3),
    ("UKFV", "Simferopol",   43.3,  32.0,  46.3,  36.7),
    ("UMMV", "Minsk",        51.2,  23.0,  56.3,  33.0),
    ("UUWV", "Moscow",       51.8,  30.0,  60.2,  45.5),
    ("URRV", "Rostov",       43.5,  36.0,  52.6,  48.5),
    ("LUUU", "Chisinau",     45.4,  26.6,  48.6,  30.2),
    ("LRBB", "Bucharest",    43.5,  20.2,  48.4,  29.8),
    ("EPWW", "Warsaw",       48.9,  14.1,  55.0,  24.3),
)

# A bound on how many of them one view asks about.
MOST_FIRS = 8


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


def firs_over(west: float, south: float, east: float,
              north: float) -> list[tuple[str, str]]:
    """Which flight information regions a rectangle touches, nearest first.

    Nearest to the middle of the view first, so a cap that bites drops the
    edges rather than the thing being looked at.
    """
    lat = (south + north) / 2
    lon = (west + east) / 2
    touching = []
    for code, name, fs, fw, fn, fe in FIRS:
        if fw > east or fe < west or fs > north or fn < south:
            continue
        away = _apart(lat, lon, (fs + fn) / 2, (fw + fe) / 2)
        touching.append((away, code, name))
    touching.sort()
    return [(code, name) for _, code, name in touching[:MOST_FIRS]]


def _walk(params: dict[str, Any], found: list[dict[str, Any]]) -> int:
    """Read every page of one query into `found`. Returns their total."""
    total = 0
    for page in range(1, MOST_PAGES + 1):
        got = _ask({**params, "pageSize": PER_PAGE, "pageNum": page})
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
    return total


def over(west: float, south: float, east: float,
         north: float) -> dict[str, Any]:
    """Every notice in force over a rectangle.

    By flight information region where the view is in one this knows, and by
    radius otherwise. The FIR query is the important half: their radius query
    stops at a hundred nautical miles, and a map of a country is four or five
    times that, so asking by radius at any useful zoom was asking for a
    refusal.
    """
    now = time.time()
    regions = firs_over(west, south, east, north)
    key = ("fir:" + ",".join(code for code, _ in regions)) if regions else (
        f"box:{round(west, 1)}/{round(south, 1)}/{round(east, 1)}/{round(north, 1)}")
    with _lock:
        held = _cache.get(key)
        if held and now - held[0] < KEEP_SECONDS:
            return held[1]

    found: list[dict[str, Any]] = []
    total = 0
    asked: list[str] = []
    short = False
    if regions:
        for code, name in regions:
            total += _walk({"icaoLocation": code}, found)
            asked.append(f"{code} ({name})")
    else:
        lat, lon, radius_km = bounds_circle(west, south, east, north)
        wanted_nm = radius_km / NM_KM
        # Their cap, honoured rather than discovered: asking for more is a
        # rejected request, and a rejected request looks like an empty sky.
        short = wanted_nm > MOST_NM
        total = _walk({
            "locationLongitude": round(lon, 4),
            "locationLatitude": round(lat, 4),
            "locationRadius": round(max(1.0, min(MOST_NM, wanted_nm))),
        }, found)
        asked.append(f"{round(min(MOST_NM, wanted_nm))} NM around "
                     f"{lat:.2f},{lon:.2f}")

    # One notice can be filed against two adjacent FIRs, so the same number
    # arrives twice. Kept once, and the first reading wins.
    seen: dict[str, dict[str, Any]] = {}
    for one in found:
        seen.setdefault(one["id"], one)
    live = [n for n in seen.values() if in_force(n, now)]

    answer = {
        "notams": [n for n in live if n["placed"]],
        "unplaced": [n for n in live if not n["placed"]],
        "count": len(live),
        # How many their service says there are, so a page cap that bites is
        # visible rather than looking like a quiet sky.
        "total": total or len(seen),
        "capped": total > len(seen) if total else False,
        # Which query was actually made. "It does not work" is otherwise
        # unanswerable from the outside, and this is the one fact that
        # separates "nothing is closed" from "nothing was asked".
        "asked": asked,
        # True when the view is wider than one radius query can cover and no
        # FIR was known for it, so part of it was not looked at.
        "partial": short,
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
        "asked": ["demo — nothing was asked of anybody"],
        "partial": False,
        "source": "demo — invented notices",
    }
