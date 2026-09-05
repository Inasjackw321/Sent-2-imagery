"""Air-threat reports from public Telegram channels, put on the map.

Monitoring channels in Ukraine, Lebanon and the wider Middle East post a
running commentary of what is in the air: drones crossing an oblast, missiles
on a course, strikes where they land. It is published to warn people, openly
and deliberately, and this reads the same public pages anyone can open in a
browser.

This is the second attempt, and the first one's mistake is worth writing down
because it is not obvious and it looked fine.

The first version asked the language model for coordinates. That is the wrong
job to give a model. Asked to read a report and return a latitude, a model
always returns a latitude: right for a capital city, recalled or interpolated
for anywhere smaller, and occasionally invented outright. Nothing in the number
says which of those happened. The result was markers in the wrong oblast, and
no way to tell them from the correct ones.

So the work is split along the seam it should always have had:

  reading    the channels' public web preview at t.me/s/<name>. No account, no
             API key -- the same HTML a logged-out visitor gets.

  reading    a model turns the prose into structure: what kind of thing, the
  meaning    NAME of the place, the NAME of where it is going. Language work,
             which is what it is for. It is asked for no numbers at all.

  placing    a gazetteer turns those names into coordinates, biased to the
             country the channel reports on. If it does not know the place,
             the event is not placed. That is a real outcome and it is kept,
             as text, rather than being quietly dropped.

  moving     an event with a destination has a course, computed from the two
             positions, and its marker is carried along it and stops when it
             arrives. Dead reckoning from a typical speed for the kind of
             thing it is -- an estimate, and everything downstream says so.

The other change is that nothing is thrown away for failing to be placeable.
A report that cannot be put on a map is still a report; it goes into the alert
stream with everything else. The old version silently discarded them, so a
patchy night looked identical to a broken feature.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import re
import threading
import time
from typing import Any

import requests

from . import config, gazetteer

# The channels, and what each one is about.
#
# `countries` is the single most valuable thing in this table. A gazetteer
# asked for "Sumy" with no country will happily return a street somewhere
# else; asked for "Sumy" in Ukraine it returns the oblast capital. Two-letter
# ISO codes, as Nominatim wants them.
CHANNELS = (
    {"name": "eRadarrua", "region": "Ukraine", "countries": "ua"},
    {"name": "kpszsu", "region": "Ukraine", "countries": "ua"},
    {"name": "mon1tor_ua", "region": "Ukraine", "countries": "ua"},
    {"name": "war_monitor", "region": "Ukraine", "countries": "ua,ru,by"},
    {"name": "redlinkleb", "region": "Lebanon", "countries": "lb,il,sy"},
    {"name": "shin_persian", "region": "Middle East", "countries": "ir,il,lb,sy,iq,ye"},
)

# The public web preview. Not the API: this is the page Telegram serves to a
# visitor with no account, and it carries the recent posts as plain HTML.
PREVIEW = "https://t.me/s/{channel}"

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "poolside/laguna-s-2.1:free"

# How long a track stays on the map after the report that made it. Dead
# reckoning decays: after twenty minutes an extrapolated position is a work of
# fiction, so it goes rather than sitting there looking authoritative.
KEEP_MINUTES = 20

# How long a report stays in the alert stream. Longer than a track, because
# text does not go stale the way an extrapolated position does -- "a strike was
# reported in Kharkiv an hour ago" is still true an hour later.
ALERT_MINUTES = 90

# Nothing older than this is worth reading on start-up.
LOOKBACK_MINUTES = 45

# The floor between two reads of the channels. The map asks once a minute, but
# it may be open in three tabs, and every read that finds something new costs a
# model call against a free-tier ceiling.
MIN_POLL_SECONDS = 55

# How fast each kind of thing travels, in km/h, for carrying a marker forward
# between reports. Rough figures for the type, not measurements of the object:
# a Shahed is a propeller drone at a couple of hundred, a cruise missile is
# subsonic and fast, a ballistic one is not usefully extrapolated at all.
SPEEDS = {
    "drone": 180.0,
    "cruise": 800.0,
    "ballistic": 0.0,
    "aircraft": 700.0,
    "explosion": 0.0,
    "unknown": 200.0,
}

# What each kind looks like, and how loud it is. `rank` orders the alert
# stream: a strike outranks a drone crossing an oblast.
KINDS = {
    "drone": {"colour": "#ff3b30", "label": "Drone", "rank": 2},
    "cruise": {"colour": "#ff3b30", "label": "Cruise missile", "rank": 3},
    "ballistic": {"colour": "#ff3b30", "label": "Ballistic", "rank": 4},
    "aircraft": {"colour": "#ff3b30", "label": "Aircraft", "rank": 2},
    "explosion": {"colour": "#b06bff", "label": "Explosion", "rank": 5},
    "alert": {"colour": "#ffb020", "label": "Air alert", "rank": 1},
    "unknown": {"colour": "#ff8a3b", "label": "Unidentified", "rank": 1},
}

# Kinds that are announcements rather than objects: worth reading, never worth
# drawing as a thing in the air with a course and a speed.
NOT_AIRBORNE = ("explosion", "alert")

MAX_EVENTS = 400
MAX_ALERTS = 200

_lock = threading.Lock()
_key: str | None = None
_seen: set[str] = set()
_events: list[dict[str, Any]] = []
_alerts: list[dict[str, Any]] = []
_counter = 0
_state = "not started"
_last_poll = 0.0


class OsintError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def set_key(key: str | None) -> bool:
    """Remember the OpenRouter key, or forget it. Never written to disk."""
    global _key
    with _lock:
        _key = (key or "").strip() or None
        return _key is not None


def has_key() -> bool:
    with _lock:
        return _key is not None


# ---------------------------------------------------------------------------
# Reading the channels
# ---------------------------------------------------------------------------

_MESSAGE = re.compile(
    r'data-post="(?P<post>[^"]+)".*?'
    r'<time[^>]*datetime="(?P<when>[^"]+)"',
    re.S)
_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>', re.S)
_TAGS = re.compile(r"<[^>]+>")


def parse_preview(page: str, channel: str) -> list[dict[str, Any]]:
    """Pull the posts out of a channel's public preview page.

    Deliberately regex rather than an HTML parser: this reads two attributes
    and a block of text out of a page whose shape has been stable for years,
    and adding a parser dependency to do it would be the larger risk.
    """
    posts: list[dict[str, Any]] = []
    # Each message is one wrapper div; splitting on it keeps a post's own text
    # with its own id rather than letting a greedy match cross into the next.
    for chunk in page.split('class="tgme_widget_message ')[1:]:
        head = _MESSAGE.search(chunk)
        if not head:
            continue
        body = _TEXT.search(chunk)
        if not body:
            continue
        text = _TAGS.sub(" ", body.group("body").replace("<br/>", "\n").replace("<br>", "\n"))
        text = html.unescape(text)
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            continue
        posts.append({
            "id": head.group("post"),
            "channel": channel,
            "when": head.group("when"),
            "text": text[:1200],
        })
    return posts


def _fetch_channel(channel: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(PREVIEW.format(channel=channel), timeout=20,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise OsintError(f"{channel} could not be read: {exc}") from exc
    if not resp.ok:
        raise OsintError(f"{channel} answered {resp.status_code}")
    return parse_preview(resp.text, channel)


# ---------------------------------------------------------------------------
# Reading the meaning
# ---------------------------------------------------------------------------

# Note what this does NOT ask for: coordinates. That is the whole difference
# between this version and the one before it.
PROMPT = """You read air-threat reports in Ukrainian, Russian, Arabic, Farsi
and Hebrew, and return JSON describing them.

Return ONLY a JSON object: {"events": [...]}. One entry per report you were
given, in the same order, using the "id" you were given. No prose, no fences.

Each event:
  "id"      the id of the message this came from, copied exactly
  "kind"    one of: drone, cruise, ballistic, aircraft, explosion, alert,
            unknown. Use "alert" for an air-raid warning or all-clear, and
            "explosion" for a strike, an interception, or something down.
  "place"   the NAME of the place the report is about, as a plain place name
            a map would recognise: "Nikopol", "Kharkiv oblast", "Beirut".
            Transliterate to Latin script. Put the place in the nominative,
            not the genitive: "Харківщини" -> "Kharkiv oblast". Null if the
            report names no place.
  "toward"  the NAME of the place it is travelling TO, or null.
  "count"   how many objects, if stated, else 1
  "summary" one short English sentence saying what is being reported, under
            110 characters. This is read aloud on a wall display.

DO NOT return coordinates, latitudes, longitudes or bearings. You are not
asked for them and they will be discarded. Somewhere else turns names into
positions; your job is the words.

Rules:
  - A DIRECTION AND A LOCATION ARE DIFFERENT THINGS. Set "toward" only when
    the report says the object is MOVING somewhere:
        "курс на Полтаву", "у напрямку Києва", "прямують до Дніпра"
    A phrase saying which PART of a region something is in is not a
    destination and "toward" must be null:
        "на північний схід Харківщини" = in the north-east OF Kharkiv oblast.
        That is "place": "Kharkiv oblast", and "toward": null.
  - If the report names no place at all, still return the event with "place"
    null. It will be listed rather than mapped. Do not invent a place.
  - Appeals for donations, channel promotion, and general commentary are not
    events: leave them out entirely.
"""


def _call_model(messages: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(
                [{"id": m["id"], "text": m["text"]} for m in messages],
                ensure_ascii=False)},
        ],
        # The whole point is machine-readable output; asking for it in the
        # response format is cheaper than repairing prose afterwards.
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }
    try:
        resp = requests.post(
            OPENROUTER, json=body, timeout=60,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # OpenRouter asks callers to identify themselves.
                "HTTP-Referer": "https://github.com/Inasjackw321/Sent-2-imagery",
                "X-Title": "EarthViewer",
            })
    except requests.RequestException as exc:
        raise OsintError(f"OpenRouter could not be reached: {exc}") from exc
    if resp.status_code == 401:
        raise OsintError("OpenRouter refused the key.")
    if resp.status_code == 429:
        raise OsintError("OpenRouter is rate limiting — the free tier has a ceiling.")
    if not resp.ok:
        raise OsintError(f"OpenRouter answered {resp.status_code}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise OsintError(f"OpenRouter sent an answer with no content: {exc}") from exc
    return read_events(content)


def read_events(content: str) -> list[dict[str, Any]]:
    """Take the events out of whatever the model actually returned.

    Models wrap JSON in code fences, prefix it with a sentence, or return a
    bare list instead of the object asked for. All three are cheap to survive
    and expensive to be surprised by at three in the morning.
    """
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        # A sentence and then the JSON: take the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise OsintError("the model did not return JSON") from None
        try:
            parsed = json.loads(text[start:end + 1])
        except ValueError as exc:
            raise OsintError(f"the model returned JSON that will not parse: {exc}") from exc

    raw = parsed.get("events") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list):
        return []
    return [clean for clean in (_clean(item) for item in raw) if clean]


# Things a model offers as a place name when it has not got one. Each of these
# has been seen; none of them is somewhere.
NON_PLACES = {
    "unknown", "unspecified", "n/a", "na", "none", "null", "not specified",
    "not stated", "somewhere", "various", "multiple", "several", "-", "?",
}


def _name(value: Any) -> str | None:
    """A place name, or None if what came back is not one."""
    text = " ".join(str(value or "").split())
    if len(text) < 2 or text.lower().strip(".") in NON_PLACES:
        return None
    # A model told not to give coordinates sometimes gives them anyway, in the
    # place-name field. A gazetteer would then look up the literal string and
    # either miss or match something absurd, so they are refused here.
    if re.fullmatch(r"[-+0-9.,°'\"NSEW\s]+", text):
        return None
    return text[:80]


def _clean(item: Any) -> dict[str, Any] | None:
    """One report from the model, checked. Names only -- no positions yet."""
    if not isinstance(item, dict):
        return None

    kind = str(item.get("kind") or "unknown").lower().strip()
    if kind not in KINDS:
        kind = "unknown"

    count = item.get("count")
    count = int(count) if isinstance(count, (int, float)) and 1 <= count <= 999 else 1

    place = _name(item.get("place"))
    toward = _name(item.get("toward"))
    # "Heading for where it already is" is not a journey, and is usually the
    # model filling a field for the sake of it.
    if toward and place and toward.lower() == place.lower():
        toward = None

    summary = " ".join(str(item.get("summary") or "").split())[:160]
    return {
        "id": str(item.get("id") or "")[:120] or None,
        "kind": kind,
        "place": place,
        "toward": toward,
        "count": count,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Placing
# ---------------------------------------------------------------------------

EARTH_KM = 6371.0088


def bearing(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """The compass course from one point to another, in degrees.

    Worked out here rather than asked for, because a report saying "курс на
    Полтаву" has told us the destination and nothing else. Turning that into a
    bearing is spherical trigonometry, which this is good at and a language
    model is not.
    """
    a, b = math.radians(lat), math.radians(to_lat)
    d = math.radians(to_lon - lon)
    y = math.sin(d) * math.cos(b)
    x = math.cos(a) * math.sin(b) - math.sin(a) * math.cos(b) * math.cos(d)
    return math.degrees(math.atan2(y, x)) % 360


def separation(lat: float, lon: float, to_lat: float, to_lon: float) -> float:
    """How far apart two points are, in kilometres. Haversine."""
    a, b = math.radians(lat), math.radians(to_lat)
    dlat = b - a
    dlon = math.radians(to_lon - lon)
    h = (math.sin(dlat / 2) ** 2
         + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2)
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def advance(lat: float, lon: float, heading: float, km: float) -> tuple[float, float]:
    """Where you get to going `km` along a bearing, on a sphere.

    The flat-earth version -- add so many degrees of latitude, so many of
    longitude -- is out by kilometres over the distances a cruise missile
    covers in ten minutes, and wrong in a way that grows with latitude, which
    is where these reports come from.
    """
    if km <= 0:
        return lat, lon
    d = km / EARTH_KM
    a = math.radians(lat)
    brg = math.radians(heading)
    sin_lat = math.sin(a) * math.cos(d) + math.cos(a) * math.sin(d) * math.cos(brg)
    sin_lat = max(-1.0, min(1.0, sin_lat))
    lat2 = math.asin(sin_lat)
    lon2 = math.radians(lon) + math.atan2(
        math.sin(brg) * math.sin(d) * math.cos(a),
        math.cos(d) - math.sin(a) * sin_lat)
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180


def place_event(item: dict[str, Any], countries: str,
                lookup=gazetteer.find) -> dict[str, Any]:
    """Turn a report's place names into positions, if they are known.

    An unplaceable report is not a failure to be swallowed. It comes back with
    `lat` None and a reason, goes into the alert stream, and is counted -- so
    "the gazetteer does not know these names" is visible as itself rather than
    as an empty map.
    """
    out = dict(item)
    out["lat"] = out["lon"] = out["heading"] = None
    out["dest_lat"] = out["dest_lon"] = out["dest_km"] = None
    out["placed"] = False

    if not item.get("place"):
        out["why_unplaced"] = "the report names no place"
        return out

    try:
        here = lookup(item["place"], countries)
    except gazetteer.GazetteerError as exc:
        out["why_unplaced"] = str(exc)
        return out
    if not here:
        out["why_unplaced"] = f'"{item["place"]}" is not in the gazetteer'
        return out

    out["lat"], out["lon"] = here["lat"], here["lon"]
    out["place_match"] = here.get("name")
    out["place_kind"] = here.get("kind")
    out["placed"] = True
    out.pop("why_unplaced", None)

    # Only airborne things travel. A strike or an alert has a place and stays
    # there, whatever else the report happens to mention.
    if not item.get("toward") or item["kind"] in NOT_AIRBORNE:
        return out
    try:
        there = lookup(item["toward"], countries)
    except gazetteer.GazetteerError:
        return out
    if not there:
        return out
    km = separation(here["lat"], here["lon"], there["lat"], there["lon"])
    if km < 1:
        # The gazetteer matched both names to the same point. Nothing to
        # travel, and a bearing between two identical points is noise.
        return out
    out["dest_lat"], out["dest_lon"] = there["lat"], there["lon"]
    out["dest_km"] = round(km, 1)
    out["heading"] = bearing(here["lat"], here["lon"], there["lat"], there["lon"])
    return out


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------


def project(event: dict[str, Any], now: float) -> dict[str, Any]:
    """An event with its marker carried forward to now.

    The reported position is kept alongside the projected one, because they are
    different claims: one is a place a channel named, the other is arithmetic.
    """
    minutes = max(0.0, (now - event["seen"]) / 60)
    speed = SPEEDS.get(event["kind"], SPEEDS["unknown"])
    out = dict(event)
    out["age_minutes"] = round(minutes, 1)
    out["arrived"] = False
    if event.get("heading") is None or speed <= 0:
        out["lat"], out["lon"] = event["origin_lat"], event["origin_lon"]
        out["projected"] = False
        return out

    km = speed * (minutes / 60)
    # A report that named where it was going has also said where the marker
    # stops. Past that point the extrapolation is not merely stale, it is
    # describing a journey the report said was over.
    limit = event.get("dest_km")
    if limit and km >= limit:
        km = limit
        out["arrived"] = True
    out["lat"], out["lon"] = advance(
        event["origin_lat"], event["origin_lon"], event["heading"], km)
    out["projected"] = True
    out["projected_km"] = round(km, 1)
    return out


def _arrived(event: dict[str, Any], now: float) -> bool:
    """Whether a track has got where the report said it was going."""
    limit, speed = event.get("dest_km"), SPEEDS.get(event["kind"], 0.0)
    if not limit or event.get("heading") is None or speed <= 0:
        return False
    return speed * (max(0.0, now - event["seen"]) / 3600) >= limit


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def _when(message: dict[str, Any]) -> float:
    try:
        return dt.datetime.fromisoformat(
            str(message.get("when")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _record(item: dict[str, Any], message: dict[str, Any], countries: str) -> None:
    """One classified report: an alert always, a track only if it placed."""
    global _counter
    seen = _when(message)
    placed = place_event(item, countries)

    _counter += 1
    ident = f"AO{_counter:04d}"
    _alerts.append({
        "id": ident,
        "kind": placed["kind"],
        "rank": KINDS[placed["kind"]]["rank"],
        "summary": placed["summary"] or (placed.get("place") or "Report"),
        "place": placed.get("place"),
        "placed": placed["placed"],
        "why_unplaced": placed.get("why_unplaced"),
        "channel": message.get("channel"),
        "region": message.get("region"),
        "seen": seen,
        "text": message.get("text", "")[:300],
    })

    if not placed["placed"]:
        return
    _events.append({
        **placed,
        "id": ident,
        "origin_lat": placed["lat"],
        "origin_lon": placed["lon"],
        "seen": seen,
        "channel": message.get("channel"),
        "region": message.get("region"),
        "source": message.get("id"),
        "text": message.get("text", "")[:300],
    })


def _expire(now: float) -> None:
    """Drop what is too old to extrapolate, and what has arrived."""
    _events[:] = [e for e in _events
                  if e["seen"] >= now - KEEP_MINUTES * 60
                  and not _arrived(e, now)][-MAX_EVENTS:]
    _alerts[:] = [a for a in _alerts
                  if a["seen"] >= now - ALERT_MINUTES * 60][-MAX_ALERTS:]


def poll() -> dict[str, Any]:
    """Read the channels once, and turn anything new into events."""
    global _state, _last_poll
    with _lock:
        key = _key
    if not key:
        raise OsintError("No OpenRouter key set — the reports cannot be read without one.")

    fresh: list[dict[str, Any]] = []
    trouble: list[str] = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=LOOKBACK_MINUTES)
    for channel in CHANNELS:
        try:
            posts = _fetch_channel(channel["name"])
        except OsintError as exc:
            trouble.append(str(exc))
            continue
        for post in posts:
            post["countries"] = channel["countries"]
            post["region"] = channel["region"]
            if post["id"] in _seen:
                continue
            try:
                when = dt.datetime.fromisoformat(post["when"].replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < cutoff:
                # Old on the first read: remembered so it is not read again,
                # but not turned into a marker.
                _seen.add(post["id"])
                continue
            fresh.append(post)

    now = time.time()
    if fresh:
        # One call for everything new, not one per message: the free tier has a
        # ceiling and six channels can post a dozen times a minute between them.
        batch = fresh[:40]
        found = _call_model(batch, key)
        by_id = {item["id"]: item for item in found if item.get("id")}
        # Geocoding is done outside the lock: it may go to the network, and
        # holding the lock across that would stall every request for the map.
        for post in batch:
            _seen.add(post["id"])
            item = by_id.get(post["id"])
            if item is None and len(batch) == 1 and found:
                # A model that ignored the ids, with only one message to
                # confuse: the single answer can only belong to it.
                item = found[0]
            if item is None:
                continue
            with _lock:
                _record(item, post, post["countries"])
        with _lock:
            _expire(now)
        _state = f"reading {len(CHANNELS)} channels"
    else:
        _state = "nothing new"

    _last_poll = now
    if trouble:
        _state = "; ".join(trouble[:2])
    return current()


def refresh() -> dict[str, Any]:
    """What is in the air, reading the channels again if it is time to.

    Every open tab asks once a minute; only the first one through the door in
    any given minute actually goes to Telegram and to the model. The rest get
    the same events carried forward to their own instant, which is what they
    wanted anyway -- the markers move continuously, the reports do not.
    """
    with _lock:
        due = time.time() - _last_poll >= MIN_POLL_SECONDS
    if not due:
        return current()
    return poll()


def current() -> dict[str, Any]:
    """Every live event and recent alert, carried forward to now."""
    now = time.time()
    with _lock:
        _expire(now)
        events = [project(e, now) for e in _events]
        alerts = sorted(_alerts, key=lambda a: a["seen"], reverse=True)
        # Over the alert window, not since the process started. A running
        # total answers a question nobody asked -- what matters is whether
        # the names coming in tonight are being found.
        placed = sum(1 for a in alerts if a["placed"])
        unplaced = len(alerts) - placed
    return {
        "events": events,
        "count": len(events),
        "alerts": [dict(a) for a in alerts],
        "state": _state,
        "keep_minutes": KEEP_MINUTES,
        "alert_minutes": ALERT_MINUTES,
        "speeds": SPEEDS,
        "kinds": KINDS,
        "channels": [c["name"] for c in CHANNELS],
        "regions": sorted({c["region"] for c in CHANNELS}),
        "model": MODEL,
        "keyed": has_key(),
        "last_poll": _last_poll or None,
        # Said out loud, because an empty map with a healthy feed behind it is
        # the failure the previous version hid.
        "reports": {"placed": placed, "unplaced": unplaced},
        "gazetteer": gazetteer.stats(),
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

# Name, destination, count, kind -- and the coordinates the gazetteer would
# return for them, so the demo runs the real placing code rather than a
# parallel copy of it that could drift out of step.
DEMO_PLACES = {
    "Nikopol": (47.5665, 34.4053), "Kherson": (46.6354, 32.6169),
    "Beryslav": (46.8397, 33.4269), "Odesa": (46.4825, 30.7233),
    "Mykolaiv": (46.9750, 31.9946), "Kharkiv oblast": (49.7, 36.3),
    "Beirut": (33.8938, 35.5018), "Ochakiv": (46.6128, 31.5406),
}

DEMO_SEED = [
    ("drone", "Nikopol", "Kherson", 2, "Two drones over Nikopol heading for Kherson"),
    ("drone", "Beryslav", "Mykolaiv", 1, "Drone over Beryslav on a course for Mykolaiv"),
    ("drone", "Kharkiv oblast", None, 3, "Three drones over Kharkiv oblast"),
    # A hundred kilometres at cruise speed: in flight when the page loads and
    # arriving a couple of minutes later, so the demo shows a marker reaching
    # where it was going and leaving, not only things in transit.
    ("cruise", "Ochakiv", "Odesa", 1, "Cruise missile past Ochakiv towards Odesa"),
    ("explosion", "Kherson", None, 1, "Explosions reported in Kherson"),
    ("alert", "Beirut", None, 1, "Air raid warning for Beirut"),
    # The case the previous version hid: a real report that cannot be placed.
    # It belongs in the alert stream and nowhere else.
    ("drone", "Somewhere unnamed", None, 1, "Drone activity reported, no location given"),
]

# How long the demo runs before starting over. Long enough for the slowest
# track to expire on age, so a full cycle shows every ending there is.
DEMO_CYCLE = (KEEP_MINUTES + 4) * 60

_demo_epoch = 0.0


def _demo_lookup(name: str, countries: str = "") -> dict[str, Any] | None:
    """A gazetteer of eight places, for the build with no network."""
    found = DEMO_PLACES.get(name)
    if not found:
        return None
    return {"lat": found[0], "lon": found[1], "name": name, "kind": "demo"}


def demo() -> dict[str, Any]:
    """Synthetic reports, for the build with no network.

    Anchored to a fixed instant rather than rebuilt against the clock on every
    call: the first version did that, and it quietly made the demo a lie -- the
    events were always the same few minutes old, so nothing ever aged, arrived
    or expired, and none of the endings could be seen.

    Everything here goes through the same place_event, project and expire code
    the live path uses. A demo that reimplements what it is demonstrating
    proves nothing about it.
    """
    global _demo_epoch
    now = time.time()
    with _lock:
        if not _demo_epoch or now - _demo_epoch > DEMO_CYCLE:
            _demo_epoch = now
        epoch = _demo_epoch

    events, alerts = [], []
    for i, (kind, place, toward, count, summary) in enumerate(DEMO_SEED, start=1):
        seen = epoch - i * 90
        item = {"kind": kind, "place": place, "toward": toward,
                "count": count, "summary": summary}
        placed = place_event(item, "ua", lookup=_demo_lookup)
        ident = f"AO{100 + i * 7:04d}"
        alerts.append({
            "id": ident, "kind": kind, "rank": KINDS[kind]["rank"],
            "summary": summary, "place": place, "placed": placed["placed"],
            "why_unplaced": placed.get("why_unplaced"),
            "channel": "demo", "region": "Ukraine", "seen": seen,
            "text": f"Demo report — {summary}.",
        })
        if not placed["placed"]:
            continue
        event = {**placed, "id": ident,
                 "origin_lat": placed["lat"], "origin_lon": placed["lon"],
                 "seen": seen, "channel": "demo", "region": "Ukraine",
                 "source": f"demo/{i}", "text": f"Demo report — {summary}."}
        if _arrived(event, now) or now - seen > KEEP_MINUTES * 60:
            continue
        events.append(project(event, now))

    alerts = [a for a in alerts if now - a["seen"] <= ALERT_MINUTES * 60]
    return {
        "events": events, "count": len(events),
        "alerts": sorted(alerts, key=lambda a: a["seen"], reverse=True),
        "state": "demo — synthetic reports",
        "keep_minutes": KEEP_MINUTES, "alert_minutes": ALERT_MINUTES,
        "speeds": SPEEDS, "kinds": KINDS,
        "channels": [c["name"] for c in CHANNELS],
        "regions": sorted({c["region"] for c in CHANNELS}),
        "model": MODEL, "keyed": True, "last_poll": now,
        # Counted from the reports' own flags, not from how many markers
        # survive. A track that has arrived or aged off the map was placed
        # perfectly well; calling it unplaced would make the gazetteer look
        # worse than it is, which is precisely the number people will read.
        "reports": {"placed": sum(1 for a in alerts if a["placed"]),
                    "unplaced": sum(1 for a in alerts if not a["placed"])},
        "gazetteer": {"remembered": len(DEMO_PLACES), "lookups": 0},
    }
