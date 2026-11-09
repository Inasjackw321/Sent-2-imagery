"""NASA's satellites as live layers: carbon dioxide, surface temperature,
fires, the air -- through GIBS.

GIBS (Global Imagery Browse Services) is how NASA publishes its Earth
observing satellites as map imagery: hundreds of products from OCO-2, MODIS,
VIIRS, AIRS, OMI and more, daily, as ordinary WMS, with no account and no key.
EUMETSAT's catalogue has nothing that measures carbon dioxide; this is where
it comes from.

What is picked out, one layer per subject, by what the product IS rather
than its exact name (NASA renames and versions them):

  Carbon dioxide       OCO-2's column-average CO2 (XCO2), in parts per
                       million. Narrow orbit tracks a few kilometres wide,
                       not a full map: OCO-2 measures a strip under itself.
  Land surface temp    MODIS: how hot the ground is, by day and by night,
                       about 1 km. Blue cold, through to red hot.
  Fires                VIIRS 375 m thermal anomalies: NASA's own fire
                       detections as a map layer.
  Carbon monoxide      From fires and traffic, AIRS / MOPITT.
  Methane              AIRS.
  Aerosols             MODIS aerosol optical depth: smoke, dust, haze.
  Nitrogen dioxide     OMI.
  Sulphur dioxide      Volcanoes and smelters.
  Night lights         VIIRS day/night band.
  True colour          VIIRS / MODIS corrected reflectance, daily.

Like EUMETSAT's layers, each is offered only if its catalogue entry declares
a recent day -- but "recent" is two months here, because OCO-2's products
reach GIBS weeks after the overpass.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import requests

from . import config, copernicus, mtg, reasons

WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"
CAPABILITIES = WMS + "?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
TILES = "/api/gibs/wms"
ATTRIBUTION = "NASA EOSDIS GIBS"

FAMILY: dict[str, Any] = {
    "key": "nasa",
    "short": "NASA",
    "label": "NASA · CO₂, surface temperature, fires and the air",
    "colour": "#7fb3ff",
    "resolution": "375 m – 10 km",
    "spans_days": True,
    # GIBS is asked for a day as a plain date, not as a range of instants.
    "time_as_date": True,
    "live_within_hours": 24 * 60,
    "wms": TILES,
    "attribution": ATTRIBUTION,
    "about": "NASA's satellites through GIBS: OCO-2's carbon dioxide — narrow "
             "tracks under the satellite, not a full map — MODIS land surface "
             "temperature, VIIRS fire detections, and carbon monoxide, "
             "methane, aerosols and more. Daily; some products arrive days or "
             "weeks after the overpass.",
    "words": (),
}

# One layer per subject, best first. `words`: any of these in the name or
# title qualifies; `prefer`: each of these present ranks a layer higher;
# `avoid`: any of these rules it out.
SUBJECTS: tuple[dict[str, Any], ...] = (
    {"key": "co2", "words": ("carbon dioxide", "carbon_dioxide", "xco2"),
     "prefer": ("total column", "total_column", "oco-2", "oco2"),
     "avoid": ("difference", "uncertainty", "quality")},
    {"key": "lst_day", "words": ("land surface temp", "land_surface_temp"),
     "prefer": ("day", "terra", "modis"), "avoid": ("night", "anomal", "8-day", "monthly")},
    {"key": "fires", "words": ("thermal anomalies", "thermal_anomalies", "fires and thermal"),
     "prefer": ("375m", "viirs", "noaa20", "all"), "avoid": ("day only", "night only")},
    {"key": "co", "words": ("carbon monoxide", "carbon_monoxide"),
     "prefer": ("total column", "day", "airs", "mopitt"), "avoid": ("night", "monthly")},
    {"key": "aerosol", "words": ("aerosol optical depth", "aerosol_optical_depth"),
     "prefer": ("combined", "value added", "modis", "terra"), "avoid": ("angstrom",)},
    {"key": "lst_night", "words": ("land surface temp", "land_surface_temp"),
     "prefer": ("night", "terra", "modis"), "avoid": ("day", "anomal", "8-day", "monthly")},
    {"key": "methane", "words": ("methane",), "prefer": ("airs", "total column", "day"),
     "avoid": ("night", "monthly")},
    {"key": "no2", "words": ("nitrogen dioxide", "nitrogen_dioxide"),
     "prefer": ("tropospheric", "omi"), "avoid": ("monthly",)},
    {"key": "so2", "words": ("sulfur dioxide", "sulfur_dioxide", "sulphur dioxide"),
     "prefer": ("planetary boundary", "pbl", "omps"), "avoid": ("monthly",)},
    {"key": "night", "words": ("day/night band", "daynightband", "black marble", "blackmarble"),
     "prefer": ("at sensor", "enhanced", "snpp"), "avoid": ("monthly",)},
    {"key": "truecolour", "words": ("correctedreflectance_truecolor", "corrected reflectance (true color)",
                                    "true color"),
     "prefer": ("viirs", "noaa20", "snpp"), "avoid": ("bands", "false")},
)

CACHE_SECONDS = 6 * 3600
DAYS_OFFERED = copernicus.DAYS_OFFERED

_lock = threading.Lock()
_xml: str | None = None
_xml_at = 0.0

_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=2, pool_maxsize=copernicus.TILE_POOL,
    max_retries=copernicus.TILE_RETRY))
_session.headers["User-Agent"] = config.USER_AGENT


class GibsError(RuntimeError):
    pass


def capabilities(refresh: bool = False, get=None) -> str:
    """NASA's catalogue, held for six hours: it is large and changes daily."""
    global _xml, _xml_at
    with _lock:
        if _xml and not refresh and time.time() - _xml_at < CACHE_SECONDS:
            return _xml
    try:
        resp = (get or _session.get)(CAPABILITIES, timeout=90)
    except requests.RequestException as exc:
        raise GibsError(f"NASA GIBS could not be reached: {reasons.why(exc)}") from exc
    if not resp.ok:
        raise GibsError(f"NASA GIBS answered {resp.status_code}")
    with _lock:
        _xml, _xml_at = resp.text, time.time()
    return resp.text


def subject_of(name: str, title: str) -> list[tuple[str, int]]:
    """Which subjects a layer could stand for, with how well it matches."""
    low = f"{name} {title}".lower()
    out = []
    for subject in SUBJECTS:
        if not any(w in low for w in subject["words"]):
            continue
        if any(w in low for w in subject["avoid"]):
            continue
        out.append((subject["key"], sum(w in low for w in subject["prefer"])))
    return out


def days_for(newest: dt.datetime | None) -> list[dict[str, str]]:
    """The last week of days up to the newest, each asked for as a date."""
    if newest is None:
        return []
    end = newest.astimezone(dt.timezone.utc).date()
    days = [(end - dt.timedelta(days=n)).isoformat() for n in range(DAYS_OFFERED - 1, -1, -1)]
    return [{"label": d, "at": d, "time": d} for d in days]


def sort_layers(xml: str, now: dt.datetime | None = None) -> dict[str, Any]:
    """The NASA family: the best current layer for each subject."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try:
        root = mtg.ET.fromstring(xml)
    except mtg.ET.ParseError as exc:
        raise GibsError(f"NASA GIBS sent a catalogue that would not parse: {exc}") from exc
    within = dt.timedelta(hours=FAMILY["live_within_hours"])
    best: dict[str, tuple[tuple, dict]] = {}
    for node in root.iter(f"{mtg._WMS_NS}Layer"):
        name = mtg._text(node, "Name")
        if not name:
            continue
        title = mtg._text(node, "Title") or name
        matches = subject_of(name, title)
        if not matches:
            continue
        newest, entry = mtg._entry(node, name, title)
        if newest is None or now - newest > within:
            continue
        for key, score in matches:
            # Best match first, then the freshest, then the shortest name --
            # usually the plain product rather than one of its variants.
            rank = (score, newest, -len(name))
            if key not in best or rank > best[key][0]:
                best[key] = (rank, {**entry, "newest_dt": newest})
    layers = []
    taken = set()
    for subject in SUBJECTS:
        if subject["key"] not in best:
            continue
        entry = dict(best[subject["key"]][1])
        if entry["id"] in taken:
            continue
        taken.add(entry["id"])
        newest = entry.pop("newest_dt")
        entry.update({
            "frames": days_for(newest),
            "step_minutes": copernicus.DAILY_STEP_MINUTES,
            "animates": False, "live": True, "family": "nasa", "subject": subject["key"],
            "age_minutes": round((now - newest).total_seconds() / 60),
            # A day as a plain date: that is how GIBS names its days.
            "time_default": newest.date().isoformat(),
        })
        layers.append(entry)
    copernicus.shortlist("nasa", layers)
    return {**FAMILY, "layers": layers, "stale": 0}


# The sorted family, for a few minutes: the catalogue is large, and the panel
# asks every five minutes.
SORTED_SECONDS = 10 * 60
_sorted: tuple[float, dict[str, Any]] | None = None


def family(refresh: bool = False) -> dict[str, Any]:
    """The NASA family for the live panel, or an empty one that says why."""
    global _sorted
    with _lock:
        held = _sorted
    if held and not refresh and time.time() - held[0] < SORTED_SECONDS:
        return held[1]
    try:
        found = sort_layers(capabilities(refresh=refresh))
    except GibsError as exc:
        return {**FAMILY, "layers": [], "stale": 0, "problem": str(exc)}
    with _lock:
        _sorted = (time.time(), found)
    return found


# ── Tiles ──────────────────────────────────────────────────────


def tile(asked: dict[str, str]) -> tuple[bytes, str, bool]:
    """One GIBS tile through this app: same-origin, so a video of it can be
    recorded, and held in the same cache as EUMETSAT's (under its own key)."""
    params = copernicus.tile_params(asked)
    if not params.get("layers"):
        raise GibsError("a tile request has to name a layer")
    key = "gibs|" + copernicus.tile_key(params)
    had = copernicus.cached_tile(key)
    if had is not None:
        return had[0], had[1], True
    try:
        resp = _session.get(WMS, params=params, timeout=copernicus.timeout_for(params))
    except requests.RequestException as exc:
        raise GibsError(f"NASA GIBS could not be reached: {reasons.why(exc)}") from exc
    if not resp.ok:
        raise GibsError(f"NASA GIBS answered {resp.status_code}")
    kind = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
    if not kind.startswith("image/"):
        raise GibsError(f"NASA GIBS sent {kind} rather than an image")
    copernicus.keep_tile(key, resp.content, kind)
    return resp.content, kind, False


def demo() -> dict[str, Any]:
    """Stand-in NASA layers for the demo build."""
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    newest = now - dt.timedelta(days=1)
    seeded = [
        ("OCO-2_Carbon_Dioxide_Total_Column_Average", "Carbon Dioxide (Total Column Average, OCO-2) (demo)", "co2"),
        ("MODIS_Terra_Land_Surface_Temp_Day", "Land Surface Temperature (Day, MODIS, Terra) (demo)", "lst_day"),
        ("VIIRS_NOAA20_Thermal_Anomalies_375m_All", "Fires and Thermal Anomalies (VIIRS, NOAA-20) (demo)", "fires"),
    ]
    layers = [{"id": ident, "title": title, "subject": subject, "family": "nasa",
               "newest": mtg._stamp(newest), "oldest": mtg._stamp(newest - dt.timedelta(days=60)),
               "time_default": newest.date().isoformat(), "frames": days_for(newest),
               "step_minutes": copernicus.DAILY_STEP_MINUTES, "animates": False,
               "live": True, "age_minutes": 24 * 60}
              for ident, title, subject in seeded]
    copernicus.shortlist("nasa", layers)
    return {**FAMILY, "layers": layers, "stale": 0}
