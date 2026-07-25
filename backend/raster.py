"""Reading Sentinel-2 bands onto the output grid (and synthesising them in demo mode)."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom

from . import config, stac
from .geo import WGS84, Grid


class BandReadError(RuntimeError):
    pass


def read_bands(scene: dict, grid: Grid, keys: list[str], mask_clouds: bool = False):
    """Return {band_key: masked float32 reflectance array} on the grid.

    Cloudy pixels are masked out when ``mask_clouds`` is set; the fraction of
    pixels dropped comes back alongside the arrays.
    """
    keys = list(dict.fromkeys(keys))
    if scene.get("demo"):
        return _demo_bands(scene, grid, keys, mask_clouds)

    missing = [k for k in keys if k not in scene.get("assets", {})]
    if missing:
        raise BandReadError(f"Scene {scene['id']} is missing bands: {', '.join(missing)}")

    offset = stac.boa_offset(scene)

    with rasterio.Env(**config.GDAL_ENV):
        with ThreadPoolExecutor(max_workers=min(6, len(keys) + 1)) as pool:
            futures = {k: pool.submit(_read_one, scene["assets"][k], grid, False) for k in keys}
            scl_future = None
            if mask_clouds and config.SCL_ASSET in scene.get("assets", {}):
                scl_future = pool.submit(
                    _read_one, scene["assets"][config.SCL_ASSET], grid, True
                )

            bands = {}
            for key, fut in futures.items():
                raw = fut.result()
                data = (raw.astype("float32") + offset) * config.REFLECTANCE_SCALE
                bands[key] = np.ma.masked_array(data, mask=np.ma.getmaskarray(raw))

            scl = scl_future.result() if scl_future else None

    cloud_fraction = 0.0
    if scl is not None:
        scl_data = np.ma.filled(scl, 0).astype("uint8")
        cloudy = np.isin(scl_data, config.CLOUD_CLASSES)
        cloud_fraction = float(cloudy.mean())
        for key in bands:
            bands[key] = np.ma.masked_array(
                bands[key].data, mask=np.ma.getmaskarray(bands[key]) | cloudy
            )

    return bands, cloud_fraction


def _read_one(href: str, grid: Grid, categorical: bool):
    resampling = Resampling.nearest if categorical else Resampling.bilinear
    try:
        with rasterio.open(href) as src:
            nodata = 0 if src.nodata is None else src.nodata
            with WarpedVRT(
                src,
                crs=grid.crs,
                transform=grid.transform,
                width=grid.width,
                height=grid.height,
                resampling=resampling,
                src_nodata=nodata,
                nodata=nodata,
            ) as vrt:
                return vrt.read(1, masked=True)
    except rasterio.RasterioIOError as exc:
        raise BandReadError(f"Could not read {href.rsplit('/', 1)[-1]}: {exc}") from exc


def aoi_mask(geometry: dict, grid: Grid) -> np.ndarray:
    """Boolean array, True *outside* the AOI polygon."""
    projected = transform_geom(WGS84, grid.crs, geometry)
    inside = rasterize(
        [(projected, 1)],
        out_shape=grid.shape,
        transform=grid.transform,
        fill=0,
        dtype="uint8",
        all_touched=True,
    )
    return inside == 0


def apply_clip(bands: dict, outside: np.ndarray) -> dict:
    return {
        k: np.ma.masked_array(v.data, mask=np.ma.getmaskarray(v) | outside)
        for k, v in bands.items()
    }


# ---------------------------------------------------------------------------
# Synthetic bands for DEMO_MODE
# ---------------------------------------------------------------------------

# Reflectance endmembers, roughly typical Sentinel-2 surface values.
_ENDMEMBERS = {
    "water": dict(coastal=.06, blue=.055, green=.048, red=.032, rededge1=.028,
                  rededge2=.022, rededge3=.018, nir=.014, nir08=.013, nir09=.010,
                  swir16=.008, swir22=.006),
    "veg": dict(coastal=.035, blue=.032, green=.062, red=.038, rededge1=.11,
                rededge2=.30, rededge3=.36, nir=.40, nir08=.42, nir09=.38,
                swir16=.20, swir22=.09),
    "soil": dict(coastal=.10, blue=.11, green=.145, red=.19, rededge1=.22,
                 rededge2=.25, rededge3=.26, nir=.28, nir08=.29, nir09=.29,
                 swir16=.33, swir22=.29),
    "urban": dict(coastal=.12, blue=.13, green=.142, red=.155, rededge1=.165,
                  rededge2=.175, rededge3=.18, nir=.19, nir08=.195, nir09=.20,
                  swir16=.225, swir22=.205),
    "snow": dict(coastal=.85, blue=.88, green=.90, red=.89, rededge1=.86,
                 rededge2=.82, rededge3=.78, nir=.72, nir08=.66, nir09=.55,
                 swir16=.08, swir22=.04),
}


def _fbm(x: np.ndarray, y: np.ndarray, seed: float, octaves: int = 5) -> np.ndarray:
    """Cheap band-limited noise: summed rotated sinusoids. Deterministic in world space."""
    total = np.zeros_like(x)
    amp, freq = 1.0, 1.0
    norm = 0.0
    for o in range(octaves):
        a = seed * 1.7 + o * 2.399
        kx, ky = math.cos(a), math.sin(a)
        b = seed * 0.9 + o * 5.113
        jx, jy = math.cos(b), math.sin(b)
        total += amp * (
            np.sin(freq * (kx * x + ky * y) + seed + o)
            * np.cos(freq * (jx * x - jy * y) * 0.87 + seed * 0.5)
        )
        norm += amp
        amp *= 0.52
        freq *= 2.07
    return total / norm


def _demo_bands(scene: dict, grid: Grid, keys: list[str], mask_clouds: bool):
    h, w = grid.shape
    x0, y0, x1, y1 = grid.bounds3857
    # World-space coordinates in units of ~2 km so patterns stay put while panning.
    xs = np.linspace(x0, x1, w, dtype="float32") / 2000.0
    ys = np.linspace(y1, y0, h, dtype="float32") / 2000.0
    X, Y = np.meshgrid(xs, ys)

    seed = int(hashlib.sha1(b"sent2-demo").hexdigest()[:6], 16) % 1000 / 10.0
    # Rank-normalised so the land-cover mix is sensible anywhere on Earth, and
    # identical between dates over the same AOI (these fields are date-free).
    elevation = _rank01(_fbm(X * 0.35, Y * 0.35, seed, 6))
    moisture = _rank01(_fbm(X * 0.6 + 11, Y * 0.6 - 7, seed + 3.3, 4))
    fields = _rank01(_fbm(X * 3.1, Y * 3.1, seed + 9.1, 2))
    builtup = _rank01(_fbm(X * 0.9 - 5, Y * 0.9 + 2, seed + 17.7, 3))

    day = dt.date.fromisoformat(scene["date"])
    doy = day.timetuple().tm_yday
    season = 0.5 - 0.5 * math.cos(2 * math.pi * (doy - 20) / 365.25)  # 0 winter, 1 summer
    if grid.center_lat < 0:
        season = 1.0 - season
    year_drift = (day.year - 2017) * 0.015  # slow, visible change over time

    water = 1.0 - _smoothstep(0.06, 0.15, elevation - 0.08 * moisture + 0.04)
    urban = _smoothstep(0.74 - year_drift, 0.86 - year_drift, builtup) * (1 - water)
    veg_pot = np.clip(0.25 + 0.9 * moisture + 0.5 * (fields - 0.5) - 0.35 * elevation, 0, 1)
    veg = veg_pot * (0.30 + 0.70 * season) * (1 - water) * (1 - urban)
    snow = _smoothstep(0.90, 0.98, elevation) * (1 - season) * (1 - water)
    soil = np.clip(1 - water - urban - veg - snow, 0, 1)

    fractions = {"water": water, "veg": veg, "soil": soil, "urban": urban, "snow": snow}
    total = sum(fractions.values()) + 1e-6
    fractions = {k: v / total for k, v in fractions.items()}

    date_seed = int(hashlib.sha1(scene["date"].encode()).hexdigest()[:6], 16) % 997
    grain = 1.0 + 0.05 * _fbm(X * 9.0, Y * 9.0, seed + date_seed * 0.37, 2)

    bands = {}
    for key in keys:
        if key not in config.BANDS:
            continue
        acc = np.zeros((h, w), dtype="float32")
        for name, frac in fractions.items():
            acc += frac * _ENDMEMBERS[name][key]
        bands[key] = np.ma.masked_array(np.clip(acc * grain, 0, 1.2).astype("float32"),
                                        mask=np.zeros((h, w), dtype=bool))

    cloud_fraction = 0.0
    cover = float(scene.get("cloud") or 0.0) / 100.0
    if cover > 0.01:
        # Rank-normalised so the cloudy fraction actually matches the scene's
        # reported cloud cover.
        cloud_field = _rank01(_fbm(X * 0.8 + 31, Y * 0.8 + 13, seed + date_seed * 0.53, 4))
        clouds = _smoothstep(1.0 - cover - 0.10, 1.0 - cover + 0.04, cloud_field)
        shadow = np.roll(np.roll(clouds, 8, axis=0), 10, axis=1) * (clouds < 0.15)
        for key in bands:
            data = bands[key].data * (1 - 0.75 * shadow)
            data = data * (1 - clouds) + clouds * (0.62 if key != "swir22" else 0.45)
            bands[key] = np.ma.masked_array(data.astype("float32"), mask=bands[key].mask)
        cloudy = clouds > 0.5
        cloud_fraction = float(cloudy.mean())
        if mask_clouds:
            for key in bands:
                bands[key] = np.ma.masked_array(bands[key].data, mask=cloudy)

    return bands, cloud_fraction


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def _rank01(field: np.ndarray) -> np.ndarray:
    """Map a field onto 0-1 by its own distribution, estimated from a subsample."""
    flat = field.ravel()
    step = max(1, flat.size // 200_000)
    quantiles = np.percentile(flat[::step], np.linspace(0, 100, 33))
    quantiles = np.maximum.accumulate(quantiles) + np.arange(33) * 1e-6
    return np.interp(field, quantiles, np.linspace(0, 1, 33)).astype("float32")
