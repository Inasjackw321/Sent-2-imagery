"""Internet connectivity, read off RIPE Atlas probes.

RIPE Atlas is a network of some twelve thousand small measurement devices,
hosted by volunteers in homes, offices and data centres, each of which keeps a
permanent connection open to the RIPE NCC. When a probe stops being reachable,
its status flips from Connected to Disconnected and the moment it happened is
recorded. That is the whole signal this layer uses.

What a probe going dark means, said plainly because it is easy to over-read:

  It is the internet connection at ONE site that went away -- or the power to
  it, or somebody unplugged it. Not wifi as such: a probe is wired into the
  network of whoever hosts it. One probe going offline is a household or an
  office. MANY probes in one area going offline in the same hour is the thing
  worth seeing: a regional power cut, an ISP failure, a shutdown, a strike on
  infrastructure. The per-country counts below exist to make that second
  pattern visible and to stop the first being mistaken for it.

  Positions are deliberately blurred by RIPE Atlas, to protect the people
  hosting the probes. A dot is "somewhere around here", a few kilometres at
  most, never an address.

No account and no key. The probe list is public.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import math
import threading
import time
from typing import Any

import requests

from . import config

PROBES_URL = "https://atlas.ripe.net/api/v2/probes/"
ATTRIBUTION = "RIPE Atlas, RIPE NCC"

# The two statuses that mean anything here. Never Connected is a probe still in
# its box; Abandoned is one RIPE has written off after a long silence. Neither
# says anything about the network today.
CONNECTED = 1
DISCONNECTED = 2

# The windows a change can be looked for over, in hours.
WINDOWS = {1: "1 h", 6: "6 h", 24: "24 h"}
DEFAULT_WINDOW = 6

# A probe offline for longer than this is not news, it is a probe nobody has
# plugged back in. Kept off the map rather than drawn as a red dot, because a
# red dot reads as "this just happened" and a month-old one did not.
OFFLINE_TOO_LONG = dt.timedelta(days=7)

# How many probes a single answer may hold, and how many pages of the probe
# list that is worth asking for. RIPE Atlas pages at up to 500. A view of all
# of Europe holds several thousand connected probes, so this caps a continental
# view at the nearest few thousand rather than at whatever sorted first.
PAGE_SIZE = 500
MOST_PAGES = 8
MOST_RETURNED = 4000

# How long one rectangle's answer is held. A probe's status is reported every
# few minutes; asking more often than this gets the same answer back.
CACHE_SECONDS = 120
CACHE_BOXES = 32

TIMEOUT = 20

# What the owner wrote about their probe, cut short. Free text from a stranger:
# shown as a text node, never as markup, and never longer than a line.
DESCRIPTION_LIMIT = 60

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})

_lock = threading.Lock()
_held: dict[str, tuple[float, dict[str, Any]]] = {}


class ConnectivityError(RuntimeError):
    pass


def forget() -> None:
    """Drop every held answer. For the tests."""
    with _lock:
        _held.clear()


# ── Reading a probe ────────────────────────────────────────────


def since_of(probe: dict[str, Any]) -> dt.datetime | None:
    """When this probe's current status began.

    Two places it can be said: `status.since` as an ISO time, and
    `status_since` as Unix seconds. Either is taken; neither is guessed at.
    """
    said = (probe.get("status") or {}).get("since") if isinstance(
        probe.get("status"), dict) else None
    if said:
        try:
            when = dt.datetime.fromisoformat(str(said).replace("Z", "+00:00"))
            return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    stamp = probe.get("status_since")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return dt.datetime.fromtimestamp(stamp, dt.timezone.utc)
    return None


def status_of(probe: dict[str, Any]) -> int | None:
    """The numeric status, from wherever this answer put it."""
    said = probe.get("status")
    if isinstance(said, dict):
        said = said.get("id")
    try:
        return int(said)
    except (TypeError, ValueError):
        return None


def position_of(probe: dict[str, Any]) -> tuple[float, float] | None:
    """(lat, lon), off the GeoJSON point. None for a probe with no position."""
    point = probe.get("geometry") or {}
    coords = point.get("coordinates") if isinstance(point, dict) else None
    if not coords or len(coords) < 2:
        return None
    try:
        lon, lat = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return None
    # 0,0 is where a probe with no position set lands, in the Atlantic.
    if lat == 0.0 and lon == 0.0:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def classify(status: int | None, since: dt.datetime | None,
             window: dt.timedelta, now: dt.datetime) -> str | None:
    """Which of the four things this probe is saying, or None to leave it off.

      down   went offline inside the window -- the thing being looked for;
      back   came back online inside the window -- the other half of it;
      up     online, and has been since before the window;
      gone   offline since before the window, but not so long that it is
             just a probe nobody plugged back in.
    """
    if status == CONNECTED:
        if since is not None and now - since <= window:
            return "back"
        return "up"
    if status == DISCONNECTED:
        if since is None:
            return "gone"
        age = now - since
        if age <= window:
            return "down"
        if age <= OFFLINE_TOO_LONG:
            return "gone"
    return None


def read_probe(probe: dict[str, Any], window: dt.timedelta,
               now: dt.datetime) -> dict[str, Any] | None:
    """One probe, in the shape the map draws. None for one not worth drawing."""
    where = position_of(probe)
    if where is None:
        return None
    status = status_of(probe)
    since = since_of(probe)
    state = classify(status, since, window, now)
    if state is None:
        return None
    text = " ".join(str(probe.get("description") or "").split())
    return {
        "id": probe.get("id"),
        "lat": where[0],
        "lon": where[1],
        "state": state,
        "since": since.isoformat().replace("+00:00", "Z") if since else None,
        "country": str(probe.get("country_code") or "").upper()[:2] or None,
        "asn": probe.get("asn_v4") or probe.get("asn_v6"),
        "description": text[:DESCRIPTION_LIMIT],
        "anchor": bool(probe.get("is_anchor")),
    }


# ── What a region is doing ─────────────────────────────────────


def by_country(probes: list[dict[str, Any]], most: int = 8) -> list[dict[str, Any]]:
    """Per country: how many are up, how many went down, how many came back.

    The share that went down is out of the probes that were online at the
    start of the window -- the ones still up, plus the ones that dropped --
    because a probe that was already offline yesterday cannot go offline
    today, and counting it would make a quiet country look half dark.

    Sorted by how many went down, then by share. A country with one probe and
    one outage is 100 % and means nothing; it sorts below one with forty
    probes and twelve outages, which means a great deal.
    """
    counts: dict[str, dict[str, int]] = {}
    for probe in probes:
        country = probe.get("country") or "??"
        row = counts.setdefault(country, {"up": 0, "down": 0, "back": 0, "gone": 0})
        row[probe["state"]] += 1
    out = []
    for country, row in counts.items():
        was_up = row["up"] + row["down"] + row["back"]
        out.append({
            "country": country, **row,
            "share_down": round(row["down"] / was_up, 3) if was_up else 0.0,
        })
    out.sort(key=lambda r: (-r["down"], -r["share_down"], r["country"]))
    return out[:most]


def summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {"up": 0, "down": 0, "back": 0, "gone": 0}
    for probe in probes:
        counts[probe["state"]] += 1
    was_up = counts["up"] + counts["down"] + counts["back"]
    return {**counts,
            "share_down": round(counts["down"] / was_up, 3) if was_up else 0.0}


# ── Asking RIPE Atlas ──────────────────────────────────────────


def _ask(params: dict[str, Any], get: Any) -> dict[str, Any]:
    send = get or _session.get
    try:
        resp = send(PROBES_URL, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ConnectivityError(f"RIPE Atlas could not be reached: {exc}") from exc
    if getattr(resp, "status_code", None) == 400 and "status__in" in params:
        # The status filter is the one parameter here not taken straight from
        # the published examples. If this version of the API refuses it, ask
        # without it and filter here instead -- the answer is the same, it is
        # only longer.
        return _ask({k: v for k, v in params.items() if k != "status__in"}, get)
    if not getattr(resp, "ok", False):
        raise ConnectivityError(
            f"RIPE Atlas answered {getattr(resp, 'status_code', '?')}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ConnectivityError("RIPE Atlas sent something that is not JSON") from exc


def _fetch(box: tuple[float, float, float, float], get: Any) -> tuple[list[dict], bool]:
    """Every connected or disconnected probe in the rectangle, page by page."""
    west, south, east, north = box
    params: dict[str, Any] = {
        "latitude__gte": round(max(south, -90.0), 4),
        "latitude__lte": round(min(north, 90.0), 4),
        "longitude__gte": round(max(west, -180.0), 4),
        "longitude__lte": round(min(east, 180.0), 4),
        "status__in": f"{CONNECTED},{DISCONNECTED}",
        "page_size": PAGE_SIZE,
        "format": "json",
    }
    found: list[dict] = []
    page = _ask(params, get)
    pages = 1
    truncated = False
    send = get or _session.get
    while True:
        found.extend(page.get("results") or [])
        following = page.get("next")
        if not following:
            break
        if pages >= MOST_PAGES:
            truncated = True
            break
        # The next page is a full URL with every filter already in it.
        try:
            resp = send(following, timeout=TIMEOUT)
            if not getattr(resp, "ok", False):
                raise ValueError(getattr(resp, "status_code", "?"))
            page = resp.json()
        except (requests.RequestException, ValueError):
            # A later page that fails costs that page, not the whole answer:
            # what arrived is still a true picture of part of the view, and
            # the answer says it is partial.
            truncated = True
            break
        pages += 1
    return found, truncated


def probes(bbox: tuple[float, float, float, float], hours: int = DEFAULT_WINDOW,
           get: Any = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Every probe in a rectangle that says something about connectivity now."""
    if hours not in WINDOWS:
        hours = DEFAULT_WINDOW
    west, south, east, north = bbox
    key = f"{west:.2f},{south:.2f},{east:.2f},{north:.2f}"
    with _lock:
        held = _held.get(key)
    if held and time.time() - held[0] < CACHE_SECONDS and get is None:
        raw, truncated = held[1]["raw"], held[1]["truncated"]
    else:
        raw, truncated = _fetch(bbox, get)
        if get is None:
            with _lock:
                if len(_held) >= CACHE_BOXES:
                    _held.pop(min(_held, key=lambda k: _held[k][0]), None)
                _held[key] = (time.time(), {"raw": raw, "truncated": truncated})
    return shape(raw, hours, bbox, truncated, now)


def shape(raw: list[dict], hours: int, bbox: tuple[float, float, float, float],
          truncated: bool = False, now: dt.datetime | None = None) -> dict[str, Any]:
    """Turn a list of probes into the answer the panel draws."""
    now = now or dt.datetime.now(dt.timezone.utc)
    window = dt.timedelta(hours=hours)
    read = [p for p in (read_probe(r, window, now) for r in raw) if p]
    # The ones that changed are the point, so they are never the ones a cap
    # drops: changes first, then the steady ones nearest the middle.
    west, south, east, north = bbox
    cx, cy = (west + east) / 2, (south + north) / 2
    weight = {"down": 0, "back": 1, "gone": 2, "up": 3}
    read.sort(key=lambda p: (weight[p["state"]],
                             math.hypot(p["lon"] - cx, p["lat"] - cy)))
    capped = len(read) > MOST_RETURNED
    shown = read[:MOST_RETURNED]
    return {
        "probes": shown,
        "count": len(read),
        "capped": capped or truncated,
        "hours": hours,
        "summary": summary(read),
        "countries": by_country(read),
        "attribution": ATTRIBUTION,
        "checked": now.isoformat().replace("+00:00", "Z"),
    }


# ── Demo ───────────────────────────────────────────────────────


def demo(bbox: tuple[float, float, float, float], hours: int = DEFAULT_WINDOW,
         now: dt.datetime | None = None) -> dict[str, Any]:
    """A believable scatter of probes in the rectangle, with one area dark.

    Mostly up, a few changing, and one cluster that went down together --
    because that cluster is what this layer exists to show, and an offline
    build that never shows one cannot be checked by eye.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    west, south, east, north = bbox
    seed = int(hashlib.sha1(f"{west:.1f},{south:.1f}".encode()).hexdigest()[:8], 16)
    raw = []
    # The dark patch: three tenths of the way in from the south-west corner.
    dark_lon = west + (east - west) * 0.3
    dark_lat = south + (north - south) * 0.3
    for i in range(160):
        a = ((seed + i * 7919) % 10007) / 10007
        b = ((seed // 3 + i * 104729) % 10009) / 10009
        lon = west + (east - west) * a
        lat = south + (north - south) * b
        near_dark = math.hypot(lon - dark_lon, lat - dark_lat) < (east - west) * 0.08
        if near_dark:
            status, ago = DISCONNECTED, dt.timedelta(minutes=20 + i % 40)
        elif i % 23 == 0:
            status, ago = DISCONNECTED, dt.timedelta(days=2)
        elif i % 17 == 0:
            status, ago = CONNECTED, dt.timedelta(minutes=30 + i)
        elif i % 29 == 0:
            status, ago = DISCONNECTED, dt.timedelta(hours=1 + i % 4)
        else:
            status, ago = CONNECTED, dt.timedelta(days=3 + i % 40)
        raw.append({
            "id": 1_000_000 + i,
            "geometry": {"type": "Point", "coordinates": [round(lon, 3), round(lat, 3)]},
            "status": {"id": status,
                       "name": "Connected" if status == CONNECTED else "Disconnected",
                       "since": (now - ago).isoformat().replace("+00:00", "Z")},
            "country_code": "UA" if i % 3 else "PL",
            "asn_v4": 15895 + i % 5,
            "description": f"Synthetic probe {i}",
        })
    out = shape(raw, hours if hours in WINDOWS else DEFAULT_WINDOW, bbox, now=now)
    out["demo"] = True
    return out


# ── One probe over time ────────────────────────────────────────
#
# The map says what each probe is doing now. Clicking one asks what it has
# been doing: when it was online, and how good its connection was while it
# was. Two of the measurements every probe runs on its own, with no account
# needed to read them:
#
#   7000  every connect and disconnect, as it happens -- the online/offline
#         band along the top of the graph;
#   1001  a ping to k.root-servers.net every few minutes, over IPv4 -- the
#         round-trip time and any lost packets underneath it.
#
# The ping is what makes a graph worth more than the dot. A connection that
# is about to fail often degrades first: the round trip climbs, packets start
# going missing. And a probe that stays connected while its pings die is a
# network that is up but not reaching the rest of the internet.

RESULTS_URL = "https://atlas.ripe.net/api/v2/measurements/{msm}/results/"
PROBE_URL = "https://atlas.ripe.net/api/v2/probes/{id}/"
CONNECTION_MSM = 7000
PING_MSM = 1001
PING_TARGET = "k.root-servers.net"

# How far back a graph can look.
HISTORY = {24: "24 h", 168: "7 days"}
DEFAULT_HISTORY = 24

# How long one probe's history is held. The graph window refreshes itself every
# minute, like the seismograph traces, and a probe pings every four -- so this
# is about two windows open on the same probe not asking twice.
HISTORY_SECONDS = 60
HISTORY_HELD = 64

PLOT_SIZE = (760, 260)

_history_held: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}


def _results(msm: int, probe_id: int, start: dt.datetime, stop: dt.datetime,
             get: Any) -> list[dict[str, Any]]:
    send = get or _session.get
    try:
        resp = send(RESULTS_URL.format(msm=msm), params={
            "probe_ids": probe_id,
            "start": int(start.timestamp()),
            "stop": int(stop.timestamp()),
            "format": "json",
        }, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ConnectivityError(f"RIPE Atlas could not be reached: {exc}") from exc
    if not getattr(resp, "ok", False):
        raise ConnectivityError(
            f"RIPE Atlas answered {getattr(resp, 'status_code', '?')} "
            f"for measurement {msm}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise ConnectivityError("RIPE Atlas sent something that is not JSON") from exc
    return body if isinstance(body, list) else []


def read_events(results: list[dict[str, Any]]) -> list[tuple[float, str]]:
    """(unix time, "up" | "down") for every connect and disconnect, in order."""
    out = []
    for row in results:
        said = str(row.get("event") or "").lower()
        when = row.get("timestamp")
        if not isinstance(when, (int, float)):
            continue
        if said.startswith("disconnect"):
            out.append((float(when), "down"))
        elif said.startswith("connect"):
            out.append((float(when), "up"))
    out.sort()
    return out


def read_pings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One point per ping round: when, the round trip, and how much was lost.

    The round trip is the median of the replies that came back rather than
    RIPE's average, so one slow reply out of three does not drag the line up.
    A round with nothing back has no round trip at all -- not zero, which
    would draw the worst moment as the best.
    """
    out = []
    for row in results:
        when = row.get("timestamp")
        if not isinstance(when, (int, float)):
            continue
        replies = sorted(r["rtt"] for r in (row.get("result") or [])
                         if isinstance(r, dict) and isinstance(r.get("rtt"), (int, float)))
        sent = int(row.get("sent") or len(row.get("result") or []) or 0)
        got = int(row.get("rcvd") if row.get("rcvd") is not None else len(replies))
        rtt = None
        if replies:
            middle = len(replies) // 2
            rtt = (replies[middle] if len(replies) % 2
                   else (replies[middle - 1] + replies[middle]) / 2)
        elif isinstance(row.get("avg"), (int, float)) and row["avg"] >= 0:
            rtt = float(row["avg"])
        out.append({"t": float(when), "rtt": rtt, "sent": sent, "rcvd": got,
                    "lost": (sent - got) / sent if sent else 0.0})
    out.sort(key=lambda p: p["t"])
    return out


def timeline(events: list[tuple[float, str]], now_state: str | None,
             start: float, end: float) -> list[tuple[float, float, str]]:
    """The window cut into stretches of online and offline.

    What it was doing before the first event is the opposite of that event --
    a probe that connected at noon was not connected at eleven. With no events
    at all, it was doing all window what it is doing now.
    """
    inside = [(t, s) for t, s in events if start <= t <= end]
    if inside:
        state = "down" if inside[0][1] == "up" else "up"
    else:
        state = now_state or "unknown"
    out = []
    at = start
    for when, became in inside:
        if when > at:
            out.append((at, when, state))
        at, state = when, became
    if end > at:
        out.append((at, end, state))
    return out


def history(probe_id: int, hours: int = DEFAULT_HISTORY, get: Any = None,
            now: dt.datetime | None = None) -> dict[str, Any]:
    """What one probe has been doing over the last day or week."""
    if hours not in HISTORY:
        hours = DEFAULT_HISTORY
    key = (int(probe_id), hours)
    if get is None:
        with _lock:
            held = _history_held.get(key)
        if held and time.time() - held[0] < HISTORY_SECONDS:
            return held[1]
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=hours)

    send = get or _session.get
    try:
        resp = send(PROBE_URL.format(id=int(probe_id)), params={"format": "json"},
                    timeout=TIMEOUT)
        about = resp.json() if getattr(resp, "ok", False) else {}
    except (requests.RequestException, ValueError):
        # The probe's own record is for the label and for what it is doing
        # now. Without it the graph is still the graph.
        about = {}
    status = status_of(about) if about else None
    now_state = {CONNECTED: "up", DISCONNECTED: "down"}.get(status)

    events = read_events(_results(CONNECTION_MSM, probe_id, start, now, get))
    try:
        pings = read_pings(_results(PING_MSM, probe_id, start, now, get))
        ping_trouble = ""
    except ConnectivityError as exc:
        # The online band is the part asked for. Pings that would not come
        # are said on the graph rather than costing the band too.
        pings, ping_trouble = [], str(exc)

    answer = {
        "id": int(probe_id),
        "hours": hours,
        "start": start.timestamp(),
        "end": now.timestamp(),
        "events": events,
        "segments": timeline(events, now_state, start.timestamp(), now.timestamp()),
        "pings": pings,
        "ping_trouble": ping_trouble,
        "asn": about.get("asn_v4") or about.get("asn_v6"),
        "country": str(about.get("country_code") or "").upper() or None,
    }
    if get is None:
        with _lock:
            if len(_history_held) >= HISTORY_HELD:
                _history_held.pop(min(_history_held, key=lambda k: _history_held[k][0]), None)
            _history_held[key] = (time.time(), answer)
    return answer


# ── Drawing it ─────────────────────────────────────────────────

_UP = (61, 220, 132)
_DOWN = (255, 77, 79)
_UNKNOWN = (90, 100, 116)
_LINE = (126, 214, 255)
_PARTLY = (255, 209, 102)


def plot(found: dict[str, Any]) -> bytes:
    """The graph: online/offline along the top, the round trip beneath.

    Offline stretches are shaded red right down through the round-trip
    panel too, so a gap in the line is read as the outage it is rather than
    as missing data.
    """
    from PIL import Image, ImageDraw

    width, height = PLOT_SIZE
    image = Image.new("RGB", (width, height), (12, 15, 20))
    draw = ImageDraw.Draw(image)
    left, right = 46, width - 12
    # The band sits a little way down rather than at the very top: the window
    # puts its "updated" stamp over the top-right corner, and the right-hand
    # end of the band is exactly where a current outage is drawn.
    band_top, band_bottom = 40, 52
    top, bottom = 62, height - 34
    start, end = found["start"], found["end"]
    span = max(end - start, 1.0)

    def x_of(t: float) -> float:
        return left + (min(max(t, start), end) - start) / span * (right - left)

    # Grid, and the times along the bottom.
    hours = found["hours"]
    ticks = 6 if hours <= 24 else 7
    for i in range(ticks + 1):
        t = start + span * i / ticks
        x = x_of(t)
        draw.line([(x, top), (x, bottom)], fill=(28, 34, 45))
        when = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        label = when.strftime("%H:%M") if hours <= 24 else when.strftime("%d %b")
        draw.text((x - 14, bottom + 4), label, fill=(120, 134, 152))

    # The band: online, offline, and unknown where nothing said either.
    for a, b, state in found["segments"]:
        colour = {"up": _UP, "down": _DOWN}.get(state, _UNKNOWN)
        draw.rectangle([x_of(a), band_top, max(x_of(b), x_of(a) + 1), band_bottom],
                       fill=colour)
        if state == "down":
            # And faintly through the panel below, so the gap in the line
            # lines up with the reason for it.
            shade = Image.new("RGB", (max(int(x_of(b) - x_of(a)), 1), bottom - top),
                              (70, 18, 22))
            image.paste(shade, (int(x_of(a)), top))
    draw = ImageDraw.Draw(image)

    pings = found["pings"]
    rtts = sorted(p["rtt"] for p in pings if p["rtt"] is not None)
    if rtts:
        # Scaled to the 98th percentile, so one huge spike does not flatten
        # everything else into the floor -- and never tighter than 20 ms, so
        # a steady 4 ms line is drawn as steady rather than as wild.
        ceiling = max(rtts[min(len(rtts) - 1, int(len(rtts) * 0.98))] * 1.25, 20.0)
        for value in (0, ceiling / 2, ceiling):
            y = bottom - value / ceiling * (bottom - top)
            draw.text((4, y - 6), f"{value:.0f}", fill=(120, 134, 152))
        # Points are joined only when they are near each other in time. A line
        # across an hour with no results would draw a round trip nobody
        # measured.
        step = _usual_step(pings)
        last = None
        for p in pings:
            if p["rtt"] is None:
                last = None
                continue
            y = bottom - min(p["rtt"], ceiling) / ceiling * (bottom - top)
            here = (x_of(p["t"]), y)
            if last and p["t"] - last[0] <= step * 3:
                draw.line([last[1], here], fill=_LINE)
            else:
                draw.point(here, fill=_LINE)
            last = (p["t"], here)
    else:
        message = (found.get("ping_trouble")
                   or f"No IPv4 pings to {PING_TARGET} from this probe in the window")
        draw.text((left + 10, (top + bottom) // 2 - 6), message[:110], fill=(150, 165, 185))

    # Loss, as ticks along the floor: red where nothing came back, amber where
    # some did. On the floor rather than as points on the line, because a
    # round with nothing back has no round trip to put a point at.
    for p in pings:
        if p["sent"] and p["rcvd"] < p["sent"]:
            x = x_of(p["t"])
            colour = _DOWN if p["rcvd"] == 0 else _PARTLY
            draw.line([(x, bottom - 7), (x, bottom)], fill=colour)

    who = " · ".join(str(v) for v in (
        f"Probe {found['id']}", found.get("country"),
        f"AS{found['asn']}" if found.get("asn") else None) if v)
    draw.text((left, 9), f"{who}   online / offline", fill=(150, 165, 185))
    draw.text((left, height - 16),
              f"round trip to {PING_TARGET}, ms   -   RIPE Atlas measurements "
              f"{CONNECTION_MSM} and {PING_MSM}", fill=(120, 134, 152))
    # The ticks, said in their own colours.
    draw.text((right - 190, height - 16), "ticks: lost", fill=(120, 134, 152))
    draw.text((right - 120, height - 16), "all", fill=_DOWN)
    draw.text((right - 96, height - 16), "some", fill=_PARTLY)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _usual_step(pings: list[dict[str, Any]]) -> float:
    """How far apart this probe's pings usually are, in seconds."""
    gaps = sorted(b["t"] - a["t"] for a, b in zip(pings, pings[1:]) if b["t"] > a["t"])
    return gaps[len(gaps) // 2] if gaps else 240.0


def demo_history(probe_id: int, hours: int = DEFAULT_HISTORY, state: str = "up",
                 since: str | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """A believable history for a demo probe, agreeing with its dot.

    Told the state and time the map is showing, because a demo probe drawn red
    whose graph said it had been online all day would look exactly like a bug.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if hours not in HISTORY:
        hours = DEFAULT_HISTORY
    start = now - dt.timedelta(hours=hours)
    try:
        changed = dt.datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        changed = None
    events: list[tuple[float, str]] = []
    if changed and changed > start.timestamp():
        became = "down" if state in ("down", "gone") else "up"
        events.append((changed, became))
        if became == "up":
            # Came back, so it went away first.
            events.insert(0, (changed - 50 * 60, "down"))
    events = [(t, s) for t, s in events if t >= start.timestamp()]
    seed = int(probe_id) % 97
    pings = []
    t = start.timestamp()
    # Up to now, not including it: the timeline's stretches end at now, so a
    # ping stamped exactly now would fall outside the last one and be drawn
    # in the middle of an outage.
    while t < now.timestamp():
        offline = any(a <= t < b and s == "down" for a, b, s in
                      timeline(events, "down" if state in ("down", "gone") else "up",
                               start.timestamp(), now.timestamp()))
        if not offline:
            hour = dt.datetime.fromtimestamp(t, dt.timezone.utc).hour
            busy = 1 + 0.35 * math.sin((hour - 14) / 24 * 2 * math.pi)
            rtt = (12 + seed % 30) * busy + 3 * math.sin(t / 900 + seed)
            lost = 1 if (int(t) // 240 + seed) % 61 == 0 else 0
            pings.append({"t": t, "rtt": round(rtt, 2), "sent": 3, "rcvd": 3 - lost,
                          "lost": lost / 3})
        t += 240
    return {
        "id": int(probe_id), "hours": hours,
        "start": start.timestamp(), "end": now.timestamp(),
        "events": events,
        "segments": timeline(events, "down" if state in ("down", "gone") else "up",
                             start.timestamp(), now.timestamp()),
        "pings": pings, "ping_trouble": "", "asn": 15895, "country": "UA",
        "demo": True,
    }
