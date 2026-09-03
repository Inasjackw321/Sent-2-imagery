"""Lightning from Meteosat Third Generation, over Europe and Africa.

MTG-I1 carries the Lightning Imager: four cameras on a satellite parked over
the Gulf of Guinea, watching the whole disc a thousand times a second for the
flash of a stroke. It sees Europe, Africa, the Middle East and the Atlantic --
which is the half of the world the American satellites cannot see, and the half
this app is mostly pointed at.

EUMETSAT publishes it through their View service as ordinary WMS, no account
and no key, which is why this can be a map layer rather than a data pipeline.

Nothing here names a layer. EUMETSAT publishes a capabilities document saying
what it serves and for what times, so this reads that and uses what is actually
there.

The important part is the liveness test, and it is deliberately not about
names. The last attempt at this feature failed by matching a product whose name
said "lightning" and whose contents were a twenty-year average, drawn on a live
map as though it were tonight. Names cannot be trusted to say that. Timestamps
can: every WMS layer declares the times it holds, so a layer is offered here
only if its newest frame is recent enough to be about now. A climatology
declares a range ending years ago and is refused by arithmetic rather than by
vocabulary -- which is a guarantee, not a hope.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from . import config

ATTRIBUTION = "EUMETSAT · MTG Lightning Imager"

# Where MTG can see. It sits at zero degrees longitude over the equator, so
# unlike the American satellites this one is looking straight at the part of
# the world this app is mostly used for.
COVERAGE = (
    "MTG sits over the Gulf of Guinea and sees Europe, Africa, the Middle East "
    "and the Atlantic. The Americas, Asia and the Pacific are over its horizon."
)

# The centre of the disc, and how far from it the instrument still sees.
SUB_SATELLITE_LON = 0.0
REACH_DEGREES = 70.0

# EUMETSAT's own catalogue of what View serves.
CAPABILITIES = ("https://view.eumetsat.int/geoserver/ows"
                "?service=WMS&version=1.3.0&request=GetCapabilities")

# Where the browser asks for the pictures themselves.
WMS = "https://view.eumetsat.int/geoserver/ows"

# How fresh the newest frame has to be for a layer to count as live.
#
# The Lightning Imager publishes accumulated products every few minutes, so an
# hour is generous. What this number really does is refuse anything older by a
# wide margin -- a climatology, a case study, a reprocessed archive -- whatever
# its name suggests.
LIVE_WITHIN = dt.timedelta(hours=6)

# What to look for. LI is the lightning instrument; FCI is the imager whose
# infrared makes the cloud tops the flashes sit in.
LIGHTNING = ("li_", "lightning", "lfl", "afa", "acc_flash")
BACKDROP = ("fci", "ir_", "infrared", "cloud", "truecolour", "true_colour")

# The catalogue is large and changes rarely.
CACHE_SECONDS = 3 * 3600

_WMS_NS = "{http://www.opengis.net/wms}"

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


class MTGError(RuntimeError):
    pass


def _text(node: ET.Element, tag: str) -> str | None:
    found = node.find(f"{_WMS_NS}{tag}")
    if found is None:
        found = node.find(tag)
    return found.text if found is not None else None


def newest_time(extent: str) -> dt.datetime | None:
    """The most recent instant a WMS time dimension covers.

    Three shapes turn up: a bare list of instants, a start/end/period
    interval, and a mixture. The end of an interval is its newest point, and
    for a list it is the last entry -- but only after sorting, because nothing
    guarantees the order.
    """
    if not extent:
        return None
    stamps: list[dt.datetime] = []
    for part in extent.replace("\n", "").split(","):
        piece = part.strip()
        if not piece:
            continue
        # start/end/period -- the middle term is the one that matters.
        if "/" in piece:
            piece = piece.split("/")[1]
        try:
            stamps.append(dt.datetime.fromisoformat(piece.replace("Z", "+00:00")))
        except ValueError:
            continue
    if not stamps:
        return None
    newest = max(stamps)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=dt.timezone.utc)
    return newest


def _stamp(when: dt.datetime) -> str:
    """An instant in the form WMS wants: UTC, seconds, trailing Z."""
    return when.astimezone(dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _entry(node: ET.Element, name: str,
           title: str) -> tuple[dt.datetime | None, dict[str, Any]]:
    default = None
    extent = ""
    for dim in list(node.findall(f"{_WMS_NS}Dimension")) + list(node.findall("Dimension")):
        if (dim.get("name") or "").lower() != "time":
            continue
        default = dim.get("default")
        extent = (dim.text or "").strip()
    newest = newest_time(extent) or newest_time(default or "")
    return newest, {
        "id": name,
        "title": title,
        # Handed to WMS as the TIME parameter, so it is normalised to the
        # trailing-Z form servers expect rather than passed on as whatever the
        # catalogue happened to write. GeoServer accepts +00:00 but not every
        # WMS does, and this costs nothing.
        "time_default": _stamp(newest) if newest else default,
        "newest": _stamp(newest) if newest else None,
    }


def parse_layers(xml: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Sort EUMETSAT's catalogue into what this app can use.

    `live` is the whole point: a layer earns that list by declaring a recent
    frame, not by being called the right thing.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise MTGError(f"EUMETSAT sent a catalogue that would not parse: {exc}") from exc

    live: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    imagery: list[dict[str, Any]] = []

    for node in root.iter(f"{_WMS_NS}Layer"):
        name = _text(node, "Name")
        if not name:
            # A grouping layer with no name of its own cannot be requested.
            continue
        title = _text(node, "Title") or name
        low = f"{name} {title}".lower()

        is_lightning = any(word in low for word in LIGHTNING)
        is_backdrop = any(word in low for word in BACKDROP)
        if not (is_lightning or is_backdrop):
            continue

        newest, entry = _entry(node, name, title)
        fresh = newest is not None and now - newest <= LIVE_WITHIN
        entry["live"] = fresh
        entry["age_minutes"] = (
            round((now - newest).total_seconds() / 60) if newest else None)

        if is_lightning:
            (live if fresh else stale).append(entry)
        elif fresh:
            imagery.append(entry)

    for group in (live, stale, imagery):
        group.sort(key=lambda item: item["id"])
    return {"live": live, "stale": stale, "imagery": imagery}


def _fetch() -> dict[str, Any]:
    try:
        resp = requests.get(CAPABILITIES, timeout=40,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise MTGError(f"EUMETSAT View could not be reached: {exc}") from exc
    if not resp.ok:
        raise MTGError(f"EUMETSAT View answered {resp.status_code}")
    found = parse_layers(resp.text)
    found["catalogue_size"] = resp.text.count("<Name>")
    return found


def layers(refresh: bool = False) -> dict[str, Any]:
    """The MTG layers EUMETSAT is serving now, live ones first."""
    global _cached, _cached_at
    with _lock:
        if _cached is not None and not refresh \
                and time.time() - _cached_at < CACHE_SECONDS:
            return dict(_cached)

    found = _fetch()
    answer = {
        **found,
        "wms": WMS,
        "attribution": ATTRIBUTION,
        "coverage": COVERAGE,
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
    }
    with _lock:
        _cached = answer
        _cached_at = time.time()
    return dict(answer)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo() -> dict[str, Any]:
    """The same shape as a live answer, for the build with no network."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    recent = _stamp(now - dt.timedelta(minutes=5))
    return {
        "live": [
            {"id": "mtg_fd:li_afa", "title": "Accumulated Flash Area (demo)",
             "time_default": recent, "newest": recent, "age_minutes": 5, "live": True},
            {"id": "mtg_fd:li_afr", "title": "Accumulated Flash Radiance (demo)",
             "time_default": recent, "newest": recent, "age_minutes": 5, "live": True},
        ],
        "stale": [],
        "imagery": [
            {"id": "mtg_fd:ir105", "title": "FCI infrared 10.5 µm (demo)",
             "time_default": recent, "newest": recent, "age_minutes": 5, "live": True},
        ],
        "catalogue_size": 3,
        "wms": WMS,
        "attribution": "synthetic",
        "coverage": COVERAGE,
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
    }
