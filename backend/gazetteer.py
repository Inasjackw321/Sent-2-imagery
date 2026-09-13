"""Place names to coordinates, via OpenStreetMap's Nominatim.

Nominatim is free and asks two things in return: identify yourself, and do not
hammer it. Both are honoured here -- one request a second at most, across the
whole process, and every answer remembered for as long as the process lives.

That rate limit is what the rest of the app uses this module for. The search
box in the map calls Nominatim directly, because it wants a list to choose
from rather than one best answer, but it takes its turn through wait_turn()
here: the policy is about this process, not about which of its functions is
asking.

The other half -- find(), and the reading it does in read_place() -- is what
turns a place named in a Telegram report into a marker on the map. It is the
half that refuses, and most of what it knows is a refusal:

  a place named in a report may only be a settlement or an administrative
  area, and only certain KINDS of those -- "place" in OpenStreetMap covers
  seas and islets as well as towns, and one of those put a drone in the middle
  of the Sea of Azov;

  a mangled town name gets answered with a lake four hundred kilometres away,
  with perfect confidence;

  and the answer has to be constrained to the country the report is about, or
  "Sumy" is as likely to be a street in another hemisphere.

None of that was cheap to learn and all of it is a wrong marker prevented. A
report whose place cannot be found is listed in the panel and not drawn, which
is a visible outcome somebody can act on. A report drawn in the wrong oblast is
not.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import config, places

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


# What a place named in a piece of prose can be: a settlement, or an
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

# And which KINDS of those, which the category alone does not settle.
#
# "place" in OpenStreetMap is not only settlements. It also covers seas,
# oceans, straits, bays, islands, islets, peninsulas, deserts and plains --
# all of which passed the category check above, and one of which put a drone
# in the middle of the Sea of Azov on a live map. A report says a drone is
# over somewhere; "the Sea of Azov" is a somewhere, and it is not what the
# report meant, and there is no way to tell from the coordinates afterwards.
#
# An allowlist rather than a list of things to refuse, because the failure is
# asymmetric. An unexpected type refused is one report in the panel instead of
# on the map, which is visible and recoverable. An unexpected type accepted is
# a confident marker in the wrong place, which is the thing this whole module
# exists to prevent.
SETTLEMENTS = frozenset((
    "city", "town", "village", "hamlet", "borough", "suburb", "quarter",
    "neighbourhood", "municipality", "isolated_dwelling", "allotments",
    "locality", "square", "city_block",
))

# Administrative areas: an oblast, a raion, a district. A report located to
# one of these is located to a region, which the caller draws differently.
AREAS = frozenset((
    "administrative", "region", "province", "state", "county", "district",
    "political", "census",
))

ACCEPTED_TYPES = SETTLEMENTS | AREAS

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
        # And what sort of place. See ACCEPTED_TYPES: "place" covers seas and
        # islets as well as towns, and a drone reported over a town does not
        # belong in the middle of the Sea of Azov.
        kind = str(candidate.get("type") or "")[:40].lower()
        if kind and kind not in ACCEPTED_TYPES:
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

    # A boundary learned earlier beats the built-in centre.
    #
    # Checked first so the upgrade below is not thrown away: once Nominatim
    # has given a real outline for an oblast it is in here, and the built-in
    # entry -- which is a centre and an extent and no shape at all -- must not
    # go on shadowing it.
    with _lock:
        learned = _known.get(key)
        if learned and learned.get("shape"):
            return dict(learned)

    # The places these reports name every night, without asking anybody.
    #
    # This is the whole of "make it fast". Nominatim is correct and it is
    # rate-limited to one request a second, so twenty reports took fourteen
    # and a half seconds to place -- measured -- and the map filled in one
    # mark at a time over a minute, which reads as a layer that does not work.
    #
    # The names are not arbitrary: the same two dozen oblasts and the same
    # hundred cities, every night. A request for "Харків" is a round trip to
    # be told something that has not moved since 1654. See backend/places.py.
    known = places.lookup(name)
    if known:
        # A region drawn from its extent is a circle over an oblast, which is
        # right enough to act on and not what the province looks like. So the
        # real outline is asked for in the background, at the rate limit,
        # behind a mark that is already on the map. Nothing waits for it and
        # the next poll draws the boundary.
        if known.get("category") == "boundary":
            improve_later(name, countries)
        return known

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


# Names whose real boundary is worth having, and the one worker that fetches
# them. A queue rather than a thread each: they all go through the same
# one-a-second gate, so more than one worker would only queue harder.
_wanted: list[tuple[str, str]] = []
_asked_for_shapes: set[str] = set()
_improver: threading.Thread | None = None


def improve_later(name: str, countries: str, urgent: bool = False) -> bool:
    """Ask for a region's real outline in the background. Never blocks.

    Returns whether it was queued -- False if it is already queued, already
    fetched, or already known with a shape.

    `urgent` jumps the queue, and exactly one thing uses it: a WARNING over a
    region. Until its outline arrives that warning is drawn as the region's
    extent, which is a rectangle and is honest about being provisional but is
    not the shape of any province. Everything else in this queue is a mark
    that is already drawn correctly and merely gains detail, so a warning
    waiting behind a dozen of those is the one case where the order matters.
    """
    global _improver
    key = _key(name, countries)
    with _lock:
        if key in _asked_for_shapes:
            return False
        learned = _known.get(key)
        if learned and learned.get("shape"):
            return False
        _asked_for_shapes.add(key)
        if urgent:
            _wanted.insert(0, (name, countries))
        else:
            _wanted.append((name, countries))
        running = _improver is not None and _improver.is_alive()
    if not running:
        _improver = threading.Thread(target=_improve, name="gazetteer-shapes",
                                     daemon=True)
        _improver.start()
    return True


# How long the outline worker waits between requests, on top of the shared
# one-a-second gate.
#
# It shares that gate with the foreground, so without this it takes every
# other slot and a report waiting to be placed queues behind a boundary
# nobody is looking at yet. The mark matters and the outline does not, so the
# outline gives way.
IMPROVE_EVERY = 3.0


def _improve() -> None:
    """Drain the queue, one rate-limited lookup at a time, unhurriedly."""
    while True:
        with _lock:
            if not _wanted:
                return
            name, countries = _wanted.pop(0)
        # Let anything urgent go first.
        time.sleep(IMPROVE_EVERY)
        try:
            found = _ask(name, countries)
        except GazetteerError:
            # It is down, or it is rate limiting. The mark is already on the
            # map from the built-in centre and stays there; the outline can
            # wait for another night.
            continue
        if not found or not found.get("shape"):
            continue
        with _lock:
            _known[_key(name, countries)] = found


def shapes_wanted() -> int:
    """How many outlines are still queued, for the panel to say."""
    with _lock:
        return len(_wanted)


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
        # And what has been asked for, or a test that clears the cache and
        # looks up the same region again would find the outline request
        # already "done" and never made.
        _wanted.clear()
        _asked_for_shapes.clear()
        _calls = 0
