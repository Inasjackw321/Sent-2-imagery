"""Render orchestration: area + dates + options -> image bytes and metadata."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

import numpy as np

from . import composite, config, enhance, raster, stac, superres
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


# ── What a render needs ────────────────────────────────────────


def _needed_bands(mode: str, preset: str, index: str) -> list[str]:
    if mode == "index":
        if index not in config.INDICES:
            raise RenderError(f"Unknown index {index!r}")
        return list(config.INDICES[index]["bands"])
    if preset not in config.COMPOSITES:
        raise RenderError(f"Unknown composite {preset!r}")
    return list(dict.fromkeys(config.COMPOSITES[preset]["bands"]))


def prepare(req: dict):
    geometry = normalise_aoi(req.get("aoi") or req.get("geometry"))
    grid = Grid(geometry_bounds(geometry), int(req.get("size") or config.DEFAULT_SIZE))
    return geometry, grid


# ── Band loading, with enhancement applied in physical units ───


def load_bands(scene: dict, geometry: dict, grid: Grid, names: list[str], req: dict):
    key = _cache_key("bands", scene["id"], grid.bounds3857, grid.shape,
                     sorted(names), bool(req.get("mask_clouds")), bool(req.get("clip")),
                     geometry if req.get("clip") else None)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    bands, cloud_fraction = raster.read_bands(
        scene, grid, names, mask_clouds=bool(req.get("mask_clouds")))
    if req.get("clip"):
        bands = raster.apply_clip(bands, raster.aoi_mask(geometry, grid))

    _cache.put(key, (bands, cloud_fraction))
    return bands, cloud_fraction


def auto_scale(dates: int) -> int:
    """How much finer a merge of this many dates can honestly be sampled."""
    for needed, scale in config.SUPERRES_STEPS:
        if dates >= needed:
            return scale
    return 1


def superres_scale(req: dict, grid: Grid, scenes: list[dict]) -> int:
    """How much finer the fusion will actually go for this request.

    Merging is one thing rather than two: several dates always mean both a
    cleaner picture and a sharper one, so the multiplier follows from how many
    dates there are unless the caller insists on a number. It is then clamped
    to what the output grid can hold, since asking for more pixels than the
    image is allowed to have would only cost time.
    """
    if len(scenes) < 2:
        return 1
    asked = req.get("superres", "auto")
    # `is True` rather than `== True`: 1 equals True in Python, and a caller
    # asking for 1x means "leave it alone", not "choose for me".
    automatic = asked is None or asked is True or asked == "auto"
    scale = auto_scale(len(scenes)) if automatic else int(asked)
    scale = min(max(scale, 1), config.MAX_SUPERRES)
    while scale > 1 and max(grid.width, grid.height) * scale > config.MAX_SIZE:
        scale -= 1
    return scale


def _gather(scenes: list[dict], geometry, grid, names, req):
    """Read every scene the render needs, merging them if there is more than one.

    Two ways of merging, and they answer different questions. The composite
    asks *what is under the cloud* and answers it by taking the middle of the
    stack. Super-resolution asks *what is smaller than a pixel* and answers it
    by reading every date onto a finer grid, where each one lands its samples
    at a slightly different place, and solving for the detail they jointly saw.
    """
    scale = superres_scale(req, grid, scenes)
    fine = grid.refined(scale)

    stacks = []
    clouds = []
    for scene in scenes:
        bands, cloud = load_bands(scene, geometry, fine, names, req)
        stacks.append(bands)
        clouds.append(cloud)

    cloud_fraction = min(clouds) if clouds else 0.0
    report = enhance.composite_report(stacks, names[0]) if len(stacks) > 1 else None

    if scale > 1:
        merged, sr_report = superres.fuse(
            stacks, scale=scale,
            restore=float(req.get("superres_restore", 0.75)),
            register=req.get("superres_register", True) is not False,
            dates=[s.get("date") for s in scenes],
        )
        return merged, cloud_fraction, report, sr_report, fine

    # One date, or a grid already at the size limit: there is nothing to fuse,
    # so the middle of the stack is the best answer available.
    merged = enhance.composite(stacks, "median") if len(stacks) > 1 else stacks[0]
    return merged, cloud_fraction, report, None, fine


def _enhance_bands(bands: dict, req: dict, applied: list[str]) -> dict:
    """Corrections that belong in reflectance, before any stretch."""
    if req.get("haze_removal"):
        bands = enhance.dark_object_subtraction(
            bands, percentile=float(req.get("haze_percentile", 1.0)),
            strength=float(req.get("haze_removal")))
        applied.append("haze removal")

    if float(req.get("denoise") or 0) > 0:
        bands = enhance.denoise(bands, float(req["denoise"]))
        applied.append("denoise")

    return bands


def _enhance_rgb(rgb: np.ndarray, req: dict, applied: list[str]) -> np.ndarray:
    """Corrections on the normalised 0-1 image, after the stretch."""
    image = rgb.astype("float32") / 255.0

    clip_limit = float(req.get("adaptive_contrast") or 0)
    if clip_limit > 0:
        image = enhance.apply_clahe_rgb(image, clip_limit=clip_limit,
                                        tiles=int(req.get("adaptive_tiles", 8)),
                                        strength=float(req.get("adaptive_strength", 1.0)))
        applied.append("adaptive contrast")

    if req.get("white_balance"):
        image = enhance.white_balance(image, float(req.get("white_balance")))
        applied.append("white balance")

    sharpen = float(req.get("sharpen") or 0)
    if sharpen > 0:
        image = enhance.unsharp(image, amount=sharpen,
                                radius=float(req.get("sharpen_radius", 1.2)))
        applied.append("detail")

    vib = float(req.get("vibrance") or 0)
    if vib:
        image = enhance.vibrance(image, vib)
        applied.append("vibrance")

    return (np.clip(image, 0, 1) * 255).round().astype("uint8")


# ── Main render ────────────────────────────────────────────────


def render(req: dict) -> dict:
    geometry, grid = prepare(req)

    scenes = req.get("scenes") or [req.get("scene") or stac.get_scene(req["scene_id"])]
    if not scenes:
        raise RenderError("No date selected")

    mode = req.get("mode", "composite")
    preset = req.get("preset") or config.SATELLITE["default_composite"]
    index_name = req.get("index", "ndvi")
    names = _needed_bands(mode, preset, index_name)

    applied: list[str] = []
    bands, cloud_fraction, composite_report, sr_report, grid = _gather(
        scenes, geometry, grid, names, req)
    if sr_report:
        applied.append(f"{sr_report['scale']}× merge of {sr_report['scenes']} dates")
    elif len(scenes) > 1:
        applied.append(f"median merge of {len(scenes)} dates")

    bands = _enhance_bands(bands, req, applied)

    legend = None
    stats = None
    hist = None
    index_arr = None
    stretch_bounds = None
    pixel_area = grid.ground_res_m ** 2

    if mode == "index":
        index_arr = composite.compute_index(bands, index_name)
        rgb, valid, legend = composite.render_index(index_arr, index_name, req)
        stats = composite.array_stats(index_arr)
        hist = composite.histogram(index_arr, span=config.INDICES[index_name]["range"])
    else:
        rgb, valid, stretch_bounds = composite.render_composite(bands, preset, req)
        rgb = _enhance_rgb(rgb, req, applied)

    rgba = composite.to_rgba(rgb, valid)

    fmt = req.get("format", "png")
    if fmt == "geotiff":
        payload, media = composite.encode_geotiff(rgba, grid), "image/tiff"
    elif fmt == "float_geotiff" and index_arr is not None:
        payload, media = composite.encode_float_geotiff(index_arr, grid), "image/tiff"
    elif fmt == "jpeg":
        payload, media = composite.encode_jpeg(rgba), "image/jpeg"
    else:
        payload, media = composite.encode_png(rgba), "image/png"

    label = (config.INDICES[index_name]["label"] if mode == "index"
             else config.COMPOSITES[preset]["label"])

    meta = {
        "scene": {k: v for k, v in scenes[0].items() if k != "assets"},
        "scenes": [{"id": s["id"], "date": s["date"], "cloud": s.get("cloud")} for s in scenes],
        "source": satellite_meta(),
        "grid": grid.as_dict(),
        "mode": mode,
        "preset": preset if mode == "composite" else None,
        "index": index_name if mode == "index" else None,
        "label": label,
        "bands": names,
        "band_labels": [config.BANDS[b]["label"] for b in names],
        "stretch": stretch_bounds,
        "legend": legend,
        "stats": stats,
        "histogram": hist,
        "enhancements": applied,
        "composite_report": composite_report,
        "superres": sr_report,
        # Pixel size is not resolution. A small area asked for at 2048 px has
        # tiny pixels and still cannot resolve anything Sentinel-2 did not: the
        # honest figure is the satellite's 10 m, divided by what the merge won.
        "native_res_m": config.SATELLITE["resolution"],
        "effective_res_m": round(
            config.SATELLITE["resolution"] / (sr_report["scale"] if sr_report else 1), 2),
        "cloud_masked_pct": round(cloud_fraction * 100, 2) if req.get("mask_clouds") else 0.0,
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "scene_area_km2": round(grid.width * grid.height * pixel_area / 1e6, 4),
        "demo": bool(scenes[0].get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def satellite_meta() -> dict:
    """Who took the picture — carried on every render for the credit line."""
    return {k: config.SATELLITE[k] for k in
            ("label", "platform", "resolution", "attribution", "provider")}
