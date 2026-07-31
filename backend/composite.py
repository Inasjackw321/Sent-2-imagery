"""Turning reflectance arrays into RGBA images: stretches, indices, colormaps, stats."""

from __future__ import annotations

import io

import numpy as np
import rasterio
from PIL import Image

from . import config
from .geo import Grid


# ---------------------------------------------------------------------------
# Colormaps
# ---------------------------------------------------------------------------


def colormap_lut(name: str, steps: int = 256) -> np.ndarray:
    """(steps, 3) uint8 lookup table built from the control points in config."""
    points = config.COLORMAPS.get(name) or config.COLORMAPS["viridis"]
    pos = np.array([p[0] for p in points], dtype="float32")
    rgb = np.array([p[1:] for p in points], dtype="float32")
    t = np.linspace(0, 1, steps, dtype="float32")
    lut = np.stack([np.interp(t, pos, rgb[:, c]) for c in range(3)], axis=1)
    return np.clip(lut, 0, 255).astype("uint8")


def apply_colormap(norm: np.ndarray, name: str) -> np.ndarray:
    lut = colormap_lut(name)
    idx = np.clip((np.nan_to_num(norm) * 255.0).round(), 0, 255).astype("uint8")
    return lut[idx]


# ---------------------------------------------------------------------------
# Stretching
# ---------------------------------------------------------------------------


def stretch_band(
    band: np.ma.MaskedArray,
    mode: str = "percentile",
    low: float = 2.0,
    high: float = 98.0,
    vmin: float | None = None,
    vmax: float | None = None,
) -> tuple[np.ndarray, float, float]:
    valid = band.compressed()
    if valid.size == 0:
        return np.zeros(band.shape, dtype="float32"), 0.0, 1.0

    if mode == "fixed" and vmin is not None and vmax is not None:
        lo, hi = float(vmin), float(vmax)
    elif mode == "minmax":
        lo, hi = float(valid.min()), float(valid.max())
    else:
        lo, hi = np.percentile(valid, [float(low), float(high)])
        lo, hi = float(lo), float(hi)

    if hi - lo < 1e-6:
        hi = lo + 1e-6
    out = np.clip((np.ma.filled(band, lo) - lo) / (hi - lo), 0, 1).astype("float32")
    return out, lo, hi


def apply_gamma(x: np.ndarray, gamma: float) -> np.ndarray:
    if abs(gamma - 1.0) < 1e-3:
        return x
    return np.power(np.clip(x, 0, 1), 1.0 / max(gamma, 0.05)).astype("float32")


# ---------------------------------------------------------------------------
# Composites and indices
# ---------------------------------------------------------------------------


def render_composite(bands: dict, preset: str, opts: dict) -> tuple[np.ndarray, np.ndarray, dict]:
    """RGB uint8 + valid mask + per-channel stretch bounds.

    Stretch modes:
      fixed             - a reflectance window (natural colour, comparable between dates)
      percentile_linked - percentiles pooled over all three bands (contrast, hue kept)
      percentile        - per band (most contrast, can shift colour balance)
      minmax            - per band full range
    """
    spec = config.COMPOSITES[preset]
    defaults = spec.get("default_stretch", {})
    mode = opts.get("stretch") or defaults.get("mode", "percentile_linked")
    low = float(opts.get("stretch_low", defaults.get("low", 2)))
    high = float(opts.get("stretch_high", defaults.get("high", 98)))
    gamma = float(opts.get("gamma") or defaults.get("gamma", 1.0))
    keys = spec["bands"]

    if mode == "percentile_linked":
        pool = np.concatenate([b.compressed() for b in (bands[k] for k in keys)])
        if pool.size:
            lo, hi = (float(v) for v in np.percentile(pool, [low, high]))
        else:
            lo, hi = 0.0, 1.0
        per_band = [(lo, hi)] * 3
    elif mode == "fixed":
        lo = float(opts.get("vmin", defaults.get("vmin", 0.0)))
        hi = float(opts.get("vmax", defaults.get("vmax", 0.30)))
        per_band = [(lo, hi)] * 3
    else:
        per_band = [None] * 3

    channels = []
    bounds = {}
    for key, fixed in zip(keys, per_band):
        if fixed is None:
            stretched, lo_b, hi_b = stretch_band(bands[key], mode, low, high)
        else:
            stretched, lo_b, hi_b = stretch_band(bands[key], "fixed", vmin=fixed[0], vmax=fixed[1])
        channels.append(apply_gamma(stretched, gamma))
        bounds[key] = [round(lo_b, 5), round(hi_b, 5)]

    rgb = (np.stack(channels, axis=-1) * 255).round().astype("uint8")
    valid = ~np.logical_or.reduce([np.ma.getmaskarray(bands[k]) for k in keys])
    return rgb, valid, {"mode": mode, "gamma": gamma, "bands": bounds}


def compute_index(bands: dict, name: str) -> np.ma.MaskedArray:
    b = {k: v.astype("float32") for k, v in bands.items()}
    needed = config.INDICES[name]["bands"]

    # Single-band "indices" (temperature, elevation, backscatter) are the band.
    if len(needed) == 1:
        band = b[needed[0]]
        return np.ma.masked_array(np.ma.filled(band, 0.0),
                                  mask=np.ma.getmaskarray(band))

    with np.errstate(divide="ignore", invalid="ignore"):
        if name == "evi":
            num = b["nir"] - b["red"]
            den = b["nir"] + 6.0 * b["red"] - 7.5 * b["blue"] + 1.0
            out = 2.5 * num / den
        elif name == "savi":
            out = 1.5 * (b["nir"] - b["red"]) / (b["nir"] + b["red"] + 0.5)
        else:
            first, second = config.INDICES[name]["bands"][:2]
            out = (b[first] - b[second]) / (b[first] + b[second])
    out = np.ma.masked_invalid(out)
    return np.ma.masked_array(np.ma.filled(out, 0.0), mask=np.ma.getmaskarray(out))


def render_index(index: np.ma.MaskedArray, name: str, opts: dict):
    spec = config.INDICES[name]
    vmin = opts.get("index_min")
    vmax = opts.get("index_max")
    if vmin is None or vmax is None:
        vmin, vmax = spec["range"]
    vmin, vmax = float(vmin), float(vmax)
    norm = np.clip((np.ma.filled(index, vmin) - vmin) / max(vmax - vmin, 1e-6), 0, 1)
    cmap = opts.get("colormap") or spec["colormap"]
    rgb = apply_colormap(norm, cmap)
    valid = ~np.ma.getmaskarray(index)
    legend = {
        "type": "continuous",
        "colormap": cmap,
        "vmin": vmin,
        "vmax": vmax,
        "label": spec["label"],
        "stops": [
            {"pos": p, "color": _hex(colormap_lut(cmap)[int(p * 255)])}
            for p in (0.0, 0.25, 0.5, 0.75, 1.0)
        ],
    }
    return rgb, valid, legend


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def render_categorical(band: np.ma.MaskedArray, table: dict, pixel_area_m2: float = 0.0):
    """Paint a classified map (land cover) with its own legend and class areas."""
    codes = np.ma.filled(band, 0).astype("int32")
    valid = ~np.ma.getmaskarray(band)
    rgb = np.zeros((*codes.shape, 3), dtype="uint8")

    entries = []
    total = max(int(valid.sum()), 1)
    for code, (label, colour) in table.items():
        selected = (codes == code) & valid
        count = int(selected.sum())
        rgb[selected] = _rgb_from_hex(colour)
        if count:
            entries.append({
                "label": label,
                "color": colour,
                "percent": round(100.0 * count / total, 2),
                "area_km2": round(count * pixel_area_m2 / 1e6, 4),
            })

    known = np.isin(codes, list(table)) | ~valid
    legend = {
        "type": "categorical",
        "label": "Land cover",
        "classes": sorted(entries, key=lambda e: -e["percent"]),
    }
    return rgb, valid & known, legend


def _rgb_from_hex(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def array_stats(arr: np.ma.MaskedArray) -> dict:
    valid = arr.compressed()
    if valid.size == 0:
        return {"count": 0}
    q = np.percentile(valid, [5, 25, 50, 75, 95])
    return {
        "count": int(valid.size),
        "min": round(float(valid.min()), 4),
        "max": round(float(valid.max()), 4),
        "mean": round(float(valid.mean()), 4),
        "std": round(float(valid.std()), 4),
        "p5": round(float(q[0]), 4),
        "p25": round(float(q[1]), 4),
        "median": round(float(q[2]), 4),
        "p75": round(float(q[3]), 4),
        "p95": round(float(q[4]), 4),
    }


def histogram(arr: np.ma.MaskedArray, bins: int = 48, span=None) -> dict:
    valid = arr.compressed()
    if valid.size == 0:
        return {"bins": [], "counts": []}
    lo, hi = (float(valid.min()), float(valid.max())) if span is None else span
    counts, edges = np.histogram(valid, bins=bins, range=(lo, hi))
    return {
        "bins": [round(float(e), 4) for e in edges[:-1]],
        "counts": [int(c) for c in counts],
        "width": round(float(edges[1] - edges[0]), 5),
    }


def class_areas(index: np.ma.MaskedArray, thresholds: list[float], labels: list[str],
                pixel_area_m2: float) -> list[dict]:
    """Bucket an index into classes and report the ground area of each."""
    valid = ~np.ma.getmaskarray(index)
    data = np.ma.filled(index, np.nan)
    edges = [-np.inf, *thresholds, np.inf]
    out = []
    total = max(int(valid.sum()), 1)
    for i, label in enumerate(labels):
        sel = valid & (data >= edges[i]) & (data < edges[i + 1])
        n = int(sel.sum())
        out.append({
            "label": label,
            "pixels": n,
            "percent": round(100.0 * n / total, 2),
            "area_km2": round(n * pixel_area_m2 / 1e6, 4),
        })
    return out


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def to_rgba(rgb: np.ndarray, valid: np.ndarray) -> np.ndarray:
    alpha = (valid.astype("uint8") * 255)[..., None]
    return np.concatenate([rgb, alpha], axis=-1)


def encode_png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def encode_jpeg(rgba: np.ndarray, quality: int = 92) -> bytes:
    img = Image.fromarray(rgba, mode="RGBA")
    flat = Image.new("RGB", img.size, (12, 14, 18))
    flat.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    flat.save(buf, format="JPEG", quality=quality, subsampling=0)
    return buf.getvalue()


def encode_geotiff(rgba: np.ndarray, grid: Grid) -> bytes:
    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=grid.height,
            width=grid.width,
            count=4,
            dtype="uint8",
            crs=grid.crs,
            transform=grid.transform,
            photometric="RGB",
            compress="deflate",
            tiled=True,
            blockxsize=256,
            blockysize=256,
        ) as dst:
            for i in range(4):
                dst.write(rgba[..., i], i + 1)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red,
                rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
                rasterio.enums.ColorInterp.alpha,
            ]
        return mem.read()


def encode_float_geotiff(arr: np.ma.MaskedArray, grid: Grid) -> bytes:
    data = np.ma.filled(arr.astype("float32"), np.nan)
    with rasterio.MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            height=grid.height,
            width=grid.width,
            count=1,
            dtype="float32",
            crs=grid.crs,
            transform=grid.transform,
            nodata=float("nan"),
            compress="deflate",
            tiled=True,
        ) as dst:
            dst.write(data, 1)
        return mem.read()
