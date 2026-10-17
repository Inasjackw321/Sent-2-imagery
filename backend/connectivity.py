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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from . import config

PROBES_URL = "https://atlas.ripe.net/api/v2/probes/"
ATTRIBUTION = "RIPE Atlas, RIPE NCC"

# RIPE's statuses. Connected and Disconnected are the two that say something
# about the network today. The others -- a probe still in its box, one RIPE
# has abandoned or written off after a long silence -- are drawn too, quietly,
# because the map is a census of every probe there is, not only of the ones
# that are news. A count that silently skipped a third of them read as "not
# all the data is here", and it was right.
NEVER_CONNECTED = 0
CONNECTED = 1
DISCONNECTED = 2

# The windows a change can be looked for over, in hours.
WINDOWS = {1: "1 h", 6: "6 h", 24: "24 h"}
DEFAULT_WINDOW = 6

# A probe offline for longer than this is not news, it is a probe nobody has
# plugged back in. Drawn small and dark grey rather than red, because a red dot
# reads as "this just happened" and a month-old one did not.
OFFLINE_TOO_LONG = dt.timedelta(days=7)

# How much of the probe list one view may ask for. RIPE Atlas pages at up to
# 500, and the whole fleet -- every probe that has ever existed, in every
# status -- is a little over forty thousand. Eighty pages is all of it, so a
# view of the whole world gets the whole world. The old limit was eight pages
# and four thousand probes, which cut a continental view off at whatever RIPE
# happened to list first.
PAGE_SIZE = 500
MOST_PAGES = 100
MOST_RETURNED = 60000

# Pages fetched at once. Once the first page says how many there are, the
# rest are asked for together rather than one after another -- eighty pages in
# a queue is over a minute; six at a time is a few seconds. Six, because it is
# a free service and this is one user's map.
PAGE_WORKERS = 6

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
             just a probe nobody plugged back in;
      dead   offline for more than a week;
      never  registered, never connected;
      inactive  abandoned or written off by RIPE, or a status this does not
             know -- drawn rather than dropped, because a probe nobody can
             account for is still a probe that was there.
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
        return "dead"
    if status == NEVER_CONNECTED:
        return "never"
    return "inactive"


# Every state a probe can be in, quietest first -- the order they are counted
# in, and the order the map draws them, so the news is on top.
ALL_STATES = ("never", "inactive", "dead", "up", "gone", "back", "down")


def read_probe(probe: dict[str, Any], window: dt.timedelta,
               now: dt.datetime) -> dict[str, Any] | None:
    """One probe, in the shape the map draws. None only for one with no place."""
    where = position_of(probe)
    if where is None:
        return None
    status = status_of(probe)
    since = since_of(probe)
    state = classify(status, since, window, now)
    text = " ".join(str(probe.get("description") or "").split())
    said = probe.get("status")
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
        # RIPE's own word for it, for the states this app lumps together:
        # "Abandoned" and "Written Off" are both "inactive" on the map.
        "status_name": str(said.get("name") or "")[:24] if isinstance(said, dict) else "",
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
        row = counts.setdefault(country, dict.fromkeys(ALL_STATES, 0))
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
    counts = dict.fromkeys(ALL_STATES, 0)
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
    if not getattr(resp, "ok", False):
        raise ConnectivityError(
            f"RIPE Atlas answered {getattr(resp, 'status_code', '?')}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ConnectivityError("RIPE Atlas sent something that is not JSON") from exc


def _fetch(box: tuple[float, float, float, float], get: Any) -> tuple[list[dict], bool]:
    """Every probe in the rectangle, whatever its status, every page of them.

    No status filter: the map draws all of them. The first page says how many
    there are; the rest are then asked for together, by page number, a few at
    a time. If an answer does not say how many there are, or its pages are not
    numbered, they are followed one by one through "next" instead -- slower,
    and the same answer.
    """
    west, south, east, north = box
    params: dict[str, Any] = {
        "latitude__gte": round(max(south, -90.0), 4),
        "latitude__lte": round(min(north, 90.0), 4),
        "longitude__gte": round(max(west, -180.0), 4),
        "longitude__lte": round(min(east, 180.0), 4),
        "page_size": PAGE_SIZE,
        "format": "json",
    }
    send = get or _session.get
    first = _ask(params, get)
    found: list[dict] = list(first.get("results") or [])
    following = first.get("next")
    if not following:
        return found, False

    numbered = page_urls(following, first.get("count"), len(found))
    if numbered is not None:
        wanted = numbered[:MOST_PAGES - 1]
        truncated = len(numbered) > len(wanted)

        def one(url: str) -> list[dict] | None:
            try:
                resp = send(url, timeout=TIMEOUT)
                if not getattr(resp, "ok", False):
                    return None
                return list(resp.json().get("results") or [])
            except (requests.RequestException, ValueError):
                return None

        with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as pool:
            for got in pool.map(one, wanted):
                if got is None:
                    # A page that failed costs that page, and the answer says
                    # it is partial rather than passing for the whole.
                    truncated = True
                else:
                    found.extend(got)
        return found, truncated

    pages = 1
    page = first
    while following:
        if pages >= MOST_PAGES:
            return found, True
        try:
            resp = send(following, timeout=TIMEOUT)
            if not getattr(resp, "ok", False):
                raise ValueError(getattr(resp, "status_code", "?"))
            page = resp.json()
        except (requests.RequestException, ValueError):
            return found, True
        found.extend(page.get("results") or [])
        following = page.get("next")
        pages += 1
    return found, False


def page_urls(following: str, count: Any, per_page: int) -> list[str] | None:
    """Every page after the first, by number, or None if that cannot be done.

    Worked out from the "next" link RIPE gave -- so every filter it carries is
    kept exactly -- with only its page number changed. None when the answer
    did not say how many there are, or the link has no page number to change
    (a cursor rather than a page), and then the pages are walked instead.
    """
    if not isinstance(count, int) or count <= 0 or per_page <= 0:
        return None
    parts = urllib.parse.urlsplit(following)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "page" for k, _v in query):
        return None
    total = math.ceil(count / per_page)
    out = []
    for number in range(2, total + 1):
        rebuilt = [(k, str(number) if k == "page" else v) for k, v in query]
        out.append(urllib.parse.urlunsplit(parts._replace(
            query=urllib.parse.urlencode(rebuilt))))
    return out


def probes(bbox: tuple[float, float, float, float], hours: int = DEFAULT_WINDOW,
           get: Any = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Every probe in a rectangle that says something about connectivity now."""
    if hours not in WINDOWS:
        hours = DEFAULT_WINDOW
    raw, truncated = _raw_probes(bbox, get)
    return shape(raw, hours, bbox, truncated, now)


def _raw_probes(bbox: tuple[float, float, float, float],
                get: Any = None) -> tuple[list[dict], bool]:
    """The probe list for a rectangle, held for a couple of minutes.

    Shared by the map and the area graph, so drawing a box over what is
    already on screen does not ask RIPE for the same probes a second time.
    """
    west, south, east, north = bbox
    key = f"{west:.2f},{south:.2f},{east:.2f},{north:.2f}"
    with _lock:
        held = _held.get(key)
    if held and time.time() - held[0] < CACHE_SECONDS and get is None:
        return held[1]["raw"], held[1]["truncated"]
    raw, truncated = _fetch(bbox, get)
    if get is None:
        with _lock:
            if len(_held) >= CACHE_BOXES:
                _held.pop(min(_held, key=lambda k: _held[k][0]), None)
            _held[key] = (time.time(), {"raw": raw, "truncated": truncated})
    return raw, truncated


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
    weight = {"down": 0, "back": 1, "gone": 2, "up": 3, "dead": 4, "inactive": 5, "never": 6}
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
        # The rest of the census, so the offline build shows every kind of
        # probe the live one does.
        elif i % 11 == 0:
            status, ago = 3, dt.timedelta(days=200)
        elif i % 13 == 0:
            status, ago = NEVER_CONNECTED, dt.timedelta(days=90)
        elif i % 19 == 0:
            status, ago = DISCONNECTED, dt.timedelta(days=30)
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
                       "name": {CONNECTED: "Connected", DISCONNECTED: "Disconnected",
                                NEVER_CONNECTED: "Never Connected"}.get(status, "Abandoned"),
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
HISTORY = {24: "24 h", 48: "48 h", 168: "7 days"}
DEFAULT_HISTORY = 24

# How long one probe's history is held. The graph window refreshes itself every
# minute, like the seismograph traces, and a probe pings every four -- so this
# is about two windows open on the same probe not asking twice.
HISTORY_SECONDS = 60
HISTORY_HELD = 64

PLOT_SIZE = (760, 260)

_history_held: dict[tuple[int, int], tuple[float, dict[str, Any]]] = {}


def _results(msm: int, probe_id: int | str, start: dt.datetime, stop: dt.datetime,
             get: Any) -> list[dict[str, Any]]:
    """One measurement's results for one probe, or for a comma-separated list."""
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


# ── A whole area over time ─────────────────────────────────────
#
# Draw a box on the map and every probe inside it becomes one line: how many
# of them were online, through the last day, two days or week. One probe
# going dark is a household. A region's line falling by a third in an hour is
# a power grid, an ISP, or a strike on infrastructure -- and it is the line
# that says which.

AREA_HISTORY = {24: "24 h", 48: "48 h", 168: "7 days"}
DEFAULT_AREA_HISTORY = 48

# How finely the line is sampled. Fine enough that an outage of an hour shows
# as a dip rather than a single column, coarse enough that a week is still a
# readable number of bars.
AREA_STEP_MINUTES = {24: 10, 48: 20, 168: 60}

# How many probes one graph is made from, and how many are asked about per
# request. A box over half of Europe holds thousands; the nearest few hundred
# to its middle say what the region is doing, and the graph says it is a
# sample when it is one.
MOST_AREA_PROBES = 2000
IDS_PER_ASK = 100
# Batches of connection logs asked for at once. Twenty batches one after
# another was most of the wait for a big area.
AREA_WORKERS = 4

# How many corners a drawn area may have. Plenty for tracing a coastline or a
# province border by hand, and a ceiling on what one request can carry.
MOST_CORNERS = 200

# Below this share of probes the line is drawn in the colour for trouble. The
# middle band is for the ordinary wobble of volunteers' home connections,
# which is never a flat 100 %.
AREA_GOOD = 0.95
AREA_TROUBLE = 0.85

AREA_SECONDS = 120
_area_held: dict[str, tuple[float, dict[str, Any]]] = {}

# The mark on every area graph. The same mark, spelled the same way, as the
# region copier and the saved video put on theirs.
WATERMARK = "@Kaldockhi"
AREA_PLOT_SIZE = (1600, 900)


def area_members(raw: list[dict[str, Any]], start: dt.datetime,
                 bbox: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """The probes whose line belongs on the graph.

    Online now, or gone offline since the window began -- both were online at
    some point inside it. A probe that was already offline before the window
    started is left out: it would sit at zero the whole way across and pull
    the line down for something that did not happen in the window.
    """
    west, south, east, north = bbox
    cx, cy = (west + east) / 2, (south + north) / 2
    out = []
    for probe in raw:
        where = position_of(probe)
        status = status_of(probe)
        if where is None or probe.get("id") is None:
            continue
        since = since_of(probe)
        if status == CONNECTED:
            state = "up"
        elif status == DISCONNECTED and since is not None and since >= start:
            state = "down"
        else:
            continue
        out.append({"id": int(probe["id"]), "lat": where[0], "lon": where[1],
                    "state": state, "since": since,
                    "country": str(probe.get("country_code") or "").upper()[:2] or None})
    out.sort(key=lambda p: math.hypot(p["lon"] - cx, p["lat"] - cy))
    return out


def online_series(timelines: list[list[tuple[float, float, str]]],
                  start: float, end: float, step: float) -> list[tuple[float, int]]:
    """(time, how many were online) at each step through the window.

    Read at the middle of each step rather than its edge, so an outage that
    starts a minute into a step is in that step and not the next one.
    """
    out = []
    t = start
    while t < end:
        middle = min(t + step / 2, end)
        online = 0
        for stretches in timelines:
            for a, b, state in stretches:
                if a <= middle < b or (b == end and middle == end):
                    online += state == "up"
                    break
        out.append((t, online))
        t += step
    return out


def summarise_area(series: list[tuple[float, int]], members: int) -> dict[str, Any]:
    """The numbers the graph is about: now, the low point, and when.

    Normalised to the most that were online at once in the window, which is
    what "100 %" means on this graph. Not to the number of probes: some of
    those are on connections that were never all up together, and a line
    that never reached the top would read as a region permanently degraded.
    """
    counts = [n for _t, n in series]
    peak = max(counts) if counts else 0
    shares = [n / peak if peak else 0.0 for n in counts]
    low_at = min(range(len(shares)), key=lambda i: shares[i]) if shares else None
    return {
        "shares": shares,
        "peak": peak,
        "now": shares[-1] if shares else None,
        "low": shares[low_at] if low_at is not None else None,
        "low_at": series[low_at][0] if low_at is not None else None,
        "online_now": counts[-1] if counts else 0,
        "members": members,
    }


def _area_name(bbox: tuple[float, float, float, float],
               members: list[dict[str, Any]], namer: Any) -> str:
    """What to call the box: the region it is in, if it is small enough to be
    in one, and otherwise the countries its probes are in."""
    west, south, east, north = bbox
    small = (east - west) <= 4.5 and (north - south) <= 3.0
    if small and namer is not None:
        try:
            name = namer((south + north) / 2, (west + east) / 2)
        except Exception:  # noqa: BLE001 -- a name is a nicety, never a failure
            name = None
        if name:
            return str(name)
    counts: dict[str, int] = {}
    for m in members:
        if m["country"]:
            counts[m["country"]] = counts.get(m["country"], 0) + 1
    if counts:
        top = sorted(counts, key=lambda c: -counts[c])[:3]
        return " · ".join(top) + (" and more" if len(counts) > 3 else "")
    return (f"{abs((south + north) / 2):.2f}°{'N' if north + south >= 0 else 'S'} "
            f"{abs((west + east) / 2):.2f}°{'E' if west + east >= 0 else 'W'}")


def inside(lon: float, lat: float, polygon: list[tuple[float, float]]) -> bool:
    """Whether a point is inside a drawn area, by counting edge crossings.

    The ordinary even-odd rule: a ray from the point crosses the outline an
    odd number of times if the point is inside. Works for any shape a hand can
    click out, concave ones included.
    """
    hit = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < cross:
                hit = not hit
    return hit


def read_corners(said: str) -> list[tuple[float, float]]:
    """A drawn area, from "lon,lat;lon,lat;...". Raises ValueError if it is not one."""
    corners = []
    for pair in (said or "").split(";"):
        if not pair.strip():
            continue
        lon_s, lat_s = pair.split(",")
        lon, lat = float(lon_s), float(lat_s)
        if not (-180 <= lon <= 180 and -90 <= lat <= 90) or math.isnan(lon + lat):
            raise ValueError(f"{pair} is not a place on Earth")
        corners.append((lon, lat))
    if len(corners) > 1 and corners[0] == corners[-1]:
        corners.pop()
    if not 3 <= len(corners) <= MOST_CORNERS:
        raise ValueError(f"an area needs 3 to {MOST_CORNERS} corners, not {len(corners)}")
    return corners


def corners_box(corners: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    return (min(c[0] for c in corners), min(c[1] for c in corners),
            max(c[0] for c in corners), max(c[1] for c in corners))


def left_out(raw: list[dict[str, Any]], start: dt.datetime) -> dict[str, int]:
    """The probes in the area that are not in the line, and why.

    Said on the picture, because a graph of "47 probes" over an area holding
    eighty reads as data that was not collected -- unless it says the other
    thirty-three were collected, and what they were.
    """
    out = {"offline_before": 0, "never": 0, "inactive": 0}
    for probe in raw:
        if position_of(probe) is None:
            continue
        status = status_of(probe)
        since = since_of(probe)
        if status == CONNECTED:
            continue
        if status == DISCONNECTED:
            if since is not None and since >= start:
                continue
            out["offline_before"] += 1
        elif status == NEVER_CONNECTED:
            out["never"] += 1
        else:
            out["inactive"] += 1
    return out


def area_history(bbox: tuple[float, float, float, float],
                 hours: int = DEFAULT_AREA_HISTORY, get: Any = None,
                 now: dt.datetime | None = None, namer: Any = None,
                 corners: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    """How much of an area's internet was up, through the window.

    The area is the box, or -- when corners are given -- the shape they
    outline, with the box only as what RIPE is asked about.
    """
    if hours not in AREA_HISTORY:
        hours = DEFAULT_AREA_HISTORY
    if corners:
        bbox = corners_box(corners)
    west, south, east, north = bbox
    shape_key = hashlib.sha1(repr(corners).encode()).hexdigest()[:12] if corners else "box"
    key = f"{west:.3f},{south:.3f},{east:.3f},{north:.3f},{hours},{shape_key}"
    if get is None:
        with _lock:
            held = _area_held.get(key)
        if held and time.time() - held[0] < AREA_SECONDS:
            return held[1]
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=hours)

    raw, truncated = _raw_probes(bbox, get)
    if corners:
        raw = [r for r in raw
               if (at := position_of(r)) is not None and inside(at[1], at[0], corners)]
    every = area_members(raw, start, bbox)
    members = every[:MOST_AREA_PROBES]

    ids = [m["id"] for m in members]
    chunks = [",".join(str(n) for n in ids[i:i + IDS_PER_ASK])
              for i in range(0, len(ids), IDS_PER_ASK)]
    events: dict[int, list[dict[str, Any]]] = {}
    # Together, a few at a time. A batch that fails fails the graph rather
    # than being left out: its probes would otherwise be drawn as if nothing
    # had happened to them, which is a line that is wrong without saying so.
    with ThreadPoolExecutor(max_workers=AREA_WORKERS) as pool:
        for rows in pool.map(lambda c: _results(CONNECTION_MSM, c, start, now, get), chunks):
            for row in rows:
                events.setdefault(row.get("prb_id"), []).append(row)

    found = _aggregate(members, events, start, now, hours)
    found.update({
        "bbox": list(bbox),
        "corners": [list(c) for c in corners] if corners else None,
        "name": _area_name(bbox, members, namer),
        "sampled": len(every) > len(members) or truncated,
        "in_box": len(every),
        "left_out": left_out(raw, start),
    })
    if get is None:
        with _lock:
            if len(_area_held) >= HISTORY_HELD:
                _area_held.pop(min(_area_held, key=lambda k: _area_held[k][0]), None)
            _area_held[key] = (time.time(), found)
    return found


def _aggregate(members: list[dict[str, Any]], events: dict[Any, list[dict[str, Any]]],
               start: dt.datetime, now: dt.datetime, hours: int) -> dict[str, Any]:
    """Each probe's online stretches, added up into one line."""
    s, e = start.timestamp(), now.timestamp()
    timelines = [timeline(read_events(events.get(m["id"], [])), m["state"], s, e)
                 for m in members]
    series = online_series(timelines, s, e, AREA_STEP_MINUTES[hours] * 60)
    countries: dict[str, int] = {}
    for m in members:
        countries[m["country"] or "??"] = countries.get(m["country"] or "??", 0) + 1
    return {
        "hours": hours,
        "start": s,
        "end": e,
        "series": series,
        "countries": dict(sorted(countries.items(), key=lambda kv: -kv[1])),
        **summarise_area(series, len(members)),
    }


def demo_area(bbox: tuple[float, float, float, float], hours: int = DEFAULT_AREA_HISTORY,
              now: dt.datetime | None = None,
              corners: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    """Sixty synthetic probes in the box, with an outage that hits a third of them.

    Its own sixty rather than the dots the demo map draws there: those are
    scattered for a map, and a box drawn over a few of them would make a
    graph of three probes, which shows nothing.

    Run through the same aggregation as the real thing, so the offline build
    exercises the arithmetic rather than drawing a picture of it.
    """
    if hours not in AREA_HISTORY:
        hours = DEFAULT_AREA_HISTORY
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(hours=hours)
    if corners:
        bbox = corners_box(corners)
    west, south, east, north = bbox
    seed = int(hashlib.sha1(f"{west:.1f},{south:.1f}".encode()).hexdigest()[:8], 16)
    members, events = [], {}
    outage = now - dt.timedelta(hours=2, minutes=40)
    for i in range(90 if corners else 60):
        pid = 2_000_000 + i
        a = ((seed + i * 7919) % 10007) / 10007
        b = ((seed // 3 + i * 104729) % 10009) / 10009
        hit = i % 3 == 0
        state = "down" if hit and i % 6 == 0 else "up"
        rows = []
        # The ordinary wobble: a home connection dropping for a few minutes
        # now and then, somewhere in the window.
        if i % 7 == 0:
            blip = start + dt.timedelta(hours=(i * 5) % hours, minutes=10)
            rows += [{"prb_id": pid, "timestamp": blip.timestamp(), "event": "disconnect"},
                     {"prb_id": pid, "timestamp": (blip + dt.timedelta(minutes=35)).timestamp(),
                      "event": "connect"}]
        if hit:
            rows.append({"prb_id": pid, "timestamp": (outage + dt.timedelta(minutes=i % 9)).timestamp(),
                         "event": "disconnect"})
            if state == "up":
                rows.append({"prb_id": pid, "timestamp": (outage + dt.timedelta(minutes=70 + i)).timestamp(),
                             "event": "connect"})
        lat, lon = south + (north - south) * b, west + (east - west) * a
        # A drawn shape keeps only the probes inside it, as the real one does.
        if corners and not inside(lon, lat, corners):
            continue
        events[pid] = rows
        members.append({"id": pid, "lat": lat, "lon": lon, "state": state,
                        "since": None, "country": "UA"})
    found = _aggregate(members, events, start, now, hours)
    found.update({"bbox": list(bbox), "name": "Synthetic region", "sampled": False,
                  "corners": [list(c) for c in corners] if corners else None,
                  "in_box": len(members), "demo": True,
                  "left_out": {"offline_before": 4, "never": 2, "inactive": 7}})
    return found


# ── Drawing the area graph ─────────────────────────────────────


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow older than 10.1 has only the small bitmap face. The graph is
        # still a graph; the words are just smaller.
        return ImageFont.load_default()


def _fitted(draw: Any, text: str, width: float, sizes: tuple[int, ...] = (22, 20, 18, 16)):
    """The largest of these faces the text fits in, and the text -- shortened
    with an ellipsis only if even the smallest will not hold it. Measured,
    rather than cut at a character count, which chopped "never connected" to
    "never" on one picture and let another run off the edge."""
    for size in sizes:
        face = _font(size)
        if draw.textlength(text, font=face) <= width:
            return face, text
    face = _font(sizes[-1])
    while text and draw.textlength(text + "…", font=face) > width:
        text = text[:-1]
    return face, text.rstrip(" ,·") + "…"


_AREA_BG = (9, 14, 27)
_AREA_INK = (226, 232, 244)
_AREA_DIM = (128, 141, 166)
_AREA_GRID = (34, 44, 66)
_AREA_GOOD = (46, 196, 182)
_AREA_WOBBLE = (255, 183, 3)
_AREA_BAD = (255, 93, 93)


def area_colour(share: float) -> tuple[int, int, int]:
    """Teal when nearly everything is up, amber for the ordinary wobble, red
    when enough has gone that it is a regional event rather than a few homes."""
    if share >= AREA_GOOD:
        return _AREA_GOOD
    if share >= AREA_TROUBLE:
        return _AREA_WOBBLE
    return _AREA_BAD


def plot_area(found: dict[str, Any]) -> bytes:
    """The area graph: one bar per step, coloured by how much was up.

    Deliberately its own look: bars rather than a filled line, a left-aligned
    title, the numbers that matter as large figures across the top, the scale
    on the right, and the mark across the middle.
    """
    from PIL import Image, ImageDraw

    width, height = AREA_PLOT_SIZE
    image = Image.new("RGB", (width, height), _AREA_BG)
    draw = ImageDraw.Draw(image)
    # A faint lift towards the top, so the picture is not one flat slab.
    for y in range(0, 260):
        shade = int(10 * (1 - y / 260))
        draw.line([(0, y), (width, y)], fill=(9 + shade, 14 + shade, 27 + shade * 2))

    left, right, top, bottom = 70, width - 130, 250, height - 120
    big, mid, small = _font(54), _font(30), _font(22)

    # Title and the line under it.
    draw.text((left, 52), f"Internet connectivity · {found['name']}", font=big, fill=_AREA_INK)
    start = dt.datetime.fromtimestamp(found["start"], dt.timezone.utc)
    end = dt.datetime.fromtimestamp(found["end"], dt.timezone.utc)
    # "to" rather than a dash: the face Pillow ships has no en dash, and draws
    # it as an empty box.
    when = (f"{start:%d %b %H:%M} to {end:%d %b %H:%M} UTC" if found["hours"] <= 48
            else f"{start:%d %b} to {end:%d %b %Y} UTC")
    gone = found.get("left_out") or {}
    not_in = sum(gone.values())
    said = (f"{when}   ·   {found['members']} probes in the line"
            + (f" (nearest of {found['in_box']})" if found.get("sampled") else "")
            + (f"   ·   {not_in} more in the area, not counted: "
               + ", ".join(f"{n} {label}" for n, label in (
                   (gone.get("offline_before", 0), "offline before the window"),
                   (gone.get("inactive", 0), "inactive"),
                   (gone.get("never", 0), "never connected")) if n)
               if not_in else ""))
    face, said = _fitted(draw, said, width - 2 * left)
    draw.text((left, 122), said, font=face, fill=_AREA_DIM)

    # The figures: now, the low point, and how many probes it rests on.
    figures = [
        ("NOW", f"{round(100 * found['now'])}%" if found["now"] is not None else "—",
         area_colour(found["now"] or 0)),
        ("LOWEST", f"{round(100 * found['low'])}%" if found["low"] is not None else "—",
         area_colour(found["low"] or 0)),
        ("ONLINE NOW", f"{found['online_now']} of {found['members']}", _AREA_INK),
    ]
    x = left
    for label, value, colour in figures:
        draw.text((x, 166), label, font=small, fill=_AREA_DIM)
        draw.text((x, 190), value, font=mid, fill=colour)
        x += 300

    # The scale, on the right, with the reference lines behind the bars.
    for share in (0, 0.25, 0.5, 0.75, 1.0):
        y = bottom - share * (bottom - top)
        for xx in range(left, right, 14):
            draw.line([(xx, y), (min(xx + 7, right), y)], fill=_AREA_GRID)
        draw.text((right + 16, y - 12), f"{round(share * 100)}%", font=small, fill=_AREA_DIM)

    # Each step is a column: a dim body, and a bright cap at the level. Solid
    # columns turned the chart into one slab, in which a dip of two probes
    # was a notch nobody could see; the caps make the level itself the thing
    # that is drawn, and the body only says which side of it is "up".
    #
    # Edges on whole pixels, worked out from the step's position rather than
    # accumulated, or the rounding leaves a stray dark seam every few columns.
    shares = found["shares"]
    count = max(len(shares), 1)
    span = right - left
    gap = 1 if span / count > 4 else 0
    for i, share in enumerate(shares):
        x0 = left + round(i * span / count)
        x1 = left + round((i + 1) * span / count) - 1 - gap
        y0 = round(bottom - share * (bottom - top))
        colour = area_colour(share)
        body = tuple(round(c * 0.32 + b * 0.68) for c, b in zip(colour, _AREA_BG))
        draw.rectangle([x0, y0, max(x1, x0), bottom], fill=body)
        draw.rectangle([x0, y0, max(x1, x0), min(y0 + 4, bottom)], fill=colour)

    # The low point, marked, so the eye goes where the story is.
    if found.get("low_at") is not None and found["low"] is not None and found["low"] < AREA_GOOD:
        i = next((k for k, (t, _n) in enumerate(found["series"]) if t == found["low_at"]), None)
        if i is not None:
            x = left + (i + 0.5) * span / count
            y = bottom - found["low"] * (bottom - top)
            draw.polygon([(x, y - 8), (x - 10, y - 26), (x + 10, y - 26)], fill=_AREA_INK)

    # Times along the bottom.
    hours = found["hours"]
    marks = 6 if hours <= 48 else 7
    for k in range(marks + 1):
        t = found["start"] + (found["end"] - found["start"]) * k / marks
        x = left + (right - left) * k / marks
        moment = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        label = moment.strftime("%H:%M") if hours <= 24 else (
            moment.strftime("%a %H:%M") if hours <= 48 else moment.strftime("%a %d"))
        draw.line([(x, bottom), (x, bottom + 8)], fill=_AREA_DIM)
        draw.text((x - 34, bottom + 14), label, font=small, fill=_AREA_DIM)

    # The source, and the key to the colours.
    footer = ("Share of probes online, against the most up at once · RIPE Atlas, RIPE NCC, "
              "connection log (measurement 7000) · positions blurred by RIPE"
              + (" · drawn area" if found.get("corners") else ""))
    face, footer = _fitted(draw, footer, width - 2 * left)
    draw.text((left, height - 52), footer, font=face, fill=_AREA_DIM)
    # In the row of figures, at the right, where it is read with them -- it
    # used to sit along the bottom and land on the times.
    kx = right - 470
    for colour, label in ((_AREA_GOOD, "95% and up"), (_AREA_WOBBLE, "85 to 95%"),
                          (_AREA_BAD, "under 85%")):
        draw.rectangle([kx, 199, kx + 18, 217], fill=colour)
        draw.text((kx + 26, 194), label, font=small, fill=_AREA_DIM)
        kx += 165

    # The mark: large and faint across the chart, and small and solid in the
    # corner, so a crop that loses one keeps the other.
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((width / 2, (top + bottom) / 2), WATERMARK,
                               font=_font(150), fill=(255, 255, 255, 30), anchor="mm")
    layer = layer.rotate(-12, resample=Image.BICUBIC, center=(width / 2, (top + bottom) / 2))
    image = Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")
    ImageDraw.Draw(image).text((width - 70, 60), WATERMARK, font=mid,
                               fill=_AREA_INK, anchor="ra")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
