"""Render orchestration: AOI + scene + options -> image bytes and metadata."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

import numpy as np

from . import composite, config, raster, stac
from .geo import Grid, geodesic_area_km2, geometry_bounds, normalise_aoi


class RenderError(RuntimeError):
    pass


class _LRU:
    def __init__(self, capacity: int = 24):
        self.capacity = capacity
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        return None

    def put(self, key, value):
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)


_cache = _LRU()


def _cache_key(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _needed_bands(mode: str, preset: str, index: str) -> list[str]:
    if mode == "index":
        if index not in config.INDICES:
            raise RenderError(f"Unknown index {index!r}")
        return list(config.INDICES[index]["bands"])
    if preset not in config.COMPOSITES:
        raise RenderError(f"Unknown composite {preset!r}")
    return list(config.COMPOSITES[preset]["bands"])


def prepare(req: dict):
    """Resolve the AOI, grid and scene shared by every render entry point."""
    geometry = normalise_aoi(req.get("aoi") or req.get("geometry"))
    grid = Grid(geometry_bounds(geometry), int(req.get("size") or config.DEFAULT_SIZE))
    return geometry, grid


def load_bands(scene: dict, geometry: dict, grid: Grid, keys: list[str], req: dict):
    key = _cache_key("bands", scene["id"], grid.bounds3857, grid.shape, sorted(keys),
                     bool(req.get("mask_clouds")), bool(req.get("clip")),
                     geometry if req.get("clip") else None)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    bands, cloud_fraction = raster.read_bands(
        scene, grid, keys, mask_clouds=bool(req.get("mask_clouds"))
    )
    if req.get("clip"):
        bands = raster.apply_clip(bands, raster.aoi_mask(geometry, grid))

    _cache.put(key, (bands, cloud_fraction))
    return bands, cloud_fraction


def render(req: dict) -> dict:
    """Render one scene over the AOI. Returns image bytes plus metadata."""
    geometry, grid = prepare(req)
    scene = req.get("scene") or stac.get_scene(req["scene_id"])

    mode = req.get("mode", "composite")
    preset = req.get("preset", "true_color")
    index_name = req.get("index", "ndvi")
    keys = _needed_bands(mode, preset, index_name)

    bands, cloud_fraction = load_bands(scene, geometry, grid, keys, req)

    legend = None
    stats = None
    hist = None
    index_arr = None

    if mode == "index":
        index_arr = composite.compute_index(bands, index_name)
        rgb, valid, legend = composite.render_index(index_arr, index_name, req)
        stats = composite.array_stats(index_arr)
        hist = composite.histogram(index_arr, span=config.INDICES[index_name]["range"])
        stretch_bounds = None
    else:
        rgb, valid, stretch_bounds = composite.render_composite(bands, preset, req)

    rgba = composite.to_rgba(rgb, valid)

    fmt = req.get("format", "png")
    if fmt == "geotiff":
        payload = composite.encode_geotiff(rgba, grid)
        media = "image/tiff"
    elif fmt == "float_geotiff" and index_arr is not None:
        payload = composite.encode_float_geotiff(index_arr, grid)
        media = "image/tiff"
    elif fmt == "jpeg":
        payload = composite.encode_jpeg(rgba)
        media = "image/jpeg"
    else:
        payload = composite.encode_png(rgba)
        media = "image/png"

    pixel_area = grid.ground_res_m ** 2
    meta = {
        "scene": {k: v for k, v in scene.items() if k != "assets"},
        "grid": grid.as_dict(),
        "mode": mode,
        "preset": preset if mode == "composite" else None,
        "index": index_name if mode == "index" else None,
        "label": (
            config.INDICES[index_name]["label"] if mode == "index"
            else config.COMPOSITES[preset]["label"]
        ),
        "bands": keys,
        "band_labels": [config.BANDS[k]["label"] for k in keys],
        "stretch": stretch_bounds,
        "legend": legend,
        "stats": stats,
        "histogram": hist,
        # Only meaningful when masking was actually requested.
        "cloud_masked_pct": round(cloud_fraction * 100, 2) if req.get("mask_clouds") else 0.0,
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "scene_area_km2": round(grid.width * grid.height * pixel_area / 1e6, 4),
        "demo": bool(scene.get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def change_detection(req: dict) -> dict:
    """Difference an index between two dates and quantify gain / loss."""
    geometry, grid = prepare(req)
    scene_a = req.get("scene_a") or stac.get_scene(req["scene_a_id"])
    scene_b = req.get("scene_b") or stac.get_scene(req["scene_b_id"])
    if scene_a["date"] > scene_b["date"]:
        scene_a, scene_b = scene_b, scene_a

    index_name = req.get("index", "ndvi")
    keys = _needed_bands("index", "", index_name)

    bands_a, cloud_a = load_bands(scene_a, geometry, grid, keys, req)
    bands_b, cloud_b = load_bands(scene_b, geometry, grid, keys, req)

    idx_a = composite.compute_index(bands_a, index_name)
    idx_b = composite.compute_index(bands_b, index_name)
    diff = np.ma.masked_array(
        idx_b.data - idx_a.data,
        mask=np.ma.getmaskarray(idx_a) | np.ma.getmaskarray(idx_b),
    )

    limit = float(req.get("diff_limit") or 0.4)
    threshold = float(req.get("threshold") or 0.1)
    norm = np.clip((np.ma.filled(diff, 0.0) + limit) / (2 * limit), 0, 1)
    rgb = composite.apply_colormap(norm, req.get("colormap") or "change")
    valid = ~np.ma.getmaskarray(diff)

    if req.get("highlight_only"):
        valid = valid & (np.abs(np.ma.filled(diff, 0.0)) >= threshold)

    rgba = composite.to_rgba(rgb, valid)

    fmt = req.get("format", "png")
    if fmt == "geotiff":
        payload, media = composite.encode_geotiff(rgba, grid), "image/tiff"
    elif fmt == "float_geotiff":
        payload, media = composite.encode_float_geotiff(diff, grid), "image/tiff"
    elif fmt == "jpeg":
        payload, media = composite.encode_jpeg(rgba), "image/jpeg"
    else:
        payload, media = composite.encode_png(rgba), "image/png"

    pixel_area = grid.ground_res_m ** 2
    classes = composite.class_areas(
        diff,
        [-threshold, threshold],
        [f"Loss (< -{threshold:g})", "Stable", f"Gain (> +{threshold:g})"],
        pixel_area,
    )

    spec = config.INDICES[index_name]
    meta = {
        "scene_a": {k: v for k, v in scene_a.items() if k != "assets"},
        "scene_b": {k: v for k, v in scene_b.items() if k != "assets"},
        "grid": grid.as_dict(),
        "index": index_name,
        "label": f"Change in {spec['label']}",
        "mode": "change",
        "stats": composite.array_stats(diff),
        "histogram": composite.histogram(diff, span=(-limit, limit)),
        "classes": classes,
        "threshold": threshold,
        "cloud_masked_pct": (round(max(cloud_a, cloud_b) * 100, 2)
                             if req.get("mask_clouds") else 0.0),
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "days_apart": _days_between(scene_a["date"], scene_b["date"]),
        "legend": {
            "type": "continuous",
            "colormap": req.get("colormap") or "change",
            "vmin": -limit,
            "vmax": limit,
            "label": f"Δ {spec['label'].split(' - ')[0]}",
            "stops": [
                {"pos": p, "color": composite._hex(
                    composite.colormap_lut(req.get("colormap") or "change")[int(p * 255)])}
                for p in (0.0, 0.25, 0.5, 0.75, 1.0)
            ],
        },
        "demo": bool(scene_a.get("demo") or scene_b.get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def _days_between(a: str, b: str) -> int:
    import datetime as dt

    return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
