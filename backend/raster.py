"""Reading Sentinel-2 bands onto the output grid."""

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


def sampling_for(grid: Grid, merging: bool) -> Resampling:
    """How to land the satellite's pixels on the output grid.

    The choice matters more than it looks. Going the other way -- a wide area
    on a coarse grid -- neighbouring pixels must be averaged or the result
    aliases. Zoomed in, they must not be: a merge recovers detail from the
    sub-pixel differences between dates, and interpolating each date on the way
    in smooths away the very differences it is about to solve for. Nearest
    keeps every date's measurements exactly where the satellite made them, and
    the fusion does the rest. For one date there is nothing to fuse, so cubic
    gives the smoothest honest enlargement.
    """
    if grid.ground_res_m >= config.SATELLITE["resolution"]:
        return Resampling.bilinear                  # downsampling: average
    return Resampling.nearest if merging else Resampling.cubic


def read_bands(scene: dict, grid: Grid, bands: list[str], mask_clouds: bool = False,
               merging: bool = False):
    """Return {band: masked float32 array} on the grid, as reflectance.

    Each band is read only over the area asked for, straight out of the
    cloud-optimised GeoTIFF, and reprojected onto the output grid as it is
    read -- so a 20 m band and a 10 m one land on the same pixels. The
    fraction of pixels dropped by cloud masking comes back alongside.
    """
    bands = list(dict.fromkeys(bands))
    sampling = sampling_for(grid, merging)

    if scene.get("demo"):
        return _demo_bands(scene, grid, bands, mask_clouds)

    unknown = [b for b in bands if b not in config.BANDS]
    if unknown:
        raise BandReadError(f"Sentinel-2 has no {', '.join(unknown)} band")

    targets = {}
    for band in bands:
        asset = config.BANDS[band]["asset"]
        if asset not in scene.get("assets", {}):
            raise BandReadError(f"Scene {scene['id']} is missing its {asset} asset")
        targets[band] = scene["assets"][asset]

    want_mask = mask_clouds and config.SCL_ASSET in scene.get("assets", {})

    with rasterio.Env(**config.GDAL_ENV):
        with ThreadPoolExecutor(max_workers=min(8, len(targets) + 1)) as pool:
            futures = {band: pool.submit(_read_one, href, grid, sampling)
                       for band, href in targets.items()}
            # Classes must never be averaged with their neighbours.
            mask_future = (pool.submit(_read_one, scene["assets"][config.SCL_ASSET],
                                       grid, Resampling.nearest) if want_mask else None)

            out = {band: _to_reflectance(fut.result(), scene)
                   for band, fut in futures.items()}
            scl = mask_future.result() if mask_future else None

    cloud_fraction = 0.0
    if scl is not None:
        cloudy = np.isin(np.ma.filled(scl, 0).astype("uint8"), config.CLOUD_CLASSES)
        cloud_fraction = float(cloudy.mean())
        for band in out:
            out[band] = np.ma.masked_array(
                out[band].data, mask=np.ma.getmaskarray(out[band]) | cloudy)

    return out, cloud_fraction


def _read_one(href: str, grid: Grid, resampling: Resampling):
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


def _to_reflectance(raw, scene: dict) -> np.ma.MaskedArray:
    """Stored numbers to surface reflectance, 0 to 1."""
    data = np.ma.filled(raw.astype("float32"), 0.0) + stac.boa_offset(scene)
    return np.ma.masked_array((data * config.REFLECTANCE_SCALE).astype("float32"),
                              mask=np.ma.getmaskarray(raw))


# ── Area of interest ───────────────────────────────────────────


def aoi_mask(geometry: dict, grid: Grid) -> np.ndarray:
    """Boolean array, True *outside* the drawn shape."""
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


# ── Synthetic bands for DEMO_MODE ──────────────────────────────

# Reflectance of each kind of ground, band by band: the demo scene is these
# five mixed together in proportions that vary over the map and the seasons.
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


def _demo_bands(scene: dict, grid: Grid, bands: list[str], mask_clouds: bool):
    h, w = grid.shape
    x0, y0, x1, y1 = grid.bounds3857
    xs = np.linspace(x0, x1, w, dtype="float32") / 2000.0
    ys = np.linspace(y1, y0, h, dtype="float32") / 2000.0
    X, Y = np.meshgrid(xs, ys)

    seed = int(hashlib.sha1(b"sent2-demo").hexdigest()[:6], 16) % 1000 / 10.0
    elevation = _rank01(_fbm(X * 0.35, Y * 0.35, seed, 6))
    moisture = _rank01(_fbm(X * 0.6 + 11, Y * 0.6 - 7, seed + 3.3, 4))
    fields = _rank01(_fbm(X * 3.1, Y * 3.1, seed + 9.1, 2))
    builtup = _rank01(_fbm(X * 0.9 - 5, Y * 0.9 + 2, seed + 17.7, 3))

    day = dt.date.fromisoformat(scene["date"])
    doy = day.timetuple().tm_yday
    season = 0.5 - 0.5 * math.cos(2 * math.pi * (doy - 20) / 365.25)
    if grid.center_lat < 0:
        season = 1.0 - season
    year_drift = (day.year - 2017) * 0.015

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

    out = {}
    for band in bands:
        acc = np.zeros((h, w), dtype="float32")
        for name, frac in fractions.items():
            acc += frac * _ENDMEMBERS[name].get(band, 0.1)
        out[band] = np.ma.masked_array((acc * grain).astype("float32"),
                                       mask=np.zeros((h, w), dtype=bool))

    cloud_fraction = 0.0
    cover = float(scene.get("cloud") or 0.0) / 100.0
    if cover > 0.01:
        cloud_field = _rank01(_fbm(X * 0.8 + 31, Y * 0.8 + 13, seed + date_seed * 0.53, 4))
        clouds = _smoothstep(1.0 - cover - 0.10, 1.0 - cover + 0.04, cloud_field)
        shadow = np.roll(np.roll(clouds, 8, axis=0), 10, axis=1) * (clouds < 0.15)
        for band in out:
            data = out[band].data * (1 - 0.75 * shadow)
            data = data * (1 - clouds) + clouds * 0.62
            out[band] = np.ma.masked_array(data.astype("float32"), mask=out[band].mask)
        # Mask everything the cloud touched, not just the solid centres --
        # the real scene classification flags thin edges and shadows too, and
        # leaving them in would streak any multi-date composite.
        cloudy = (clouds > 0.15) | (shadow > 0.15)
        cloud_fraction = float(cloudy.mean())
        if mask_clouds:
            for band in out:
                out[band] = np.ma.masked_array(out[band].data, mask=cloudy)

    return out, cloud_fraction


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def _rank01(field: np.ndarray) -> np.ndarray:
    flat = field.ravel()
    step = max(1, flat.size // 200_000)
    quantiles = np.percentile(flat[::step], np.linspace(0, 100, 33))
    quantiles = np.maximum.accumulate(quantiles) + np.arange(33) * 1e-6
    return np.interp(field, quantiles, np.linspace(0, 1, 33)).astype("float32")
