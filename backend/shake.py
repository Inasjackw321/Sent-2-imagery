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

# Every one of them records the same way: a vertical geophone at 100 samples a
# second, on location 00. Written once rather than repeated per station, so a
# fifth is a line rather than four.
LOCATION = "00"
CHANNEL = "EHZ"

# How long the real coordinates are kept once fetched. A Shake moves when
# somebody carries it to another room, which is not often, but it is a thing
# that happens -- unlike a borehole.
PLACES_SECONDS = 12 * 3600
PLACES_TIMEOUT = 20

_lock = threading.Lock()
_placed: dict[str, dict[str, float]] = {}
_placed_at = 0.0
_trouble = ""


class ShakeError(RuntimeError):
    pass


def forget() -> None:
    """Drop the fetched coordinates. For the tests."""
    global _placed_at, _trouble
    with _lock:
        _placed.clear()
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


def stations(refresh: bool = False, get: Any = None) -> dict[str, Any]:
    """The four Shakes, as the panel draws them.

    Each carries where it is, whether that position is the instrument's own or
    the town it is in, and what to ask for to plot it.
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
            "view": VIEW_URL.format(net=NETWORK, code=shake["code"],
                                    loc=LOCATION, channel=CHANNEL),
        })
    return {
        "stations": out,
        "network": NETWORK,
        "channel": CHANNEL,
        "source": ATTRIBUTION,
        "attribution": ATTRIBUTION,
        "trouble": trouble,
        "about": ("Hobby seismographs, not research instruments: a geophone on "
                  "somebody's floor. A door closing registers on one. They are "
                  "here because they are where the federated networks are not."),
    }


def demo_stations() -> dict[str, Any]:
    """The same four, on their towns, without asking anybody.

    The stations are named in the source rather than looked up, so the offline
    panel is the real one with the pins a shade less precise -- which is
    exactly what the live panel shows until the station index answers.
    """
    out = stations(get=_nobody)
    out["demo"] = True
    out["trouble"] = ""
    return out


def _nobody(*_args: Any, **_kwargs: Any) -> Any:
    raise requests.RequestException("offline")


def ours(network: str) -> bool:
    """Whether a station belongs to this network rather than the others."""
    return str(network or "").upper() == NETWORK
