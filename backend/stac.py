"""Finding Sentinel scenes in the catalogue.

Earth Search is a free, anonymous STAC API in front of the Sentinel archives on
AWS Open Data: no account, no key, no token. A search returns one flat record
per pass over the area, carrying the URLs of the band files it is made of, so
the rest of the app never has to know how the catalogue is shaped.

Sentinel-1 and Sentinel-2 live in separate collections and are searched
separately -- a cloud-cover filter would throw away every radar scene, because
radar has no such property -- then interleaved into one list of passes over the
area, each one saying which satellite took it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

import requests

from . import config
from .geo import geometry_bounds

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


class SceneSearchError(RuntimeError):
    pass


def _best_href(asset: dict) -> str | None:
    """Prefer an https URL; fall back to whatever the item offers."""
    alternates = asset.get("alternate") or {}
    for key in ("https", "http"):
        if key in alternates and alternates[key].get("href"):
            return alternates[key]["href"]
    href = asset.get("href")
    if href and href.startswith("s3://"):
        # Public buckets serve the same object over https.
        bucket, _, path = href[5:].partition("/")
        return f"https://{bucket}.s3.amazonaws.com/{path}"
    return href


# ── Search ─────────────────────────────────────────────────────


def _iso_day(value: str | None, fallback: dt.date) -> str:
    return str(value)[:10] if value else fallback.isoformat()


def normalise_satellites(value) -> list[str]:
    """Whatever the caller asked for, as a list of known satellite keys."""
    if value in (None, "", "all", "both"):
        return list(config.SATELLITES)
    keys = [value] if isinstance(value, str) else list(value)
    chosen = [k for k in keys if k in config.SATELLITES]
    if not chosen:
        raise SceneSearchError(f"Unknown satellite: {keys}")
    return chosen


def search_scenes(
    geometry: dict,
    start: str | None = None,
    end: str | None = None,
    max_cloud: float = 30.0,
    limit: int = 60,
    demo: bool | None = None,
    satellites=None,
) -> dict[str, Any]:
    """Every pass over the area from the satellites asked for, newest first."""
    today = dt.date.today()
    start = _iso_day(start, today - dt.timedelta(days=365))
    end = _iso_day(end, today)
    wanted = normalise_satellites(satellites)

    if demo if demo is not None else config.DEMO_MODE:
        return {"scenes": _demo_scenes(geometry, start, end, max_cloud, limit, wanted),
                "demo": True, "satellites": wanted}

    scenes: list[dict] = []
    matched = 0
    for key in wanted:
        found, count = _search_one(config.SATELLITES[key], geometry, start, end,
                                   max_cloud, limit)
        scenes.extend(found)
        matched += count or 0

    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    return {"scenes": scenes, "demo": False, "matched": matched, "satellites": wanted}


def _search_one(sat: dict, geometry: dict, start: str, end: str,
                max_cloud: float, limit: int) -> tuple[list[dict], int]:
    payload: dict[str, Any] = {
        "collections": [sat["collection"]],
        "intersects": geometry,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": min(int(limit), 100),
    }
    if sat["cloud_filter"]:
        payload["query"] = {"eo:cloud_cover": {"lt": float(max_cloud)}}
    try:
        resp = _session.post(f"{config.STAC_URL}/search", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SceneSearchError(
            f"Could not reach the {sat['short']} catalogue: {exc}") from exc

    found = [s for s in (scene_summary(item, sat["key"])
                         for item in data.get("features", [])) if s]
    return found, data.get("numberMatched") or 0


def scene_summary(item: dict, satellite: str | None = None) -> dict[str, Any] | None:
    """Flatten a STAC item into what the app needs."""
    props = item.get("properties", {})
    assets = item.get("assets", {})

    hrefs = {name: href for name, asset in assets.items()
             if (href := _best_href(asset))}
    if not hrefs:
        return None

    key = satellite or config.satellite_for_collection(item.get("collection"))
    sat = config.satellite(key)
    when = props.get("datetime") or props.get("start_datetime") or ""
    cloud = props.get("eo:cloud_cover")

    return {
        "id": item.get("id"),
        "satellite": key,
        "datetime": when,
        "date": when[:10],
        "cloud": round(float(cloud), 1) if cloud is not None else None,
        "platform": props.get("platform") or sat["platform"],
        "tile": _tile_label(props) or _radar_label(props),
        "epsg": props.get("proj:epsg"),
        "orbit": props.get("sat:relative_orbit"),
        "orbit_state": props.get("sat:orbit_state"),
        "boa_offset_applied": bool(props.get("earthsearch:boa_offset_applied", False)),
        "sun_elevation": props.get("view:sun_elevation"),
        "thumbnail": (assets.get("thumbnail") or {}).get("href"),
        "assets": hrefs,
        "demo": False,
    }


def _tile_label(props: dict) -> str:
    """The MGRS square the scene comes from, e.g. 30UXC."""
    return "".join(str(props.get(k, "")) for k in
                   ("mgrs:utm_zone", "mgrs:latitude_band", "mgrs:grid_square"))


def _radar_label(props: dict) -> str:
    """A radar pass has no tile: which way it was flying is what identifies it."""
    state = str(props.get("sat:orbit_state") or "")[:3].upper()
    orbit = props.get("sat:relative_orbit")
    mode = props.get("sar:instrument_mode") or ""
    return " ".join(str(p) for p in (mode, state, orbit and f"#{orbit}") if p)


def get_scene(scene_id: str, satellite: str | None = None) -> dict[str, Any]:
    """Re-fetch one scene, so the client never has to hold asset URLs."""
    if scene_id.startswith("demo-"):
        return _demo_scene_from_id(scene_id)

    # Sentinel-1 ids start with S1, Sentinel-2's with S2, so a caller who did
    # not say which collection to look in usually need not.
    key = satellite or ("sentinel-1" if scene_id.upper().startswith("S1")
                        else config.DEFAULT_SATELLITE)
    sat = config.satellite(key)
    url = f"{config.STAC_URL}/collections/{sat['collection']}/items/{scene_id}"
    try:
        resp = _session.get(url, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SceneSearchError(f"Scene {scene_id} could not be fetched: {exc}") from exc

    summary = scene_summary(resp.json(), sat["key"])
    if not summary:
        raise SceneSearchError(f"Scene {scene_id} has no readable assets")
    return summary


def boa_offset(scene: dict) -> int:
    """Sentinel-2's baseline 04.00 DN offset, where it applies.

    From January 2022 the ground segment shifted the stored numbers by -1000 so
    that a few genuinely dark pixels would stop clipping at zero. Scenes older
    than that, and scenes the catalogue has already corrected, need nothing.
    """
    if scene.get("boa_offset_applied"):
        return 0
    return config.BOA_OFFSET if scene.get("date", "") >= config.BOA_OFFSET_DATE else 0


# ── Synthetic scenes (DEMO_MODE) ───────────────────────────────


_DEMO_CADENCE = {"sentinel-2": 5, "sentinel-1": 6}
_DEMO_HOUR = {"sentinel-2": "10:30:00", "sentinel-1": "17:52:00"}


def _demo_scenes(geometry, start, end, max_cloud, limit, satellites=None) -> list[dict]:
    """A believable list of passes for exploring offline.

    Both satellites fly over, on their own cadences and at their own times of
    day, so the offline app behaves like the real one right down to the radar
    pass landing on a different date from the optical one.
    """
    west, south, _, _ = geometry_bounds(geometry)
    seed = int(hashlib.sha1(f"{west:.3f},{south:.3f}".encode()).hexdigest()[:8], 16)
    end_d = dt.date.fromisoformat(end)
    per_satellite = max(1, min(int(limit), 60) // max(len(satellites or [1]), 1))

    scenes: list[dict] = []
    for key in satellites or [config.DEFAULT_SATELLITE]:
        sat = config.satellite(key)
        start_d = dt.date.fromisoformat(max(start, sat["since"]))
        day = end_d
        i = 0
        taken = 0
        while day >= start_d and taken < per_satellite:
            cloud = round(abs(math.sin(seed * 0.017 + i * 1.7)) * 55, 1)
            # Radar does not care about cloud, so it is never filtered by it.
            if sat["cloud_filter"] and cloud > max_cloud:
                pass
            else:
                scenes.append(_demo_scene(day, cloud, seed, key))
                taken += 1
            day -= dt.timedelta(days=_DEMO_CADENCE[key])
            i += 1
    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    return scenes


def _demo_scene(day: dt.date, cloud: float, seed: int, satellite: str | None = None) -> dict:
    key = satellite or config.DEFAULT_SATELLITE
    sat = config.satellite(key)
    radar = sat["kind"] == "radar"
    suffix = "-s1" if radar else ""
    return {
        "id": f"demo-{day.isoformat()}-{seed % 9973}-{int(round(cloud * 10))}{suffix}",
        "satellite": key,
        "datetime": f"{day.isoformat()}T{_DEMO_HOUR[key]}Z",
        "date": day.isoformat(),
        # Radar records no cloud cover, and pretending otherwise would make the
        # date list lie about what the pass can see through.
        "cloud": None if radar else cloud,
        "platform": f"{sat['platform']} (synthetic)",
        "tile": "IW ASC #59" if radar else "DEMO",
        "epsg": 3857,
        "orbit": 59 if radar else 108,
        "orbit_state": "ascending" if radar else None,
        "boa_offset_applied": True,
        "sun_elevation": None if radar else 45.0,
        "thumbnail": None,
        "assets": {},
        "demo": True,
    }


def _demo_scene_from_id(scene_id: str) -> dict:
    parts = scene_id.split("-")
    if len(parts) < 6:
        raise SceneSearchError(f"Malformed demo scene id: {scene_id}")
    day = dt.date.fromisoformat("-".join(parts[1:4]))
    satellite = "sentinel-1" if parts[-1] == "s1" else config.DEFAULT_SATELLITE
    return _demo_scene(day, int(parts[5]) / 10.0, int(parts[4]), satellite)
