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

from . import config, gazetteer, reports

# The channels, and what each one is about.
#
# `countries` is the single most valuable thing in this table. A gazetteer
# asked for "Sumy" with no country will happily return a street somewhere
# else; asked for "Sumy" in Ukraine it returns the oblast capital. Two-letter
# ISO codes, as Nominatim wants them.
CHANNELS = (
    # The monitoring channels report on both sides of the border -- Belgorod
    # and Bryansk as much as Sumy -- so their lookups have to be allowed to
    # land there. Ukraine stays first, which is where the weight of their
    # reporting is and which Nominatim's ranking follows.
    {"name": "eRadarrua", "region": "Ukraine", "countries": "ua,ru,by"},
    # The air force's own channel, and it reports on Ukraine.
    {"name": "kpszsu", "region": "Ukraine", "countries": "ua"},
    {"name": "mon1tor_ua", "region": "Ukraine", "countries": "ua,ru,by"},
    {"name": "war_monitor", "region": "Ukraine", "countries": "ua,ru,by"},
    {"name": "redlinkleb", "region": "Lebanon", "countries": "lb,il,sy"},
    {"name": "shin_persian", "region": "Middle East", "countries": "ir,il,lb,sy,iq,ye"},
)

# The public web preview. Not the API: this is the page Telegram serves to a
# visitor with no account, and it carries the recent posts as plain HTML.
PREVIEW = "https://t.me/s/{channel}"

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"

# The models to try, in order, all free. One free model being exhausted is the
# ordinary case rather than the exceptional one -- they share a daily ceiling
# per account and the popular ones reach it first -- so a list beats a single
# name, and the layer only gives up on the model step when every one of them
# has refused.
MODELS = (
    "poolside/laguna-s-2.1:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "qwen/qwen-2.5-72b-instruct:free",
)
MODEL = MODELS[0]

# How long to leave OpenRouter alone after it says no. Doubling each time, up
# to a quarter of an hour.
#
# The version before this had no backoff at all, and worse: it only recorded
# that it had polled AFTER the model answered. So a rate limit meant the next
# page refresh tried again immediately, and every refresh after that, which is
# the one thing guaranteed to keep a rate limit in place.
BACKOFF_START = 120.0
BACKOFF_MAX = 900.0

# How long a marker stays on the map, when its kind does not say otherwise.
#
# Twenty minutes, and the number is about dead reckoning rather than about
# news: a position extrapolated from one report gets worse every second, and
# after twenty minutes it is a work of fiction. It goes rather than sitting
# there looking authoritative.
#
# Which is why it is only the default. That reasoning applies to something in
# flight and to nothing else -- see `keep` in KINDS below.
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

# What each kind is, how fast it goes and how it moves.
#
#   speed   km/h. A rough figure for the type, never a measurement of the
#           object. A Shahed is a propeller aircraft at a couple of hundred; a
#           jet-powered one is three times that; a cruise missile is subsonic
#           and fast; a ballistic one is quicker than anything else here by an
#           order of magnitude and is over in minutes.
#
#   motion  how the marker behaves.
#             "track"  carried along its course, and stops when it arrives
#             "orbit"  circles the place it was reported over
#             "still"  stays exactly where the report put it
#
#           Orbit is the one that is about honesty rather than realism. A
#           reconnaissance drone is on station: it is over somewhere, going
#           round, and it is not going anywhere else. Carrying it off in a
#           straight line for twenty minutes would put it in the next country
#           and claim something the report never said. Circling says "it is
#           here, and it is still flying", which is exactly what was reported.
#
#   keep    how many minutes the marker stays, when it differs from the
#           twenty-minute default.
#
#           The default expires things because their POSITION rots, not
#           because the news does. A marker carried along a course for twenty
#           minutes is describing a journey nobody watched, so it goes. But a
#           strike does not move: where it happened is where it happened, and
#           that is as true six hours later as it was at the time. The only
#           reason to take one off the map is that it has stopped being what
#           is going on, which is a much longer clock.
#
#   rank    orders the alert stream. A strike outranks a drone crossing an
#           oblast, which outranks a warning.
KINDS = {
    "recon":     {"colour": "#4cc2ff", "label": "Recon drone",
                  "speed": 140.0, "motion": "orbit", "rank": 2},
    "drone":     {"colour": "#ff3b30", "label": "Drone",
                  "speed": 180.0, "motion": "track", "rank": 3},
    "jet_drone": {"colour": "#ff3b30", "label": "Jet drone",
                  "speed": 550.0, "motion": "track", "rank": 4},
    "cruise":    {"colour": "#ff6a3b", "label": "Cruise missile",
                  "speed": 850.0, "motion": "track", "rank": 5},
    "ballistic": {"colour": "#ff2d6f", "label": "Ballistic missile",
                  "speed": 3500.0, "motion": "track", "rank": 6},
    "aircraft":  {"colour": "#ffd23b", "label": "Aircraft",
                  "speed": 750.0, "motion": "track", "rank": 3},
    "helicopter": {"colour": "#ffd23b", "label": "Helicopter",
                   "speed": 220.0, "motion": "track", "rank": 2},
    "explosion": {"colour": "#b06bff", "label": "Explosion",
                  "speed": 0.0, "motion": "still", "rank": 7,
                  # Six hours. A strike is a fact about a place rather than a
                  # guess about one, so nothing about it decays -- and the
                  # night's damage read together is most of why anyone opens
                  # this layer. Long enough to hold an evening's worth.
                  "keep": 360},
    "alert":     {"colour": "#ffb020", "label": "Air alert",
                  "speed": 0.0, "motion": "still", "rank": 1},
    "unknown":   {"colour": "#ff8a3b", "label": "Unidentified",
                  "speed": 200.0, "motion": "track", "rank": 2},
}

# Kept as its own table because the browser wants it as one, and because
# reading a speed out of the same place both ends do is how the two stay in
# step. Everything here is derived, never edited on its own.
SPEEDS = {name: look["speed"] for name, look in KINDS.items()}
MOTION = {name: look["motion"] for name, look in KINDS.items()}
KEEP = {name: look.get("keep", KEEP_MINUTES) for name, look in KINDS.items()}

# Kinds that are announcements or places rather than things in flight.
NOT_AIRBORNE = tuple(name for name, look in KINDS.items() if look["motion"] == "still")

# The orbit a reconnaissance drone is drawn flying. Not a measurement of
# anything -- a real racetrack is longer and not a circle -- but the size and
# the pace are chosen to read as "on station over here" at the zoom these are
# looked at, which is all the marker is claiming.
ORBIT_KM = 9.0
ORBIT_MINUTES = 7.0

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
# When OpenRouter may be asked again, and how long the last rest was.
_resting_until = 0.0
_resting_for = 0.0
_model_in_use = MODEL


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
  "kind"    one of: recon, drone, jet_drone, cruise, ballistic, aircraft,
            helicopter, explosion, alert, unknown.
              recon      a reconnaissance or observation UAV, one that
                         loiters: "розвідувальний БпЛА", "Orlan", "ZALA",
                         "Supercam", "борт-розвідник"
              jet_drone  a jet-powered one: "реактивний БпЛА", "Shahed-238"
              drone      any other one-way attack UAV: "Shahed", "Geran"
              alert      an air-raid warning or an all-clear
              explosion  a strike, an interception, or something brought down
  "place"   the NAME of the place the report is about, as a plain place name
            a map would recognise: "Nikopol", "Kharkiv oblast", "Beirut".
            Transliterate to Latin script, and use the standard English
            spelling of the town: "Кагарлик" is "Kaharlyk". Put the place in
            the nominative, not the genitive: "Харківщини" -> "Kharkiv
            oblast". Null if the report names no place.
  "region"  the oblast, governorate or province the place is in, if the
            report says or if you know it: "Kyiv oblast". Null otherwise.
            This is used to tell places with similar names apart, so it
            matters more than it looks.
  "toward"  the NAME of the place it is travelling TO, or null.
  "course"  the compass direction it is travelling, when the report gives one
            and names no destination. One of: N, NE, E, SE, S, SW, W, NW,
            NNE, ENE, ESE, SSE, SSW, WSW, WNW, NNW. Null otherwise.
  "count"   how many objects, if stated, else 1
  "summary" one short English sentence saying what is being reported, under
            110 characters. This is read aloud on a wall display.

DO NOT return coordinates, latitudes, longitudes or bearings. You are not
asked for them and they will be discarded. Somewhere else turns names into
positions; your job is the words.

Rules:
  - A DIRECTION AND A LOCATION ARE DIFFERENT THINGS. Set "toward" or
    "course" only when the report says the object is MOVING:
        "курс на Полтаву", "у напрямку Києва"   -> toward: "Poltava"/"Kyiv"
        "курсом на північ", "рухаються на південь" -> course: "N" / "S"
    A phrase saying which PART of a region something is in is not a
    direction, and both must be null:
        "на північний схід Харківщини" = in the north-east OF Kharkiv oblast.
        That is "place": "Kharkiv oblast", "toward" and "course" both null.
  - "повз X курсом на північ" means it is passing X and heading north:
        "place": "X", "course": "N", "toward": null.
  - If the report names no place at all, still return the event with "place"
    null. It will be listed rather than mapped. Do not invent a place.
  - Appeals for donations, channel promotion, and general commentary are not
    events: leave them out entirely.
"""


class RateLimited(OsintError):
    """OpenRouter said no for now. Different from a broken key or a bad reply.

    Its own class because the caller does something specific about it: backs
    off, and falls through to reading the reports without a model rather than
    showing an empty map.
    """


def _ask_model(model: str, messages: list[dict[str, Any]],
               key: str) -> list[dict[str, Any]]:
    body = {
        "model": model,
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
    if resp.status_code in (402, 429):
        raise RateLimited(retry_after(resp), model)
    if resp.status_code >= 500:
        raise RateLimited(60.0, model)
    if not resp.ok:
        raise OsintError(f"OpenRouter answered {resp.status_code}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise OsintError(f"OpenRouter sent an answer with no content: {exc}") from exc
    # A 200 whose content is null. It happens: a model that answers with a
    # tool call, a reasoning-only reply, or a refusal leaves the field empty
    # rather than omitting it, so the key is present and the value is None.
    # This used to reach read_events and raise AttributeError -- which is not
    # an OsintError, so it went straight past every handler and 500ed the
    # endpoint instead of falling back to reading the reports without it.
    if not isinstance(content, str) or not content.strip():
        raise OsintError(f"{model} answered with no content")
    return read_events(content)


def retry_after(resp: Any) -> float:
    """How long the service asked to be left alone for, in seconds.

    Honoured rather than guessed at. OpenRouter sends `Retry-After` in
    seconds, or `X-RateLimit-Reset` as a millisecond timestamp of when the
    window rolls over; either beats a number invented here, and ignoring both
    is how a client keeps its own rate limit alive.
    """
    headers = getattr(resp, "headers", {}) or {}
    plain = headers.get("Retry-After") or headers.get("retry-after")
    if plain:
        try:
            return max(1.0, min(3600.0, float(str(plain).strip())))
        except ValueError:
            pass
    resets = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if resets:
        try:
            # Milliseconds since the epoch, so seconds if it is implausibly
            # large for a duration.
            value = float(str(resets).strip())
            when = value / 1000 if value > 1e11 else value
            if when > time.time():
                return max(1.0, min(3600.0, when - time.time()))
        except ValueError:
            pass
    return 0.0


def _call_model(messages: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Read the messages with whichever free model will answer.

    Free models share a daily ceiling and the popular ones reach it first, so
    one refusing is routine. Only when every one of them has refused is this a
    rate limit as far as the caller is concerned.
    """
    global _model_in_use
    asked, wait = [], 0.0
    for model in MODELS:
        try:
            found = _ask_model(model, messages, key)
        except RateLimited as exc:
            asked.append(model.split("/")[-1])
            wait = max(wait, float(exc.args[0] or 0))
            continue
        _model_in_use = model
        return found
    raise RateLimited(wait, ", ".join(asked))

def read_events(content: str) -> list[dict[str, Any]]:
    """Take the events out of whatever the model actually returned.

    Models wrap JSON in code fences, prefix it with a sentence, or return a
    bare list instead of the object asked for. All three are cheap to survive
    and expensive to be surprised by at three in the morning.
    """
    text = content.strip() if isinstance(content, str) else ""
    if not text:
        raise OsintError("the model returned nothing")
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


# The sixteen points of the compass, as degrees. A report saying "курсом на
# північ" has given a course as surely as one naming a town, and the version
# that only understood destinations drew it as a stationary burst -- a thing
# that had landed, which is the opposite of what it said.
COMPASS = {
    "n": 0, "nne": 22.5, "ne": 45, "ene": 67.5,
    "e": 90, "ese": 112.5, "se": 135, "sse": 157.5,
    "s": 180, "ssw": 202.5, "sw": 225, "wsw": 247.5,
    "w": 270, "wnw": 292.5, "nw": 315, "nnw": 337.5,
}

# Written out, in case the model answers in words rather than letters.
COMPASS_WORDS = {
    "north": "n", "north-east": "ne", "northeast": "ne", "east": "e",
    "south-east": "se", "southeast": "se", "south": "s",
    "south-west": "sw", "southwest": "sw", "west": "w",
    "north-west": "nw", "northwest": "nw",
    "north-north-east": "nne", "east-north-east": "ene",
    "east-south-east": "ese", "south-south-east": "sse",
    "south-south-west": "ssw", "west-south-west": "wsw",
    "west-north-west": "wnw", "north-north-west": "nnw",
}


def read_course(value: Any) -> float | None:
    """A compass course as degrees, or None if that is not what this is."""
    text = " ".join(str(value or "").split()).lower().strip(" .")
    if not text:
        return None
    text = COMPASS_WORDS.get(text, text)
    return float(COMPASS[text]) if text in COMPASS else None


def _clean(item: Any) -> dict[str, Any] | None:
    """One report from the model, checked. Names only -- no positions yet."""
    if not isinstance(item, dict):
        return None

    kind = str(item.get("kind") or "unknown").lower().strip().replace("-", "_")
    if kind not in KINDS:
        kind = "unknown"

    count = item.get("count")
    count = int(count) if isinstance(count, (int, float)) and 1 <= count <= 999 else 1

    place = _name(item.get("place"))
    region = _name(item.get("region"))
    toward = _name(item.get("toward"))
    # "Heading for where it already is" is not a journey, and is usually the
    # model filling a field for the sake of it.
    if toward and place and toward.lower() == place.lower():
        toward = None

    course = read_course(item.get("course"))
    # A thing that is not in flight has no course, whatever the sentence
    # around it happened to mention.
    if kind in NOT_AIRBORNE:
        course, toward = None, None

    summary = " ".join(str(item.get("summary") or "").split())[:160]
    return {
        "id": str(item.get("id") or "")[:120] or None,
        "kind": kind,
        "place": place,
        "region": region,
        "toward": toward,
        "course": course,
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


# How wide an alert is drawn when the gazetteer gave no extent to go on, by
# what sort of place it matched. Round numbers, and deliberately modest: an
# area drawn too large claims a warning covers ground nobody mentioned.
AREA_FALLBACK_KM = {
    "administrative": 45.0, "state": 45.0, "region": 45.0, "province": 45.0,
    "county": 25.0, "district": 25.0, "municipality": 15.0,
    "city": 12.0, "town": 6.0, "village": 3.0, "hamlet": 2.0,
    "suburb": 3.0, "neighbourhood": 2.0,
}
AREA_DEFAULT_KM = 8.0
AREA_MAX_KM = 120.0


# What counts as a region rather than a place within one. These are
# Nominatim's own words for an administrative area of some size; a town, a
# village or a suburb is not one however large it happens to be.
REGION_KINDS = ("administrative", "state", "region", "province", "county",
                "district", "governorate", "emirate", "country")


def is_region(place: dict[str, Any]) -> bool:
    """Whether a match is an area in its own right rather than a spot in one."""
    if place.get("category") == "boundary":
        return True
    return str(place.get("kind") or "").lower() in REGION_KINDS


def area_km(place: dict[str, Any]) -> float:
    """How far around a place an alert reaches, in kilometres.

    Taken from the extent the gazetteer reported rather than assumed, because
    the difference between the two cases this has to cover is enormous: a
    strike in a village is a couple of kilometres and an air alert over an
    oblast is a hundred. Drawing both the same size would either lose the
    strike in a blob or shrink the oblast to a dot.

    Half the diagonal of the bounding box, which for a roughly square oblast
    is close and for a long thin one is generous. Capped, because a country's
    own bounding box would otherwise shade a continent.
    """
    box = place.get("bbox")
    if box:
        south, north, west, east = box
        across = separation(south, west, north, east)
        if across > 0:
            return round(min(AREA_MAX_KM, max(1.5, across / 2)), 1)
    return AREA_FALLBACK_KM.get(str(place.get("kind") or "").lower(), AREA_DEFAULT_KM)


def _look(lookup, name: str, region: str | None, countries: str):
    """Find a place, using the region the report gave to tell it apart.

    This is the other half of the Kaharlyk failure. "Kaharlyk" on its own is a
    town in Kyiv oblast and also a handful of smaller things elsewhere;
    "Kaharlyk, Kyiv oblast" is one of them. Asking with the region first costs
    a lookup that is almost always cached and removes a whole class of
    confident wrong answers.

    The bare name is still tried afterwards, because the region may be one the
    gazetteer spells differently, and a right town found without it beats no
    town at all.
    """
    if region and region.lower() not in name.lower():
        found = lookup(f"{name}, {region}", countries)
        if found:
            return found
    # The name as written first, then the de-inflected forms. In that order on
    # purpose: what the report actually said is the best thing to ask for, and
    # a guess at its nominative is only worth trying once that has failed.
    for attempt in reports.variants(name):
        found = lookup(attempt, countries)
        if found:
            return found
    return None


def place_event(item: dict[str, Any], countries: str, lookup=None) -> dict[str, Any]:
    """Turn a report's place names into positions, if they are known.

    An unplaceable report is not a failure to be swallowed. It comes back with
    `lat` None and a reason, goes into the alert stream, and is counted -- so
    "the gazetteer does not know these names" is visible as itself rather than
    as an empty map.
    """
    # Resolved here rather than as a default argument. A default is bound
    # once, when the function is defined, so a gazetteer swapped out later --
    # in a test, or for the demo -- was silently ignored and the real one
    # called instead.
    lookup = lookup or gazetteer.find

    out = dict(item)
    out["lat"] = out["lon"] = out["heading"] = None
    out["dest_lat"] = out["dest_lon"] = out["dest_km"] = None
    out["motion"] = "still"
    out["placed"] = False

    if not item.get("place"):
        out["why_unplaced"] = "the report names no place"
        return out

    try:
        here = _look(lookup, item["place"], item.get("region"), countries)
    except gazetteer.GazetteerError as exc:
        out["why_unplaced"] = str(exc)
        return out
    if not here:
        out["why_unplaced"] = f'"{item["place"]}" is not in the gazetteer'
        return out

    out["lat"], out["lon"] = here["lat"], here["lon"]
    out["place_match"] = here.get("name")
    out["place_kind"] = here.get("kind")
    out["place_category"] = here.get("category")
    out["area_km"] = area_km(here)
    # The real outline, kept only where the report named a REGION. A strike in
    # a town is a point in it, not the whole town, and drawing the municipal
    # boundary round one would claim the damage followed the council's border.
    # A warning over an oblast is genuinely about that oblast, and there the
    # boundary is the honest shape -- a circle over the middle both misses
    # ground the warning covers and covers ground it does not.
    # The outline of the region, where the report named one -- and the reason
    # it is drawn depends on what was reported, because there are two quite
    # different things to say and one shape to say them with:
    #
    #   "covers"   a warning or a strike ACROSS a region. The whole area is
    #              under it, and that is a claim about the ground.
    #
    #   "located"  something in flight, reported only to oblast precision.
    #              The region is not under anything; it is how precisely
    #              anybody knows where the thing is.
    #
    # The second is the one this was missing. A drone reported over an oblast
    # was drawn as a marker on the oblast's centroid, which says its position
    # is known to a few kilometres when the report located it to a couple of
    # hundred. Showing the region says what was actually known.
    out["shape"] = here.get("shape") if is_region(here) else None
    out["region_scope"] = (
        "covers" if MOTION.get(item["kind"], "track") == "still" else "located"
    ) if out["shape"] else None
    # Kept meaning what it always meant: the whole region is under this.
    out["region_wide"] = out["region_scope"] == "covers"
    out["placed"] = True
    out["motion"] = MOTION.get(item["kind"], "track")
    out.pop("why_unplaced", None)

    # Something on station is not going anywhere, so a course would be a
    # claim the report did not make. It circles instead.
    if out["motion"] != "track":
        return out

    # A compass course is a real answer and is used when no destination was
    # named. A destination is better, so it wins where there is one.
    out["heading"] = item.get("course")

    if not item.get("toward"):
        return out
    try:
        there = _look(lookup, item["toward"], item.get("region"), countries)
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
    motion = event.get("motion") or MOTION.get(event["kind"], "track")
    out = dict(event)
    out["age_minutes"] = round(minutes, 1)
    out["arrived"] = False

    if motion == "orbit" and speed > 0:
        # Round and round the place it was reported over. The marker points
        # along the circle, which is the tangent -- a quarter turn ahead of
        # where it is on the ring.
        angle = (360.0 * minutes / ORBIT_MINUTES) % 360.0
        out["lat"], out["lon"] = advance(
            event["origin_lat"], event["origin_lon"], angle, ORBIT_KM)
        out["heading"] = (angle + 90) % 360
        out["projected"] = True
        out["orbiting"] = True
        return out

    if event.get("heading") is None or speed <= 0 or motion != "track":
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
    motion = event.get("motion") or MOTION.get(event["kind"], "track")
    # Something circling has nowhere to arrive at; it leaves on age like
    # anything else, but it never finishes a journey it was not on.
    if motion != "track" or not limit or event.get("heading") is None or speed <= 0:
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
        "by": item.get("by", "model"),
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
        "by": item.get("by", "model"),
        "origin_lat": placed["lat"],
        "origin_lon": placed["lon"],
        "seen": seen,
        "channel": message.get("channel"),
        "region": message.get("region"),
        "source": message.get("id"),
        "text": message.get("text", "")[:300],
    })


def keep_minutes(kind: str) -> int:
    """How long a marker of this kind stays on the map."""
    return KEEP.get(kind, KEEP_MINUTES)


def _alive(event: dict[str, Any], now: float) -> bool:
    return event["seen"] >= now - keep_minutes(event["kind"]) * 60


def _expire(now: float) -> None:
    """Drop what is too old to extrapolate, and what has arrived."""
    _events[:] = [e for e in _events
                  if _alive(e, now) and not _arrived(e, now)][-MAX_EVENTS:]
    # A report must not leave the list while its marker is still on the map:
    # a burst over a town with nothing in the panel to explain it is worse
    # than either on its own. So the stream holds each kind at least as long
    # as the map does.
    _alerts[:] = [a for a in _alerts
                  if a["seen"] >= now - max(ALERT_MINUTES,
                                            keep_minutes(a["kind"])) * 60][-MAX_ALERTS:]


def reset() -> None:
    """Forget everything read so far. For tests and for starting over."""
    global _counter, _state, _last_poll, _resting_until, _resting_for
    with _lock:
        _seen.clear()
        _events.clear()
        _alerts.clear()
        _counter = 0
        _state = "not started"
        _last_poll = _resting_until = _resting_for = 0.0


def _rest(now: float, asked: float) -> None:
    """Leave OpenRouter alone for a while, doubling each time it says no.

    The service's own Retry-After wins where it sent one -- it knows when its
    window rolls over and this does not.
    """
    global _resting_until, _resting_for
    grown = min(BACKOFF_MAX, max(BACKOFF_START, _resting_for * 2))
    _resting_for = grown
    _resting_until = now + max(grown, asked)


def _wake() -> None:
    """The model answered, so forget the backoff."""
    global _resting_until, _resting_for
    _resting_until = _resting_for = 0.0


def poll() -> dict[str, Any]:
    """Read the channels once, and turn anything new into events."""
    global _state, _last_poll
    with _lock:
        key = _key

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
    if not fresh:
        _state = "; ".join(trouble[:2]) if trouble else "nothing new"
        _last_poll = now
        return current()

    # One call for everything new, not one per message: the free tier has a
    # ceiling and six channels can post a dozen times a minute between them.
    batch = fresh[:40]
    found: list[dict[str, Any]] = []
    limited = ""

    if not key:
        # Perfectly workable. The model reads these better, but it is not
        # required to read them at all, and demanding a key before showing
        # anything made the layer look broken to anyone who had not signed up
        # for one.
        limited = "no OpenRouter key"
    elif _resting_until > now:
        limited = f"waiting {round((_resting_until - now) / 60)} min before asking again"
    else:
        try:
            found = _call_model(batch, key)
        except RateLimited as exc:
            limited = f"every free model is rate limited ({exc.args[1]})"
            _rest(now, float(exc.args[0] or 0))
        except OsintError as exc:
            limited = str(exc)
            _rest(now, 0.0)
        else:
            _wake()

    by_id = {item["id"]: item for item in found if item.get("id")}

    # Geocoding is done outside the lock: it may go to the network, and
    # holding the lock across that would stall every request for the map.
    read_by_rules = 0
    for post in batch:
        _seen.add(post["id"])
        item = by_id.get(post["id"])
        if item is None and len(batch) == 1 and found:
            # A model that ignored the ids, with only one message to
            # confuse: the single answer can only belong to it.
            item = found[0]
        if item is None:
            # No model reading for this one -- it is resting, it refused, or
            # it skipped the message. Read it here instead.
            #
            # This is the difference between a quiet night and a dead layer.
            # Before this, a rate limit meant an empty map and a line of red
            # text, which from the outside is indistinguishable from the
            # feature being broken. These reports are formulaic enough to read
            # without a model, so they are.
            plain = reports.read(post.get("text", ""))
            if not plain:
                continue
            item = _clean({**plain, "id": post["id"]})
            if item is None:
                continue
            item["by"] = "rules"
            read_by_rules += 1
        else:
            item["by"] = "model"
        with _lock:
            _record(item, post, post["countries"])

    with _lock:
        _expire(now)

    if limited and read_by_rules:
        _state = (f"{limited} — reading {read_by_rules} of {len(batch)} reports "
                  "without it")
    elif limited:
        _state = limited
    else:
        _state = f"reading {len(CHANNELS)} channels"
    if trouble:
        _state = "; ".join([*trouble[:1], _state])

    _last_poll = now
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
        "keep": KEEP,
        "alert_minutes": ALERT_MINUTES,
        "speeds": SPEEDS,
        "kinds": KINDS,
        # The browser recomputes the same motion between polls, so it needs
        # the same numbers. Sent rather than duplicated there.
        "orbit": {"km": ORBIT_KM, "minutes": ORBIT_MINUTES},
        "channels": [c["name"] for c in CHANNELS],
        "regions": sorted({c["region"] for c in CHANNELS}),
        "model": _model_in_use,
        "models": list(MODELS),
        "keyed": has_key(),
        "read_by": {
            "model": sum(1 for a in alerts if a.get("by") == "model"),
            "rules": sum(1 for a in alerts if a.get("by") == "rules"),
        },
        "resting_seconds": max(0, round(_resting_until - now)),
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
    "Kaharlyk": (49.8556, 30.8125), "Zaporizhzhia": (47.8388, 35.1396),
    "Kyiv oblast": (50.05, 30.75),
    # The Russian side, so the demo shows it being treated exactly the same.
    "Белгородская область": (50.72, 37.5),
}

DEMO_SEED = [
    # kind, place, toward, course, count, summary
    ("drone", "Nikopol", "Kherson", None, 2,
     "Two drones over Nikopol heading for Kherson"),
    # A course and no destination -- the case that drew a stationary burst
    # over a town it was flying past, which is what prompted all of this.
    ("jet_drone", "Kaharlyk", None, "N", 1,
     "Jet drone past Kaharlyk on a course north"),
    # On station: it circles rather than setting off across the country.
    ("recon", "Zaporizhzhia", None, None, 1,
     "Reconnaissance drone loitering over Zaporizhzhia"),
    # Located to an oblast and nowhere finer, which is most of what these
    # channels actually post. The demo needs one so the "somewhere in this
    # region" band is drawn at all in the build with no network.
    ("drone", "Kharkiv oblast", None, None, 3,
     "Three drones over Kharkiv oblast"),
    # A hundred kilometres at cruise speed: in flight when the page loads and
    # arriving a couple of minutes later, so the demo shows a marker reaching
    # where it was going and leaving, not only things in transit.
    ("cruise", "Ochakiv", "Odesa", None, 1,
     "Cruise missile past Ochakiv towards Odesa"),
    # A course and no destination, so it never arrives and stays for its full
    # twenty minutes -- which is what makes its own drawing checkable at all.
    ("cruise", "Nikopol", None, "W", 2, "Two cruise missiles past Nikopol, heading west"),
    ("explosion", "Kherson", None, None, 1, "Explosions reported in Kherson"),
    # Four hours old: two thirds of the way through a strike's six, so the
    # demo shows a faded one beside a fresh one. Without it the build with no
    # network only ever draws markers at full strength, and whether an old
    # strike reads as old could not be checked at all.
    ("explosion", "Zaporizhzhia", None, None, 1,
     "Earlier strike reported in Zaporizhzhia", 240),
    ("alert", "Beirut", None, None, 1, "Air raid warning for Beirut"),
    # A warning covering a whole region rather than a town, so the demo shows
    # the boundary being drawn instead of a circle over the middle of it.
    ("alert", "Kyiv oblast", None, None, 1, "Air raid warning across Kyiv oblast"),
    # A Russian-side report, read and drawn by the same code as every other:
    # same kinds, same region outline, same everything.
    ("alert", "Белгородская область", None, None, 1,
     "UAV danger across Belgorod oblast"),
    # The case the previous version hid: a real report that cannot be placed.
    # It belongs in the alert stream and nowhere else.
    ("drone", "Somewhere unnamed", None, None, 1,
     "Drone activity reported, no location given"),
]


# How long the demo runs before starting over.
#
# Long enough for everything in flight to expire on age, so a cycle shows
# every ending a track has: arriving, and timing out. Deliberately NOT long
# enough to outlive a strike -- that would mean six hours of demo with nothing
# moving on it after the first twenty minutes. The strikes simply carry over
# from one cycle to the next, which is what they do in the real thing too.
DEMO_CYCLE = (KEEP_MINUTES + 4) * 60

_demo_epoch = 0.0


# Roughly how big each demo place is, so the alert areas differ the way real
# ones do: a strike in a town is a few kilometres across and a warning over an
# oblast is most of a region.
DEMO_EXTENT = {"Kharkiv oblast": 1.6, "Beirut": 0.09, "Kyiv oblast": 1.3,
               "Белгородская область": 1.1}


def _demo_ring(lat: float, lon: float, half: float) -> dict[str, Any]:
    """A rough outline for a demo region: a lumpy ring, not a rectangle.

    Lumpy on purpose. A neat box would look like a bounding box and would not
    show whether the page is drawing a real boundary or falling back to one.
    """
    ring = []
    for i in range(24):
        angle = 2 * math.pi * i / 24
        wobble = half * (0.72 + 0.28 * math.cos(3 * angle))
        ring.append([round(lon + wobble * math.cos(angle) * 1.5, 4),
                     round(lat + wobble * math.sin(angle), 4)])
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _demo_lookup(name: str, countries: str = "") -> dict[str, Any] | None:
    """A small gazetteer, for the build with no network."""
    found = DEMO_PLACES.get(name)
    if not found:
        return None
    lat, lon = found
    half = DEMO_EXTENT.get(name, 0.06)
    region = half > 1
    return {
        "lat": lat, "lon": lon, "name": name,
        "kind": "administrative" if region else "town",
        "category": "boundary" if region else "place",
        "bbox": [lat - half, lat + half, lon - half, lon + half],
        "shape": _demo_ring(lat, lon, half) if region else None,
    }


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
    for i, row in enumerate(DEMO_SEED, start=1):
        kind, place, toward, course, count, summary = row[:6]
        # Staggered by position, unless the row says how old it should be.
        seen = epoch - (row[6] * 60 if len(row) > 6 else i * 90)
        item = {"kind": kind, "place": place, "toward": toward,
                "course": read_course(course), "region": None,
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
        if _arrived(event, now) or not _alive(event, now):
            continue
        events.append(project(event, now))

    alerts = [a for a in alerts
              if now - a["seen"] <= max(ALERT_MINUTES, keep_minutes(a["kind"])) * 60]
    return {
        "events": events, "count": len(events),
        "alerts": sorted(alerts, key=lambda a: a["seen"], reverse=True),
        "state": "demo — synthetic reports",
        "keep_minutes": KEEP_MINUTES, "keep": KEEP,
        "alert_minutes": ALERT_MINUTES,
        "speeds": SPEEDS, "kinds": KINDS,
        "orbit": {"km": ORBIT_KM, "minutes": ORBIT_MINUTES},
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
