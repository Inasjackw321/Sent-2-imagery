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
