"""Raspberry Shake seismographs, kept apart from the professional ones.

The seismic panel next door draws the open federated networks: research-grade
instruments in vaults, indexed by the FDSN nodes. Raspberry Shakes are not
those. They are a few hundred pounds of geophone and a single-board computer,
usually on somebody's floor, and network AM has thousands of them.

That difference is the reason they get their own panel rather than being
merged into the other list, and it is worth stating plainly rather than
leaving as a styling choice:

  THE INSTRUMENTS ARE NOT COMPARABLE. A Shake's noise floor is a house, not
  bedrock -- a door closing shows up on it. Read beside a borehole
  seismometer's trace as though the two were the same kind of measurement, it
  would make a quiet research station look dead and a busy hallway look
  seismically active.

  THE SITING IS NOT SURVEYED. A professional station's coordinates are where
  the instrument is. A Shake's are where its owner put the pin, to whatever
  precision they felt like.

  AND THEY ARE WHERE THE OTHERS ARE NOT. Which is the whole point of having
  them: Ukraine has very few open federated stations, and the Shakes are in
  Zaporizhzhia, Kharkiv, Rivne. For watching for the ground shaking near a
  city under attack, an amateur instrument in the city beats a research one
  in another country. The same is true along the Gulf of Finland and in the
  Emirates, which is why those are here too.

The ones below were asked for by name. Their recordings come from Raspberry
Shake's own FDSN service rather than from the federated data centres -- the AM
network archives its own data and the EarthScope and ORFEUS nodes have never
held it, so asking them is a guaranteed round trip to nothing.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
from typing import Any

import requests

from . import config
from .reasons import why

NETWORK = "AM"

# Raspberry Shake run their own FDSN endpoints. Both are the published
# standard, the same shape the federated nodes answer.
STATION_URL = "https://data.raspberryshake.org/fdsnws/station/1/query"
DATA_URL = "https://data.raspberryshake.org/fdsnws/dataselect/1/query"

# Where a reader goes to see the station itself, which is worth linking
# because their own viewer shows the live helicorder.
VIEW_URL = "https://dataview.raspberryshake.org/#/{net}/{code}/{loc}/{channel}"

ATTRIBUTION = "Raspberry Shake community network (AM)"

# The stations asked for, with the place each one is in.
#
# The coordinates here are the CITY, not the instrument: a Shake's real
# position comes from the station service below, and until that answers the
# pin is the town it is in. Said out loud in the panel rather than quietly
# presented as a surveyed position -- these are somebody's front room and the
# difference between the two matters when reading a trace.
# An entry marked `given` arrived with its own coordinates rather than only a
# place name, so its pin is the position that was published for the
# instrument instead of the middle of the town it is in. Told apart in the
# panel, because "in this city somewhere" and "here" are different claims and
# a pin looks identical either way.
SHAKES: tuple[dict[str, Any], ...] = (
    {"code": "RD834", "place": "Zaporizhzhia", "lat": 47.8388, "lon": 35.1396},
    {"code": "R2DB7", "place": "Kharkiv", "lat": 49.9935, "lon": 36.2304},
    {"code": "S29F5", "place": "Khrystynivka", "lat": 48.8114, "lon": 29.9686},
    {"code": "SE569", "place": "Rivne", "lat": 50.6199, "lon": 26.2516},
    # Estonia, on the Gulf of Finland between Sillamäe and Narva -- about
    # twenty kilometres from the Russian border.
    {"code": "S5D35", "place": "Sillamäe, Estonia",
     "lat": 59.2364007, "lon": 27.3449578, "given": True},
    # Three in the Emirates: two in Dubai and one in Abu Dhabi.
    {"code": "R1F39", "place": "Dubai, United Arab Emirates",
     "lat": 25.2361516, "lon": 55.3570216, "given": True},
    {"code": "S994C", "place": "Dubai, United Arab Emirates",
     "lat": 25.0898023, "lon": 55.4304727, "given": True},
    {"code": "R85A6", "place": "Abu Dhabi, United Arab Emirates",
     "lat": 24.4302849, "lon": 54.4478012, "given": True},
)

# What one of these can be asked for, best first.
#
# EHZ is the geophone every Shake has. SHZ is the same thing on the older
# boards. ENZ is the accelerometer in a 4D, which hears a lorry rather than a
# distant earthquake. HDF is the other instrument entirely: a Boom is a
# barometer sampling fast enough to hear, and what it records is pressure in
# the air rather than motion in the ground -- explosions, sonic booms, thunder,
# machinery. It is a microphone, and it is listed as one.
CHANNELS = ("EHZ", "SHZ", "ENZ", "HDF")
MICROPHONE = "HDF"

# Every named station above records the same way: a vertical geophone at 100
# samples a second, on location 00. Written once rather than repeated per
# station, so a fifth is a line rather than four.
LOCATION = "00"
CHANNEL = "EHZ"

# How long the real coordinates are kept once fetched. A Shake moves when
# somebody carries it to another room, which is not often, but it is a thing
# that happens -- unlike a borehole.
PLACES_SECONDS = 12 * 3600
PLACES_TIMEOUT = 20

# How many of them a rectangle may come back with, and how long that answer is
# kept. The limit is not politeness to the index -- it is that a thousand dots
# over a European city is a smear rather than a map, and the nearest sixty to
# the middle of the view are the ones somebody is looking at.
NEARBY_LIMIT = 60
NEARBY_SECONDS = 30 * 60
NEARBY_TIMEOUT = 25
# How many rectangles are remembered at once. Panning a map makes a new one
# every time, and without a ceiling this would grow for as long as the process
# lives.
NEARBY_BOXES = 24

# An owner's own description of their station. Kept because "Muscat rooftop"
# says more than any coordinate, and cut short because it is free text from a
# stranger and the panel has a column, not a paragraph.
NAME_LIMIT = 48

_lock = threading.Lock()
_placed: dict[str, dict[str, float]] = {}
_placed_at = 0.0
_trouble = ""
_boxes: dict[str, tuple[float, dict[str, Any]]] = {}


class ShakeError(RuntimeError):
    pass


def forget() -> None:
    """Drop the fetched coordinates. For the tests."""
    global _placed_at, _trouble
    with _lock:
        _placed.clear()
        _boxes.clear()
        _placed_at = 0.0
        _trouble = ""


def read_places(text: str) -> dict[str, dict[str, float]]:
    """Coordinates out of an FDSN station reply, keyed by station code.

    The text format, because it is the one every FDSN service agrees on and
    parsing it needs no XML. Header lines start with '#'.
    """
    out: dict[str, dict[str, float]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 6:
            continue
        code = parts[1]
        try:
            lat, lon = float(parts[2]), float(parts[3])
        except ValueError:
            continue
        # 0,0 is the null island a station with no position set lands on, and
        # it is in the Atlantic. Better to keep the town.
        if lat == 0.0 and lon == 0.0:
            continue
        out[code] = {"lat": lat, "lon": lon,
                     "elevation_m": _number(parts[4]),
                     "name": parts[5] if len(parts) > 5 else ""}
    return out


def _number(said: str) -> float | None:
    try:
        return round(float(said))
    except (TypeError, ValueError):
        return None


def places(refresh: bool = False,
           get: Any = None) -> tuple[dict[str, dict[str, float]], str]:
    """Where these Shakes actually are, and why they are not, if they are not.

    Never raises. A station service that cannot be reached leaves the pins on
    their towns, which is a worse position and a perfectly usable map -- and
    the panel says which it is showing.
    """
    global _placed_at, _trouble
    with _lock:
        if _placed and not refresh and time.time() - _placed_at < PLACES_SECONDS:
            return dict(_placed), _trouble
    send = get or requests.get
    try:
        resp = send(STATION_URL, params={
            "net": NETWORK,
            "sta": ",".join(s["code"] for s in SHAKES),
            "level": "station",
            "format": "text",
            "nodata": "404",
        }, timeout=PLACES_TIMEOUT,
            headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        with _lock:
            _trouble = ("Raspberry Shake's station index could not be reached: "
                        f"{why(exc)}")
            return dict(_placed), _trouble
    if not getattr(resp, "ok", False):
        with _lock:
            _trouble = (f"Raspberry Shake's station index answered "
                        f"{getattr(resp, 'status_code', '?')}")
            return dict(_placed), _trouble
    found = read_places(getattr(resp, "text", "") or "")
    with _lock:
        if found:
            _placed.update(found)
            _placed_at = time.time()
            _trouble = ""
        else:
            _trouble = "Raspberry Shake's station index listed none of these"
        return dict(_placed), _trouble


def read_channels(text: str) -> dict[str, dict[str, Any]]:
    """One entry per station out of a channel-level FDSN reply.

    A station answers with a row per channel, and often several instrument
    generations of each. They are one dot on the map, so the rows are folded
    together: the position off the first of them, every channel kept so the
    panel can say what the thing actually is, and the best one chosen for the
    trace button.

    Rows for an instrument that has been switched off are dropped here rather
    than asked about in the query. `endafter` is in the standard and the
    federated nodes honour it, but a filter written down the wire is a filter
    this cannot test, and a station that stopped recording in 2019 has no
    live trace to draw.
    """
    out: dict[str, dict[str, Any]] = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 8:
            continue
        net, code, loc, cha = parts[0], parts[1], parts[2], parts[3]
        if not ours(net) or not code:
            continue
        try:
            lat, lon = float(parts[4]), float(parts[5])
        except ValueError:
            continue
        # 0,0 is where a station with no position set lands, and it is in the
        # Atlantic. Better no dot than a dot off Ghana.
        if lat == 0.0 and lon == 0.0:
            continue
        if not still_running(parts[16] if len(parts) > 16 else ""):
            continue
        entry = out.setdefault(code, {
            "code": code, "lat": lat, "lon": lon,
            "elevation_m": _number(parts[6]) if len(parts) > 6 else None,
            "channels": [], "channel": cha, "loc": loc,
        })
        if cha not in entry["channels"]:
            entry["channels"].append(cha)
        if _rank(cha) < _rank(entry["channel"]):
            entry["channel"], entry["loc"] = cha, loc
    return out


def _rank(channel: str) -> int:
    return (CHANNELS.index(channel) if channel in CHANNELS else len(CHANNELS))


def still_running(ended: str) -> bool:
    """Whether a channel row is for an instrument that has not been retired.

    An empty end time means open-ended, which is the usual case for something
    sitting in a living room. Anything unparseable is kept: a station is not
    written off over a date this could not read.
    """
    said = (ended or "").strip()
    if not said:
        return True
    try:
        when = dt.datetime.fromisoformat(said.replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when > dt.datetime.now(dt.timezone.utc)


def read_names(text: str) -> dict[str, str]:
    """What each station's owner called it, out of a station-level reply.

    Free text from a stranger, so it is cut to a column's worth and otherwise
    left alone -- it is shown as a text node, never as markup.
    """
    out: dict[str, str] = {}
    for code, found in read_places(text).items():
        name = " ".join(str(found.get("name") or "").split())[:NAME_LIMIT]
        if name:
            out[code] = name
    return out


def nearby(box: tuple[float, float, float, float],
           refresh: bool = False, get: Any = None) -> dict[str, Any]:
    """Every Shake and Boom inside a rectangle, from the AM index.

    The named list above is a decision; this is a question. Most of this
    network is nowhere near anything that was asked for by name, and the only
    way to know what is watching a given coast is to ask what is there --
    which is the same thing the professional panel next door does, against a
    different index.

    Never raises. A rectangle the index would not answer for comes back empty
    with the reason attached, because the named stations are still worth
    drawing and a panel that throws loses them too.
    """
    west, south, east, north = box
    key = f"{west:.2f},{south:.2f},{east:.2f},{north:.2f}"
    with _lock:
        held = _boxes.get(key)
        if held and not refresh and time.time() - held[0] < NEARBY_SECONDS:
            return dict(held[1])

    where = {
        "net": NETWORK,
        "minlatitude": max(south, -90.0), "maxlatitude": min(north, 90.0),
        "minlongitude": west, "maxlongitude": east,
        "format": "text", "nodata": "204",
    }
    text, trouble = _ask({**where, "level": "channel",
                          "cha": ",".join(CHANNELS)}, get)
    found = read_channels(text)
    # Names are worth having and not worth failing over: a list of codes is a
    # usable panel, and a second request that went wrong must not empty the
    # first one.
    names = read_names(_ask({**where, "level": "station"}, get)[0]) if found else {}

    middle = ((west + east) / 2, (south + north) / 2)
    order = sorted(found.values(),
                   key=lambda s: math.hypot(s["lon"] - middle[0],
                                            s["lat"] - middle[1]))
    answer = {
        "stations": [_discovered(s, names.get(s["code"], ""))
                     for s in order[:NEARBY_LIMIT]],
        "count": len(order),
        "capped": len(order) > NEARBY_LIMIT,
        "trouble": trouble,
    }
    with _lock:
        if len(_boxes) >= NEARBY_BOXES:
            _boxes.pop(min(_boxes, key=lambda k: _boxes[k][0]), None)
        _boxes[key] = (time.time(), answer)
    return dict(answer)


def _discovered(found: dict[str, Any], name: str) -> dict[str, Any]:
    """One station off the index, in the shape the panel draws."""
    channel = found["channel"]
    return {
        "network": NETWORK,
        "station": found["code"],
        # Its owner's description if it has one, and otherwise where it is.
        # Not a town: this app has no offline gazetteer, and guessing the
        # nearest city from a coordinate is how a Shake in Seeb gets labelled
        # Muscat and then quoted as though somebody had checked.
        "place": name or _position(found["lat"], found["lon"]),
        "named": bool(name),
        "loc": found["loc"],
        "channel": channel,
        "channels": list(found["channels"]),
        # Which instrument the trace button will draw from. A Boom is not a
        # seismograph and its trace is not ground motion.
        "kind": "microphone" if channel == MICROPHONE else "seismograph",
        "hears_air": MICROPHONE in found["channels"],
        "lat": found["lat"],
        "lon": found["lon"],
        "elevation_m": found["elevation_m"],
        "placed": "station",
        "asked_for": False,
        "view": VIEW_URL.format(net=NETWORK, code=found["code"],
                                loc=found["loc"], channel=channel),
    }


def _position(lat: float, lon: float) -> str:
    return (f"{abs(lat):.3f}°{'N' if lat >= 0 else 'S'} "
            f"{abs(lon):.3f}°{'E' if lon >= 0 else 'W'}")


def _ask(params: dict[str, Any], get: Any = None) -> tuple[str, str]:
    """One request to the AM station index. Never raises."""
    send = get or requests.get
    try:
        resp = send(STATION_URL, params=params, timeout=NEARBY_TIMEOUT,
                    headers={"User-Agent": config.USER_AGENT})
    except requests.RequestException as exc:
        return "", ("Raspberry Shake's station index could not be reached: "
                    f"{why(exc)}")
    # A 204 needs no branch of its own. It is the index saying there are none
    # inside this rectangle, it is a success as far as the response is
    # concerned, and it arrives with an empty body -- which falls through to
    # an empty answer with nothing to report, which is exactly right. That is
    # what `nodata=204` in the query is for: the alternative, 404, would have
    # to be told apart from a genuine one.
    if not getattr(resp, "ok", False):
        return "", ("Raspberry Shake's station index answered "
                    f"{getattr(resp, 'status_code', '?')}")
    return getattr(resp, "text", "") or "", ""


def stations(refresh: bool = False, get: Any = None,
             box: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    """The Shakes the panel draws: the named ones, and what is in view.

    Each carries where it is, whether that position is the instrument's own or
    the town it is in, and what to ask for to plot it.

    The named ones are always in the answer, wherever the map happens to be
    looking. They were asked for by name because somebody is watching those
    places, and a list that emptied itself when the map moved would be a list
    that is only ever right by accident.
    """
    found, trouble = places(refresh=refresh, get=get)
    out = []
    for shake in SHAKES:
        real = found.get(shake["code"]) or {}
        out.append({
            "network": NETWORK,
            "station": shake["code"],
            "place": shake["place"],
            "loc": LOCATION,
            "channel": CHANNEL,
            "lat": real.get("lat", shake["lat"]),
            "lon": real.get("lon", shake["lon"]),
            "elevation_m": real.get("elevation_m"),
            # Whose figure this position is, in order of how much it is worth:
            # the station index's own, the one published with the station, or
            # the middle of the town. The panel says which, because the three
            # look identical as a pin and are three different claims.
            "placed": ("station" if real.get("lat") is not None
                       else "given" if shake.get("given") else "town"),
            "kind": "seismograph",
            "channels": [CHANNEL],
            "hears_air": False,
            "asked_for": True,
            "named": True,
            "view": VIEW_URL.format(net=NETWORK, code=shake["code"],
                                    loc=LOCATION, channel=CHANNEL),
        })

    in_view: dict[str, Any] = {"stations": [], "count": 0, "capped": False,
                               "trouble": ""}
    if box is not None:
        in_view = nearby(box, refresh=refresh, get=get)
        seen = {s["station"] for s in out}
        out.extend(s for s in in_view["stations"] if s["station"] not in seen)

    # One trouble line, not two. Both halves ask the same index, so when it is
    # down they fail together and saying it twice reads like two faults.
    said = trouble or in_view["trouble"]
    return {
        "stations": out,
        "network": NETWORK,
        "channel": CHANNEL,
        "in_view": in_view["count"],
        "capped": in_view["capped"],
        "searched": box is not None,
        "microphones": sum(1 for s in out if s["kind"] == "microphone"),
        "source": ATTRIBUTION,
        "attribution": ATTRIBUTION,
        "trouble": said,
        "about": ("Hobby seismographs, not research instruments: a geophone on "
                  "somebody's floor. A door closing registers on one. They are "
                  "here because they are where the federated networks are not. "
                  "Some are Booms instead: a microphone for the air rather "
                  "than the ground."),
    }


def demo_stations(box: tuple[float, float, float, float] | None = None) -> dict[str, Any]:
    """The named ones on their towns, without asking anybody.

    The stations are named in the source rather than looked up, so the offline
    panel is the real one with the pins a shade less precise -- which is
    exactly what the live panel shows until the station index answers.

    A rectangle gets a few synthetic neighbours inside it, one of them a Boom.
    Not decoration: the offline build is where this panel gets looked at
    without a network, and a search that always comes back empty there cannot
    be told apart from a search that is broken.
    """
    out = stations(get=_nobody)
    if box is not None:
        west, south, east, north = box
        seen = {s["station"] for s in out["stations"]}
        made = []
        for i, (dx, dy, channel) in enumerate((
                (0.35, 0.40, "EHZ"), (0.62, 0.30, "EHZ"), (0.48, 0.66, "HDF"))):
            code = f"R{(abs(int((west + south) * 97)) + i) % 9973:04X}"
            if code in seen:
                continue
            made.append(_discovered({
                "code": code,
                "lat": round(south + (north - south) * dy, 4),
                "lon": round(west + (east - west) * dx, 4),
                "elevation_m": 20 + i * 15,
                "channels": [channel], "channel": channel, "loc": LOCATION,
            }, ""))
        out["stations"].extend(made)
        out["in_view"] = len(made)
        out["searched"] = True
        out["microphones"] = sum(1 for s in out["stations"]
                                 if s["kind"] == "microphone")
    out["demo"] = True
    out["trouble"] = ""
    return out


def _nobody(*_args: Any, **_kwargs: Any) -> Any:
    raise requests.RequestException("offline")


def ours(network: str) -> bool:
    """Whether a station belongs to this network rather than the others."""
    return str(network or "").upper() == NETWORK
