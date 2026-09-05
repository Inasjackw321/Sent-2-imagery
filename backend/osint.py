"""Air-threat reports from public Telegram channels, put on the map.

Ukraine's air force command and a handful of monitoring channels post a running
commentary of what is in the air: drones crossing an oblast, cruise missiles on
a heading, strikes where they land. It is published to warn people, openly and
deliberately, and this reads the same public pages anyone can open in a browser.

Three parts, and the seams between them matter:

  reading    the channels' public web preview at t.me/s/<name>. No account, no
             API key, no session -- the same HTML a logged-out visitor gets.

  reading    the messages are prose in Ukrainian and Russian, so a language
  meaning    model turns them into structured events: what, where, which way.
             It is asked for JSON and given a strict shape, and anything that
             comes back malformed or without a position is dropped rather than
             guessed at.

  moving     an event has a heading, so its marker is carried forward along it
             between reports. This is dead reckoning from a typical speed for
             the kind of thing it is -- an estimate, not an observation -- and
             everything downstream is built to say so.

That last point is the one to be careful about. A marker sliding across a map
looks like tracking, and it is not: it is a guess extrapolated from one report,
and it gets worse every second until the next one. So tracks expire quickly, the
panel says what is measured and what is inferred, and nothing is ever placed
without a position that came from the message itself.
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

from . import config

# The channels, by their public name. These are read as anyone reads them.
CHANNELS = ("eRadarrua", "kpszsu", "mon1tor_ua", "war_monitor")

# The public web preview. Not the API: this is the page Telegram serves to a
# visitor with no account, and it carries the recent posts as plain HTML.
PREVIEW = "https://t.me/s/{channel}"

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "poolside/laguna-s-2.1:free"

# How long a track stays on the map after the report that made it. Dead
# reckoning decays: after twenty minutes an extrapolated position is a work of
# fiction, so it goes rather than sitting there looking authoritative.
KEEP_MINUTES = 20

# Nothing older than this is worth reading on start-up.
LOOKBACK_MINUTES = 45

# The floor between two reads of the channels. The map asks once a minute, but
# it may be open in three tabs, and every read that finds something new costs a
# model call against a free-tier ceiling. So the interval belongs here, on the
# one thing that knows how long it has been, rather than in each browser.
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

# What each kind looks like on the map. The colours follow the reference the
# map was asked to match: red for the things still in the air, violet for the
# ones already reported down or struck.
KINDS = {
    "drone": {"colour": "#ff3b30", "label": "Drone"},
    "cruise": {"colour": "#ff3b30", "label": "Cruise missile"},
    "ballistic": {"colour": "#ff3b30", "label": "Ballistic"},
    "aircraft": {"colour": "#ff3b30", "label": "Aircraft"},
    "explosion": {"colour": "#b06bff", "label": "Explosion"},
    "unknown": {"colour": "#ff8a3b", "label": "Unidentified"},
}

MAX_EVENTS = 400

_lock = threading.Lock()
_key: str | None = None
_seen: set[str] = set()
_events: list[dict[str, Any]] = []
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

PROMPT = """You convert Ukrainian and Russian air-threat reports into JSON.

Return ONLY a JSON object: {"events": [...]}. One entry per distinct airborne
object or strike that has a definite location. No prose, no code fences.

Each event:
  "kind"     one of: drone, cruise, ballistic, aircraft, explosion, unknown
  "lat"      decimal degrees, the place the report names
  "lon"      decimal degrees
  "place"    the place name as written, transliterated to Latin script
  "heading"  degrees clockwise from north the object is travelling, or null
  "toward"   the place it is heading for, if the report names one, else null
  "count"    how many objects, if stated, else 1
  "text"     the sentence this came from, trimmed to 160 characters

Rules, and they matter more than completeness:
  - Only emit an event if the report names a place you can place on a map.
    Never estimate a position from context. Omit rather than guess.
  - "курс на X" / "у напрямку X" means it is heading toward X: set "toward"
    and work "heading" out from the current place to X.
  - Reports of air-raid alerts, all-clears, statistics, appeals for donations
    and general commentary are not events. Skip them.
  - If a report says something was shot down or has landed, that is
    "explosion" with heading null.
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


def _clean(item: Any) -> dict[str, Any] | None:
    """One event, checked, or None.

    A position that did not come from the report is the thing this exists to
    refuse. Everything else can be defaulted; that cannot.
    """
    if not isinstance(item, dict):
        return None
    lat, lon = item.get("lat"), item.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    # 0,0 is where a model puts a position it does not have.
    if abs(lat) < 0.01 and abs(lon) < 0.01:
        return None

    kind = str(item.get("kind") or "unknown").lower().strip()
    if kind not in KINDS:
        kind = "unknown"

    heading = item.get("heading")
    if isinstance(heading, (int, float)) and math.isfinite(heading):
        heading = float(heading) % 360
    else:
        heading = None

    count = item.get("count")
    count = int(count) if isinstance(count, (int, float)) and 1 <= count <= 999 else 1

    return {
        "kind": kind,
        "lat": float(lat),
        "lon": float(lon),
        "place": str(item.get("place") or "")[:80] or None,
        "heading": heading,
        "toward": str(item.get("toward") or "")[:80] or None,
        "count": count,
        "text": str(item.get("text") or "")[:200],
    }


# ---------------------------------------------------------------------------
# Moving
# ---------------------------------------------------------------------------

EARTH_KM = 6371.0088


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


def project(event: dict[str, Any], now: float) -> dict[str, Any]:
    """An event with its marker carried forward to now.

    The reported position is kept alongside the projected one, because they are
    different claims: one is what a channel said, the other is arithmetic.
    """
    minutes = max(0.0, (now - event["seen"]) / 60)
    speed = SPEEDS.get(event["kind"], SPEEDS["unknown"])
    out = dict(event)
    out["age_minutes"] = round(minutes, 1)
    if event["heading"] is None or speed <= 0:
        out["lat"], out["lon"] = event["origin_lat"], event["origin_lon"]
        out["projected"] = False
        return out
    km = speed * (minutes / 60)
    out["lat"], out["lon"] = advance(
        event["origin_lat"], event["origin_lon"], event["heading"], km)
    out["projected"] = True
    out["projected_km"] = round(km, 1)
    return out


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------


def _record(found: list[dict[str, Any]], message: dict[str, Any]) -> None:
    global _counter
    when = message.get("when")
    try:
        seen = dt.datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        seen = time.time()
    for item in found:
        _counter += 1
        _events.append({
            **item,
            "id": f"AO#{_counter:03d}",
            "origin_lat": item["lat"],
            "origin_lon": item["lon"],
            "seen": seen,
            "channel": message.get("channel"),
            "source": message.get("id"),
        })


def _expire(now: float) -> None:
    cutoff = now - KEEP_MINUTES * 60
    _events[:] = [e for e in _events if e["seen"] >= cutoff][-MAX_EVENTS:]


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
            posts = _fetch_channel(channel)
        except OsintError as exc:
            trouble.append(str(exc))
            continue
        for post in posts:
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
        # ceiling and four channels can post a dozen times a minute between them.
        found = _call_model(fresh[:40], key)
        with _lock:
            by_source: dict[str, list[dict[str, Any]]] = {}
            for item in found:
                by_source.setdefault(item.pop("id", ""), []).append(item)
            for post in fresh:
                _seen.add(post["id"])
                _record(by_source.get(post["id"], found if len(fresh) == 1 else []), post)
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
    """Every live event, carried forward to now."""
    now = time.time()
    with _lock:
        _expire(now)
        events = [project(e, now) for e in _events]
    return {
        "events": events,
        "count": len(events),
        "state": _state,
        "keep_minutes": KEEP_MINUTES,
        "speeds": SPEEDS,
        "kinds": KINDS,
        "channels": list(CHANNELS),
        "model": MODEL,
        "keyed": has_key(),
        "last_poll": _last_poll or None,
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo() -> dict[str, Any]:
    """Synthetic events over the Dnipro, for the build with no network."""
    now = time.time()
    seed = [
        ("drone", 47.62, 34.42, 200, "Nikopol district"),
        ("drone", 47.43, 34.28, 215, "Kherson oblast north"),
        ("drone", 47.35, 33.98, 190, "Beryslav"),
        ("cruise", 46.42, 32.05, 250, "Black Sea coast"),
        ("explosion", 46.55, 32.28, None, "Kherson"),
        ("explosion", 46.38, 31.85, None, "Ochakiv"),
    ]
    events = []
    for i, (kind, lat, lon, heading, place) in enumerate(seed, start=1):
        events.append({
            "id": f"AO#{100 + i * 37:03d}",
            "kind": kind, "lat": lat, "lon": lon,
            "origin_lat": lat, "origin_lon": lon,
            "heading": heading, "toward": None, "place": place, "count": 1,
            "text": f"Demo event over {place}.",
            "seen": now - i * 90, "age_minutes": round(i * 1.5, 1),
            "projected": heading is not None,
            "channel": "demo", "source": f"demo/{i}",
        })
    return {
        "events": events, "count": len(events),
        "state": "demo — synthetic events", "keep_minutes": KEEP_MINUTES,
        "speeds": SPEEDS, "kinds": KINDS, "channels": list(CHANNELS),
        "model": MODEL, "keyed": True, "last_poll": now,
    }
