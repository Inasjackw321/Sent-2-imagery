"""Scene discovery against the Earth Search STAC API (plus a synthetic fallback)."""

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


def _iso_day(value: str | None, fallback: dt.date) -> str:
    if not value:
        return fallback.isoformat()
    return str(value)[:10]


def search_scenes(
    geometry: dict,
    start: str | None = None,
    end: str | None = None,
    max_cloud: float = 30.0,
    limit: int = 60,
    demo: bool | None = None,
) -> dict[str, Any]:
    """Find Sentinel-2 L2A scenes intersecting the AOI."""
    today = dt.date.today()
    start = _iso_day(start, today - dt.timedelta(days=365))
    end = _iso_day(end, today)

    if demo if demo is not None else config.DEMO_MODE:
        return {"scenes": _demo_scenes(geometry, start, end, max_cloud, limit), "demo": True}

    payload = {
        "collections": [config.STAC_COLLECTION],
        "intersects": geometry,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": min(int(limit), 100),
        "query": {"eo:cloud_cover": {"lt": float(max_cloud)}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }

    try:
        resp = _session.post(f"{config.STAC_URL}/search", json=payload, timeout=45)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SceneSearchError(
            f"Could not reach the Sentinel-2 catalogue at {config.STAC_URL}: {exc}"
        ) from exc

    scenes = [scene_summary(item) for item in data.get("features", [])]
    scenes = [s for s in scenes if s]
    return {"scenes": scenes, "demo": False, "matched": data.get("numberMatched")}


def scene_summary(item: dict) -> dict[str, Any] | None:
    """Flatten a STAC item into what the UI needs, keeping the asset hrefs."""
    props = item.get("properties", {})
    assets = item.get("assets", {})
    hrefs = {
        key: assets[key]["href"]
        for key in list(config.BANDS) + [config.SCL_ASSET, "visual", "thumbnail"]
        if key in assets and "href" in assets[key]
    }
    if not hrefs:
        return None

    when = props.get("datetime") or props.get("start_datetime") or ""
    return {
        "id": item.get("id"),
        "datetime": when,
        "date": when[:10],
        "cloud": round(float(props.get("eo:cloud_cover") or 0.0), 1),
        "nodata": round(float(props.get("s2:nodata_pixel_percentage") or 0.0), 1),
        "platform": props.get("platform") or props.get("constellation") or "sentinel-2",
        "tile": "".join(
            str(props.get(k, ""))
            for k in ("mgrs:utm_zone", "mgrs:latitude_band", "mgrs:grid_square")
        )
        or props.get("grid:code", ""),
        "epsg": props.get("proj:epsg"),
        "boa_offset_applied": bool(props.get("earthsearch:boa_offset_applied", False)),
        "thumbnail": assets.get("thumbnail", {}).get("href"),
        "assets": hrefs,
        "demo": False,
    }


def get_scene(scene_id: str) -> dict[str, Any]:
    """Re-fetch a single scene by id so the client never has to hold asset hrefs."""
    if scene_id.startswith("demo-"):
        return _demo_scene_from_id(scene_id)

    url = f"{config.STAC_URL}/collections/{config.STAC_COLLECTION}/items/{scene_id}"
    try:
        resp = _session.get(url, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SceneSearchError(f"Scene {scene_id} could not be fetched: {exc}") from exc
    summary = scene_summary(resp.json())
    if not summary:
        raise SceneSearchError(f"Scene {scene_id} has no readable assets")
    return summary


def boa_offset(scene: dict) -> int:
    """DN offset to apply before scaling to reflectance."""
    if scene.get("boa_offset_applied"):
        return 0
    return config.BOA_OFFSET if scene.get("date", "") >= config.BOA_OFFSET_DATE else 0


# ---------------------------------------------------------------------------
# Synthetic scenes (DEMO_MODE) -- never presented as real data.
# ---------------------------------------------------------------------------


def _demo_scenes(geometry, start, end, max_cloud, limit) -> list[dict]:
    west, south, east, north = geometry_bounds(geometry)
    seed = int(hashlib.sha1(f"{west:.3f},{south:.3f}".encode()).hexdigest()[:8], 16)
    start_d = dt.date.fromisoformat(start)
    end_d = dt.date.fromisoformat(end)

    scenes = []
    day = end_d
    i = 0
    while day >= start_d and len(scenes) < min(int(limit), 60):
        cloud = round(abs(math.sin(seed * 0.017 + i * 1.7)) * 55, 1)
        if cloud <= max_cloud:
            scenes.append(_demo_scene(day, cloud, seed, i))
        day -= dt.timedelta(days=5)
        i += 1
    return scenes


def _demo_scene(day: dt.date, cloud: float, seed: int, index: int) -> dict:
    # Cloud cover rides along in the id so a scene can be rebuilt from the id alone.
    return {
        "id": f"demo-{day.isoformat()}-{seed % 9973}-{int(round(cloud * 10))}",
        "datetime": f"{day.isoformat()}T10:30:00Z",
        "date": day.isoformat(),
        "cloud": cloud,
        "nodata": 0.0,
        "platform": "synthetic",
        "tile": "DEMO",
        "epsg": 3857,
        "boa_offset_applied": True,
        "thumbnail": None,
        "assets": {},
        "demo": True,
    }


def _demo_scene_from_id(scene_id: str) -> dict:
    parts = scene_id.split("-")
    if len(parts) < 6:
        raise SceneSearchError(f"Malformed demo scene id: {scene_id}")
    day = dt.date.fromisoformat("-".join(parts[1:4]))
    seed, cloud_tenths = int(parts[4]), int(parts[5])
    return _demo_scene(day, cloud_tenths / 10.0, seed, 0)
