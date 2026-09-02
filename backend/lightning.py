"""Lightning from NASA, by way of the GOES Lightning Mappers.

Every GOES weather satellite carries a GLM -- a camera staring at the whole
disc of the earth, 500 times a second, watching for the flash of a lightning
stroke through the cloud tops. NASA republishes it through GIBS as map tiles,
the same service this app already uses for the daily cloud mosaic, so it needs
no account, no key and no parsing: it is a tile layer like any other.

What it does not do is cover the world. The GOES satellites sit over the
Americas -- East at 75 degrees west, West at 137 -- and a geostationary camera
sees rather less than a hemisphere. Europe, Africa and Asia are off the edge of
both, and no amount of waiting will put lightning there. That is a property of
where the satellites are, not a fault, and the panel says so plainly, because
a blank map that means "nothing is happening here" and a blank map that means
"nothing can be seen from here" look exactly alike.

The layer identifiers are not written down here. GIBS publishes its own
catalogue, so this asks for it and takes whatever GLM layers it actually
offers, with the time extents it actually has. Hard-coding a name is how you
ship a layer that quietly returns nothing the day it is renamed -- and how the
last version of this file failed, betting on a service whose protocol could
not be checked from here.
"""

from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from typing import Any

import requests

from . import config

ATTRIBUTION = "NASA GIBS · GOES GLM"

# The sentence the panel shows under an empty map. Written here beside the
# reason for it rather than in the page, so it cannot drift away from the fact.
COVERAGE = (
    "GOES watches the Americas and the oceans either side of them. Europe, "
    "Africa and Asia are over the horizon from both satellites, so they stay "
    "blank however active the weather is."
)

# GIBS's own catalogue of what it serves, in the projection the map uses.
CAPABILITIES = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
                "/1.0.0/WMTSCapabilities.xml")

# The tile template every GIBS layer follows. The front end fills this in.
TILES = ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"
         "/{layer}/default/{time}/{matrix}/{z}/{y}/{x}.{fmt}")

# What counts as live lightning: the GOES mapper, and nothing else.
#
# This has been wrong twice in opposite directions. First it wanted "glm" and
# "flash" together in the identifier and found nothing. Then it took anything
# saying "lightning", and found plenty -- OTD and DMSP-OLS, which are real NASA
# lightning data and are climatologies: multi-year averages from instruments
# that stopped collecting years ago. Drawn on a live map with a live legend
# they look exactly like tonight's storms, which is the worst of the three
# outcomes. Finding nothing is honest; finding the wrong thing is not.
#
# So both words, and both of them meaning what they say: it is GOES, and it is
# the Geostationary Lightning Mapper.
LIVE = ("goes", "glm")

# The giveaways that a lightning layer is an average rather than an
# observation. Any of these and it is history, whatever else the name says.
CLIMATOLOGY = ("climatology", "hrmc", "hrac", "hryc", "otd", "lis_",
               "_lis", "dmsp", "ols", "annual", "monthly", "mean")

# A wider net, used only to explain a miss. If nothing matches above, these are
# the layers worth showing to whoever has to work out why -- otherwise the
# answer is "no layers" with nothing to go on, which is where the last attempt
# left off.
NEARBY = ("glm", "lightning", "flash", "goes")

# The greyscale cloud-top picture the flashes are usually drawn over. Band 13
# is the clean infrared window -- what the satellite sees of cloud temperature,
# day or night, which is why a storm looks like a storm in it. GeoColor is the
# prettier composite and goes dark at night, so the infrared is preferred.
BACKDROP = ("band13", "clean_infrared", "cleanir", "geocolor", "infrared")

# The catalogue is a couple of megabytes and changes about never. Fetched once
# and held for the day.
CACHE_SECONDS = 6 * 3600

_NS = {
    "wmts": "http://www.opengis.net/wmts/1.0",
    "ows": "http://www.opengis.net/ows/1.1",
}

_lock = threading.Lock()
_cached: dict[str, Any] | None = None
_cached_at = 0.0


class LightningError(RuntimeError):
    pass


def _entry(layer: ET.Element, ident: str, title: str, low: str) -> dict[str, Any]:
    """One layer, in the shape the front end needs to build a tile URL."""
    matrix = layer.findtext(
        f"{{{_NS['wmts']}}}TileMatrixSetLink/{{{_NS['wmts']}}}TileMatrixSet")
    fmt = layer.findtext(f"{{{_NS['wmts']}}}Format") or "image/png"

    default = None
    values: list[str] = []
    for dim in layer.findall(f"{{{_NS['wmts']}}}Dimension"):
        if (dim.findtext(f"{{{_NS['ows']}}}Identifier") or "").lower() != "time":
            continue
        default = dim.findtext(f"{{{_NS['wmts']}}}Default")
        values = [v.text for v in dim.findall(f"{{{_NS['wmts']}}}Value") if v.text]

    return {
        "id": ident,
        "title": title,
        "matrix": matrix,
        "format": (fmt.rsplit("/", 1)[-1] or "png").replace("jpeg", "jpg"),
        "time_default": default,
        "time_values": values,
        # East and West see different halves of the Americas, and which one a
        # viewer wants depends on where they are looking.
        "satellite": "west" if "west" in low else "east" if "east" in low else None,
    }


def parse_layers(xml: str) -> dict[str, list[dict[str, Any]]]:
    """Pull the GOES layers out of a WMTS capabilities document.

    Three sorts. The picture people mean by "lightning from GOES" is two
    layers -- the flashes, and the greyscale infrared cloud tops they are drawn
    over, because on their own the flashes are dots in a void. The third is the
    climatologies, which are separated out precisely so they cannot be served
    as live.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise LightningError(f"GIBS sent a catalogue that would not parse: {exc}") from exc

    flashes: list[dict[str, Any]] = []
    cloud: list[dict[str, Any]] = []
    old: list[dict[str, Any]] = []
    for layer in root.iter(f"{{{_NS['wmts']}}}Layer"):
        ident = layer.findtext(f"{{{_NS['ows']}}}Identifier") or ""
        title = layer.findtext(f"{{{_NS['ows']}}}Title") or ident
        # Both, because GIBS is not consistent about which one carries the
        # instrument name.
        low = f"{ident} {title}".lower()

        stale = any(word in low for word in CLIMATOLOGY)
        if all(word in low for word in LIVE) and not stale:
            flashes.append(_entry(layer, ident, title, low))
        elif "lightning" in low and stale:
            # Kept, but kept apart. It is worth being able to look at, and it
            # must never be mistaken for what is happening now.
            old.append(_entry(layer, ident, title, low))
        elif "goes" in low and any(word in low for word in BACKDROP):
            cloud.append(_entry(layer, ident, title, low))

    for group in (flashes, cloud, old):
        group.sort(key=lambda entry: entry["id"])
    return {"lightning": flashes, "imagery": cloud, "climatology": old}


def near_misses(xml: str, limit: int = 12) -> list[str]:
    """Layer names worth looking at when nothing matched.

    A miss that says only "no layers" is a dead end -- it cannot tell you
    whether NASA has stopped publishing, renamed the product, or whether the
    filter is simply wrong. This lists what is actually there under any related
    word, so the next step is reading a list rather than guessing again.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    seen: list[str] = []
    for layer in root.iter(f"{{{_NS['wmts']}}}Layer"):
        ident = layer.findtext(f"{{{_NS['ows']}}}Identifier") or ""
        if any(word in ident.lower() for word in NEARBY) and ident not in seen:
            seen.append(ident)
        if len(seen) >= limit:
            break
    return seen


def _fetch() -> tuple[list[dict[str, Any]], list[str], int]:
    try:
        resp = requests.get(CAPABILITIES, timeout=30,
                            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        raise LightningError(f"NASA GIBS could not be reached: {exc}") from exc
    if not resp.ok:
        raise LightningError(f"NASA GIBS answered {resp.status_code}")
    found = parse_layers(resp.text)
    total = resp.text.count("</Layer>")
    misses = [] if found["lightning"] else near_misses(resp.text)
    return found, misses, total


def layers(refresh: bool = False) -> dict[str, Any]:
    """The GLM layers GIBS is currently serving, with the tile template."""
    global _cached, _cached_at
    with _lock:
        fresh = _cached is not None and time.time() - _cached_at < CACHE_SECONDS
        if fresh and not refresh:
            return dict(_cached)

    found, nearby, total = _fetch()
    answer = {
        "layers": found["lightning"],
        "imagery": found["imagery"],
        "climatology": found["climatology"],
        "template": TILES,
        "attribution": ATTRIBUTION,
        "coverage": COVERAGE,
        # Only populated when nothing matched, and then it is the whole point
        # of the response.
        "nearby": nearby,
        "catalogue_size": total,
    }
    with _lock:
        _cached = answer
        _cached_at = time.time()
    return dict(answer)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo() -> dict[str, Any]:
    """What the layer picker gets in the demo build.

    The same shape as the real answer, with the identifiers GIBS used when this
    was written. Demo mode has no network, and a picker with nothing in it
    would make the layer look broken rather than offline.
    """
    return {
        "layers": [
            {"id": "GOES-East_GLM_Flash_Extent_Density",
             "title": "GOES-East GLM Flash Extent Density (demo)",
             "matrix": "GoogleMapsCompatible_Level7", "format": "png",
             "time_default": "default", "time_values": [], "satellite": "east"},
            {"id": "GOES-West_GLM_Flash_Extent_Density",
             "title": "GOES-West GLM Flash Extent Density (demo)",
             "matrix": "GoogleMapsCompatible_Level7", "format": "png",
             "time_default": "default", "time_values": [], "satellite": "west"},
        ],
        "imagery": [
            {"id": "GOES-East_ABI_Band13_Clean_Infrared",
             "title": "GOES-East infrared cloud tops (demo)",
             "matrix": "GoogleMapsCompatible_Level7", "format": "png",
             "time_default": "default", "time_values": [], "satellite": "east"},
            {"id": "GOES-West_ABI_Band13_Clean_Infrared",
             "title": "GOES-West infrared cloud tops (demo)",
             "matrix": "GoogleMapsCompatible_Level7", "format": "png",
             "time_default": "default", "time_values": [], "satellite": "west"},
        ],
        "template": TILES,
        "attribution": "synthetic",
        "coverage": COVERAGE,
        "climatology": [],
        "nearby": [],
        "catalogue_size": 4,
    }
