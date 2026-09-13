"""NEPTUN's live threat feed, read as a second source beside the channels.

https://neptun.in.ua/developers — open, free, read-only, no key. Two endpoints
matter here:

  /api/v1/threats   every active track: type, position, course, group size
  /api/v1/alerts    the official air alerts, by raion and by oblast

This is a different KIND of source from the Telegram channels and that is the
point of having it. The channels are prose that has to be read and geocoded,
and every step of that can be wrong: a name in a case no rule handles, a town
the gazetteer does not know, a course written in the instrumental. NEPTUN has
already done that work and hands over coordinates. So the two sources fail
independently, which is the only kind of redundancy worth having.

What this module is careful about, and why
------------------------------------------

Their documentation asks for three things, and all three are about not
overstating what is known. They are honoured here exactly:

  areaOnly. "There is no dot." The sources named a region and nothing finer,
  so lat/lon is the region's CENTROID -- a made-up point in the middle of a
  province. Their words: such a record must not be drawn as a place, without
  extrapolation, without a course, and without a "how many km from me",
  "otherwise you will show a person a made-up number as a fact". So an
  areaOnly track arrives here with its heading, course and destination
  stripped, marked as located-to-a-region, and it is drawn as the region
  rather than as a point in it.

  advisory. "Surveillance, not a signal to hide." A MiG-31K taking off is
  worth recording and is not a reason to take cover, and drawing it like one
  teaches people to ignore the signal that matters. These are marked and drawn
  quietly.

  Attribution. A visible link to NEPTUN beside the data. That is the only
  condition of use and the panel carries it; see ATTRIBUTION below.

  Politeness. The snapshot is CDN-cached and they ask for no more than one
  poll every five seconds. The floor here is longer than that and enforced in
  one place, because "the tracker polls every thirty seconds" is a fact about
  today's configuration rather than a guarantee about this module.

One thing deliberately NOT used: the WebSocket stream and the JS SDK. Both are
better for a page that is watching continuously, and both would mean the
browser talking to neptun.in.ua directly -- which this app does not do for
Telegram either. Everything is fetched here, so a viewer's address goes to
this app and nowhere else, and the same is true of the pictures and the tiles.
The REST snapshot is the shape that fits that promise, and at a thirty-second
poll the difference is one refresh.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from . import config

BASE = "https://neptun.in.ua"
THREATS = f"{BASE}/api/v1/threats"
ALERTS = f"{BASE}/api/v1/alerts"

# The boundary files their alert keys index into. Fetched once and kept, which
# is what finally gives an oblast warning its real outline without a Nominatim
# request per province.
OBLAST_SHAPES = f"{BASE}/oblasts.geojson"

# Their terms, in the only form that satisfies them: a visible link beside the
# data. Carried in the feed payload so the page cannot show the marks without
# also having this to hand.
ATTRIBUTION = {
    "text": "Дані: Карта повітряних тривог — NEPTUN",
    "english": "Data: Ukraine air-alert map — NEPTUN",
    "url": f"{BASE}/",
    # Theirs, verbatim in substance: an aggregator, not an official alert
    # system. Anything about human safety has to say so.
    "caveat": ("NEPTUN is an information aggregator, not an official alert "
               "system. Data may contain inaccuracies or delays — always "
               "follow official alerts."),
}

# They ask for no more than one REST poll every five seconds. This is longer
# on purpose: the caller polls every thirty seconds or more, so a shorter
# floor here would only matter when something has gone wrong, which is exactly
# when a runaway loop would hammer somebody else's origin.
MIN_INTERVAL = 15.0

# Long enough for a slow CDN edge, short enough that a hung fetch does not hold
# a poll open past its own interval.
TIMEOUT = 12.0

# A ceiling on a single answer, so a malformed or hostile response cannot turn
# into an unbounded number of marks.
MOST_THREATS = 600
MOST_ALERTS = 200


class NeptunError(RuntimeError):
    pass


_lock = threading.Lock()
_last_call = 0.0
_shapes: dict[str, Any] | None = None
_shapes_at = 0.0

# Their type vocabulary, onto the kinds this map draws.
#
# Written out rather than passed through, because the two vocabularies were
# designed for different jobs and the differences are the interesting part:
#
#   recon is folded into drone here exactly as the channel reader folds it --
#   telling a reconnaissance drone from an attack one needs the airframe, and
#   the map draws both the same.
#
#   ballistic is a missile. The difference is enormous in life and nil on this
#   map: both are inbound and both are drawn where they were reported.
#
#   kab is a guided bomb, and it is NOT a missile. It is released from an
#   aircraft close to the line and travels tens of kilometres, so a KAB mark
#   means something quite different about where the danger is. It gets its own
#   kind rather than being flattened into one that would say the wrong thing.
#
#   mig31k is an aircraft, and it is the standard advisory case -- a MiG-31K
#   taking off is a reason to pay attention and not a reason to take cover.
KINDS = {
    "uav": "drone",
    "recon": "drone",
    "missile": "missile",
    "ballistic": "missile",
    "kab": "bomb",
    "mig31k": "aircraft",
    "unknown": "unknown",
}

# Statuses worth drawing. "resolved" is a track that has ended; drawing one
# would be saying something is in the air that they have said is not.
DRAWN = ("active", "stale")


def _text(value: Any, limit: int = 160) -> str | None:
    out = " ".join(str(value or "").split())[:limit]
    return out or None


def _number(value: Any) -> float | None:
    """A finite number, or None. Strings accepted: JSON is JSON."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


class TooSoon(NeptunError):
    """Asked again before the interval was up. Not a failure -- a skip."""


def claim_turn() -> bool:
    """Take the next slot if it is due, without waiting. False if it is not.

    Skipping rather than blocking, which is the whole difference between a
    rate limit on a fetch somebody is waiting for and a rate limit inside a
    poll loop. The gazetteer waits because a report cannot be placed without
    its answer; this must not, because the poll has four channels to get
    through and the previous snapshot is still perfectly good. A blocking
    version made every test that calls poll() wait fifteen seconds.
    """
    global _last_call
    with _lock:
        if time.time() - _last_call < MIN_INTERVAL:
            return False
        _last_call = time.time()
        return True


def _get(url: str, *, paced: bool = True) -> Any:
    if paced and not claim_turn():
        raise TooSoon("asked again before the interval was up")
    try:
        resp = requests.get(url, timeout=TIMEOUT,
                            headers={"User-Agent": config.USER_AGENT,
                                     "Accept": "application/json"})
    except requests.RequestException as exc:
        raise NeptunError(f"NEPTUN could not be reached: {exc}") from exc
    if resp.status_code == 429:
        raise NeptunError("NEPTUN is rate limiting — polling too often")
    if not resp.ok:
        raise NeptunError(f"NEPTUN answered {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise NeptunError("NEPTUN answered something that is not JSON") from exc


def read_threat(raw: Any) -> dict[str, Any] | None:
    """One track from their snapshot, as an event this map can draw.

    None for anything that cannot be drawn honestly: a resolved track, one
    with no usable position, or an entry that is not an object at all.
    """
    if not isinstance(raw, dict):
        return None
    status = _text(raw.get("status"), 20) or "active"
    if status.lower() not in DRAWN:
        return None

    lat, lon = _number(raw.get("lat")), _number(raw.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    kind = KINDS.get((_text(raw.get("type"), 20) or "unknown").lower(), "unknown")

    # "There is no dot." Their words, and the whole of why this field exists:
    # lat/lon is the centroid of a province, not a place anybody reported. So
    # everything that would claim point precision is dropped here rather than
    # left for a caller to remember -- the course, the speed, the destination.
    area_only = bool(raw.get("areaOnly"))

    heading = None if area_only else _number(raw.get("heading"))
    if heading is None and not area_only:
        moving = raw.get("velocity")
        if isinstance(moving, dict):
            heading = _number(moving.get("bearingDeg"))
    if heading is not None:
        heading %= 360.0

    # Group size. Their 0-or-absent means "not stated", which is one thing,
    # not zero things.
    count = _number(raw.get("count"))
    count = int(count) if count and 1 <= count <= 999 else 1

    # The place, as specifically as they named it -- except for areaOnly,
    # where the region IS the answer and the finer fields are absent by
    # contract.
    place = (None if area_only else
             _text(raw.get("locality")) or _text(raw.get("district")))
    region = _text(raw.get("region"))

    return {
        "kind": kind,
        "lat": lat,
        "lon": lon,
        "heading": heading,
        "count": count,
        "place": place or region,
        "region": region,
        # Marked so the map can draw it as a region rather than as a point,
        # and so nothing downstream computes a distance from it.
        "area_only": area_only,
        # Surveillance rather than a signal to hide. Drawn quietly.
        "advisory": bool(raw.get("advisory")),
        "confidence": _text(raw.get("confidenceLevel"), 10),
        "quality": _text(raw.get("positionQuality"), 20),
        "uncertainty_km": _number(raw.get("uncertaintyKm")),
        "sources": int(_number(raw.get("sourceCount")) or 0),
        "status": status.lower(),
        "title": _text(raw.get("title"), 60),
        "summary": _text(raw.get("explanationShort"), 200),
        "updated_at": _text(raw.get("updatedAt"), 40),
        # Their track id, which is stable across updates. Used as the source
        # id so an upserted track replaces its own mark instead of adding one.
        "id": _text(raw.get("id"), 80),
    }


def threats() -> list[dict[str, Any]]:
    """Every active track, as events. Raises NeptunError if it cannot be read."""
    payload = _get(THREATS)
    raw = payload.get("threats") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise NeptunError("NEPTUN's snapshot had no threats list")
    out = []
    for item in raw[:MOST_THREATS]:
        got = read_threat(item)
        if got:
            out.append(got)
    return out


def read_alerts(payload: Any) -> list[dict[str, Any]]:
    """The official alerts, oblasts and raions alike, as one list.

    Oblasts first: where a whole province is under alert and one of its raions
    is too, the province is the bigger statement and the one worth drawing.
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict[str, Any]] = []
    for scope, key in (("oblast", "oblasts"), ("raion", "raions")):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:MOST_ALERTS]:
            if not isinstance(row, dict):
                continue
            name = _text(row.get("name"))
            if not name:
                continue
            out.append({
                "scope": scope,
                # Their key indexes into oblasts.geojson and raions.geojson,
                # which is how a warning gets a real outline without a
                # geocoding request per province.
                "key": _text(row.get("key"), 80),
                "name": name,
                "oblast": _text(row.get("oblast")) or (name if scope == "oblast" else None),
                "since": _text(row.get("since"), 40),
            })
    return out


def alerts() -> list[dict[str, Any]]:
    """The official air alerts. Raises NeptunError if it cannot be read."""
    return read_alerts(_get(ALERTS))


def index_shapes(payload: Any) -> dict[str, Any]:
    """Their oblast boundaries, keyed by both their key and their name.

    Both, because the alert feed gives a key and the rest of this app thinks
    in names -- and a warning read from a Telegram channel has only a name.
    One index serves both without a second copy to fall out of step.
    """
    out: dict[str, Any] = {}
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return out
    for feature in features:
        if not isinstance(feature, dict):
            continue
        shape = feature.get("geometry")
        if not isinstance(shape, dict) or shape.get("type") not in (
                "Polygon", "MultiPolygon"):
            continue
        props = feature.get("properties")
        props = props if isinstance(props, dict) else {}
        for field in ("key", "name", "name_uk", "NAME_1", "oblast"):
            label = _text(props.get(field), 80)
            if label:
                out.setdefault(label.casefold(), shape)
    return out


# How long the boundaries are trusted. They are administrative borders; a day
# is generous and the refetch costs one request.
SHAPES_FOR = 24 * 3600


def shapes() -> dict[str, Any]:
    """The oblast outlines, fetched once and kept. {} if they cannot be had.

    Never raises. A missing boundary means a warning is drawn from its extent
    instead, which is the behaviour without this module at all -- so the whole
    feature degrades to what it was rather than to an error.
    """
    global _shapes, _shapes_at
    with _lock:
        if _shapes is not None and time.time() - _shapes_at < SHAPES_FOR:
            return _shapes
    try:
        # Not paced with the snapshot: this is a static file fetched about
        # once a day, and making it queue behind the threat poll would delay
        # the thing people are actually waiting for.
        found = index_shapes(_get(OBLAST_SHAPES, paced=False))
    except NeptunError:
        found = {}
    with _lock:
        # A failed fetch is remembered only briefly, so a blip does not cost a
        # day of outlines.
        _shapes = found
        _shapes_at = time.time() if found else time.time() - SHAPES_FOR + 300
        return _shapes


def shape_for(name: str | None) -> Any:
    """The outline for a region by name or key, or None."""
    if not name:
        return None
    return shapes().get(str(name).casefold())


def forget() -> None:
    """Drop what is cached. For tests and for starting over."""
    global _shapes, _shapes_at, _last_call
    with _lock:
        _shapes, _shapes_at, _last_call = None, 0.0, 0.0


def status() -> dict[str, Any]:
    """What this source is doing, for the panel."""
    with _lock:
        return {
            "name": "neptun.in.ua",
            "attribution": ATTRIBUTION,
            "outlines": len(_shapes or {}),
            "last_call": _last_call or None,
        }
