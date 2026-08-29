"""Turning Telegram messages into pins on a map.

Three steps, and the honesty of the third is the whole thing.

  1. **What happened.** A small local model reads the message and says which
     of a handful of event types it reports. If no model is running, a word
     list does the same job less well, and every alert records which of the two
     read it so a crude answer is never mistaken for a careful one.

  2. **Where.** The model returns a place *name*, not coordinates -- asking a
     1B model for latitude and longitude gets you plausible numbers that are
     wrong, which is the worst possible failure for something drawn on a map.
     The name is then looked up in a real gazetteer.

  3. **Whether to draw it at all.** A message with no place, or a place the
     gazetteer does not know, is kept and listed but not pinned. A pin is a
     claim about a location and there is no honest way to make one up.

Nothing here is precision geolocation. A pin sits on the centroid of a named
town, which may be several kilometres from whatever the message describes, and
the alert says so.
"""

from __future__ import annotations

import datetime as dt
import threading
import time

import requests

from . import config, llm

# How many alerts are kept at once. Older ones fall off the end: this is a
# live picture, not an archive, and an unbounded list in memory is a leak.
MAX_ALERTS = 500

# A place name looked up once stays looked up. Nominatim asks for no more than
# one request a second and these channels repeat the same towns endlessly.
GEOCODE_SECONDS = 7 * 24 * 3600
GEOCODE_GAP = 1.1

# Kinds, with the marker each one draws. The glyph is drawn in the browser;
# this is the vocabulary both ends agree on.
KINDS = {
    "explosion": {"label": "Explosion", "colour": "#ff5f4d"},
    "missile": {"label": "Missile", "colour": "#ff2d55"},
    "drone": {"label": "Drone", "colour": "#ffb454"},
    "aircraft": {"label": "Aircraft", "colour": "#4cc2ff"},
    "artillery": {"label": "Artillery", "colour": "#ff8f5e"},
    "air_defence": {"label": "Air defence", "colour": "#37e0a0"},
    "alert": {"label": "Air raid alert", "colour": "#c77dff"},
    "other": {"label": "Other", "colour": "#93a1b8"},
}

# The fallback reader. Crude on purpose: it exists so the feature still does
# something without a model, and it is never allowed to pretend otherwise.
#
# Ordered, because a message saying a drone was shot down is about air defence
# and a message saying a drone is inbound is about a drone -- and the first
# match wins, so the more specific phrases come first.
WORDS = [
    ("air_defence", ["ппо", "пво", "сбит", "збит", "shot down",
                     "air defence", "air defense", "перехопл", "перехвач"]),
    ("missile", ["ракет", "raket", "missile", "калибр", "кинжал", "искандер",
                 "iskander", "kalibr", "крилат"]),
    ("drone", ["шахед", "shahed", "бпла", "дрон", "drone", "uav", "герань",
               "безпілотн", "беспилотн"]),
    ("explosion", ["вибух", "взрыв", "explosion", "blast", "детонац"]),
    ("artillery", ["обстріл", "обстрел", "shelling", "artillery", "града",
                   "мінометн", "минометн"]),
    ("aircraft", ["злет", "взлет", "взліт", "takeoff", "took off", "борт",
                  "літак", "самолет", "aircraft", "ту-95", "миг-31", "міг-31"]),
    ("alert", ["тривог", "тревог", "air raid", "alert", "укриття", "укрытие"]),
]

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

_lock = threading.Lock()
_alerts: list[dict] = []
_seen: set[str] = set()
_places: dict[str, tuple[float, dict | None]] = {}
_last_geocode = 0.0


class AlertError(RuntimeError):
    pass


# ── Reading a message ──────────────────────────────────────────


def classify(text: str) -> dict:
    """What kind of event a message reports, and where it says it happened."""
    read = llm.read(text)
    if read and (read["place"] or read["kind"] != "other"):
        return read
    return by_words(text)


def by_words(text: str) -> dict:
    """The fallback: match known words, and never guess at a place.

    Deliberately returns no place at all. Pulling a town name out of free text
    with a regular expression works for the cases you thought of and invents
    nonsense for the rest, and a wrong pin is worse than no pin.
    """
    low = (text or "").lower()
    for kind, words in WORDS:
        if any(word in low for word in words):
            return {"kind": kind, "place": "", "confident": False, "by": "words"}
    return {"kind": "other", "place": "", "confident": False, "by": "words"}


# ── Finding the place ──────────────────────────────────────────


def locate(place: str) -> dict | None:
    """A place name to a position, or None if the gazetteer does not know it."""
    name = (place or "").strip()
    if len(name) < 2:
        return None

    key = name.lower()
    with _lock:
        held = _places.get(key)
    if held and time.time() - held[0] < GEOCODE_SECONDS:
        return held[1]

    global _last_geocode
    with _lock:
        wait = GEOCODE_GAP - (time.time() - _last_geocode)
    if wait > 0:
        # Nominatim asks for a second between requests and it is free, so it
        # gets one.
        time.sleep(min(wait, GEOCODE_GAP))
    with _lock:
        _last_geocode = time.time()

    found = None
    try:
        resp = _session.get(
            config.NOMINATIM_URL,
            params={"q": name, "format": "json", "limit": 1},
            timeout=12,
        )
        resp.raise_for_status()
        rows = resp.json()
        if rows:
            row = rows[0]
            found = {
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
                "matched": row.get("display_name", name),
                # What kind of thing was matched, which is how a reader can
                # tell a town centre from a whole oblast.
                "scale": row.get("addresstype") or row.get("type") or "place",
            }
    except (requests.RequestException, ValueError, KeyError, IndexError):
        found = None

    with _lock:
        _places[key] = (time.time(), found)
    return found


# ── The list of alerts ─────────────────────────────────────────


def add(message: dict) -> dict:
    """Read one Telegram message and keep it. Returns the alert."""
    text = (message.get("text") or "").strip()
    read = classify(text)
    where = locate(read["place"]) if read["place"] else None

    alert = {
        "id": message["id"],
        "channel": message.get("channel", ""),
        "channel_title": message.get("channel_title", ""),
        "at": message.get("at") or _now(),
        "text": text[:600],
        "kind": read["kind"],
        "label": KINDS.get(read["kind"], KINDS["other"])["label"],
        "place": read["place"],
        "read_by": read["by"],
        "confident": bool(read["confident"]),
        "lat": where["lat"] if where else None,
        "lon": where["lon"] if where else None,
        "matched": where["matched"] if where else None,
        "scale": where["scale"] if where else None,
        "url": message.get("url"),
    }

    with _lock:
        _alerts.insert(0, alert)
        _seen.add(alert["id"])
        del _alerts[MAX_ALERTS:]
    return alert


def held(kinds: list[str] | None = None, placed_only: bool = False) -> dict:
    """Everything currently held, newest first."""
    with _lock:
        rows = list(_alerts)
    if kinds:
        wanted = set(kinds)
        rows = [a for a in rows if a["kind"] in wanted]
    if placed_only:
        rows = [a for a in rows if a["lat"] is not None]
    return {
        "alerts": rows,
        "count": len(rows),
        "plotted": sum(1 for a in rows if a["lat"] is not None),
        "kinds": KINDS,
        "model": llm.last_known(),
    }


def known(message_id: str) -> bool:
    with _lock:
        return message_id in _seen


def forget() -> None:
    with _lock:
        _alerts.clear()
        _seen.clear()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Synthetic alerts (DEMO_MODE) ───────────────────────────────

_DEMO = [
    ("drone", "Група БпЛА курсом на Київ", "Kyiv", 50.4501, 30.5234),
    ("missile", "Ракетна небезпека! Харків", "Kharkiv", 49.9935, 36.2304),
    ("explosion", "Вибухи в Одесі", "Odesa", 46.4825, 30.7233),
    ("air_defence", "Працює ППО по цілях", "Mykolaiv", 46.9750, 31.9946),
    ("aircraft", "Зліт МіГ-31К з аеродрому", "Savasleyka", 55.4667, 42.3333),
    ("artillery", "Обстріл прикордоння", "Sumy", 50.9077, 34.7981),
    ("alert", "Повітряна тривога в області", "Dnipro", 48.4647, 35.0462),
]


def demo(count: int = 24) -> dict:
    """Believable alerts for exploring offline.

    Spread over the last few hours rather than all at once, because a live
    feed arrives over time and a map showing a dozen simultaneous events would
    misrepresent what the layer normally looks like.
    """
    now = dt.datetime.now(dt.timezone.utc)
    rows = []
    for i in range(count):
        kind, text, place, lat, lon = _DEMO[i % len(_DEMO)]
        drift = ((i * 37) % 100 - 50) / 400.0
        rows.append({
            "id": f"demo:{i}",
            "channel": "demo_channel",
            "channel_title": "Demo channel",
            "at": (now - dt.timedelta(minutes=i * 11)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "text": text,
            "kind": kind,
            "label": KINDS[kind]["label"],
            "place": place,
            "read_by": "demo",
            "confident": True,
            "lat": lat + drift, "lon": lon + drift,
            "matched": f"{place} (synthetic)",
            "scale": "city",
            "url": None,
        })
    return {
        "alerts": rows, "count": len(rows), "plotted": len(rows),
        "kinds": KINDS, "demo": True,
        "model": {"ok": False, "detail": "Demo mode: alerts are synthetic.", "models": []},
    }
