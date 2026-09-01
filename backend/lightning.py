"""Live lightning, from the Blitzortung volunteer network.

Blitzortung is a few thousand amateur-built receivers listening for the radio
crack a lightning stroke makes. Each station reports when it heard one; a
server works out where the stroke was from the differences in arrival time and
publishes the fix within a second or two. It is free, needs no account, and
covers Europe densely, North America and Australia well, and the oceans and
much of Africa and Asia hardly at all -- so an empty map is as likely to mean
"nobody is listening there" as "nothing is happening there", and the panel
says so rather than letting a blank read as calm.

The feed is a websocket that never stops, so this works the way the AIS one
does: a background thread holds the connection, strokes go into a ring buffer
with the time they arrived, and the front end asks for whatever is inside its
own map view. Nothing is written to disk.

The messages are compressed with the network's own scheme -- a variant of LZW
over the JSON text -- so `inflate` below is the first thing that runs on
anything received. That decoder is the one part of this file with tests: it is
pure text in, text out, and a mistake in it looks like corrupt JSON rather than
like a wrong position.

Blitzortung's data is published for non-commercial use. This app is free and
unmonetised, which is the case it is used under here; anyone putting it behind
a paywall needs to take that up with them first.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import threading
import time
from typing import Any

import websockets

ATTRIBUTION = "Blitzortung.org contributors"

# The network runs several equivalent websocket servers and expects clients to
# spread themselves across them rather than all landing on one.
ENDPOINTS = [
    "wss://ws1.blitzortung.org:3000/",
    "wss://ws7.blitzortung.org:3000/",
    "wss://ws8.blitzortung.org:3000/",
]

# What the server wants before it will send anything.
HELLO = json.dumps({"a": 111})

# How long a stroke stays on the map. Lightning is over in microseconds, so
# every one of these is a memory of something already finished; half an hour is
# long enough to show where a storm has been without pretending it is still
# happening.
KEEP_SECONDS = 30 * 60

# A busy hour worldwide is a few thousand strokes. The ceiling is generous but
# finite, because this runs for as long as the app does.
MAX_STROKES = 20000

# How long to wait before reconnecting, and the ceiling on that wait. A network
# that is down should not be asked again immediately, forever.
RETRY_SECONDS = 3.0
RETRY_CEILING = 120.0


class LightningError(RuntimeError):
    pass


def inflate(packed: str) -> str:
    """Undo the network's compression.

    A variant of LZW: the dictionary starts as the single characters, every
    code above 255 stands for a string learned earlier in the message, and the
    awkward case is a code that refers to the entry currently being built --
    which is the previous string plus its own first character.

    Written out rather than pulled from a library because it is not quite any
    standard LZW: the codes arrive as UTF-16 code units in a string, not as a
    packed bitstream, so no bit-width bookkeeping is involved.
    """
    if not packed:
        return ""

    table: dict[int, str] = {}
    previous = packed[0]
    out = [previous]
    next_code = 256

    for char in packed[1:]:
        code = ord(char)
        if code < 256:
            entry = char
        elif code in table:
            entry = table[code]
        else:
            # The self-referential case: this code is the one about to be
            # defined, so it can only be the previous string plus its own
            # first character.
            entry = previous + previous[0]
        out.append(entry)
        table[next_code] = previous + entry[0]
        next_code += 1
        previous = entry

    return "".join(out)


def _stroke(message: dict[str, Any], now: float) -> dict[str, Any] | None:
    """One stroke, in the shape the map wants, or None if it is not one."""
    lat = message.get("lat")
    lon = message.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    # The timestamp is in nanoseconds since the epoch.
    stamp = message.get("time")
    when = stamp / 1e9 if isinstance(stamp, (int, float)) and stamp > 0 else now
    return {
        "lat": float(lat),
        "lon": float(lon),
        "time": when,
        # How many receivers heard it. More stations means a tighter fix, so
        # this is the closest thing to a confidence the feed offers.
        "stations": len(message.get("sig") or ()) or None,
    }


class Listener:
    """Holds the connection and the recent strokes.

    One of these for the whole process. It starts on the first request rather
    than at import, so an app nobody has asked for lightning from never opens
    the socket at all.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._strokes: list[dict[str, Any]] = []
        self._thread: threading.Thread | None = None
        self._state = "not started"
        self._since: float | None = None
        self._seen = 0

    # -- what the API asks --

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._state = "connecting"
            self._thread = threading.Thread(
                target=self._run, name="lightning", daemon=True)
            self._thread.start()

    def recent(self, west: float, south: float, east: float, north: float,
               minutes: float) -> list[dict[str, Any]]:
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [s for s in self._strokes
                    if s["time"] >= cutoff
                    and south <= s["lat"] <= north
                    and west <= s["lon"] <= east]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "held": len(self._strokes),
                "seen": self._seen,
                "since": self._since,
                "attribution": ATTRIBUTION,
            }

    # -- the thread --

    def _run(self) -> None:
        asyncio.run(self._listen_forever())

    async def _listen_forever(self) -> None:
        wait = RETRY_SECONDS
        while True:
            endpoint = random.choice(ENDPOINTS)
            try:
                await self._listen(endpoint)
                wait = RETRY_SECONDS
            except Exception as exc:  # noqa: BLE001 - the reason is reported, not raised
                with self._lock:
                    self._state = f"reconnecting — {exc}"
                # Backing off, because a network that just refused will very
                # likely refuse again a second from now.
                await asyncio.sleep(wait + random.uniform(0, 1))
                wait = min(wait * 2, RETRY_CEILING)

    async def _listen(self, endpoint: str) -> None:
        async with websockets.connect(endpoint, open_timeout=20,
                                      close_timeout=3, max_size=2 ** 20) as socket:
            await socket.send(HELLO)
            with self._lock:
                self._state = "listening"
                self._since = time.time()
            async for raw in socket:
                self._take(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))

    def _take(self, raw: str) -> None:
        try:
            message = json.loads(inflate(raw))
        except (ValueError, IndexError):
            return
        if not isinstance(message, dict):
            return
        now = time.time()
        stroke = _stroke(message, now)
        if stroke is None:
            return
        with self._lock:
            self._strokes.append(stroke)
            self._seen += 1
            self._prune(now)

    def _prune(self, now: float) -> None:
        """Drop what is too old, then whatever is left over the ceiling."""
        cutoff = now - KEEP_SECONDS
        if self._strokes and self._strokes[0]["time"] < cutoff:
            self._strokes = [s for s in self._strokes if s["time"] >= cutoff]
        if len(self._strokes) > MAX_STROKES:
            del self._strokes[: len(self._strokes) - MAX_STROKES]


_listener = Listener()


def strokes(west: float, south: float, east: float, north: float,
            minutes: float = 30.0) -> dict[str, Any]:
    """Recent strokes inside a box, starting the listener if need be."""
    _listener.start()
    found = _listener.recent(west, south, east, north, minutes)
    return {
        "strokes": found,
        "count": len(found),
        "minutes": minutes,
        **_listener.status(),
    }


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo(west: float, south: float, east: float, north: float,
         minutes: float = 30.0) -> dict[str, Any]:
    """Synthetic strokes, for the demo build and for testing the map.

    Clustered rather than scattered: real lightning comes in storms, and a
    uniform sprinkle would make the map look wrong in a way that is hard to
    name and easy to ship.
    """
    rng = random.Random(int(time.time() // 60))
    now = time.time()
    span_lon = max(east - west, 0.01)
    span_lat = max(north - south, 0.01)

    cells = [(west + rng.random() * span_lon, south + rng.random() * span_lat)
             for _ in range(rng.randint(1, 3))]
    out = []
    for _ in range(rng.randint(40, 120)):
        clon, clat = rng.choice(cells)
        # A storm cell is tens of kilometres across, not tenths of a degree at
        # every latitude: the longitude spread widens as the cosine shrinks.
        spread = 0.35
        lat = clat + rng.gauss(0, spread * span_lat / 4)
        lon = clon + rng.gauss(0, spread * span_lon / 4) / max(
            0.2, math.cos(math.radians(clat)))
        if not (south <= lat <= north and west <= lon <= east):
            continue
        out.append({
            "lat": lat, "lon": lon,
            "time": now - rng.random() * minutes * 60,
            "stations": rng.randint(4, 30),
        })
    out.sort(key=lambda s: s["time"])
    return {
        "strokes": out,
        "count": len(out),
        "minutes": minutes,
        "state": "demo — synthetic strokes",
        "held": len(out),
        "seen": len(out),
        "since": now,
        "attribution": "synthetic",
    }
