"""The rest of the Sentinel family, as live map layers.

Sentinel-1 and Sentinel-2 are in the imagery flow: you draw an area, pick a
date, and a scene is rendered from cloud-optimised GeoTIFFs read a window at a
time. Sentinel-3 and Sentinel-5P cannot join them, and the reason is worth
stating rather than leaving as an unexplained gap.

Those two are published as NetCDF granules -- whole swaths in one file, not
tiled, not cloud-optimised. Reading a hundred-kilometre box out of one means
downloading the entire granule, and the pipeline here is built on windowed
reads of COGs. It is not a small change; it is a different pipeline.

But they are served as ordinary WMS by EUMETSAT View, the same service the
lightning layer already uses, with no account and no key. So they arrive here
as live map layers instead: a picture of the whole disc or swath, current,
drawn under the imagery.

What each one is for:

  Sentinel-3   OLCI and SLSTR. Three hundred metres, and the whole planet
               every day or two -- coarse next to Sentinel-2's ten metres, and
               the only one of the family that sees everywhere every day. Ocean
               colour, land surface temperature, fires, and true colour at a
               scale where you can see a weather system rather than a field.

  Sentinel-5P  TROPOMI. Atmospheric chemistry at about seven kilometres,
               daily: nitrogen dioxide over cities and shipping lanes, methane,
               carbon monoxide from fires, sulphur dioxide from volcanoes. Not
               a picture of the ground at all -- a picture of the air above it.

As with the lightning, nothing here names a layer. EUMETSAT publishes what it
serves and for when; this reads that and offers what is actually there, and a
layer reaches the live list only by declaring a recent frame. A product whose
name says "Sentinel-3" and whose contents are a 2019 reprocessing is refused
by arithmetic rather than by vocabulary.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from . import mtg

# What to look for, and what to call it. The keys are matched against a
# layer's name and title, lower-cased; the first family that matches wins.
#
# Deliberately broad. EUMETSAT renames and reorganises, and a family that
# matches nothing shows as empty rather than as an error -- which is the
# honest outcome and tells whoever is looking that the catalogue has moved.
FAMILIES: tuple[dict[str, Any], ...] = (
    {
        "key": "sentinel-5p",
        "short": "Sentinel-5P",
        "label": "Sentinel-5P · atmospheric chemistry",
        "words": ("sentinel-5p", "sentinel_5p", "s5p", "tropomi"),
        "colour": "#c58cff",
        "resolution": "≈7 km",
        "about": "TROPOMI measures the air rather than the ground: nitrogen "
                 "dioxide over cities and shipping lanes, methane, carbon "
                 "monoxide from fires, sulphur dioxide from volcanoes. Daily, "
                 "and coarse by design — a single pixel is a small county.",
    },
    {
        "key": "sentinel-3",
        "short": "Sentinel-3",
        "label": "Sentinel-3 · ocean and land, daily",
        "words": ("sentinel-3", "sentinel_3", "olci", "slstr", "s3a", "s3b"),
        "colour": "#4ce0b3",
        "resolution": "300 m – 1 km",
        "about": "Three hundred metres and the whole planet every day or two. "
                 "Coarse next to Sentinel-2's ten, and the only Sentinel that "
                 "sees everywhere every day — a weather system rather than a "
                 "field.",
    },
)

# How many days of history to offer.
#
# A single instant from one of these is one orbit strip, a few hundred
# kilometres wide -- which on a world map looks like a broken layer rather
# than a satellite that has not been over the rest of the world yet. A day of
# strips is the whole globe, and a week of days is a week you can step
# through.
DAYS_OFFERED = 7

# How fresh a frame has to be to count as live.
#
# A day, not the lightning layer's six hours: these are polar orbiters, so a
# given place is only passed over every day or two, and the newest frame in
# the catalogue is genuinely a few hours to a day old even when everything is
# working perfectly.
LIVE_WITHIN = dt.timedelta(hours=36)

ATTRIBUTION = "Contains modified Copernicus Sentinel data · EUMETSAT View"


class CopernicusError(RuntimeError):
    pass


def family_of(name: str, title: str) -> str | None:
    """Which Sentinel this layer belongs to, if any."""
    low = f"{name} {title}".lower()
    for family in FAMILIES:
        if any(word in low for word in family["words"]):
            return family["key"]
    return None


def days_offered(newest: dt.datetime | None) -> list[str]:
    """The last week of whole days, newest last, as WMS date ranges.

    Each entry is one day rather than one instant, because an instant is one
    orbit strip: a few hundred kilometres of the planet and nothing else. A
    WMS given a range draws everything inside it, so "this whole day" is every
    pass that day, which for a polar orbiter at three hundred metres is the
    globe.
    """
    if newest is None:
        return []
    end = newest.astimezone(dt.timezone.utc).date()
    return [(end - dt.timedelta(days=n)).isoformat()
            for n in range(DAYS_OFFERED - 1, -1, -1)]


def sort_layers(xml: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """Split EUMETSAT's catalogue into the Sentinel families it serves.

    The liveness test is the lightning layer's, for the same reason: a name is
    not evidence of a date. Every WMS layer declares the times it holds, so a
    reprocessed archive is refused by comparing those against the clock rather
    than by reading its title hopefully.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        root = mtg.ET.fromstring(xml)
    except mtg.ET.ParseError as exc:
        raise CopernicusError(
            f"EUMETSAT sent a catalogue that would not parse: {exc}") from exc

    found: dict[str, list[dict[str, Any]]] = {f["key"]: [] for f in FAMILIES}
    stale: dict[str, int] = {f["key"]: 0 for f in FAMILIES}

    for node in root.iter(f"{mtg._WMS_NS}Layer"):
        name = mtg._text(node, "Name")
        if not name:
            # A grouping layer with no name of its own cannot be requested.
            continue
        title = mtg._text(node, "Title") or name
        key = family_of(name, title)
        if key is None:
            continue

        newest, entry = mtg._entry(node, name, title)
        # A day at a time, and the whole week as one composite. What a WMS
        # does with a TIME range is draw everything in it, so a day of orbit
        # strips comes back as a covered globe rather than as one pass.
        entry["days"] = days_offered(newest)
        entry["whole_week"] = (
            f"{entry['days'][0]}/{entry['days'][-1]}" if entry["days"] else None)
        fresh = newest is not None and now - newest <= LIVE_WITHIN
        entry["live"] = fresh
        entry["age_minutes"] = (
            round((now - newest).total_seconds() / 60) if newest else None)
        entry["family"] = key
        if fresh:
            found[key].append(entry)
        else:
            stale[key] += 1

    for group in found.values():
        group.sort(key=lambda item: item["id"])

    return {
        "families": [
            {**family,
             "layers": found[family["key"]],
             "stale": stale[family["key"]]}
            for family in FAMILIES
        ],
        "wms": mtg.WMS,
        "attribution": ATTRIBUTION,
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
        "days_offered": DAYS_OFFERED,
    }


def layers(refresh: bool = False) -> dict[str, Any]:
    """What EUMETSAT is serving from Sentinel-3 and Sentinel-5P right now."""
    try:
        xml = mtg.capabilities(refresh=refresh)
    except mtg.MTGError as exc:
        raise CopernicusError(str(exc)) from exc
    found = sort_layers(xml)
    found["catalogue_size"] = xml.count("<Name>")
    return found


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo() -> dict[str, Any]:
    """The same shape as a live answer, for the build with no network."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    recent = mtg._stamp(now - dt.timedelta(hours=3))

    days = days_offered(now)

    def entry(ident: str, title: str) -> dict[str, Any]:
        return {"id": ident, "title": title, "time_default": recent,
                "newest": recent, "age_minutes": 180, "live": True,
                "days": days, "whole_week": f"{days[0]}/{days[-1]}"}

    seeded = {
        "sentinel-3": [entry("copernicus:s3_olci_truecolour",
                             "Sentinel-3 OLCI true colour (demo)"),
                       entry("copernicus:s3_slstr_lst",
                             "Sentinel-3 SLSTR land surface temperature (demo)")],
        "sentinel-5p": [entry("copernicus:s5p_no2",
                              "Sentinel-5P nitrogen dioxide (demo)")],
    }
    return {
        "families": [
            {**family, "layers": seeded[family["key"]], "stale": 0}
            for family in FAMILIES
        ],
        "wms": mtg.WMS,
        "attribution": "synthetic",
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
        "days_offered": DAYS_OFFERED,
        "catalogue_size": 3,
    }
