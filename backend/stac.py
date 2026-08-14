"""Finding Sentinel-2 scenes in the catalogue.

Earth Search is a free, anonymous STAC API in front of the Sentinel-2 archive
on AWS Open Data: no account, no key, no token. A search returns one flat
record per pass over the area, carrying the URLs of the band files it is made
of, so the rest of the app never has to know how the catalogue is shaped.
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


def search_scenes(
    geometry: dict,
    start: str | None = None,
    end: str | None = None,
    max_cloud: float = 30.0,
    limit: int = 60,
    demo: bool | None = None,
) -> dict[str, Any]:
    """Every Sentinel-2 pass over the area, newest first."""
    today = dt.date.today()
    start = _iso_day(start, today - dt.timedelta(days=365))
    end = _iso_day(end, today)

    if demo if demo is not None else config.DEMO_MODE:
        return {"scenes": _demo_scenes(geometry, start, end, max_cloud, limit),
                "demo": True}

    payload: dict[str, Any] = {
        "collections": [config.STAC_COLLECTION],
        "intersects": geometry,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": float(max_cloud)}},
        "limit": min(int(limit), 100),
    }
    try:
        resp = _session.post(f"{config.STAC_URL}/search", json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SceneSearchError(f"Could not reach the Sentinel-2 catalogue: {exc}") from exc

    scenes = [s for s in (scene_summary(item) for item in data.get("features", [])) if s]
    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    return {"scenes": scenes, "demo": False, "matched": data.get("numberMatched")}


def scene_summary(item: dict) -> dict[str, Any] | None:
    """Flatten a STAC item into what the app needs."""
    props = item.get("properties", {})
    assets = item.get("assets", {})

    hrefs = {name: href for name, asset in assets.items()
             if (href := _best_href(asset))}
    if not hrefs:
        return None

    when = props.get("datetime") or props.get("start_datetime") or ""
    cloud = props.get("eo:cloud_cover")

    return {
        "id": item.get("id"),
        "datetime": when,
        "date": when[:10],
        "cloud": round(float(cloud), 1) if cloud is not None else None,
        "platform": props.get("platform") or config.SATELLITE["platform"],
        "tile": _tile_label(props),
        "epsg": props.get("proj:epsg"),
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


def get_scene(scene_id: str) -> dict[str, Any]:
    """Re-fetch one scene, so the client never has to hold asset URLs."""
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
    """Sentinel-2's baseline 04.00 DN offset, where it applies.

    From January 2022 the ground segment shifted the stored numbers by -1000 so
    that a few genuinely dark pixels would stop clipping at zero. Scenes older
    than that, and scenes the catalogue has already corrected, need nothing.
    """
    if scene.get("boa_offset_applied"):
        return 0
    return config.BOA_OFFSET if scene.get("date", "") >= config.BOA_OFFSET_DATE else 0


# ── Synthetic scenes (DEMO_MODE) ───────────────────────────────


def _demo_scenes(geometry, start, end, max_cloud, limit) -> list[dict]:
    """A believable list of passes, every five days, for exploring offline."""
    west, south, _, _ = geometry_bounds(geometry)
    seed = int(hashlib.sha1(f"{west:.3f},{south:.3f}".encode()).hexdigest()[:8], 16)
    start_d = dt.date.fromisoformat(max(start, config.SATELLITE["since"]))
    end_d = dt.date.fromisoformat(end)

    scenes: list[dict] = []
    day = end_d
    i = 0
    while day >= start_d and len(scenes) < min(int(limit), 60):
        cloud = round(abs(math.sin(seed * 0.017 + i * 1.7)) * 55, 1)
        if cloud <= max_cloud:
            scenes.append(_demo_scene(day, cloud, seed))
        day -= dt.timedelta(days=5)
        i += 1
    return scenes


def _demo_scene(day: dt.date, cloud: float, seed: int) -> dict:
    return {
        "id": f"demo-{day.isoformat()}-{seed % 9973}-{int(round(cloud * 10))}",
        "datetime": f"{day.isoformat()}T10:30:00Z",
        "date": day.isoformat(),
        "cloud": cloud,
        "platform": f"{config.SATELLITE['platform']} (synthetic)",
        "tile": "DEMO",
        "epsg": 3857,
        "boa_offset_applied": True,
        "sun_elevation": 45.0,
        "thumbnail": None,
        "assets": {},
        "demo": True,
    }


def _demo_scene_from_id(scene_id: str) -> dict:
    parts = scene_id.split("-")
    if len(parts) < 6:
        raise SceneSearchError(f"Malformed demo scene id: {scene_id}")
    day = dt.date.fromisoformat("-".join(parts[1:4]))
    return _demo_scene(day, int(parts[5]) / 10.0, int(parts[4]))
