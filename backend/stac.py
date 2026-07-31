"""Scene discovery across every catalogue the app can read."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import threading
import time
from typing import Any

import requests

from . import config, sources
from .geo import geometry_bounds

_session = requests.Session()
_session.headers.update({"User-Agent": config.USER_AGENT})


class SceneSearchError(RuntimeError):
    pass


# ── Asset signing (Planetary Computer) ─────────────────────────

_token_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}


def _sas_token(source: sources.Source) -> str | None:
    """Fetch (and cache) an anonymous read token for a Planetary Computer collection."""
    provider = sources.PROVIDERS[source.provider]
    endpoint = provider.get("signing")
    if not endpoint:
        return None

    with _token_lock:
        cached = _tokens.get(source.collection)
        if cached and cached[1] > time.time() + 60:
            return cached[0]

    try:
        resp = _session.get(f"{endpoint}/{source.collection}", timeout=30)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SceneSearchError(
            f"Could not get a read token for {source.label}: {exc}"
        ) from exc

    token = payload.get("token")
    expiry = payload.get("msft:expiry")
    seconds = time.time() + 3000
    if expiry:
        try:
            seconds = dt.datetime.fromisoformat(
                expiry.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    with _token_lock:
        _tokens[source.collection] = (token, seconds)
    return token


def sign_href(source: sources.Source, href: str) -> str:
    """Add credentials to an asset URL if its provider needs them."""
    if not sources.PROVIDERS[source.provider].get("signing"):
        return href
    if not href.startswith(("http://", "https://")):
        return href                            # local file or vsi path
    if "?" in href and "st=" in href:          # already signed
        return href
    token = _sas_token(source)
    if not token:
        return href
    joiner = "&" if "?" in href else "?"
    return f"{href}{joiner}{token}"


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
    source_key: str | None = None,
    start: str | None = None,
    end: str | None = None,
    max_cloud: float = 30.0,
    limit: int = 60,
    demo: bool | None = None,
) -> dict[str, Any]:
    """Find scenes from one source intersecting the area."""
    source = sources.get(source_key)
    today = dt.date.today()
    start = _iso_day(start, today - dt.timedelta(days=365))
    end = _iso_day(end, today)

    if demo if demo is not None else config.DEMO_MODE:
        scenes = _demo_scenes(geometry, source, start, end, max_cloud, limit)
        return {"scenes": scenes, "demo": True, "source": source.key}

    payload: dict[str, Any] = {
        "collections": [source.collection],
        "intersects": geometry,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": min(int(limit), 100),
    }
    if source.cloud_property:
        payload["query"] = {source.cloud_property: {"lt": float(max_cloud)}}
    if source.key == "landsat-thermal":
        payload.setdefault("query", {})["platform"] = {
            "in": ["landsat-8", "landsat-9"]}

    url = f"{sources.PROVIDERS[source.provider]['url']}/search"
    try:
        resp = _session.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise SceneSearchError(
            f"Could not reach {sources.PROVIDERS[source.provider]['label']}: {exc}"
        ) from exc

    scenes = [s for s in (scene_summary(item, source) for item in data.get("features", [])) if s]
    scenes.sort(key=lambda s: s["datetime"], reverse=True)
    return {
        "scenes": scenes,
        "demo": False,
        "source": source.key,
        "matched": data.get("numberMatched"),
    }


def scene_summary(item: dict, source: sources.Source) -> dict[str, Any] | None:
    """Flatten a STAC item into what the app needs."""
    props = item.get("properties", {})
    assets = item.get("assets", {})

    hrefs: dict[str, str] = {}
    for asset_name, asset in assets.items():
        href = _best_href(asset)
        if href:
            hrefs[asset_name] = href
    if not hrefs:
        return None

    when = props.get("datetime") or props.get("start_datetime") or ""
    cloud = props.get(source.cloud_property) if source.cloud_property else None

    return {
        "id": item.get("id"),
        "source": source.key,
        "collection": item.get("collection", source.collection),
        "datetime": when,
        "date": when[:10],
        "cloud": round(float(cloud), 1) if cloud is not None else None,
        "platform": props.get("platform") or source.platform,
        "instrument": (props.get("instruments") or [None])[0],
        "tile": _tile_label(props),
        "epsg": props.get("proj:epsg"),
        "boa_offset_applied": bool(props.get("earthsearch:boa_offset_applied", False)),
        "sun_elevation": props.get("view:sun_elevation"),
        "off_nadir": props.get("view:off_nadir"),
        "thumbnail": (assets.get("thumbnail") or assets.get("rendered_preview") or {}).get("href"),
        "assets": hrefs,
        "demo": False,
    }


def _tile_label(props: dict) -> str:
    mgrs = "".join(str(props.get(k, "")) for k in
                   ("mgrs:utm_zone", "mgrs:latitude_band", "mgrs:grid_square"))
    if mgrs:
        return mgrs
    wrs = props.get("landsat:wrs_path"), props.get("landsat:wrs_row")
    if all(wrs):
        return f"{wrs[0]}/{wrs[1]}"
    return str(props.get("grid:code", "") or "")


def get_scene(scene_id: str, source_key: str | None = None) -> dict[str, Any]:
    """Re-fetch one scene, so the client never has to hold asset URLs."""
    source = sources.get(source_key)
    if scene_id.startswith("demo-"):
        return _demo_scene_from_id(scene_id, source)

    base = sources.PROVIDERS[source.provider]["url"]
    url = f"{base}/collections/{source.collection}/items/{scene_id}"
    try:
        resp = _session.get(url, timeout=45)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise SceneSearchError(f"Scene {scene_id} could not be fetched: {exc}") from exc

    summary = scene_summary(resp.json(), source)
    if not summary:
        raise SceneSearchError(f"Scene {scene_id} has no readable assets")
    return summary


def boa_offset(scene: dict, source: sources.Source) -> int:
    """Sentinel-2's baseline 04.00 DN offset, where it applies."""
    if sources.SCALES[source.scale]["kind"] != "sentinel2":
        return 0
    if scene.get("boa_offset_applied"):
        return 0
    return config.BOA_OFFSET if scene.get("date", "") >= config.BOA_OFFSET_DATE else 0


# ── Synthetic scenes (DEMO_MODE) ───────────────────────────────


def _demo_scenes(geometry, source, start, end, max_cloud, limit) -> list[dict]:
    west, south, _, _ = geometry_bounds(geometry)
    seed = int(hashlib.sha1(f"{west:.3f},{south:.3f}".encode()).hexdigest()[:8], 16)
    start_d = dt.date.fromisoformat(max(start, source.since))
    end_d = dt.date.fromisoformat(end)
    cadence = _demo_cadence(source)

    scenes: list[dict] = []
    day = end_d
    i = 0
    while day >= start_d and len(scenes) < min(int(limit), 60):
        cloud = round(abs(math.sin(seed * 0.017 + i * 1.7)) * 55, 1)
        if source.cloud_property is None or cloud <= max_cloud:
            scenes.append(_demo_scene(day, cloud, seed, source))
        day -= dt.timedelta(days=cadence)
        i += 1
    return scenes


def _demo_cadence(source: sources.Source) -> int:
    return {"optical": 5, "thermal": 8, "sar": 12, "dem": 3650, "landcover": 365}.get(
        source.kind, 5)


def _demo_scene(day: dt.date, cloud: float, seed: int, source: sources.Source) -> dict:
    return {
        "id": f"demo-{day.isoformat()}-{seed % 9973}-{int(round(cloud * 10))}-{source.key}",
        "source": source.key,
        "collection": source.collection,
        "datetime": f"{day.isoformat()}T10:30:00Z",
        "date": day.isoformat(),
        "cloud": cloud if source.cloud_property else None,
        "platform": f"{source.platform} (synthetic)",
        "instrument": None,
        "tile": "DEMO",
        "epsg": 3857,
        "boa_offset_applied": True,
        "sun_elevation": 45.0,
        "off_nadir": 0.0,
        "thumbnail": None,
        "assets": {},
        "demo": True,
    }


def _demo_scene_from_id(scene_id: str, source: sources.Source) -> dict:
    parts = scene_id.split("-")
    if len(parts) < 6:
        raise SceneSearchError(f"Malformed demo scene id: {scene_id}")
    day = dt.date.fromisoformat("-".join(parts[1:4]))
    seed, cloud_tenths = int(parts[4]), int(parts[5])
    if len(parts) > 6:
        source = sources.get("-".join(parts[6:]))
    return _demo_scene(day, cloud_tenths / 10.0, seed, source)
