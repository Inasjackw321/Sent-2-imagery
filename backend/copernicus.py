"""Satellites that cannot go through the imagery pipeline, as live map layers.

The imagery flow is: draw an area, pick a date, and a scene is rendered from
cloud-optimised GeoTIFFs read a window at a time. Sentinel-1, Sentinel-2 and
Landsat live there. Everything in this module cannot, and the reason is worth
stating rather than leaving as an unexplained gap in the picker.

Sentinel-3 and Sentinel-5P are published as NetCDF granules -- whole swaths in
one file, not tiled, not cloud-optimised. Reading a hundred-kilometre box out
of one means downloading the entire granule, and the pipeline here is built on
windowed reads of COGs. It is not a small change; it is a different pipeline.
The weather satellites are further still from it: a geostationary full disc is
not a scene over your area at all.

But EUMETSAT View serves all of them as ordinary WMS, with no account and no
key -- the same service the lightning layer already uses. So they arrive here
as live map layers instead: the current picture, drawn under the imagery.

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

  Meteosat     MSG and MTG, parked over the meridian. Not a Sentinel, and here
               because it answers what the Sentinels cannot: what is happening
               right now. The whole disc every ten minutes, day and night,
               which is weather moving rather than weather on one morning.

  Metop        Europe's polar weather satellites, crossing at dawn and dusk
               where the Sentinels cross at midday -- the same ground, lit from
               the other side. AVHRR, IASI and ASCAT.

As with the lightning, nothing here names a layer. EUMETSAT publishes what it
serves and for when; this reads that and offers what is actually there, and a
layer reaches the live list only by declaring a recent frame. A product whose
name says "Sentinel-3" and whose contents are a 2019 reprocessing is refused by
arithmetic rather than by vocabulary -- and how recent is recent depends on the
orbit, so a geostationary satellite is held to hours where a polar one is held
to a day and a half.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import requests
from urllib3.util.retry import Retry

from . import config, mtg, reasons

# What to look for, and what to call it. The keys are matched against a
# layer's name and title, lower-cased; the first family that matches wins.
#
# Deliberately broad. EUMETSAT renames and reorganises, and a family that
# matches nothing shows as empty rather than as an error -- which is the
# honest outcome and tells whoever is looking that the catalogue has moved.
FAMILIES: tuple[dict[str, Any], ...] = (
    # First, and not a Sentinel.
    #
    # This tuple's order is two things at once: which family claims a layer
    # whose name could belong to either, and the order the panel draws them
    # in. Meteosat leads on the second count -- it is the picture people mean
    # by "the satellite", the only one that updates while you watch and the
    # only one that can be played as an animation, so it opens the panel and
    # the rest sit below it. On the first count nothing changes: no Sentinel
    # or Metop product matches "meteosat", "seviri", "fci" or "msg", so
    # putting these words first cannot take a layer from anyone.
    #
    # They are here at all because they answer the question the Sentinels
    # cannot: what is happening right now.
    #
    # A polar orbiter passes over at a fixed local time and is gone. These two
    # families are the alternative -- one parked over a fixed longitude
    # photographing its whole disc every few minutes, one crossing at dawn and
    # dusk where the Sentinels cross at midday. Same service, same discovery,
    # same refusal to trust a name over a timestamp.
    {
        "key": "mtg",
        "short": "Meteosat",
        "label": "Meteosat · Europe and Africa, every few minutes",
        # MTG is the new generation, MSG the one it is replacing; both are
        # flying and EUMETSAT serves both. SEVIRI and FCI are their imagers.
        "words": ("meteosat", "seviri", "_fci", "fci_", "msg_", "mtg_fd",
                  "mtg_"),
        # The lightning imager rides on the same spacecraft and is already its
        # own layer. See family_of().
        "avoid": ("li_", "lightning", "flash", "_afa", "accumulated flash"),
        "colour": "#ffb74d",
        "resolution": "500 m – 3 km",
        # Geostationary. One instant is already the whole disc, so compositing
        # a day would blend a hundred and forty frames of a moving sky into
        # mud. And ten-minute imagery that is six hours old is broken, not
        # normal -- the polar orbiters' day-and-a-half window would call a dead
        # service healthy.
        "spans_days": False,
        "live_within_hours": 6,
        "about": "Parked over the Greenwich meridian, photographing the whole "
                 "disc every ten minutes — Europe, Africa and the Atlantic, "
                 "day and night. Coarse, but the only thing here that shows "
                 "weather moving rather than weather on one particular "
                 "morning.",
    },
    {
        "key": "sentinel-5p",
        "short": "Sentinel-5P",
        "label": "Sentinel-5P · atmospheric chemistry",
        "words": ("sentinel-5p", "sentinel_5p", "s5p", "tropomi"),
        "colour": "#c58cff",
        "resolution": "≈7 km",
        # A polar orbiter: passes over at a fixed local time, so one instant is
        # one strip and a whole day of strips is the planet.
        "spans_days": True,
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
        "spans_days": True,
        "about": "Three hundred metres and the whole planet every day or two. "
                 "Coarse next to Sentinel-2's ten, and the only Sentinel that "
                 "sees everywhere every day — a weather system rather than a "
                 "field.",
    },
    {
        "key": "metop",
        "short": "Metop",
        "label": "Metop · dawn and dusk, pole to pole",
        "words": ("metop", "avhrr", "iasi", "ascat"),
        "colour": "#7fc4ff",
        "resolution": "1 km – 25 km",
        "spans_days": True,
        "about": "Europe's polar weather satellites, crossing at dawn and "
                 "dusk rather than midday — the same ground the Sentinels see, "
                 "lit from the other side. AVHRR for cloud and land, ASCAT for "
                 "wind over the sea.",
    },
)

# How many days of history to offer a satellite that flies over.
#
# A single instant from one of these is one orbit strip, a few hundred
# kilometres wide -- which on a world map looks like a broken layer rather
# than a satellite that has not been over the rest of the world yet. A day of
# strips is the whole globe, and a week of days is a week you can scrub back
# through.
DAYS_OFFERED = 7

# And how many frames to offer one that stares.
#
# Meteosat photographs its whole disc every ten minutes, so its history is not
# days but the last few hours -- which is the one thing here that can be played
# as an animation, because consecutive frames are the same view a few minutes
# apart rather than two different strips of the planet.
#
# Forty-eight of them is eight hours at ten-minute cadence: asked for, and
# affordable now that the tiles come through this app and are held. Fetched
# straight from EUMETSAT by the browser it would have been eight hours of
# imagery downloaded again on every loop.
FRAMES_OFFERED = 48

# How long to wait for the catalogue to say. Every family declares what it
# believes its own cadence to be, and the declared one from the service wins
# where there is one -- this is what gets used when the time dimension is a
# bare list of instants with no period in it.
ASSUMED_STEP_MINUTES = {"mtg": 10}
DAILY_STEP_MINUTES = 24 * 60

# The few products of each satellite worth having in front of you.
#
# EUMETSAT serves a dozen or more per spacecraft and the panel offered every
# one of them: four satellites, forty-odd buttons, and the four or five anybody
# opens buried somewhere among them. Nothing is taken away -- the rest are one
# click behind "all products" -- but a panel that opens showing everything is a
# panel that opens showing nothing in particular.
#
# Matched against a layer's name and title, lower-cased, the same way families
# are. Deliberately about what the product IS rather than what it is called
# this year, for the same reason: EUMETSAT renames things.
EVERYDAY: dict[str, tuple[str, ...]] = {
    # What the ground looks like, how hot it is, and where it is burning.
    "sentinel-3": ("truecolour", "true_colour", "true colour", "natural",
                   "lst", "land surface temperature", "chl", "chlorophyll",
                   "fire", "frp"),
    # The four gases anyone comes to TROPOMI for.
    "sentinel-5p": ("no2", "nitrogen dioxide", "ch4", "methane",
                    "carbon monoxide", "so2", "sulphur", "sulfur",
                    "aerosol index"),
    # A picture of the sky now, by day and by night.
    "mtg": ("truecolour", "true_colour", "true colour", "natural",
            "ir108", "ir_108", "infrared", "water vapour", "water_vapour",
            "dust"),
    "metop": ("truecolour", "true_colour", "true colour", "natural",
              "ascat", "wind", "ndvi"),
    # NASA through GIBS (gibs.py): carbon dioxide first, then heat and fire.
    # Daytime surface temperature only: night's is one click behind.
    "nasa": ("carbon dioxide", "surface_temp_day", "temperature (day",
             "thermal anomalies", "carbon monoxide", "aerosol"),
}

# The one product each family opens on and keeps in front of you, whatever
# the shortlist does: pinned. Meteosat's is the Fire Temperature RGB -- fires
# burn yellow and white out of a dark scene, day and night, every ten minutes
# -- and the map switches it on as it loads.
#
# Matched by words, as the shortlist is, because EUMETSAT renames products.
# Where both Meteosat generations serve one, the newer imager (MTG FCI, ids
# starting "mtg") wins: finer pixels and a frame every ten minutes.
PINNED: dict[str, tuple[str, ...]] = {
    "mtg": ("fire temperature", "firetemperature", "fire_temperature"),
}

# And at most this many of them, even where the words above match more. The
# complaint this answers is the length of the list, so the shortlist has to be
# short whatever the catalogue does.
MOST_EVERYDAY = 5

# How fresh a frame has to be to count as live.
#
# A day, not the lightning layer's six hours: these are polar orbiters, so a
# given place is only passed over every day or two, and the newest frame in
# the catalogue is genuinely a few hours to a day old even when everything is
# working perfectly.
LIVE_WITHIN = dt.timedelta(hours=36)

ATTRIBUTION = "Contains modified Copernicus data · EUMETSAT View"

# Where the browser asks for a tile: this app, not EUMETSAT. See tile() at the
# bottom of this file for why it is worth a hop.
TILES = "/api/copernicus/wms"


class CopernicusError(RuntimeError):
    pass


def family_of(name: str, title: str) -> str | None:
    """Which family this layer belongs to, if any.

    A family may also name words that disqualify a layer. That exists for one
    real case: the lightning imager flies on Meteosat, so every pattern broad
    enough to catch Meteosat's pictures also catches it -- and lightning is
    already its own layer, with its own panel, its own six-hour freshness rule
    and its own way of drawing. Offering it here as well would be two different
    answers to the same question, which is the same reason Sentinel-2 is kept
    out of this module entirely.
    """
    low = f"{name} {title}".lower()
    for family in FAMILIES:
        if any(word in low for word in family.get("avoid", ())):
            continue
        if any(word in low for word in family["words"]):
            return family["key"]
    return None


def everyday_rank(key: str, name: str, title: str) -> int:
    """How far up its family's shortlist a product sits, or a large number.

    The position of the first word it matches, so EVERYDAY doubles as an order
    of preference rather than only a filter. That decides two things: which
    product the panel opens on -- Meteosat's true colour, which is the picture
    people mean -- and, where more products match than the shortlist holds,
    which five survive the cap. Alphabetical order was deciding both, and
    alphabetical order is not an opinion about what anyone wants to look at.
    """
    low = f"{name} {title}".lower()
    for at, word in enumerate(EVERYDAY.get(key, ())):
        if word in low:
            return at
    return len(EVERYDAY.get(key, ())) + 1


def is_everyday(key: str, name: str, title: str) -> bool:
    """Whether this is one of the products the panel shows without being asked."""
    return everyday_rank(key, name, title) <= len(EVERYDAY.get(key, ()))


def pinned_of(key: str, group: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The family's pinned product, if the catalogue has one. See PINNED."""
    words = PINNED.get(key, ())
    found = [entry for entry in group
             if any(w in f"{entry['id']} {entry['title']}".lower() for w in words)]
    if not found:
        return None
    return min(found, key=lambda e: (not e["id"].lower().startswith("mtg"), e["id"]))


def shortlist(key: str, group: list[dict[str, Any]]) -> None:
    """Mark the few of a family's products that open with the panel.

    Nothing is dropped. This only decides what is in front of you before you
    ask for the rest, which is the whole of the difference between a panel with
    five buttons on it and a panel with forty.

    A family where nothing matched gets its first few instead of nothing at
    all. EUMETSAT renames and reorganises, and "the shortlist went empty so the
    satellite now offers no products" is a worse failure than the long list
    this is shortening -- the escape hatch has to work without a deploy.
    """
    for entry in group:
        entry["rank"] = everyday_rank(key, entry["id"], entry["title"])
        entry["everyday"] = is_everyday(key, entry["id"], entry["title"])
        entry["pinned"] = False
    # Most wanted first, and alphabetical within a rank so the order is stable
    # across polls rather than whatever the catalogue happened to list.
    group.sort(key=lambda entry: (entry["rank"], entry["id"]))
    pin = pinned_of(key, group)
    if pin is not None:
        # First in the list and always on it: the cap below counts it among
        # the five rather than ever cutting it.
        pin["pinned"] = pin["everyday"] = True
        group.remove(pin)
        group.insert(0, pin)
    kept = [entry for entry in group if entry["everyday"]]
    if not kept:
        for entry in group[:MOST_EVERYDAY]:
            entry["everyday"] = True
        return
    for entry in kept[MOST_EVERYDAY:]:
        entry["everyday"] = False


# ── Detect fires: every product that can see a fire, in one place ──
#
# Fire is the question people bring to this panel most, and the products
# that answer it were scattered over four satellites' lists under four
# naming schemes. So they are gathered into one family at the top, in the
# order worth reaching for them, and taken out of the families they came
# from. Each keeps where its tiles come from and how its days are named.

FIRE_FAMILY: dict[str, Any] = {
    "key": "fires",
    "short": "🔥 Detect fires",
    "label": "Detect fires · every satellite product that sees fire",
    "colour": "#ff7043",
    "resolution": "375 m – 3 km",
    "spans_days": False,
    "about": "Every product here that detects fire. Meteosat's Fire "
             "Temperature shows heat every ten minutes, day and night — the "
             "first to see a fire start. VIIRS (375 m) and MODIS (1 km) are "
             "NASA's fire detections, a few passes a day between them. "
             "Sentinel-3's fire radiative power is daily at 1 km. Use "
             "Highlight an area to play any of them back: the last 48 hours "
             "of Meteosat, or 14 days of the others.",
    "words": (),
}

# The order the products are offered in, by what is in their name: the one
# that sees a fire first, then the sharpest detections, then the rest.
FIRE_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("meteosat", ("fire temperature", "firetemperature", "fire_temperature")),
    ("viirs_noaa20", ("noaa20", "noaa-20")),
    ("viirs_snpp", ("snpp", "suomi")),
    ("viirs_noaa21", ("noaa21", "noaa-21")),
    ("modis_terra", ("terra",)),
    ("modis_aqua", ("aqua",)),
    ("slstr", ("frp", "fire radiative")),
)

FIRE_WORDS = ("fire temperature", "firetemperature", "fire_temperature", "frp",
              "fire radiative", "thermal anomalies", "thermal_anomalies",
              "fires and thermal")


def is_fire(entry: dict[str, Any]) -> bool:
    """Whether a product detects fire."""
    low = f"{entry.get('id', '')} {entry.get('title', '')}".lower()
    return any(word in low for word in FIRE_WORDS)


def fire_rank(entry: dict[str, Any]) -> int:
    low = f"{entry.get('id', '')} {entry.get('title', '')}".lower()
    for at, (_, words) in enumerate(FIRE_ORDER):
        if any(w in low for w in words):
            return at
    return len(FIRE_ORDER)


def fire_title(entry: dict[str, Any], source: dict[str, Any]) -> str:
    """A button-sized name: which satellite, and how sharp or how often."""
    if entry.get("short"):
        return entry["short"]
    low = f"{entry.get('id', '')} {entry.get('title', '')}".lower()
    if any(w in low for w in FIRE_ORDER[0][1]):
        gen = "MSG" if ("msg" in low or "seviri" in low) else "MTG"
        return f"Meteosat {gen} Fire Temperature · 10 min"
    if "frp" in low or "fire radiative" in low:
        sat = " 3A" if ("3a" in low or "s3a" in low) else " 3B" if ("3b" in low or "s3b" in low) else ""
        return f"Sentinel-{sat.strip() or '3'} SLSTR fire power · 1 km"
    return f"{source.get('short', '')} · {entry.get('title', '')}".strip(" ·")


def gather_fires(families: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The families with a Detect fires family first, holding every fire
    product, and those products taken out of the families they came from.

    Each product carries its own tile address, orbit kind and day naming from
    the family it came from, because one family now holds products from two
    services and both kinds of orbit.
    """
    gathered: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for family in families:
        keep = []
        for entry in family.get("layers", []):
            if not is_fire(entry):
                keep.append(entry)
                continue
            gathered.append({
                **entry,
                "full_title": entry.get("title"),
                "title": fire_title(entry, family),
                "source": family.get("short"),
                "source_family": family.get("key"),
                "wms": family.get("wms") or TILES,
                "attribution": family.get("attribution") or ATTRIBUTION,
                "spans_days": bool(family.get("spans_days")),
                "time_as_date": bool(family.get("time_as_date")),
                "everyday": True,
            })
        rest.append({**family, "layers": keep})
    gathered.sort(key=lambda e: (fire_rank(e), e["id"]))
    # The first one -- Meteosat's, where there is one -- opens with the map.
    for at, entry in enumerate(gathered):
        entry["pinned"] = at == 0
        entry["rank"] = at
    fires = {**FIRE_FAMILY, "layers": gathered, "stale": 0,
             "live_within_hours": 24 * 60}
    return [fires, *rest]


def live_within(family: dict[str, Any]) -> dt.timedelta:
    """How old the newest frame may be before this family counts as stale.

    A day and a half suits a polar orbiter, which only passes over a given
    place every day or two. It would call a dead geostationary service healthy,
    so Meteosat sets its own and gets the lightning layer's six hours.
    """
    hours = family.get("live_within_hours")
    return dt.timedelta(hours=hours) if hours else LIVE_WITHIN


def days_offered(newest: dt.datetime | None) -> list[str]:
    """The last week of whole days, newest last, as plain dates.

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


def step_for(family: dict[str, Any], declared: int | None) -> int:
    """How far apart this layer's frames are, in minutes.

    What the service said, where it said anything. A family's own number is a
    fallback for a catalogue that published a bare list of instants with no
    period in it, and a day is the fallback to the fallback -- a satellite
    that flies over has nothing finer to offer than the day it flew over on.
    """
    if declared and declared > 0:
        return declared
    return ASSUMED_STEP_MINUTES.get(family["key"], DAILY_STEP_MINUTES)


def frames_for(family: dict[str, Any], newest: dt.datetime | None,
               declared: int | None) -> list[dict[str, str]]:
    """The moments this layer can be shown at, oldest first.

    One list whatever the orbit, because the panel that draws it is one
    scrubber whatever the orbit -- but the two kinds of moment are genuinely
    different things and each says which it is.

    A satellite that flies over offers whole DAYS, each asked for as the range
    that covers it, because one instant of it is one orbit strip. A satellite
    that stares offers INSTANTS a few minutes apart, because that is what it
    published and because consecutive ones are the same view moments apart --
    which is the only thing here that can honestly be played as an animation.
    """
    if newest is None:
        return []
    if family.get("spans_days"):
        return [{"label": day, "at": day,
                 # A bare date is midnight exactly on some servers, which
                 # would be one instant and one strip again.
                 "time": f"{day}T00:00:00Z/{day}T23:59:59Z"}
                for day in days_offered(newest)]
    step = dt.timedelta(minutes=step_for(family, declared))
    end = newest.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    moments = [end - step * n for n in range(FRAMES_OFFERED - 1, -1, -1)]
    return [{"label": when.strftime("%H:%M"), "at": when.date().isoformat(),
             "time": mtg._stamp(when)}
            for when in moments]


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

    by_key = {f["key"]: f for f in FAMILIES}
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

        family = by_key[key]
        newest, entry = mtg._entry(node, name, title)
        # Every moment this layer can be shown at, oldest first: whole days
        # for a satellite that flies over, instants a few minutes apart for
        # one that stares. The panel is one scrubber over either.
        entry["frames"] = frames_for(family, newest, entry.get("step_minutes"))
        entry["step_minutes"] = step_for(family, entry.get("step_minutes"))
        # Only a satellite that stares has frames close enough together for
        # consecutive ones to be the same view moments apart, which is what an
        # animation is. A week of orbit strips played as a loop is a slideshow
        # of seven different days.
        entry["animates"] = not family.get("spans_days")
        fresh = newest is not None and now - newest <= live_within(family)
        entry["live"] = fresh
        entry["age_minutes"] = (
            round((now - newest).total_seconds() / 60) if newest else None)
        entry["family"] = key
        if fresh:
            found[key].append(entry)
        else:
            stale[key] += 1

    for key, group in found.items():
        # shortlist orders them too -- most wanted first, alphabetical within
        # a rank -- so there is no separate sort to fall out of step with it.
        shortlist(key, group)

    return {
        "families": [
            {**family,
             "layers": found[family["key"]],
             "stale": stale[family["key"]],
             "live_within_hours": live_within(family).total_seconds() / 3600}
            for family in FAMILIES
        ],
        "wms": TILES,
        "attribution": ATTRIBUTION,
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
        "days_offered": DAYS_OFFERED,
    }


def layers(refresh: bool = False) -> dict[str, Any]:
    """What EUMETSAT is serving from each of these families right now."""
    try:
        xml = mtg.capabilities(refresh=refresh)
    except mtg.MTGError as exc:
        raise CopernicusError(str(exc)) from exc
    found = sort_layers(xml)
    found["catalogue_size"] = xml.count("<Name>")
    return found


# ---------------------------------------------------------------------------
# History over an area
# ---------------------------------------------------------------------------
#
# The panel's loop is the last eight hours over whatever is on screen. This is
# the other question: the last two days over one place you mark out -- a fire
# you want to watch start, spread and die down, which is what the Fire
# Temperature RGB is for. One picture of the area per frame rather than tiles
# of the whole view, so two days is a few hundred small images rather than
# tens of thousands of tiles.

# The longest history offered, in hours. Two days at Meteosat's ten minutes is
# 288 frames.
MOST_HISTORY_HOURS = 48

# And a ceiling on the frames whatever the cadence, so a layer declaring a
# one-minute period cannot turn a request into thousands of fetches.
MOST_HISTORY_FRAMES = 300


def find_layer(found: dict[str, Any], layer_id: str) -> tuple[dict, dict] | None:
    """(family, layer) for one id in a catalogue answer, or None."""
    for family in found.get("families", []):
        for entry in family.get("layers", []):
            if entry.get("id") == layer_id:
                return family, entry
    return None


def history_frames(entry: dict[str, Any], hours: float,
                   now: dt.datetime | None = None) -> list[dict[str, str]]:
    """Every frame of one staring layer over the last `hours`, oldest first.

    Counted back from its newest frame at its own cadence, and stopped at the
    oldest the service says it holds -- a frame from before that is a refusal
    waiting to happen, so it is not asked for.
    """
    newest = entry.get("newest")
    if not newest:
        return []
    end = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    step = dt.timedelta(minutes=max(1, int(entry.get("step_minutes") or 10)))
    hours = min(max(float(hours), 0.0), MOST_HISTORY_HOURS)
    # Two days at ten minutes is 288 frames: the newest and the 287 before it.
    count = int(dt.timedelta(hours=hours) / step)
    held = entry.get("oldest")
    if held:
        start = dt.datetime.fromisoformat(held.replace("Z", "+00:00"))
        count = min(count, int((end - start) / step) + 1)
    count = max(0, min(count, MOST_HISTORY_FRAMES))
    moments = [end - step * n for n in range(count - 1, -1, -1)]
    return [{"label": when.strftime("%H:%M"), "at": when.date().isoformat(),
             "time": mtg._stamp(when)} for when in moments]


# For a satellite that flies over once a day, the area loop is days instead:
# one whole-day picture each -- every pass that day over the box -- for the
# last two weeks. Two days of a polar orbiter is two pictures, which is not a
# loop; a fortnight is a fire's whole life.
AREA_DAYS = 14
MOST_AREA_DAYS = 31


def history_days(entry: dict[str, Any], days: int,
                 as_date: bool = False) -> list[dict[str, str]]:
    """The last `days` whole days of one fly-over layer, oldest first.

    Asked for as the range that covers each day, as the live panel does,
    because one instant of a polar orbiter is one orbit strip. Stopped at the
    oldest day the service says it holds.
    """
    newest = entry.get("newest")
    if not newest:
        return []
    end = dt.datetime.fromisoformat(newest.replace("Z", "+00:00")).date()
    days = max(1, min(int(days), MOST_AREA_DAYS))
    first = end - dt.timedelta(days=days - 1)
    held = entry.get("oldest")
    if held:
        first = max(first, dt.datetime.fromisoformat(held.replace("Z", "+00:00")).date())
    out = []
    day = first
    while day <= end:
        stamp = day.isoformat()
        # A service that names its days by date (NASA's GIBS) is asked for
        # the date; the rest for the range of instants that covers the day.
        out.append({"label": stamp, "at": stamp,
                    "time": stamp if as_date else f"{stamp}T00:00:00Z/{stamp}T23:59:59Z"})
        day += dt.timedelta(days=1)
    return out


def history(found: dict[str, Any], layer_id: str, hours: float,
            days: int = AREA_DAYS) -> dict[str, Any]:
    """The frames to fetch for an area loop of one layer.

    Meteosat, which stares: every frame of the last `hours` (see
    history_frames). A satellite that flies over: the last `days` whole days.
    """
    hit = find_layer(found, layer_id)
    if hit is None:
        raise CopernicusError(f"{layer_id} is not a layer being served now")
    family, entry = hit
    # A product can carry these itself: the Detect fires family holds
    # products from both kinds of orbit and from two services.
    daily = bool(entry.get("spans_days", family.get("spans_days")))
    as_date = bool(entry.get("time_as_date", family.get("time_as_date")))
    frames = history_days(entry, days, as_date=as_date) if daily else history_frames(entry, hours)
    step = 24 * 60 if daily else (entry.get("step_minutes") or 10)
    return {"layer": layer_id, "title": entry.get("title"), "family": family["short"],
            "daily": daily, "step_minutes": step, "frames": frames,
            "hours": round(len(frames) * step / 60, 1),
            "span": (f"last {len(frames)} days" if daily
                     else f"last {round(len(frames) * step / 60)} hours"),
            # A day of a polar orbiter has gaps between its swaths, and a
            # transparent PNG shows the map through them; a JPEG would paint
            # them black. Meteosat's disc has no gaps, and a JPEG is a tenth
            # of the size.
            "format": "image/png" if daily else "image/jpeg",
            "wms": entry.get("wms") or family.get("wms") or TILES,
            "attribution": (entry.get("attribution") or family.get("attribution")
                            or found.get("attribution"))}


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


def demo_tile(asked: dict[str, str]) -> bytes:
    """A stand-in frame for the demo build: a dark scene and a fire that moves.

    Placed in map coordinates rather than in the picture, so a tile and a
    whole-area image of the same time agree, and moving with the time asked
    for, so a loop of them visibly plays.
    """
    import io

    import numpy as np
    from PIL import Image

    params = tile_params(asked)
    width = min(max(int(params.get("width", 256) or 256), 16), 2048)
    height = min(max(int(params.get("height", 256) or 256), 16), 2048)
    try:
        x0, y0, x1, y1 = (float(v) for v in params.get("bbox", "").split(","))
    except ValueError:
        x0, y0, x1, y1 = -2e7, -2e7, 2e7, 2e7
    try:
        when = dt.datetime.fromisoformat(params.get("time", "").replace("Z", "+00:00"))
        phase = (when.timestamp() / 600) % 288 / 288
    except ValueError:
        phase = 0.0
    xs = np.linspace(x0, x1, width)[None, :]
    ys = np.linspace(y1, y0, height)[:, None]
    # A fire that circles a point in the eastern Mediterranean once a demo day,
    # and two smaller ones that flare and die down. A few kilometres across,
    # as a real fire is to Meteosat: a point, not a region -- so it tests
    # what finding a real fire takes.
    cx = 3.9e6 + 1.5e5 * np.cos(2 * np.pi * phase)
    cy = 3.6e6 + 1.5e5 * np.sin(2 * np.pi * phase)
    heat = np.exp(-((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * 4e3 ** 2))
    for k, (fx, fy) in enumerate(((3.75e6, 3.5e6), (4.05e6, 3.72e6))):
        flare = 0.5 + 0.5 * np.sin(2 * np.pi * (phase * 3 + k / 2))
        heat = np.maximum(heat, flare * np.exp(-((xs - fx) ** 2 + (ys - fy) ** 2)
                                               / (2 * 2.5e3 ** 2)))
    ground = 0.5 + 0.5 * np.sin(xs / 9e4) * np.cos(ys / 7e4)
    rgb = np.stack([60 + 40 * ground + 195 * heat, 20 + 30 * ground + 220 * heat,
                    70 + 40 * ground + 60 * heat], axis=-1)
    out = io.BytesIO()
    Image.fromarray(np.clip(rgb, 0, 255).astype("uint8"), "RGB").save(out, "PNG")
    return out.getvalue()


def demo() -> dict[str, Any]:
    """The same shape as a live answer, for the build with no network."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    by_key = {f["key"]: f for f in FAMILIES}

    def entry(ident: str, title: str, *, spans: bool = True,
              minutes: int = 180) -> dict[str, Any]:
        when = now - dt.timedelta(minutes=minutes)
        stamp = mtg._stamp(when)
        family = by_key["mtg"] if not spans else by_key["sentinel-3"]
        return {"id": ident, "title": title, "time_default": stamp,
                "newest": stamp, "age_minutes": minutes, "live": True,
                "oldest": mtg._stamp(when - dt.timedelta(days=3)),
                # Built the same way the live answer builds them, so the
                # offline build cannot grow a scrubber the real one never
                # shows -- or lose one it does.
                "frames": frames_for(family, when, None),
                "step_minutes": step_for(family, None),
                "animates": not spans}

    seeded = {
        "sentinel-3": [entry("copernicus:s3_olci_truecolour",
                             "Sentinel-3 OLCI true colour (demo)"),
                       entry("copernicus:s3_slstr_lst",
                             "Sentinel-3 SLSTR land surface temperature (demo)"),
                       entry("copernicus:sentinel3a_slstr_level2_frp",
                             "SLSTR Level 2 Fire Radiative Power (FRP) - Sentinel-3A (demo)")],
        "sentinel-5p": [entry("copernicus:s5p_no2",
                              "Sentinel-5P nitrogen dioxide (demo)"),
                        entry("copernicus:s5p_ch4",
                              "Sentinel-5P methane (demo)")],
        # Minutes rather than hours old, because six hours would be stale for
        # a satellite that publishes every ten minutes -- and the demo should
        # not show a state the live service would refuse.
        "mtg": [entry("mtg_fd:rgb_truecolour", "MTG FCI true colour (demo)",
                      spans=False, minutes=20),
                entry("mtg_fd:rgb_firetemperature",
                      "MTG FCI Fire Temperature RGB (demo)",
                      spans=False, minutes=20),
                entry("msg_fes:rgb_naturalcolour",
                      "MSG SEVIRI natural colour (demo)",
                      spans=False, minutes=25)],
        "metop": [entry("metop:avhrr_truecolour",
                        "Metop AVHRR true colour (demo)"),
                  entry("metop:ascat_winds", "Metop ASCAT ocean winds (demo)")],
    }
    for key, group in seeded.items():
        shortlist(key, group)
    return {
        "families": [
            {**family, "layers": seeded[family["key"]], "stale": 0,
             "live_within_hours": live_within(family).total_seconds() / 3600}
            for family in FAMILIES
        ],
        "wms": TILES,
        "attribution": "synthetic",
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
        "days_offered": DAYS_OFFERED,
        "catalogue_size": sum(len(v) for v in seeded.values()),
    }


# ---------------------------------------------------------------------------
# The tiles themselves
# ---------------------------------------------------------------------------
#
# The browser used to ask EUMETSAT for these directly. Two things wanted
# changing, and one endpoint answers both.
#
# Playing eight hours of a satellite that publishes every ten minutes is
# forty-eight frames of a dozen tiles each, fetched again on every loop and
# again for every person watching. Held here, the second loop and the second
# viewer are free, and a frame can be fetched BEFORE it is wanted rather than
# while the animation waits for it.
#
# And a cross-origin image taints a canvas. Everything this app draws to a
# canvas -- the exported picture, and now a recorded video -- is unreadable
# the moment a tile from another origin is drawn into it: the browser refuses
# to hand back the pixels. Served from this app's own origin, they are
# ordinary same-origin images and the canvas stays readable. That is not a
# convenience; without it a video of the satellite cannot exist at all.

import collections
import threading

# How much to keep. A tile is a few tens of kilobytes; three hundred megabytes
# is a few thousand of them, which is several hours of a handful of views.
# Least-recently-used, so the frames being looped over stay and the ones
# nobody has asked for in an hour fall out.
TILE_CACHE_BYTES = 300 * 1024 * 1024

# And how long a tile is worth keeping at all. These are published frames of a
# fixed past moment, so they never change -- but the cache is memory, and a
# process left running for a week should not still be holding Tuesday.
TILE_CACHE_SECONDS = 12 * 3600

# What a request may ask to be forwarded. An allow-list rather than a
# block-list: this endpoint sends requests to somebody else's server on behalf
# of whoever calls it, and the set of WMS parameters is small and known, so
# there is no reason to forward a name nobody here recognises.
#
# sld and sld_body used to be on this list and are deliberately not any more.
# A WMS SLD is a styling document, and `sld` is a URL the *upstream* server
# fetches -- so forwarding one lends this proxy out as the first step of a
# request EUMETSAT makes on behalf of whoever called here, at an address of
# their choosing. Nothing in this app has ever set either, so nothing is lost
# by not passing them on.
TILE_PARAMS = frozenset({
    "service", "request", "version", "layers", "styles", "format",
    "transparent", "width", "height", "crs", "srs", "bbox", "time",
    "bgcolor", "exceptions", "tiled", "dim_date",
})

# How long one tile gets. A tile is a few tens of kilobytes of a picture that
# already exists on somebody's disk; if it has not arrived in this long, the
# service is not slow, it is not answering.
#
# It used to be forty seconds, which was the flaw behind the wall of identical
# timeouts in the log: one playable loop asks for tens of tiles at once, so an
# unreachable EUMETSAT meant tens of requests each sitting for forty seconds
# and then failing separately.
TILE_TIMEOUT = 12

# And how long to stop asking after a tile could not be fetched at all.
#
# The failure that matters here is not one tile's -- it is the host being
# unreachable, which every other tile in flight is about to discover for
# itself at its own timeout. Once one has found out, the rest are refused
# straight away: the answer is the same and it arrives immediately rather
# than twelve seconds later.
#
# Short, because it has to stop being true the moment the service comes back.
TILE_SULK_SECONDS = 20

# And how many failures in a row it takes before the host is treated as gone.
#
# It was one, and that was the blank map on Windows: EUMETSAT closes idle
# connections, the pool hands out a closed one, the request fails with
# "Remote end closed connection without response" -- and every tile for the
# next twenty seconds was refused on the strength of one stale socket, while
# the host was answering perfectly well. A run of failures is an outage; one
# is a bad connection, and the retry below deals with those.
TILE_TRIP_AFTER = 4

_tile_trouble: tuple[str, float] = ("", 0.0)
_tile_streak = 0

# How long a whole-area picture gets: the area loop asks EUMETSAT to render
# one image of up to 768 pixels a side, which is a dozen tiles' work in one
# request. Twelve seconds was timing those out ("Read timed out") on a busy
# afternoon.
AREA_TIMEOUT = 30
AREA_PIXELS = 512 * 512

# How many requests may be with EUMETSAT at once, from this app.
#
# The live layer, the frames fetched ahead, a recording and an area loop can
# all be asking together, and the pool used to let sixteen through. A free
# service under that answers by closing connections -- the "Connection
# aborted" in the log -- so the rest wait their turn here instead.
UPSTREAM_AT_ONCE = 6

# One connection pool to EUMETSAT, kept open between tiles.
#
# Every tile used to be a bare requests.get, which is a new TCP connection and
# a new TLS handshake per tile -- two or three round trips to another country
# before the first byte of a picture a few tens of kilobytes long. A frame is
# thirty-odd tiles and the loop is forty-eight frames, so the handshakes were a
# good share of the whole wait. Pooled, a tile after the first rides a
# connection that is already open.
#
# Sized for the browser's side of it: six connections per origin, two layers
# (the one on screen and the one fetching ahead), and some slack for a second
# person watching.
TILE_POOL = 16

# Retried, quietly, before anything counts as a failure: a connection the
# server had already closed, a reset, a 502 or 503 from a server catching
# its breath. Twice, with a short backoff, honouring Retry-After. GET only,
# which is all this sends.
TILE_RETRY = Retry(
    total=2, connect=2, read=2, status=2, backoff_factor=0.4,
    status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True, raise_on_status=False)

_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=TILE_POOL, max_retries=TILE_RETRY))
_session.headers["User-Agent"] = config.USER_AGENT
_upstream = threading.BoundedSemaphore(UPSTREAM_AT_ONCE)

# Tiles being fetched right now, so a second ask for the same one waits for
# the first rather than fetching it again.
#
# This is the case "newest first" creates on purpose: the walk ahead starts on
# the newest frame, which is the frame already on screen, so for the first
# second both layers want exactly the same thirty tiles. Without this they went
# upstream twice.
_inflight: dict[str, threading.Event] = {}
_inflight_lock = threading.Lock()

# How long a second asker waits for the first before giving up and asking
# itself. Longer than one tile's own timeout, so it only lapses if the first
# fetch has hung somewhere this cannot see.
INFLIGHT_WAIT = TILE_TIMEOUT + 3

_tiles: "collections.OrderedDict[str, tuple[bytes, str, float]]" = \
    collections.OrderedDict()
_tiles_bytes = 0
_tiles_lock = threading.Lock()


def tile_key(params: dict[str, str]) -> str:
    """One name for one tile, whatever order the parameters arrived in."""
    return "&".join(f"{k}={params[k]}" for k in sorted(params))


def tile_params(asked: dict[str, str]) -> dict[str, str]:
    """The parameters worth forwarding, normalised.

    Lower-cased keys, because WMS is case-insensitive about them and Leaflet
    and a hand-written URL do not agree -- and two spellings of one request
    would be two entries in the cache for one picture.
    """
    out = {}
    for key, value in asked.items():
        low = str(key).lower()
        if low in TILE_PARAMS and value is not None:
            out[low] = str(value)
    # An empty time is "no time", not a time of nothing. The page always sends
    # the parameter, so that the tile's address is the same however the layer
    # asking for it was built -- but forwarded empty, a strict WMS answers
    # TIME= with an exception rather than with the default frame. Only time:
    # an empty STYLES is required by WMS 1.3.0 and has to go through as it is.
    if out.get("time") == "":
        del out["time"]
    return out


def cached_tile(key: str, now: float | None = None) -> tuple[bytes, str] | None:
    """A tile already held, or None. Reading one makes it recently used."""
    now = time.time() if now is None else now
    with _tiles_lock:
        had = _tiles.get(key)
        if had is None:
            return None
        body, kind, at = had
        if now - at > TILE_CACHE_SECONDS:
            _forget(key)
            return None
        _tiles.move_to_end(key)
        return body, kind


def _forget(key: str) -> None:
    """Drop one tile. The lock is already held."""
    global _tiles_bytes
    had = _tiles.pop(key, None)
    if had is not None:
        _tiles_bytes -= len(had[0])


def keep_tile(key: str, body: bytes, kind: str,
              now: float | None = None) -> None:
    """Hold a tile, dropping the least recently used until it fits."""
    global _tiles_bytes
    now = time.time() if now is None else now
    if len(body) > TILE_CACHE_BYTES:
        # One tile larger than the whole cache would empty it and then not
        # fit. Not held at all rather than held destructively.
        return
    with _tiles_lock:
        _forget(key)
        _tiles[key] = (body, kind, now)
        _tiles_bytes += len(body)
        while _tiles_bytes > TILE_CACHE_BYTES and len(_tiles) > 1:
            _forget(next(iter(_tiles)))


def tiles_held() -> dict[str, Any]:
    """What the cache is holding. For the panel and for the tests."""
    with _tiles_lock:
        return {"tiles": len(_tiles), "bytes": _tiles_bytes,
                "cap_bytes": TILE_CACHE_BYTES}


def forget_tiles() -> None:
    """Empty it. For the tests."""
    global _tiles_bytes, _tile_trouble
    global _tile_streak
    with _tiles_lock:
        _tiles.clear()
        _tiles_bytes = 0
        _tile_trouble = ("", 0.0)
        _tile_streak = 0


def sulking(now: float | None = None) -> str:
    """Why a tile is not even being asked for, or "" if it is.

    A tile is not retried while the host is known to be unreachable. What is
    remembered is the reason, so the refusal says the same thing the request
    would have -- just immediately.
    """
    said, at = _tile_trouble
    if not said:
        return ""
    now = time.time() if now is None else now
    if now - at >= TILE_SULK_SECONDS:
        return ""
    return said


def _tile_trouble_was(said: str, now: float | None = None) -> None:
    """Count a failure to reach the host, or with "" a success.

    The breaker trips on a run of TILE_TRIP_AFTER failures, not on one: see
    there. A success ends the run and clears any trip.
    """
    global _tile_trouble, _tile_streak
    with _tiles_lock:
        if not said:
            _tile_streak = 0
            _tile_trouble = ("", 0.0)
            return
        _tile_streak += 1
        if _tile_streak >= TILE_TRIP_AFTER:
            _tile_trouble = (said, time.time() if now is None else now)


def tile(asked: dict[str, str]) -> tuple[bytes, str, bool]:
    """One WMS tile, from the cache if it is there. Returns (body, type, hit).

    Raises CopernicusError if EUMETSAT cannot be reached or refuses, so the
    caller can answer with a status rather than with a broken image.
    """
    params = tile_params(asked)
    if not params.get("layers"):
        raise CopernicusError("a tile request has to name a layer")
    key = tile_key(params)
    had = cached_tile(key)
    if had is not None:
        return had[0], had[1], True
    # Cache first, breaker second: a frame already held is served even while
    # the host is unreachable, which is most of what the cache is for.
    known = sulking()
    if known:
        raise CopernicusError(known)

    # Somebody already fetching this exact tile? Wait for theirs.
    with _inflight_lock:
        running = _inflight.get(key)
        if running is None:
            _inflight[key] = mine = threading.Event()
    if running is not None:
        running.wait(INFLIGHT_WAIT)
        had = cached_tile(key)
        if had is not None:
            return had[0], had[1], True
        # Theirs failed or hung. Asking again is the honest answer: the
        # failure might have been theirs alone, and if the host is down the
        # breaker above will say so without a second wait.
        known = sulking()
        if known:
            raise CopernicusError(known)
        return _fetch_tile(key, params)
    try:
        return _fetch_tile(key, params)
    finally:
        with _inflight_lock:
            _inflight.pop(key, None)
        mine.set()


def timeout_for(params: dict[str, str]) -> int:
    """A tile's timeout, or a whole-area picture's longer one."""
    try:
        pixels = int(params.get("width", 256)) * int(params.get("height", 256))
    except ValueError:
        return TILE_TIMEOUT
    return AREA_TIMEOUT if pixels > AREA_PIXELS else TILE_TIMEOUT


def _fetch_tile(key: str, params: dict[str, str]) -> tuple[bytes, str, bool]:
    """Ask EUMETSAT for one tile, over the pooled connection, and hold it."""
    try:
        with _upstream:
            resp = _session.get(mtg.WMS, params=params, timeout=timeout_for(params))
    except requests.RequestException as exc:
        said = f"EUMETSAT View could not be reached: {reasons.why(exc)}"
        _tile_trouble_was(said)
        raise CopernicusError(said) from exc
    _tile_trouble_was("")
    if not resp.ok:
        raise CopernicusError(f"EUMETSAT View answered {resp.status_code}")
    kind = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
    if not kind.startswith("image/"):
        # A WMS answers an error as XML with a 200. Cached, that would be a
        # broken tile held for twelve hours.
        raise CopernicusError(f"EUMETSAT View sent {kind} rather than an image")
    keep_tile(key, resp.content, kind)
    return resp.content, kind, False
