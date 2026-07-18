"""Pixel-level change detection between two co-registered RGB images.

The two images come from `sentinel.render_pair`, so they share a grid and a
common radiometric scaling. We difference them, suppress the global
illumination/seasonal shift, threshold, and group the surviving pixels into
change regions. This finds *where* things changed; the vision model
(detector.describe_change) then says *what* changed.
"""

import numpy as np
from scipy import ndimage
from PIL import Image

# Category -> highlight colour (kept in sync with the frontend legend).
CATEGORY_COLORS = {
    "damage": "#ff3d71",
    "movement": "#00e5ff",
    "construction": "#ffd400",
    "other": "#b388ff",
    "none": "#8b98a9",
}


def change_regions(before: np.ndarray, after: np.ndarray, mask: np.ndarray,
                   min_area: int = 10, max_area_frac: float = 0.25) -> tuple[list[dict], np.ndarray]:
    """Return (regions, diff_map). Each region:
    {"bbox": [x1,y1,x2,y2], "area", "intensity", "score"} in pixel coords."""
    diff = np.abs(after.astype(np.int32) - before.astype(np.int32)).sum(axis=2)
    diff = np.where(mask, diff, 0).astype(np.float32)

    valid = diff[mask]
    if valid.size == 0:
        return [], diff
    # Remove the whole-scene brightness shift so only local changes remain.
    med = float(np.median(valid))
    d = np.clip(diff - med, 0, None)
    d[~mask] = 0

    thr = max(40.0, float(np.percentile(d[mask], 98)))
    binary = d > thr
    binary = ndimage.binary_opening(binary, iterations=1)

    labels, n = ndimage.label(binary)
    H, W = diff.shape
    regions: list[dict] = []
    for i in range(1, n + 1):
        ys, xs = np.where(labels == i)
        area = int(xs.size)
        if area < min_area or area > max_area_frac * H * W:
            continue
        intensity = float(d[ys, xs].mean())
        regions.append({
            "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
            "area": area,
            "intensity": intensity,
            "score": intensity * area,
        })
    regions.sort(key=lambda r: -r["score"])
    return regions, d


def heat_overlay(after: np.ndarray, diff: np.ndarray, regions: list[dict]) -> Image.Image:
    """The 'after' image tinted red where it changed, with region outlines."""
    img = after.astype(np.float32).copy()
    if diff.max() > 0:
        heat = np.clip(diff / (np.percentile(diff[diff > 0], 95) + 1e-6), 0, 1)
    else:
        heat = np.zeros_like(diff)
    # blend toward red proportional to change intensity
    img[..., 0] = img[..., 0] * (1 - 0.6 * heat) + 255 * 0.6 * heat
    img[..., 1] = img[..., 1] * (1 - 0.6 * heat)
    img[..., 2] = img[..., 2] * (1 - 0.6 * heat)
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    return out


def crop_pair(before: np.ndarray, after: np.ndarray, bbox: list[int],
              pad_frac: float = 0.6, min_pad: int = 8):
    """Crop both images around a region bbox (with padding) for the model."""
    x1, y1, x2, y2 = bbox
    H, W = before.shape[:2]
    pad = max(min_pad, int(max(x2 - x1, y2 - y1) * pad_frac))
    cx1, cy1 = max(0, x1 - pad), max(0, y1 - pad)
    cx2, cy2 = min(W, x2 + pad), min(H, y2 + pad)
    b = Image.fromarray(before[cy1:cy2, cx1:cx2], "RGB")
    a = Image.fromarray(after[cy1:cy2, cx1:cx2], "RGB")
    return b, a


def region_bounds_latlon(bbox_px: list[int], area_bbox: list[float], W: int, H: int) -> list[list[float]]:
    """Pixel region bbox -> Leaflet [[south,west],[north,east]] within the area."""
    west, south, east, north = area_bbox
    x1, y1, x2, y2 = bbox_px
    return [
        [north - (y2 / H) * (north - south), west + (x1 / W) * (east - west)],
        [north - (y1 / H) * (north - south), west + (x2 / W) * (east - west)],
    ]
