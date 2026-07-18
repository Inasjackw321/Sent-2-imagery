"""Fetch the latest Sentinel-2 imagery from the free Earth Search STAC API (AWS).

No API key required. Scenes are Cloud-Optimized GeoTIFFs, so we only download
the window covering the requested area, reprojected to EPSG:4326 so it drops
straight onto a web map.
"""

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds
from PIL import Image

STAC_URL = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
MAX_PIXELS = 2048  # longest edge of the downloaded image


def bbox_around(lat: float, lon: float, size_km: float) -> list[float]:
    """Square bounding box (WGS84) of size_km per side centred on a point."""
    half = size_km / 2.0
    dlat = half / 111.32
    dlon = half / (111.32 * max(0.01, math.cos(math.radians(lat))))
    return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]


def search_latest(bbox: list[float], max_cloud: float = 30.0, limit: int = 12) -> list[dict]:
    """Return recent scenes covering bbox, newest first."""
    body = {
        "collections": [COLLECTION],
        "bbox": bbox,
        "limit": limit,
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    r = requests.post(STAC_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("features", [])


def search_range(bbox: list[float], days: int = 30, max_cloud: float = 20.0,
                 limit: int = 40) -> list[dict]:
    """Return scenes covering bbox from the last `days` days, newest first."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    body = {
        "collections": [COLLECTION],
        "bbox": bbox,
        "limit": limit,
        "datetime": f"{start.isoformat()}/{end.isoformat()}",
        "query": {"eo:cloud_cover": {"lt": max_cloud}},
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    r = requests.post(STAC_URL, json=body, timeout=60)
    r.raise_for_status()
    return r.json().get("features", [])


def pick_before_after(items: list[dict]) -> tuple[dict, dict] | None:
    """From a range search (newest first), pick the newest and the oldest
    scene on distinct days, preferring the clearest scene per day."""
    by_day: dict[str, dict] = {}
    for it in items:
        day = str(it["properties"]["datetime"])[:10]
        cur = by_day.get(day)
        if cur is None or it["properties"].get("eo:cloud_cover", 100) < cur["properties"].get("eo:cloud_cover", 100):
            by_day[day] = it
    days = sorted(by_day)
    if len(days) < 2:
        return None
    return by_day[days[0]], by_day[days[-1]]   # (before, after)


def grid_size(bbox: list[float], max_px: int = 1400) -> tuple[int, int]:
    """Fixed output pixel grid for a bbox at ~10 m/pixel, capped to max_px.
    Depends only on the bbox, so two dates share an identical grid."""
    mid_lat = (bbox[1] + bbox[3]) / 2.0
    w_m = (bbox[2] - bbox[0]) * 111320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    h_m = (bbox[3] - bbox[1]) * 111320.0
    w, h = w_m / 10.0, h_m / 10.0
    scale = min(1.0, max_px / max(w, h, 1))
    return max(1, int(w * scale)), max(1, int(h * scale))


def read_raw(item: dict, bbox: list[float], out_wh: tuple[int, int]) -> np.ndarray:
    """Read the red/green/blue reflectance bands onto a fixed bbox pixel grid.

    Data is placed into a zero canvas by its geographic position, so scenes
    that only partly cover the bbox still align pixel-for-pixel across dates
    (uncovered pixels stay 0 / nodata)."""
    out_w, out_h = out_wh
    px_lon = (bbox[2] - bbox[0]) / out_w
    px_lat = (bbox[3] - bbox[1]) / out_h
    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint16)

    for b, asset in enumerate(("red", "green", "blue")):
        href = item["assets"][asset]["href"]
        with rasterio.open(href) as src:
            with WarpedVRT(src, crs="EPSG:4326", resampling=Resampling.bilinear) as vrt:
                cl = max(bbox[0], vrt.bounds.left)
                cb = max(bbox[1], vrt.bounds.bottom)
                cr = min(bbox[2], vrt.bounds.right)
                ct = min(bbox[3], vrt.bounds.top)
                if cr <= cl or ct <= cb:
                    continue  # no overlap; leave nodata
                col0 = int(round((cl - bbox[0]) / px_lon))
                col1 = int(round((cr - bbox[0]) / px_lon))
                row0 = int(round((bbox[3] - ct) / px_lat))
                row1 = int(round((bbox[3] - cb) / px_lat))
                ow, oh = max(1, col1 - col0), max(1, row1 - row0)
                window = from_bounds(cl, cb, cr, ct, transform=vrt.transform)
                data = vrt.read(1, window=window, out_shape=(oh, ow),
                                resampling=Resampling.bilinear)
                canvas[row0:row0 + oh, col0:col0 + ow, b] = data[:oh, :ow]
    return canvas


def render_pair(before_raw: np.ndarray, after_raw: np.ndarray):
    """Stretch two raw-reflectance arrays with a SHARED per-band scaling so
    they are radiometrically comparable (real changes are preserved) while
    still looking good. Returns (before_rgb, after_rgb, valid_mask)."""
    mask = before_raw.any(axis=-1) & after_raw.any(axis=-1)
    before8 = np.zeros(before_raw.shape, np.uint8)
    after8 = np.zeros(after_raw.shape, np.uint8)
    if not mask.any():
        return before8, after8, mask
    for b in range(3):
        pool = np.concatenate([before_raw[..., b][mask], after_raw[..., b][mask]]).astype(np.float32)
        lo, hi = np.percentile(pool, (2.0, 98.0))
        if hi - lo < 1:
            hi = lo + 1
        for src, dst in ((before_raw, before8), (after_raw, after8)):
            v = np.clip((src[..., b].astype(np.float32) - lo) / (hi - lo), 0, 1) ** 0.85
            dst[..., b] = (v * 255).astype(np.uint8)
    before8[~mask] = 0
    after8[~mask] = 0
    return before8, after8, mask


def bounds_latlon(bbox: list[float]) -> list[list[float]]:
    """bbox [w,s,e,n] -> Leaflet [[south, west], [north, east]]."""
    return [[bbox[1], bbox[0]], [bbox[3], bbox[2]]]


def _stretch(img: np.ndarray) -> np.ndarray:
    """Per-band percentile contrast stretch on raw reflectance. Stretching
    each band independently balances the colour (desert reads as natural tan
    instead of a red/orange cast) and uses the full display range whether the
    scene is bright desert or dark ocean. Nodata (black) pixels are excluded."""
    mask = img.any(axis=-1)
    if not mask.any():
        return np.zeros(img.shape, np.uint8)
    out = np.zeros(img.shape, np.uint8)
    for b in range(img.shape[-1]):
        band = img[..., b].astype(np.float32)
        lo, hi = np.percentile(band[mask], (2.0, 98.0))
        if hi - lo < 1:
            hi = lo + 1
        # mild gamma lift so shadows and water keep visible detail
        v = np.clip((band - lo) / (hi - lo), 0, 1) ** 0.85
        out[..., b] = (v * 255).astype(np.uint8)
    return out


def download_visual(item: dict, bbox: list[float], out_path: str) -> dict:
    """Windowed read of the scene's raw 10 m red/green/blue reflectance
    bands, warped to EPSG:4326 and rendered with our own contrast stretch.

    The ready-made 'visual' (TCI) product clips bright surfaces like
    desert to pure white, destroying detail — the raw bands don't.

    Saves a PNG to out_path and returns metadata including the exact
    geographic bounds of the saved image.
    """
    bands = []
    out_w = out_h = 0
    bottom = left = top = right = 0.0
    for asset in ("red", "green", "blue"):
        href = item["assets"][asset]["href"]
        with rasterio.open(href) as src:
            with WarpedVRT(src, crs="EPSG:4326", resampling=Resampling.bilinear) as vrt:
                left = max(bbox[0], vrt.bounds.left)
                bottom = max(bbox[1], vrt.bounds.bottom)
                right = min(bbox[2], vrt.bounds.right)
                top = min(bbox[3], vrt.bounds.top)
                if right <= left or top <= bottom:
                    raise ValueError("Requested area does not overlap this scene")

                window = from_bounds(left, bottom, right, top, transform=vrt.transform)
                w, h = int(window.width), int(window.height)
                scale = min(1.0, MAX_PIXELS / max(w, h))
                out_w, out_h = max(1, int(w * scale)), max(1, int(h * scale))

                bands.append(vrt.read(
                    indexes=1,
                    window=window,
                    out_shape=(out_h, out_w),
                    resampling=Resampling.bilinear,
                ))

    img = _stretch(np.stack(bands, axis=-1))
    Image.fromarray(img, "RGB").save(out_path)

    props = item["properties"]
    return {
        "scene_id": item["id"],
        "datetime": props.get("datetime"),
        "cloud_cover": props.get("eo:cloud_cover"),
        # Leaflet wants [[south, west], [north, east]]
        "bounds": [[bottom, left], [top, right]],
        "width": out_w,
        "height": out_h,
    }
