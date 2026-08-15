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
    ceiling: float = 1.0,
) -> tuple[np.ndarray, float, float]:
    """Normalise a band to 0-1.

    `ceiling` above 1 keeps what is brighter than the window instead of
    flattening it, so a highlight roll-off downstream still has something left
    to work with.
    """
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
    out = np.clip((np.ma.filled(band, lo) - lo) / (hi - lo), 0, ceiling).astype("float32")
    return out, lo, hi


def apply_gamma(x: np.ndarray, gamma: float) -> np.ndarray:
    if abs(gamma - 1.0) < 1e-3:
        return x
    return np.power(np.clip(x, 0, 1), 1.0 / max(gamma, 0.05)).astype("float32")


def soft_highlights(x: np.ndarray, knee: float = 0.72) -> np.ndarray:
    """Roll the highlights off instead of clipping them flat.

    A hard clip turns every bright surface -- sand, concrete, rooftops -- into
    the same pure white, and takes the colour with it: once all three channels
    are pinned at 255 the pixel has no hue left. Compressing the top of the
    range instead keeps both the texture and the tint, the way film does with
    a shoulder rather than a wall.
    """
    span = max(1.0 - knee, 1e-6)
    rolled = knee + span * (1.0 - np.exp(-(np.asarray(x, dtype="float32") - knee) / span))
    return np.clip(np.where(x <= knee, x, rolled), 0, 1).astype("float32")


# ---------------------------------------------------------------------------
# Composites and indices
# ---------------------------------------------------------------------------


def from_decibels(band: np.ma.MaskedArray) -> np.ma.MaskedArray:
    """Decibels back to linear power, for display.

    Radar is measured, merged and read in decibels, because that is the scale
    on which backscatter is meaningful and on which speckle averages out. But
    it is the wrong scale to *look* at. A logarithm spreads every surface
    evenly across the range, so a stretch in decibels gives water, soil,
    vegetation and concrete roughly equal shares of the histogram and the
    picture comes out looking like a poster. In linear power the scene is
    naturally dark with a long bright tail, which is why real radar imagery
    reads as black water and a few brilliant targets rather than a wash of
    colour.
    """
    return np.ma.masked_array(
        np.power(10.0, np.ma.filled(band, -40.0).astype("float32") / 10.0),
        mask=np.ma.getmaskarray(band))


LUMA = (0.2126, 0.7152, 0.0722)


def tone_map(rgb: np.ndarray, gamma: float = 1.0, knee: float = 0.72) -> np.ndarray:
    """Bring a stretched image into range without bending its colour.

    Gamma and a highlight roll-off applied to each channel on its own are what
    make satellite imagery drift in colour as it brightens. The three channels
    are curved separately, so they climb at different rates and the ratios
    between them -- which is what colour *is* -- come out somewhere else. A red
    roof lit brightly turns orange; a bright green field turns yellow. Push
    hard enough and everything converges on white, because each channel
    saturates in turn.

    Doing it once, to the pixel, avoids all of that. The curve is applied to
    the brightest of the three channels, and all three are then scaled by the
    single factor that one changed by. Scaling together leaves their ratios
    exactly as they were, and the ratios are the colour: both the hue and the
    saturation come out mathematically untouched, with only the brightness
    moved.

    The brightest channel rather than the luminance, because the brightest is
    the one that would have hit white first. Curving that keeps the result
    inside the gamut by construction -- nothing ever needs clipping, and no
    colour has to be given up to fit.
    """
    rgb = np.asarray(rgb, dtype="float32")
    peak = rgb.max(axis=-1)
    safe = np.maximum(peak, 1e-6)

    curved = soft_highlights(peak, knee) if knee < 1.0 else np.clip(peak, 0, 1)
    curved = apply_gamma(curved, gamma)

    out = rgb * (curved / safe)[..., None]
    return np.clip(out, 0, 1).astype("float32")


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

    if spec.get("from_db"):
        bands = {k: from_decibels(bands[k]) for k in keys}

    if mode == "percentile_linked":
        pool = np.concatenate([b.compressed() for b in (bands[k] for k in keys)])
        if pool.size:
            lo, hi = (float(v) for v in np.percentile(pool, [low, high]))
        else:
            lo, hi = 0.0, 1.0
        per_band = [(lo, hi)] * 3
    elif mode == "fixed":
        # A composite can name a window per channel. Radar needs that: its
        # three channels sit in genuinely different ranges -- VH returns some
        # 6 to 10 dB weaker than VV, and the ratio is a difference rather than
        # a level -- so one window across all three would be meaningless.
        windows = spec.get("windows")
        asked = opts.get("vmin") is not None and opts.get("vmax") is not None
        if windows and not asked:
            per_band = [(float(a), float(b)) for a, b in windows]
        else:
            lo = float(opts.get("vmin", defaults.get("vmin", 0.0)))
            hi = float(opts.get("vmax", defaults.get("vmax", 0.30)))
            per_band = [(lo, hi)] * 3
    else:
        per_band = [None] * 3

    # Anything brighter than the window is kept rather than flattened, so the
    # roll-off below has room to compress it. Done per channel and identically,
    # which is what stops the compression from shifting hue.
    knee = float(opts.get("highlight_knee", 0.72))
    rolloff = knee < 1.0
    ceiling = 2.2 if rolloff else 1.0

    channels = []
    bounds = {}
    for key, fixed in zip(keys, per_band):
        if fixed is None:
            stretched, lo_b, hi_b = stretch_band(bands[key], mode, low, high, ceiling=ceiling)
        else:
            stretched, lo_b, hi_b = stretch_band(bands[key], "fixed", vmin=fixed[0],
                                                 vmax=fixed[1], ceiling=ceiling)
        channels.append(stretched)
        bounds[key] = [round(lo_b, 5), round(hi_b, 5)]

    # The curve is applied once, to the whole pixel, rather than three times to
    # three channels -- which is what keeps the colour where the ground put it.
    toned = tone_map(np.stack(channels, axis=-1), gamma=gamma,
                     knee=knee if rolloff else 1.0)
    rgb = (toned * 255).round().astype("uint8")
    valid = ~np.logical_or.reduce([np.ma.getmaskarray(bands[k]) for k in keys])
    return rgb, valid, {"mode": mode, "gamma": gamma, "knee": knee, "bands": bounds}


def compute_index(bands: dict, name: str) -> np.ma.MaskedArray:
    """An index from its bands. Most are the normalised difference of two."""
    b = {k: v.astype("float32") for k, v in bands.items()}

    with np.errstate(divide="ignore", invalid="ignore"):
        if name == "radar_ratio":
            # Already in decibels, so subtracting is dividing.
            out = b["vv"] - b["vh"]
        elif name == "evi":
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
