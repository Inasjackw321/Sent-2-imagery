"""Global AIS, from aisstream.io.

Digitraffic covers the Baltic and asks for nothing. This covers the world and
asks for an account: a free API key, pasted in by whoever is running the app.
The key is held in memory for as long as the process lives and is never
written anywhere.

The service is a stream rather than a request. Holding a socket open for hours
would deliver tens of thousands of messages nobody is looking at, so instead
it is opened briefly, listened to for a few seconds, and closed -- and never
more often than once every five minutes. That floor is enforced here rather
than in the page, because a rule the front end keeps is a rule that a reload
can break.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import threading
import time

import websockets
import websockets.exceptions as ws_errors

# The name of the "server said no" exception moved between websockets
# releases. Catching whichever exists keeps a rejected API key reading as a
# rejected API key rather than as an AttributeError from the except clause.
# Newest name first, and only fall back if it is absent -- naming the old one
# on a release that still carries it for compatibility raises a deprecation
# warning for nothing.
_REFUSED = next(
    ((getattr(ws_errors, name),) for name in ("InvalidStatus", "InvalidStatusCode")
     if hasattr(ws_errors, name)),
    (ws_errors.WebSocketException,))


class StreamError(RuntimeError):
    pass


ENDPOINT = "wss://stream.aisstream.io/v0/stream"

# The floor. Nothing asks the service more often than this, whatever the map
# does, and a request inside the window is answered from what was already
# collected.
MIN_INTERVAL_SECONDS = 300

# How long to listen once connected. Long enough for a quiet box to say
# something, short enough that nobody is left staring at a spinner. Only asked
# for once every five minutes, so a few extra seconds costs little.
LISTEN_SECONDS = 12.0

# And a hard ceiling on how many messages to take, so a shipping lane in rush
# hour cannot hold the connection open on volume alone.
MAX_MESSAGES = 4000

_lock = threading.Lock()
_key: str | None = None
_cache: dict = {"at": 0.0, "box": None, "data": None}

# AIS ship type is a two-digit code; the tens digit is the category. Same
# reading as the Digitraffic source uses, so the map colours mean one thing.
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

NAV_STATUS = {
    0: "under way (engine)", 1: "at anchor", 2: "not under command",
    3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
    6: "aground", 7: "fishing", 8: "under way (sailing)", 15: "undefined",
}


def set_key(key: str | None) -> bool:
    """Remember the API key, or forget it. Never written to disk."""
    global _key
    with _lock:
        _key = (key or "").strip() or None
        # A new key means whatever was collected under the old one is not
        # what the caller is asking about.
        _cache.update(at=0.0, box=None, data=None)
        return _key is not None


def has_key() -> bool:
    with _lock:
        return _key is not None


def seconds_until_next() -> float:
    """How long before the service may be asked again."""
    with _lock:
        waited = time.time() - _cache["at"]
    return max(0.0, MIN_INTERVAL_SECONDS - waited)


def _kind(ship_type) -> tuple[str, str]:
    try:
        tens = int(ship_type) // 10
    except (TypeError, ValueError):
        return "other", "Unknown"
    return TYPES.get(tens, ("other", "Other"))


async def _collect(key: str, box: tuple[float, float, float, float]) -> tuple[dict, int]:
    """Open the stream, take what arrives for a few seconds, close it."""
    west, south, east, north = box
    subscribe = {
        "APIKey": key,
        # aisstream wants [[[lat, lon], [lat, lon]]] -- south-west then
        # north-east, and latitude first, which is the opposite order to the
        # rest of this app. Getting it backwards returns nothing rather than
        # an error, so it is worth being explicit.
        "BoundingBoxes": [[[south, west], [north, east]]],
        "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
    }

    seen: dict[int, dict] = {}
    static: dict[int, dict] = {}
    messages = 0
    try:
        async with websockets.connect(ENDPOINT, open_timeout=20, close_timeout=5) as socket:
            await socket.send(json.dumps(subscribe))
            deadline = time.monotonic() + LISTEN_SECONDS
            while time.monotonic() < deadline and messages < MAX_MESSAGES:
                left = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=max(0.1, left))
                except asyncio.TimeoutError:
                    break
                messages += 1
                # A bad key comes back as a plain text line rather than as a
                # refused handshake, so it has to be recognised here or it
                # looks like an empty sea.
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                if not text.lstrip().startswith("{"):
                    raise StreamError(f"aisstream said: {text.strip()[:160]}")
                with contextlib.suppress(json.JSONDecodeError):
                    _absorb(json.loads(text), seen, static)
    except _REFUSED as exc:
        raise StreamError("aisstream refused the connection — check the API key.") from exc
    except (OSError, ws_errors.WebSocketException, asyncio.TimeoutError) as exc:
        raise StreamError(f"aisstream could not be reached: {exc}") from exc

    # Names arrive on a different message type to positions, and often later.
    for mmsi, extra in static.items():
        if mmsi in seen:
            seen[mmsi].update({k: v for k, v in extra.items() if v is not None})
    return seen, messages


def _absorb(message: dict, seen: dict, static: dict) -> None:
    """Fold one message into what is known, whichever type it is."""
    kind = message.get("MessageType")
    meta = message.get("MetaData") or {}
    mmsi = meta.get("MMSI") or meta.get("MMSI_String")
    try:
        mmsi = int(mmsi)
    except (TypeError, ValueError):
        return

    if kind == "PositionReport":
        report = (message.get("Message") or {}).get("PositionReport") or {}
        lat = report.get("Latitude", meta.get("latitude"))
        lon = report.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None:
            return
        speed = report.get("Sog")
        course = report.get("Cog")
        heading = report.get("TrueHeading")
        # Whatever is already known about this ship comes first, so a later
        # report overwrites an earlier one. The other way round -- which is
        # how this was first written -- freezes every vessel at the first
        # position it sent and quietly ignores the rest of the window.
        entry = {
            "mmsi": mmsi,
            "name": None, "callsign": None, "destination": None,
            "draught": None, "length": None,
            "category": "other", "type": "Unknown",
            **seen.get(mmsi, {}),
        }
        entry.update({
            "lat": float(lat), "lon": float(lon),
            # The same "not available" values as everywhere else in AIS:
            # 102.3 knots, 360 degrees, heading 511.
            "speed": None if speed is None or speed >= 102.0 else round(float(speed), 1),
            "course": None if course is None or course >= 360 else round(float(course), 1),
            "heading": None if heading is None or heading >= 511 else int(heading),
            "status": NAV_STATUS.get(report.get("NavigationalStatus")),
            "age_min": _age(meta.get("time_utc")),
        })
        name = (meta.get("ShipName") or "").strip()
        if name:
            entry["name"] = name
        seen[mmsi] = entry
    elif kind == "ShipStaticData":
        data = (message.get("Message") or {}).get("ShipStaticData") or {}
        category, label = _kind(data.get("Type"))
        dimension = data.get("Dimension") or {}
        length = (dimension.get("A") or 0) + (dimension.get("B") or 0)
        static[mmsi] = {
            "name": (data.get("Name") or "").strip() or None,
            "callsign": (data.get("CallSign") or "").strip() or None,
            "destination": (data.get("Destination") or "").strip() or None,
            "draught": data.get("MaximumStaticDraught") or None,
            "length": length or None,
            "category": category, "type": label,
        }


def _age(stamp) -> float | None:
    """Minutes since the message was sent, from aisstream's own timestamp.

    It arrives as "2026-08-28 10:00:00.123 +0000 UTC", which is Go's layout
    rather than anything datetime parses, so the useful part is taken off the
    front and the rest ignored.
    """
    if not isinstance(stamp, str):
        return None
    head = stamp.strip()[:19]
    try:
        when = dt.datetime.strptime(head, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    return max(0.0, round((now - when).total_seconds() / 60.0, 1))


def vessels_in(box: tuple[float, float, float, float], limit: int = 900) -> dict:
    """Every ship the stream reported in this box, at most once every 5 minutes.

    Inside the window the last collection is returned unchanged, with how long
    is left before it will go again -- so the map can say so rather than
    looking stale for no reason.
    """
    if not has_key():
        raise StreamError("No aisstream API key has been set.")

    with _lock:
        age = time.time() - _cache["at"]
        fresh = _cache["data"] is not None and age < MIN_INTERVAL_SECONDS
        cached = _cache["data"] if fresh else None
        key = _key

    if cached is not None:
        return {**cached, "cached": True, "next_in": round(seconds_until_next())}

    try:
        found, messages = asyncio.run(_collect(key, box))
    except RuntimeError as exc:
        # asyncio.run refuses to nest, which would mean a caller already
        # inside a loop -- worth naming rather than surfacing as a mystery.
        raise StreamError(f"Could not run the stream: {exc}") from exc

    ships = sorted(found.values(), key=lambda v: v["mmsi"])[:limit]
    out = {
        "vessels": ships,
        "count": len(found),
        "covered": True,
        "source": "aisstream.io",
        "attribution": "aisstream.io",
        "fetched": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "window_seconds": LISTEN_SECONDS,
        # How much arrived at all, which is the difference between "nothing is
        # sailing there" and "nothing is getting through".
        "messages": messages,
    }
    with _lock:
        _cache.update(at=time.time(), box=box, data=out)
    return {**out, "cached": False, "next_in": MIN_INTERVAL_SECONDS}
