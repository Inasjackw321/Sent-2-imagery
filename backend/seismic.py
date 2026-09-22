"""Earthquakes, and the instruments that recorded them.

Two different things share this module because they answer each other.

  * **Events** come from the USGS, which publishes every earthquake it locates
    anywhere on Earth, worldwide, usually within a few minutes. No account, no
    key, and the same feed the USGS's own map is drawn from.

  * **Stations** come from EarthScope's FDSN service, the index of the global
    seismograph networks -- roughly twenty thousand instruments whose data is
    open. For any one of them you can ask for the last few hours of ground
    motion and get back a plotted trace: an actual seismogram, from an actual
    instrument, of whatever the ground under it has been doing.

The pairing with the imagery is the point. A magnitude on a map is a dot; the
trace is the evidence behind the dot, and Sentinel-1 over the same ground a day
later is what the shaking did to it.

Everything here is keyless. Nothing in it needs an account.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import re
import threading
import time
from typing import Any

import numpy as np
import requests

from . import config, miniseed, shake


class SeismicLookupError(RuntimeError):
    pass


# The USGS event service. FDSN is a standard, so this is the same query shape
# the station service below takes -- one of the nicer things about the format.
EVENTS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# The station index, asked of everyone who keeps one.
#
# EarthScope's copy answers for much of the federation, but not all of it: a
# European node knows about instruments that never reach an American index, and
# the same is true in the other direction. Asking one and calling it the world
# is how a map of twenty thousand open seismographs shows you four hundred.
#
# These are queried together and the answers merged on network and station, so
# an instrument listed by three of them is still one dot. A node that is slow
# or down costs nothing but its own share of the answer.
STATION_SERVICES = [
    ("EarthScope", "https://service.earthscope.org/fdsnws/station/1/query"),
    ("EarthScope (legacy)", "https://service.iris.edu/fdsnws/station/1/query"),
    ("ORFEUS", "https://www.orfeus-eu.org/fdsnws/station/1/query"),
    ("GEOFON", "https://geofon.gfz-potsdam.de/fdsnws/station/1/query"),
    ("INGV", "https://webservices.ingv.it/fdsnws/station/1/query"),
    ("RESIF", "https://ws.resif.fr/fdsnws/station/1/query"),
    ("Koeri", "https://eida.koeri.boun.edu.tr/fdsnws/station/1/query"),
    ("NOA", "https://eida.gein.noa.gr/fdsnws/station/1/query"),
    ("BGR", "https://eida.bgr.de/fdsnws/station/1/query"),
    ("ETH", "https://eida.ethz.ch/fdsnws/station/1/query"),
]

# The legacy host duplicates the first entry exactly, so it is only worth
# asking when the one that replaced it did not answer.
STATION_FALLBACKS = {"EarthScope (legacy)"}

# How long the whole round of station services gets. They run together, so this
# is the slowest one that will still be waited for rather than the sum.
STATIONS_TIMEOUT = 25

# Where the recordings come from.
#
# This used to be one archive's own endpoint that drew the picture for you.
# That was a mistake twice over: it was a single archive's extension rather
# than a standard, and it has since been retired -- it answers 410 Gone with a
# page of HTML. Worse, one archive was never going to be enough. The station
# index is federated, so it describes instruments all over the world, but each
# one's waveforms are held wherever its network archives them. A Turkish or
# Albanian station's data has never been in Seattle.
#
# So: fdsnws/dataselect, which is a published standard that every one of these
# implements, asked of each in turn until one has the recording. The reply is
# raw miniSEED and is decoded and drawn here.
DATA_CENTRES = [
    ("EarthScope", "https://service.earthscope.org/fdsnws/dataselect/1/query"),
    ("EarthScope (legacy)", "https://service.iris.edu/fdsnws/dataselect/1/query"),
    ("ORFEUS", "https://www.orfeus-eu.org/fdsnws/dataselect/1/query"),
    ("GEOFON", "https://geofon.gfz-potsdam.de/fdsnws/dataselect/1/query"),
    ("INGV", "https://webservices.ingv.it/fdsnws/dataselect/1/query"),
    ("RESIF", "https://ws.resif.fr/fdsnws/dataselect/1/query"),
]

ATTRIBUTION = {
    "events": "USGS Earthquake Hazards Program",
    "stations": "EarthScope / FDSN (formerly IRIS)",
}

# How far back the event feed can be asked to go, in hours.
WINDOWS = {24: "24 h", 168: "7 days", 720: "30 days"}

# A trace is only worth plotting over a window a human can read. Ten minutes
# shows a single arrival; six hours shows a day's background and any teleseism
# that turned up in it.
TRACE_MINUTES = {10: "10 min", 60: "1 hour", 360: "6 hours"}

# Vertical channels of every band and instrument code worth having.
#
# The middle letter is the instrument: H is a seismometer, N and L are
# accelerometers, which is what most of the dense urban networks are made of.
# Restricting to H alone -- as this used to -- silently drops every strong
# motion station in Japan, Italy, Turkey and California, which is a great many
# of the instruments anyone would want to look at.
#
# The first letter is the sample rate band, from V at a sample every ten
# seconds up to H and E at a hundred a second. All of them can be plotted.
#
# Horizontal components are still left out: they mostly duplicate the picture
# and would treble the size of every answer.
CHANNELS = ",".join([
    "BHZ", "HHZ", "SHZ", "EHZ", "LHZ", "MHZ", "VHZ", "CHZ", "DHZ", "GHZ",
    "HNZ", "BNZ", "ENZ", "SNZ", "LNZ",
    "HLZ", "BLZ", "ELZ",
])

MAX_EVENTS = 1200
# Raised twice: from 600, which cut a wide view down to a fraction of what was
# there, and again once the markers moved onto a canvas, where twenty thousand
# cost about as much as one. This is roughly every open station on Earth, so
# the cap is now a guard against a runaway rather than a working limit.
MAX_STATIONS = 25000

# The station index changes when someone installs an instrument, so it is
# barely worth re-asking. Events move constantly.
EVENTS_SECONDS = 120
STATIONS_SECONDS = 30 * 60

# The data centre plots the trace itself, which takes a while for a long
# window over a slow link.
TRACE_TIMEOUT = 60

# How far behind "now" to end the window. Even a well-connected station takes a
# few minutes to get its samples into the archive, and asking right up to the
# present reliably returns an empty plot from an instrument that is fine.
TRACE_LAG_MINUTES = 6

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _cached(key: str, ttl: float):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def _keep(key: str, value: dict) -> dict:
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def _get(url: str, params: dict, *, timeout: int = 30,
         empty: tuple[int, ...] = (204,)) -> requests.Response:
    try:
        resp = _session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise SeismicLookupError(f"{url.split('/')[2]} could not be reached: {exc}") from exc
    # FDSN says 204 for "your query was fine and matched nothing", which is an
    # answer rather than a failure and must not be raised over. The plotting
    # service says 404 for the same thing, so callers can name that too.
    if resp.status_code in empty:
        return resp
    if not resp.ok:
        raise SeismicLookupError(
            f"{url.split('/')[2]} answered {resp.status_code}: "
            f"{_short(resp.text) or resp.reason}"
        )
    return resp


# ── Earthquakes ────────────────────────────────────────────────


def quakes(
    bbox: tuple[float, float, float, float],
    hours: int = 168,
    min_magnitude: float = 2.5,
) -> dict:
    """Every located earthquake in a rectangle, strongest first."""
    west, south, east, north = bbox
    key = f"q:{west:.2f},{south:.2f},{east:.2f},{north:.2f}:{hours}:{min_magnitude}"
    if (hit := _cached(key, EVENTS_SECONDS)) is not None:
        return hit

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    resp = _get(EVENTS_URL, {
        "format": "geojson",
        "starttime": since.strftime("%Y-%m-%dT%H:%M:%S"),
        "minlatitude": max(south, -90), "maxlatitude": min(north, 90),
        "minlongitude": west, "maxlongitude": east,
        "minmagnitude": min_magnitude,
        "orderby": "magnitude",
        "limit": MAX_EVENTS,
    })
    features = resp.json().get("features", []) if resp.content else []

    out = []
    for feature in features:
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = float(coords[0]), float(coords[1])
        out.append({
            "id": feature.get("id"),
            "lon": lon, "lat": lat,
            # Third coordinate is depth in kilometres, and negative values are
            # real: a shallow event can be located above sea level.
            "depth_km": round(float(coords[2]), 1) if len(coords) > 2 and coords[2] is not None else None,
            "magnitude": props.get("mag"),
            "magnitude_type": props.get("magType"),
            "place": props.get("place"),
            "time": _moment(props.get("time")),
            # How many people felt it strongly enough to say so, and the
            # strongest shaking reported. Both are often absent.
            "felt": props.get("felt"),
            "intensity": props.get("cdi") or props.get("mmi"),
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
        })

    return _keep(key, {
        "quakes": out,
        "count": len(out),
        "hours": hours,
        "min_magnitude": min_magnitude,
        "source": ATTRIBUTION["events"],
        "attribution": ATTRIBUTION["events"],
    })


def _moment(millis) -> str | None:
    try:
        return dt.datetime.fromtimestamp(
            float(millis) / 1000.0, dt.timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError):
        return None


# ── Seismograph stations ───────────────────────────────────────


def stations(bbox: tuple[float, float, float, float]) -> dict:
    """Every open seismograph in a rectangle, from everyone who indexes one."""
    west, south, east, north = bbox
    key = f"s:{west:.1f},{south:.1f},{east:.1f},{north:.1f}"
    if (hit := _cached(key, STATIONS_SECONDS)) is not None:
        return hit

    now = dt.datetime.now(dt.timezone.utc)
    params = {
        # Text is a fraction of the size of StationXML and carries everything
        # needed to put a dot on a map and ask for its trace afterwards.
        "format": "text", "level": "channel",
        "minlatitude": max(south, -90), "maxlatitude": min(north, 90),
        "minlongitude": west, "maxlongitude": east,
        "channel": CHANNELS,
        # Only instruments that are still running: a station decommissioned in
        # 1998 has no live trace to show.
        "endafter": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "includerestricted": "false",
        "nodata": "204",
    }

    primary = [s for s in STATION_SERVICES if s[0] not in STATION_FALLBACKS]
    answers, trouble = _ask_all(primary, params)

    # The legacy host is the same index under an old name, so it is only worth
    # asking when the one that replaced it said nothing at all.
    if not any(answers.values()):
        spare = [s for s in STATION_SERVICES if s[0] in STATION_FALLBACKS]
        more, more_trouble = _ask_all(spare, params)
        answers.update(more)
        trouble.extend(more_trouble)

    if not any(answers.values()) and trouble:
        raise SeismicLookupError(
            "No station index answered. " + _short("; ".join(trouble), 240))

    found = _parse_channels(
        "\n".join(text for text in answers.values() if text),
        centre=((west + east) / 2, (south + north) / 2))
    found["services"] = sorted(name for name, text in answers.items() if text)
    # Named rather than counted, so a thin answer over Europe can be traced to
    # the node that did not reply rather than looking like empty ground.
    found["missing"] = sorted(name for name, text in answers.items() if not text)
    return _keep(key, found)


def _ask_all(services, params) -> tuple[dict[str, str], list[str]]:
    """Ask every index at once and collect whatever comes back.

    Together rather than one after another: ten services in series would take
    ten times the slowest of them, and the whole point of asking all of them is
    that no single one knows about every instrument.
    """
    answers: dict[str, str] = {}
    trouble: list[str] = []
    lock = threading.Lock()

    def ask(name: str, url: str) -> None:
        try:
            resp = _get(url, params, timeout=STATIONS_TIMEOUT)
            text = resp.text if resp.content else ""
        except SeismicLookupError as exc:
            with lock:
                answers[name] = ""
                trouble.append(f"{name}: {exc}")
            return
        with lock:
            answers[name] = text

    threads = [threading.Thread(target=ask, args=(n, u), daemon=True)
               for n, u in services]
    for thread in threads:
        thread.start()
    for thread in threads:
        # A node that has stopped answering must not hold up the nine that did.
        thread.join(timeout=STATIONS_TIMEOUT + 5)
    return answers, trouble


def _parse_channels(text: str, centre: tuple[float, float] | None = None) -> dict:
    """Pipe-separated FDSN channel rows to one entry per station.

    A station reports several channels and often several instrument
    generations of each. They are all the same dot on the map, so the rows are
    folded onto the station and the best channel kept for the trace.
    """
    held: dict[tuple[str, str], dict] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        net, sta, loc, cha = parts[0], parts[1], parts[2], parts[3]
        try:
            lat, lon, elev = float(parts[4]), float(parts[5]), float(parts[6])
        except ValueError:
            continue
        entry = held.setdefault((net, sta), {
            "network": net, "station": sta, "lat": lat, "lon": lon,
            "elevation_m": round(elev),
            # Channel-level rows carry no site name -- that column only exists
            # at station level -- but they do name the instrument, which is
            # the more interesting half of it anyway.
            "instrument": parts[10] if len(parts) > 10 else "",
            "channels": [],
            "loc": loc, "channel": cha,
        })
        if cha not in entry["channels"]:
            entry["channels"].append(cha)
        # Broadband beats short period for a first look, so let it win the
        # channel that the trace button will ask for.
        if _rank(cha) < _rank(entry["channel"]):
            entry["channel"], entry["loc"] = cha, loc

    # Nearest the middle of the view first. This used to sort by network name
    # and then truncate, which meant a wide view silently returned whichever
    # networks happened to sort early in the alphabet -- and the interface
    # described that as "nearest shown", which it was not.
    if centre is not None:
        cx, cy = centre
        out = sorted(held.values(),
                     key=lambda s: math.hypot(s["lon"] - cx, s["lat"] - cy))
    else:
        out = sorted(held.values(), key=lambda s: (s["network"], s["station"]))
    return {
        "stations": out[:MAX_STATIONS],
        "count": len(out),
        "capped": len(out) > MAX_STATIONS,
        "source": ATTRIBUTION["stations"],
        "attribution": ATTRIBUTION["stations"],
    }


# Which channel a station's trace is asked for when it offers several.
#
# Seismometers first, because they are what a seismogram normally means, and
# within those the faster bands, because they show an arrival rather than a
# smear. Accelerometers come last: they are deaf to small distant events by
# design, which is exactly what makes them useful in a city and useless as a
# first look. A station that has nothing else still gets plotted with one.
_CHANNEL_ORDER = [
    "HHZ", "BHZ", "SHZ", "EHZ", "MHZ", "LHZ", "CHZ", "DHZ", "GHZ", "VHZ",
    "HNZ", "BNZ", "ENZ", "SNZ", "LNZ",
    "HLZ", "BLZ", "ELZ",
]


def _rank(channel: str) -> int:
    return _CHANNEL_ORDER.index(channel) if channel in _CHANNEL_ORDER else len(_CHANNEL_ORDER)


# ── The trace itself ───────────────────────────────────────────


def centres_for(network: str) -> list[tuple[str, str]]:
    """Which archives could hold this network's recordings.

    Everything open is tried in turn, with one exception: network AM, the
    Raspberry Shakes, is archived by Raspberry Shake and by nobody else. The
    federated nodes have never held it, so asking them is six timeouts and a
    minute of somebody's afternoon for a guaranteed nothing.
    """
    if shake.ours(network):
        return [(shake.ATTRIBUTION, shake.DATA_URL)]
    return list(DATA_CENTRES)


def trace(network: str, station: str, channel: str, loc: str = "", minutes: int = 60) -> bytes:
    """A plotted seismogram: the last `minutes` of ground motion, as a PNG.

    The data centres are tried in turn, because which one holds a given
    station's recordings depends on the network that runs it and there is no
    way to know from the metadata alone. The first one with data wins.
    """
    end = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=TRACE_LAG_MINUTES)
    start = end - dt.timedelta(minutes=minutes)

    # The location code in the metadata and the one the archive filed the
    # recording under do not always agree, and a mismatch returns nothing at
    # all rather than an explanation. So the exact code is tried first and any
    # code second.
    locations = [loc or "--"]
    if locations[0] != "*":
        locations.append("*")

    troubles: list[str] = []
    for label, url in centres_for(network):
        for location in locations:
            try:
                resp = _get(url, {
                    "net": network, "sta": station, "cha": channel, "loc": location,
                    "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
                    "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
                    "nodata": "204",
                }, timeout=TRACE_TIMEOUT, empty=(204, 404))
            except SeismicLookupError as exc:
                troubles.append(f"{label}: {_short(str(exc))}")
                break
            if resp.status_code in (204, 404) or not resp.content:
                continue
            try:
                reading = miniseed.samples(resp.content)
            except miniseed.MiniSeedError as exc:
                troubles.append(f"{label}: {exc}")
                continue
            return plot(reading, f"{network}.{station}.{channel}", label, minutes)

    detail = f" Tried: {'; '.join(troubles)}." if troubles else ""
    raise SeismicLookupError(
        f"No data centre has a recording of {network}.{station} {channel} for "
        f"the last {minutes} minutes. Either the station is offline or its "
        f"telemetry is running behind.{detail} A longer window often finds it."
    )


def _short(message: str, limit: int = 120) -> str:
    """One line of an error, with any HTML page taken out of it.

    A retired endpoint answers with a whole document, and pasting that into the
    interface buries the one useful sentence in markup.
    """
    text = re.sub(r"<[^>]*>", " ", message)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


# ── Drawing it ─────────────────────────────────────────────────

PLOT_SIZE = (760, 250)

# The line across the trace, and what it means.
#
# A seismogram from this app is raw counts from an instrument nobody here has
# calibrated, and the vertical scale is whatever that hour happened to need.
# So a line at a fixed number of counts would mean one thing on one station
# and something else on the next, and nothing at all on a third.
#
# What can be said instrument by instrument is how loud a thing is against the
# ground's own background at that place. That background is measured from the
# trace itself, robustly -- the median of the absolute deviations, which the
# constant below turns into the standard deviation it would imply for ordinary
# noise. Robust because the events are in the data too, and an average that
# includes them raises the line by exactly the amount the event was worth.
#
# How far above the background the line sits is NOT a fixed multiple, and the
# reason is worth stating because a fixed one is the obvious thing to write.
# An hour of a 20 Hz station is seventy thousand samples; six hours of a 100 Hz
# Raspberry Shake is two million. Ordinary background noise reaches further
# from quiet the more often you look at it -- over two million samples it will
# touch five times its own spread with no event anywhere near -- so one
# multiple cannot mean the same thing in both windows. Drawn at five, the line
# would be a fair "that was something" on the short window and a line the
# noise crosses on its own on the long one.
#
# So the level is set from the length of the window: high enough that
# background alone would be expected to cross it about once in a hundred
# windows like this one. The number of multiples that works out to is printed
# on the plot, because it changes.
#
# It is NOT a line above which something is an earthquake. A lorry on a nearby
# road crosses it. So does a door, on a Shake in somebody's house. What it
# says is "something happened here that this window's ordinary noise does not
# explain", which is the question a reader of this panel is actually asking.
CROSSINGS_ALLOWED = 0.01

# Turns a median absolute deviation into the standard deviation of the normal
# distribution that would produce it. Standard, and the reason the background
# can be measured without the events in the data spoiling the measurement.
MAD_TO_SIGMA = 1.4826

# Where the line may sit, in multiples of the background, from the arithmetic
# for clean noise alone. The burst measurement below can take it higher.
LOUD_LEAST, LOUD_MOST = 3.5, 8.0

# And how high it may go once the station's own background is taken into
# account. A guard against a measurement gone wrong rather than a working
# limit: a floor in a busy building measures twenty to thirty times its own
# quiet level, and anything past sixty is a fault, not a site.
LOUD_CEILING = 60.0

# A hair more room than the line needs, so it is inside the plot rather than
# ruled along its edge.
LOUD_HEADROOM = 1.15

# How the burst level below is measured, and why in pieces.
#
# The window is cut into minutes and each minute's loudest hundredth is taken;
# the answer is the middle of those. A background full of short bursts has
# them in every minute, so the middle minute is as bursty as the rest and the
# measurement finds them. An event sits in one minute, or in five of sixty,
# and the middle minute has never heard of it.
#
# That second half is the whole reason for the chunking, and it is not a
# refinement. Measured over the window as a whole, a four-hundred-second
# teleseism is eleven per cent of an hour: it dominates any percentile you
# could pick, raises the line well above itself, and the largest earthquake in
# the window is the one thing the line then fails to mark.
BURST_PIECES = 60
BURST_LEAST_SAMPLES = 8

# Which part of each minute is read as "the loud end of the background".
BURST_PERCENTILE = 99.9

# And how far above that the line goes.
#
# Measured rather than picked. The loudest single sample of an hour runs about
# one and a half to one and three quarter times the typical minute's loudest
# hundredth, on clean noise and on four kinds of synthetic busy floor -- so at
# one and four fifths the very loudest burst of the hour still grazed the line
# in nine of thirty-two bursty hours, and at two it stopped: no crossings in
# thirty-two, while a bang of any real size clears the line several times
# over. Above the bursts, under the events, which is where a reader wants it.
BURST_MARGIN = 2.0


def burst_level(values: Any, spread: float) -> float:
    """How loud this station's own background gets, in multiples of quiet.

    The reason the line cannot be arithmetic on clean noise alone.

    That arithmetic assumes the background is the smooth, well-behaved kind a
    normal distribution describes, which is roughly true in a vault on
    bedrock. It is not remotely true of a geophone on somebody's floor. A
    Raspberry Shake's ordinary hour is a stream of short sharp bursts --
    footsteps, a door, a lorry on the road outside -- reaching ten or twenty
    times the quiet level, and a line drawn where clean noise would rarely
    reach is one that station crosses a dozen times an hour. That is worse
    than no line: it marks everything, so it means nothing.

    So the background's own loudness is measured rather than assumed, and the
    line goes above whatever is found. A vault station measures about three
    and is left where the arithmetic put it; a busy house measures fourteen
    and gets a bar four times higher, which is the honest place for it.
    """
    if getattr(values, "size", 0) < BURST_LEAST_SAMPLES * 2 or spread <= 0:
        return 0.0
    pieces = max(2, min(BURST_PIECES, int(values.size) // BURST_LEAST_SAMPLES))
    loud = [float(np.percentile(np.abs(piece), BURST_PERCENTILE))
            for piece in np.array_split(values, pieces)]
    typical = float(np.median(loud)) / spread
    return typical if np.isfinite(typical) else 0.0


def loud_sigmas(count: int, allowed: float = CROSSINGS_ALLOWED) -> float:
    """How many backgrounds up the line goes for clean noise, over `count`
    samples.

    The level pure noise would be expected to cross `allowed` times in a
    window this long. Solved rather than looked up: the chance of one sample
    of normal noise being further than k from quiet is erfc(k / root two), so
    the level wanted is where that chance times the number of samples comes to
    the number of crossings we are willing to call background.

    This is the floor. What a station's own background actually does to it is
    burstiness(), below.
    """
    if count < 2:
        return LOUD_LEAST
    want = max(1e-15, allowed / count)
    low, high = LOUD_LEAST, LOUD_MOST
    for _ in range(60):
        middle = (low + high) / 2
        if math.erfc(middle / math.sqrt(2)) > want:
            low = middle
        else:
            high = middle
    return round((low + high) / 2, 1)


def loud_level(values: Any) -> tuple[float, float]:
    """Where loud starts for this trace: (counts, multiples of background).

    Zero counts when there is nothing to measure -- a flat channel, or one
    that is all spike -- and the line is left off rather than drawn somewhere
    arbitrary.
    """
    if getattr(values, "size", 0) < 2:
        return 0.0, LOUD_LEAST
    spread = float(np.median(np.abs(values))) * MAD_TO_SIGMA
    if not np.isfinite(spread) or spread <= 0:
        return 0.0, loud_sigmas(int(values.size))
    # The higher of the two: where clean noise would rarely reach, and where
    # this station's own background actually gets to.
    sigmas = max(loud_sigmas(int(values.size)),
                 burst_level(values, spread) * BURST_MARGIN)
    sigmas = round(min(sigmas, LOUD_CEILING), 1)
    return spread * sigmas, sigmas


def plot(reading: dict, title: str, source: str, minutes: int) -> bytes:
    """Draw a seismogram.

    Down-sampled to the width of the plot by taking the highest and lowest
    sample in each column rather than every nth one. Picking every nth sample
    would drop the peaks -- which are the entire point of a seismogram -- and
    quietly turn an earthquake into a quiet afternoon.
    """
    from PIL import Image, ImageDraw

    values = np.asarray(reading["samples"], dtype="float64")
    values = values[np.isfinite(values)]
    if not values.size:
        raise SeismicLookupError("the recording held no usable samples")
    values = values - np.median(values)

    width, height = PLOT_SIZE
    middle = height // 2
    image = Image.new("RGB", (width, height), (12, 15, 20))
    draw = ImageDraw.Draw(image)

    plot_top, plot_bottom = 26, height - 26
    span = (plot_bottom - plot_top) / 2

    for x in range(0, width, width // 6):
        draw.line([(x, plot_top), (x, plot_bottom)], fill=(28, 34, 45))
    draw.line([(0, middle), (width, middle)], fill=(40, 50, 66))

    # Scaled to the strongest excursion, ignoring the very largest few samples
    # so that one spike of instrument noise does not flatten the whole trace.
    peak = float(np.percentile(np.abs(values), 99.9)) or float(np.abs(values).max()) or 1.0

    # The line is a reference, so the scale has to include it -- otherwise a
    # quiet hour, whose largest excursion is three times the background, would
    # put the line off the top of the picture and leave nothing to compare the
    # trace against. Including it also does something useful on its own: on a
    # quiet hour the trace draws small and well inside the lines, which is
    # what a quiet hour looks like.
    level, sigmas = loud_level(values)
    if level > 0:
        peak = max(peak, level * LOUD_HEADROOM)

    columns = np.array_split(values, min(width, values.size))
    for x, column in enumerate(columns):
        low, high = float(column.min()), float(column.max())
        y1 = middle - max(-1.0, min(1.0, high / peak)) * span
        y2 = middle - max(-1.0, min(1.0, low / peak)) * span
        draw.line([(x, y1), (x, y2)], fill=(126, 214, 255))

    # Drawn after the trace, so it is legible over the busy part, and dashed
    # so it reads as a line somebody put there rather than as something the
    # ground did.
    if level > 0:
        for edge in (middle - (level / peak) * span, middle + (level / peak) * span):
            for x in range(0, width, 12):
                draw.line([(x, edge), (min(x + 6, width), edge)], fill=(232, 70, 70))
        # On the left, under the station name. The right-hand end of the plot
        # is where the reader is told when the picture was fetched, and the
        # line moves up and down with how quiet the hour was -- so a label
        # over there would sooner or later land on top of that.
        draw.text((10, max(22, middle - (level / peak) * span - 15)),
                  f"louder than background x{sigmas:g}", fill=(232, 110, 110))

    rate = reading.get("rate") or 1.0
    seconds = values.size / rate
    draw.text((10, 8), f"{title}   {rate:g} Hz", fill=(150, 165, 185))
    draw.text((10, height - 18),
              f"{seconds / 60:.0f} min of {minutes} asked for   -   {source}",
              fill=(120, 134, 152))
    draw.text((width - 92, height - 18), "counts, demeaned", fill=(120, 134, 152))

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ── Synthetic seismology (DEMO_MODE) ───────────────────────────


# The smallest synthetic event generated, before the requested filter is
# applied on top of it.
_DEMO_FLOOR = 0.6


def demo_quakes(bbox: tuple[float, float, float, float], hours: int = 168,
                min_magnitude: float = 2.5) -> dict:
    """Believable seismicity for exploring offline.

    Clustered along a line rather than scattered, because real earthquakes
    follow faults and a field of random dots would misrepresent the layer.
    """
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    out = []
    for i in range(40):
        t = (i + 0.5) / 40
        wobble = 0.06 * math.sin(t * 11) + 0.03 * math.sin(t * 29)
        # Gutenberg-Richter in spirit: small ones far outnumber large ones.
        magnitude = round(_DEMO_FLOOR + 6.4 * (1 - t) ** 3, 1)
        # The filter is applied here rather than baked into the range, so the
        # magnitude slider does something offline instead of looking broken.
        if magnitude < min_magnitude:
            continue
        out.append({
            "id": f"demo{i:03d}",
            "lon": west + span_x * t,
            "lat": south + span_y * (0.45 + wobble + 0.12 * math.sin(t * 4)),
            "depth_km": round(4 + 60 * abs(math.sin(t * 6)), 1),
            "magnitude": magnitude,
            "magnitude_type": "mb",
            "place": f"{10 + i * 3} km demo region",
            "time": (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=hours * t)).isoformat(timespec="seconds"),
            "felt": int(200 * (magnitude - _DEMO_FLOOR)) or None,
            "intensity": round(magnitude - 1.5, 1),
            "tsunami": False,
            "url": None,
        })
    out.sort(key=lambda q: -q["magnitude"])
    return {
        "quakes": out, "count": len(out), "hours": hours,
        "min_magnitude": min_magnitude, "demo": True,
        "source": "Synthetic", "attribution": "Synthetic demo data",
    }


def demo_stations(bbox: tuple[float, float, float, float]) -> dict:
    west, south, east, north = bbox
    span_x, span_y = east - west, north - south
    out = []
    for i in range(12):
        row, col = divmod(i, 4)
        out.append({
            "network": "XX",
            "station": f"DM{i:02d}",
            "lat": south + span_y * (0.2 + 0.3 * row),
            "lon": west + span_x * (0.15 + 0.23 * col),
            "elevation_m": 40 + i * 37,
            "instrument": "Synthetic broadband",
            "channels": ["BHZ", "HHZ"],
            "loc": "00", "channel": "BHZ",
        })
    return {
        "stations": out, "count": len(out), "capped": False, "demo": True,
        "services": ["Synthetic"], "missing": [],
        "source": "Synthetic", "attribution": "Synthetic demo data",
    }


def demo_trace(network: str, station: str, channel: str, minutes: int = 60) -> bytes:
    """A drawn seismogram that looks like one, without pretending to be real.

    Background noise with one arrival in it: a sharp onset that decays away,
    which is the shape a distant earthquake actually makes. Drawn through the
    same plotting code as a real recording, so what is on screen offline is
    laid out exactly like what arrives when the services are reachable.
    """
    from PIL import Image, ImageDraw

    rate = 20.0
    count = int(minutes * 60 * rate)
    # Deterministic: the same station always draws the same trace, so a redraw
    # does not look like new data arriving.
    seed = sum(ord(c) for c in f"{network}{station}{channel}")
    t = np.arange(count) / rate
    ground = (
        220 * np.sin(t * 1.7 + seed)
        + 140 * np.sin(t * 4.3 + seed * 0.7)
        + 90 * np.sin(t * 0.41)
    )
    onset = int(count * 0.42)
    arrival = np.zeros(count)
    after = np.arange(count - onset)
    arrival[onset:] = (
        4200 * np.exp(-after / (count * 0.06)) * np.sin(after * 0.9 + seed * 0.1)
    )

    png = plot(
        {"samples": ground + arrival, "rate": rate, "start": None, "channel": channel},
        f"{network}.{station}.{channel}", "Synthetic demo data", minutes,
    )

    # Say so on the picture itself. A seismogram is exactly the kind of image
    # that gets screenshotted away from the app that made it.
    image = Image.open(io.BytesIO(png)).convert("RGB")
    ImageDraw.Draw(image).text(
        (10, image.height - 32), "SYNTHETIC - demo mode, not a real recording",
        fill=(255, 170, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
