"""Fetch the latest Sentinel-2 imagery from the free Earth Search STAC API (AWS).

No API key required. Scenes are Cloud-Optimized GeoTIFFs, so we only download
the window covering the requested area, reprojected to EPSG:4326 so it drops
straight onto a web map.
"""

import math

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


def _stretch(img: np.ndarray) -> np.ndarray:
    """Percentile contrast stretch on raw reflectance so every scene —
    bright desert or dark ocean — uses the full display range. A single
    stretch across all bands preserves the colour balance; nodata (black)
    pixels are excluded from the stats."""
    valid = img[img.any(axis=-1)].astype(np.float32)
    if valid.size == 0:
        return np.zeros(img.shape, np.uint8)
    lo, hi = np.percentile(valid, (2.0, 98.0))
    if hi - lo < 1:
        hi = lo + 1
    out = np.clip((img.astype(np.float32) - lo) / (hi - lo), 0, 1)
    # mild gamma lift so shadows and water keep visible detail
    out = out ** 0.85
    return (out * 255).astype(np.uint8)


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
