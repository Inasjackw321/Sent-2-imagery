"""Aircraft counting + movement time series from a stack of co-registered scenes.

Sentinel-2 revisits every ~5 days, so a month gives several clear scenes of the
same place, all on the same pixel grid (see sentinel.read_stack). In each scene
we count aircraft as compact, aircraft-sized blobs that stand out against their
immediate surroundings (a parked jet contrasts with the smooth apron around it),
using a *local* background so a plane is detected whether or not it was there on
other dates. Counting them per date gives the movement time series.
"""

import numpy as np
from scipy import ndimage

# Aircraft at 10 m/pixel: a fighter ~2 px, an airliner/transport ~5-8 px.
# Count compact blobs in this area range; reject specks and large structures.
AREA_MIN = 2
AREA_MAX = 90
LOCAL = 15          # local-background window (~150 m) the object must stand out from
COMPACT_MIN = 0.35  # filled-fraction of the bbox, to reject roads / edges


def median_background(stack: list[np.ndarray], masks: list[np.ndarray]) -> np.ndarray:
    """Per-pixel median across all dates (ignoring nodata) — the static scene."""
    arr = np.stack(stack).astype(np.float32)          # N,H,W,3
    valid = np.stack(masks)                            # N,H,W
    arr[~valid] = np.nan
    with np.errstate(all="ignore"):
        bg = np.nanmedian(arr, axis=0)
    return np.nan_to_num(bg).astype(np.float32)


def detect_planes(scene: np.ndarray, mask: np.ndarray) -> tuple[list[dict], np.ndarray]:
    """Count aircraft-sized objects that contrast with their local surroundings.
    Returns (blobs, contrast) with each blob's centroid, area and pixel bbox."""
    gray = scene.astype(np.float32).mean(axis=2)
    local = ndimage.uniform_filter(gray, size=LOCAL)
    d = np.abs(gray - local)
    d = np.where(mask, d, 0.0)
    valid = d[mask]
    if valid.size == 0:
        return [], d

    thr = max(16.0, float(np.percentile(valid, 98)))
    binary = ndimage.binary_opening(d > thr, iterations=1) & mask
    labels, n = ndimage.label(binary)
    blobs: list[dict] = []
    for i in range(1, n + 1):
        ys, xs = np.where(labels == i)
        area = int(xs.size)
        if area < AREA_MIN or area > AREA_MAX:
            continue
        w = xs.max() - xs.min() + 1
        h = ys.max() - ys.min() + 1
        if area < COMPACT_MIN * w * h:       # too sparse -> road / field edge
            continue
        if max(w, h) > 6 * min(w, h):        # very elongated -> linear feature
            continue
        blobs.append({
            "x": float(xs.mean()), "y": float(ys.mean()), "area": area,
            "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
        })
    return blobs, d


def movement_series(stack: list[np.ndarray], masks: list[np.ndarray],
                    dates: list[str]) -> tuple[np.ndarray, list[dict], list[list[dict]]]:
    """Return (background, series, per_date_blobs).
    series item: {"date", "count"}. per_date_blobs[i] is the blob list for date i."""
    bg = median_background(stack, masks)
    series, per_date = [], []
    for scene, mask, date in zip(stack, masks, dates):
        blobs, _ = detect_planes(scene, mask)
        series.append({"date": date, "count": len(blobs)})
        per_date.append(blobs)
    return bg, series, per_date
