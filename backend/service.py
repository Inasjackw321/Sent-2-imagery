"""Render orchestration: area + satellite + options -> image bytes and metadata."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

import numpy as np

from . import composite, config, enhance, raster, sources, stac
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
    if mode == "categorical":
        return ["classification"]
    if preset not in config.COMPOSITES:
        raise RenderError(f"Unknown composite {preset!r}")
    wanted = []
    for alias in config.COMPOSITES[preset]["bands"]:
        wanted.extend(config.DERIVED_BANDS.get(alias, (alias,)))
    return list(dict.fromkeys(wanted))


def _check_support(source, aliases: list[str], what: str) -> None:
    missing = [a for a in aliases if a not in source.bands]
    if missing:
        raise RenderError(
            f"{source.label} does not carry {', '.join(missing)} — {what} needs it"
        )


def _add_derived(bands: dict, preset: str) -> dict:
    """Fill in channels computed from other bands, such as the radar ratio."""
    for alias in config.COMPOSITES.get(preset, {}).get("bands", []):
        if alias in bands or alias not in config.DERIVED_BANDS:
            continue
        left, right = config.DERIVED_BANDS[alias]
        if left in bands and right in bands:
            bands[alias] = np.ma.masked_array(
                (bands[left].data - bands[right].data).astype("float32"),
                mask=np.ma.getmaskarray(bands[left]) | np.ma.getmaskarray(bands[right]),
            )
    return bands


def prepare(req: dict):
    geometry = normalise_aoi(req.get("aoi") or req.get("geometry"))
    grid = Grid(geometry_bounds(geometry), int(req.get("size") or config.DEFAULT_SIZE))
    return geometry, grid


# ── Band loading, with enhancement applied in physical units ───


def load_bands(scene: dict, geometry: dict, grid: Grid, keys: list[str], req: dict,
               source=None):
    source = source or sources.get(scene.get("source"))
    key = _cache_key("bands", scene["id"], source.key, grid.bounds3857, grid.shape,
                     sorted(keys), bool(req.get("mask_clouds")), bool(req.get("clip")),
                     geometry if req.get("clip") else None)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    bands, cloud_fraction = raster.read_bands(
        scene, grid, keys, source=source, mask_clouds=bool(req.get("mask_clouds"))
    )
    if req.get("clip"):
        bands = raster.apply_clip(bands, raster.aoi_mask(geometry, grid))

    _cache.put(key, (bands, cloud_fraction))
    return bands, cloud_fraction


def _gather(scenes: list[dict], geometry, grid, keys, req, source):
    """Read every scene the render needs, compositing them if there is more than one."""
    stacks = []
    clouds = []
    for scene in scenes:
        bands, cloud = load_bands(scene, geometry, grid, keys, req, source)
        stacks.append(bands)
        clouds.append(cloud)

    method = req.get("composite_method", "median")
    merged = enhance.composite(stacks, method) if len(stacks) > 1 else stacks[0]
    report = enhance.composite_report(stacks, keys[0]) if len(stacks) > 1 else None
    return merged, (min(clouds) if clouds else 0.0), report


def _enhance_bands(bands: dict, req: dict, source, grid: Grid, scenes: list[dict],
                   geometry, applied: list[str]) -> dict:
    """Corrections that belong in physical units, before any stretch."""
    if req.get("haze_removal"):
        bands = enhance.dark_object_subtraction(
            bands, percentile=float(req.get("haze_percentile", 1.0)),
            strength=float(req.get("haze_removal")))
        applied.append("haze removal")

    denoise_strength = float(req.get("denoise") or 0)
    if denoise_strength > 0:
        bands = enhance.denoise(bands, denoise_strength)
        applied.append("denoise")

    if req.get("pansharpen") and source.pan:
        pan_bands, _ = load_bands(scenes[0], geometry, grid, ["pan"], req, source)
        colour_keys = [k for k in bands if k in
                       ("red", "green", "blue", "nir", "nir08", "swir16", "swir22")]
        bands = enhance.pansharpen(
            bands, pan_bands.get("pan"), colour_keys,
            strength=float(req.get("pansharpen")),
            method=req.get("pansharpen_method", "highpass"))
        applied.append("pan-sharpening")

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
    source = sources.get(req.get("source"))

    scenes = req.get("scenes") or [req.get("scene") or stac.get_scene(
        req["scene_id"], source.key)]
    if not scenes:
        raise RenderError("No scene selected")

    mode = req.get("mode", "composite")
    preset = req.get("preset") or source.default_composite
    index_name = req.get("index", "ndvi")

    if mode == "categorical" or source.kind == "landcover":
        mode = "categorical"
    keys = _needed_bands(mode, preset, index_name)
    _check_support(source, keys, "this visualisation")

    applied: list[str] = []
    bands, cloud_fraction, composite_report = _gather(scenes, geometry, grid, keys, req, source)
    if len(scenes) > 1:
        applied.append(f"{len(scenes)}-scene {req.get('composite_method', 'median')} composite")

    bands = _enhance_bands(bands, req, source, grid, scenes, geometry, applied)
    bands = _add_derived(dict(bands), preset)

    legend = None
    stats = None
    hist = None
    index_arr = None
    classes = None
    stretch_bounds = None
    pixel_area = grid.ground_res_m ** 2

    if mode == "categorical":
        rgb, valid, legend = composite.render_categorical(
            bands["classification"], sources.WORLDCOVER_CLASSES, pixel_area)
        classes = legend["classes"]
    elif mode == "index":
        index_arr = composite.compute_index(bands, index_name)
        rgb, valid, legend = composite.render_index(index_arr, index_name, req)
        stats = composite.array_stats(index_arr)
        hist = composite.histogram(index_arr, span=config.INDICES[index_name]["range"])
        legend["unit"] = config.INDICES[index_name].get("unit", "")
    else:
        rgb, valid, stretch_bounds = composite.render_composite(bands, preset, req)
        rgb = _enhance_rgb(rgb, req, applied)

    if req.get("hillshade") and "elevation" in bands:
        shade = enhance.hillshade(bands["elevation"], grid.ground_res_m,
                                 azimuth=float(req.get("sun_azimuth", 315)),
                                 altitude=float(req.get("sun_altitude", 45)),
                                 exaggeration=float(req.get("exaggeration", 1.0)))
        blend = float(req.get("hillshade") if req.get("hillshade") is not True else 0.6)
        rgb = np.clip(rgb * (1 - blend + blend * shade[..., None] * 1.6), 0, 255).astype("uint8")
        applied.append("hillshade")

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
             else "Land cover" if mode == "categorical"
             else config.COMPOSITES[preset]["label"])

    meta = {
        "scene": {k: v for k, v in scenes[0].items() if k != "assets"},
        "scenes": [{"id": s["id"], "date": s["date"], "cloud": s.get("cloud")} for s in scenes],
        "source": _source_meta(source),
        "grid": grid.as_dict(),
        "mode": mode,
        "preset": preset if mode == "composite" else None,
        "index": index_name if mode == "index" else None,
        "label": label,
        "bands": keys,
        "band_labels": [config.BANDS.get(k, {}).get("label", k.upper()) for k in keys],
        "stretch": stretch_bounds,
        "legend": legend,
        "stats": stats,
        "histogram": hist,
        "classes": classes,
        "enhancements": applied,
        "composite_report": composite_report,
        "cloud_masked_pct": round(cloud_fraction * 100, 2) if req.get("mask_clouds") else 0.0,
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "scene_area_km2": round(grid.width * grid.height * pixel_area / 1e6, 4),
        "demo": bool(scenes[0].get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def _source_meta(source) -> dict:
    return {
        "key": source.key,
        "label": source.label,
        "platform": source.platform,
        "kind": source.kind,
        "resolution": source.resolution,
        "attribution": source.attribution,
        "provider": sources.PROVIDERS[source.provider]["label"],
    }


# ── Change detection ───────────────────────────────────────────


def change_detection(req: dict) -> dict:
    geometry, grid = prepare(req)
    source = sources.get(req.get("source"))
    scene_a = req.get("scene_a") or stac.get_scene(req["scene_a_id"], source.key)
    scene_b = req.get("scene_b") or stac.get_scene(req["scene_b_id"], source.key)
    if scene_a["date"] > scene_b["date"]:
        scene_a, scene_b = scene_b, scene_a

    index_name = req.get("index", "ndvi")
    keys = _needed_bands("index", "", index_name)
    _check_support(source, keys, "this comparison")

    applied: list[str] = []
    bands_a, cloud_a = load_bands(scene_a, geometry, grid, keys, req, source)
    bands_b, cloud_b = load_bands(scene_b, geometry, grid, keys, req, source)
    bands_a = _enhance_bands(bands_a, req, source, grid, [scene_a], geometry, applied)
    bands_b = _enhance_bands(bands_b, req, source, grid, [scene_b], geometry, [])

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
        diff, [-threshold, threshold],
        [f"Loss (< -{threshold:g})", "Stable", f"Gain (> +{threshold:g})"],
        pixel_area,
    )

    spec = config.INDICES[index_name]
    colormap = req.get("colormap") or "change"
    meta = {
        "scene_a": {k: v for k, v in scene_a.items() if k != "assets"},
        "scene_b": {k: v for k, v in scene_b.items() if k != "assets"},
        "source": _source_meta(source),
        "grid": grid.as_dict(),
        "index": index_name,
        "label": f"Change in {spec['label']}",
        "mode": "change",
        "stats": composite.array_stats(diff),
        "histogram": composite.histogram(diff, span=(-limit, limit)),
        "classes": classes,
        "threshold": threshold,
        "enhancements": applied,
        "cloud_masked_pct": (round(max(cloud_a, cloud_b) * 100, 2)
                             if req.get("mask_clouds") else 0.0),
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "days_apart": _days_between(scene_a["date"], scene_b["date"]),
        "legend": {
            "type": "continuous",
            "colormap": colormap,
            "vmin": -limit,
            "vmax": limit,
            "label": f"Δ {spec['label'].split(' - ')[0]}",
            "stops": [
                {"pos": p, "color": composite._hex(composite.colormap_lut(colormap)[int(p * 255)])}
                for p in (0.0, 0.25, 0.5, 0.75, 1.0)
            ],
        },
        "demo": bool(scene_a.get("demo") or scene_b.get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def _days_between(a: str, b: str) -> int:
    import datetime as dt

    return abs((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days)
