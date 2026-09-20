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
from typing import Any

from . import mtg

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
# apart rather than two different strips of the planet. Twenty-four of them is
# four hours at ten-minute cadence: long enough to watch a front move, short
# enough that a loop is a loop rather than a download.
FRAMES_OFFERED = 24

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
    # Most wanted first, and alphabetical within a rank so the order is stable
    # across polls rather than whatever the catalogue happened to list.
    group.sort(key=lambda entry: (entry["rank"], entry["id"]))
    kept = [entry for entry in group if entry["everyday"]]
    if not kept:
        for entry in group[:MOST_EVERYDAY]:
            entry["everyday"] = True
        return
    for entry in kept[MOST_EVERYDAY:]:
        entry["everyday"] = False


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
        "wms": mtg.WMS,
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
# Demo
# ---------------------------------------------------------------------------


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
                             "Sentinel-3 SLSTR land surface temperature (demo)")],
        "sentinel-5p": [entry("copernicus:s5p_no2",
                              "Sentinel-5P nitrogen dioxide (demo)"),
                        entry("copernicus:s5p_ch4",
                              "Sentinel-5P methane (demo)")],
        # Minutes rather than hours old, because six hours would be stale for
        # a satellite that publishes every ten minutes -- and the demo should
        # not show a state the live service would refuse.
        "mtg": [entry("mtg_fd:rgb_truecolour", "MTG FCI true colour (demo)",
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
        "wms": mtg.WMS,
        "attribution": "synthetic",
        "live_within_hours": LIVE_WITHIN.total_seconds() / 3600,
        "days_offered": DAYS_OFFERED,
        "catalogue_size": sum(len(v) for v in seeded.values()),
    }
