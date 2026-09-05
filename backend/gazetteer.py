"""Place names to coordinates, via OpenStreetMap's Nominatim.

Pulled out into its own module for one reason: a language model must never be
the thing that decides where a marker goes.

Asked to read a report and return coordinates, a model will always return
coordinates. For a capital city they will be about right. For a district, a
village, or an oblast named in the genitive they are recalled, interpolated or
invented, and there is nothing in the number to say which. The failure is
silent and it looks exactly like success -- a marker, on a map, in the wrong
country.

So the two jobs are separated. Reading Ukrainian and Russian prose and saying
"this is a drone and the place named is Nikopol" is language work, which a
model is good at. Turning "Nikopol" into 47.57 N 34.40 E is a lookup in a
gazetteer, which is what a gazetteer is for. This is the second half.

Nominatim is free and asks two things in return: identify yourself, and do not
hammer it. Both are honoured here -- one request a second at most, and
everything remembered, which costs almost nothing because these reports name
the same two dozen oblasts over and over.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import config

# Nominatim's usage policy is one request per second for a service like this.
MIN_INTERVAL = 1.05

# Nothing forgotten inside a session. Place names in these reports repeat
# relentlessly -- the same oblasts, the same dozen cities -- so after a few
# minutes almost every lookup is answered from here.
MAX_REMEMBERED = 4000

# How long a "there is no such place" answer is trusted. Shorter than the
# positive ones because a miss is often a transliteration this cannot yet
# handle rather than a place that does not exist.
MISS_SECONDS = 900

_lock = threading.Lock()
_known: dict[str, dict[str, Any] | None] = {}
_missed_at: dict[str, float] = {}
_last_call = 0.0
_calls = 0


class GazetteerError(RuntimeError):
    pass


def _key(name: str, countries: str) -> str:
    return f"{countries}|{' '.join(name.lower().split())}"


def wait_turn() -> None:
    """Hold the caller until a second has passed since the last request."""
    global _last_call
    while True:
        with _lock:
            gap = time.time() - _last_call
            if gap >= MIN_INTERVAL:
                _last_call = time.time()
                return
        time.sleep(min(MIN_INTERVAL - gap, MIN_INTERVAL))


# What a place named in an air-threat report can be: a settlement, or an
# administrative area. Nothing else.
#
# This is not fussiness, it is the fix for a real and very convincing failure.
# A report reading "past Kaharlyk, on a course north" had its town name mangled
# to "Kagul", and Nominatim's best match for that was озеро Кагул -- a lake,
# four hundred kilometres away in a different oblast. It answered with perfect
# confidence and the marker went on the map.
#
# A lake is not somewhere a drone is reported over. Neither is a shop, a
# roundabout or a farm building. Refusing the whole category costs nothing --
# no report has ever meant one -- and turns a wrong answer into no answer,
# which is the trade this module exists to make.
ACCEPTED = ("place", "boundary")

# Asked for a few rather than one, because the first hit is often a street or
# a business that happens to share the name and the settlement is behind it.
CANDIDATES = 6


# How much detail to keep in a boundary, in degrees. An oblast outline at full
# resolution is tens of thousands of points -- a megabyte of coastline to draw
# a warning with. At a hundredth of a degree, roughly a kilometre, the shape is
# still unmistakably the region and the payload is a few hundred points.
POLYGON_THRESHOLD = 0.01

# Past this many points a shape is dropped and the caller falls back to a
# circle. A guard against a country with a fractal coastline arriving whole.
MAX_POINTS = 3000


def _ask(name: str, countries: str) -> dict[str, Any] | None:
    global _calls
    params = {
        "q": name, "format": "jsonv2", "limit": CANDIDATES,
        # Asked for on every lookup rather than in a second call for the ones
        # that turn out to be regions. It is one request either way thanks to
        # the cache, and a town's outline is a handful of points.
        "polygon_geojson": 1,
        "polygon_threshold": POLYGON_THRESHOLD,
    }
    if countries:
        # The single most valuable parameter here. Without it "Sumy" is as
        # likely to be a street in another hemisphere, and half the point of
        # placing these reports is that they land in the right country.
        params["countrycodes"] = countries
    wait_turn()
    try:
        resp = requests.get(config.NOMINATIM_URL, params=params, timeout=20,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise GazetteerError(f"the gazetteer could not be reached: {exc}") from exc
    if resp.status_code == 429:
        raise GazetteerError("the gazetteer is rate limiting")
    if not resp.ok:
        raise GazetteerError(f"the gazetteer answered {resp.status_code}")
    with _lock:
        _calls += 1
    try:
        found = resp.json()
    except ValueError:
        return None
    return read_place(found)


def read_bbox(raw: Any) -> list[float] | None:
    """Nominatim's bounding box as [south, north, west, east], or None.

    It arrives as four strings, and in that order -- not the [west, south,
    east, north] most things use. Reordering it here rather than at each
    reader is one place to get it wrong instead of several.
    """
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        south, north, west, east = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if not (-90 <= south <= north <= 90
            and -180 <= west <= 180 and -180 <= east <= 180):
        return None
    return [south, north, west, east]


def count_points(geometry: Any) -> int:
    """How many coordinate pairs a GeoJSON geometry holds."""
    if not isinstance(geometry, dict):
        return 0
    coords = geometry.get("coordinates")

    def walk(node: Any) -> int:
        if not isinstance(node, (list, tuple)) or not node:
            return 0
        if isinstance(node[0], (int, float)):
            return 1
        return sum(walk(part) for part in node)

    return walk(coords)


def read_shape(raw: Any) -> dict[str, Any] | None:
    """A usable outline out of Nominatim's polygon, or None.

    Only the shapes that are actually areas. A point or a line comes back for
    plenty of places, and drawing a warning as a one-pixel dot or a squiggle
    would be worse than the circle it replaces.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    points = count_points(raw)
    if not 4 <= points <= MAX_POINTS:
        return None
    return {"type": raw["type"], "coordinates": raw["coordinates"]}


def read_place(found: Any) -> dict[str, Any] | None:
    """The best usable result out of whatever Nominatim sent back.

    "Usable" is doing the work: the first result is taken only if it is a
    settlement or an administrative area. Anything else is passed over, and if
    nothing in the list qualifies the answer is None.
    """
    if not isinstance(found, list):
        return None
    for candidate in found:
        if not isinstance(candidate, dict):
            continue
        try:
            lat, lon = float(candidate["lat"]), float(candidate["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        category = str(candidate.get("category") or candidate.get("class") or "")[:40]
        if category and category not in ACCEPTED:
            continue
        return {
            "lat": lat,
            "lon": lon,
            "name": str(candidate.get("display_name") or "")[:200] or None,
            "category": category or None,
            # How big the place is, as Nominatim measured it. Carried because
            # a report is about an area, not a point: a strike in a village
            # and an air alert over an oblast are the same shape of statement
            # about two things a hundred kilometres different in size, and
            # only the gazetteer knows which is which.
            "bbox": read_bbox(candidate.get("boundingbox")),
            # The real outline, where the place has one. A warning covering an
            # oblast is about that oblast, and a circle over the middle of it
            # both misses ground the warning covers and covers ground it does
            # not. The boundary is the thing the report actually named.
            "shape": read_shape(candidate.get("geojson")),
            # An oblast and a street corner are both "a place" and should not
            # be drawn as though they were equally precise. The caller decides
            # what to do about it; this only reports what was matched.
            "kind": str(candidate.get("type") or category or "")[:40] or None,
        }
    return None


def find(name: str, countries: str = "") -> dict[str, Any] | None:
    """Where a place is, or None if the gazetteer does not know it.

    None is a real answer and the caller is expected to respect it. The whole
    point of this module is that not knowing where somewhere is beats putting
    a marker down anyway.
    """
    name = " ".join(str(name or "").split())
    if len(name) < 2:
        return None
    key = _key(name, countries)

    with _lock:
        if key in _known:
            hit = _known[key]
            if hit is not None:
                return dict(hit)
            if time.time() - _missed_at.get(key, 0) < MISS_SECONDS:
                return None

    place = _ask(name, countries)

    with _lock:
        _known[key] = place
        if place is None:
            _missed_at[key] = time.time()
        if len(_known) > MAX_REMEMBERED:
            _known.clear()
            _missed_at.clear()
    return dict(place) if place else None


def remember(name: str, countries: str, place: dict[str, Any] | None) -> None:
    """Put an answer in without asking for it. For seeding and for tests."""
    with _lock:
        _known[_key(name, countries)] = place
        if place is None:
            _missed_at[_key(name, countries)] = time.time()


def stats() -> dict[str, Any]:
    with _lock:
        return {"remembered": len(_known), "lookups": _calls}


def forget() -> None:
    global _calls
    with _lock:
        _known.clear()
        _missed_at.clear()
        _calls = 0
