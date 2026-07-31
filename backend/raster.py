"""Reading any supported satellite's bands onto the output grid."""

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

from . import config, sources, stac
from .geo import WGS84, Grid


class BandReadError(RuntimeError):
    pass


def read_bands(scene: dict, grid: Grid, aliases: list[str], source=None,
               mask_clouds: bool = False):
    """Return {alias: masked float32 array} on the grid, in physical units.

    Values come back as reflectance, decibels, kelvin or metres depending on
    the satellite. The fraction of pixels dropped by cloud masking comes back
    alongside.
    """
    source = source or sources.get(scene.get("source"))
    aliases = list(dict.fromkeys(aliases))

    if scene.get("demo"):
        return _demo_bands(scene, grid, aliases, source, mask_clouds)

    unknown = [a for a in aliases if a not in source.bands]
    if unknown:
        raise BandReadError(
            f"{source.label} has no {', '.join(unknown)} band"
        )

    targets = {}
    for alias in aliases:
        asset, index = sources.resolve_band(source, alias)
        if asset not in scene.get("assets", {}):
            raise BandReadError(f"Scene {scene['id']} is missing its {asset} asset")
        targets[alias] = (scene["assets"][asset], index)

    mask_spec = sources.CLOUD_MASKS.get(source.cloud_mask) if mask_clouds else None
    if mask_spec and mask_spec["asset"] not in scene.get("assets", {}):
        mask_spec = None

    with rasterio.Env(**config.GDAL_ENV):
        with ThreadPoolExecutor(max_workers=min(8, len(targets) + 1)) as pool:
            futures = {
                alias: pool.submit(_read_one, stac.sign_href(source, href), grid, index, False)
                for alias, (href, index) in targets.items()
            }
            mask_future = None
            if mask_spec:
                mask_future = pool.submit(
                    _read_one,
                    stac.sign_href(source, scene["assets"][mask_spec["asset"]]),
                    grid, 1, True,
                )

            bands = {alias: _to_physical(fut.result(), scene, source)
                     for alias, fut in futures.items()}
            qa = mask_future.result() if mask_future else None

    cloud_fraction = 0.0
    if qa is not None:
        cloudy = _cloudy_pixels(qa, mask_spec)
        cloud_fraction = float(cloudy.mean())
        for alias in bands:
            bands[alias] = np.ma.masked_array(
                bands[alias].data,
                mask=np.ma.getmaskarray(bands[alias]) | cloudy,
            )

    return bands, cloud_fraction


def _read_one(href: str, grid: Grid, index: int, categorical: bool):
    resampling = Resampling.nearest if categorical else Resampling.bilinear
    try:
        with rasterio.open(href) as src:
            if index > src.count:
                raise BandReadError(
                    f"{href.rsplit('/', 1)[-1]} has {src.count} band(s), needed {index}")
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
                return vrt.read(index, masked=True)
    except rasterio.RasterioIOError as exc:
        raise BandReadError(f"Could not read {href.rsplit('/', 1)[-1]}: {exc}") from exc


def _to_physical(raw, scene: dict, source) -> np.ma.MaskedArray:
    """Apply the satellite's own scaling to get physical units."""
    spec = sources.SCALES[source.scale]
    mask = np.ma.getmaskarray(raw)
    data = raw.astype("float32")

    if spec["kind"] == "db":
        # Radar: work in decibels, where the dynamic range is readable.
        with np.errstate(divide="ignore", invalid="ignore"):
            power = np.ma.filled(data, 0.0)
            if source.scale == "amplitude_db":
                power = power ** 2
            out = 10.0 * np.log10(np.where(power > 0, power, np.nan))
        mask = mask | ~np.isfinite(out)
        out = np.nan_to_num(out, nan=-40.0, posinf=0.0, neginf=-40.0)
        return np.ma.masked_array(out.astype("float32"), mask=mask)

    offset = stac.boa_offset(scene, source)
    out = (np.ma.filled(data, 0.0) + offset) * spec["mul"] + spec["add"]
    return np.ma.masked_array(out.astype("float32"), mask=mask)


def _cloudy_pixels(qa, spec) -> np.ndarray:
    """Turn a quality band into a boolean cloud mask."""
    data = np.ma.filled(qa, 0).astype("uint32")
    if spec["kind"] == "classes":
        return np.isin(data, spec["cloudy"])

    cloudy = np.zeros(data.shape, dtype=bool)
    for bit in spec["cloudy_bits"]:
        cloudy |= (data & (1 << bit)) > 0
    return cloudy


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

_ENDMEMBERS = {
    "water": dict(coastal=.06, blue=.055, green=.048, red=.032, rededge1=.028,
                  rededge2=.022, rededge3=.018, nir=.014, nir08=.013, nir09=.010,
                  swir16=.008, swir22=.006, cirrus=.002, pan=.045, lwir=288.0,
                  vv=-22.0, vh=-28.0, elevation=0.0, classification=80),
    "veg": dict(coastal=.035, blue=.032, green=.062, red=.038, rededge1=.11,
                rededge2=.30, rededge3=.36, nir=.40, nir08=.42, nir09=.38,
                swir16=.20, swir22=.09, cirrus=.004, pan=.075, lwir=298.0,
                vv=-9.0, vh=-15.0, elevation=180.0, classification=10),
    "soil": dict(coastal=.10, blue=.11, green=.145, red=.19, rededge1=.22,
                 rededge2=.25, rededge3=.26, nir=.28, nir08=.29, nir09=.29,
                 swir16=.33, swir22=.29, cirrus=.006, pan=.17, lwir=310.0,
                 vv=-12.0, vh=-19.0, elevation=240.0, classification=60),
    "urban": dict(coastal=.12, blue=.13, green=.142, red=.155, rededge1=.165,
                  rededge2=.175, rededge3=.18, nir=.19, nir08=.195, nir09=.20,
                  swir16=.225, swir22=.205, cirrus=.007, pan=.15, lwir=316.0,
                  vv=-5.0, vh=-12.0, elevation=210.0, classification=50),
    "snow": dict(coastal=.85, blue=.88, green=.90, red=.89, rededge1=.86,
                 rededge2=.82, rededge3=.78, nir=.72, nir08=.66, nir09=.55,
                 swir16=.08, swir22=.04, cirrus=.05, pan=.88, lwir=270.0,
                 vv=-16.0, vh=-23.0, elevation=1400.0, classification=70),
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


def _demo_bands(scene: dict, grid: Grid, aliases: list[str], source, mask_clouds: bool):
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

    # Coarser satellites see a blurrier world; the pan band sees a sharper one.
    softness = max(0.0, math.log2(max(source.resolution, 1.0) / 10.0)) * 1.1

    bands = {}
    for alias in aliases:
        if alias not in source.bands:
            continue
        acc = np.zeros((h, w), dtype="float32")
        for name, frac in fractions.items():
            acc += frac * _ENDMEMBERS[name].get(alias, 0.1)

        if alias == "classification":
            # Hard-label each pixel with whichever cover type dominates it.
            names = list(fractions)
            stacked = np.stack([fractions[n] for n in names])
            winner = stacked.argmax(axis=0)
            codes = np.array([_ENDMEMBERS[n]["classification"] for n in names],
                             dtype="float32")
            bands[alias] = np.ma.masked_array(codes[winner],
                                              mask=np.zeros((h, w), bool))
            continue

        if alias == "elevation":
            acc = elevation * 900.0 + fields * 60.0
        elif alias in ("vv", "vh"):
            acc = acc + 2.0 * (grain - 1.0) * 30.0     # radar speckle
        elif alias == "lwir":
            acc = acc + (season - 0.5) * 8.0
        else:
            acc = acc * grain

        if softness > 0.2 and alias != "pan":
            from scipy import ndimage
            acc = ndimage.gaussian_filter(acc, sigma=softness)

        bands[alias] = np.ma.masked_array(acc.astype("float32"),
                                          mask=np.zeros((h, w), dtype=bool))

    cloud_fraction = 0.0
    cover = float(scene.get("cloud") or 0.0) / 100.0
    if cover > 0.01 and source.kind in ("optical", "thermal"):
        cloud_field = _rank01(_fbm(X * 0.8 + 31, Y * 0.8 + 13, seed + date_seed * 0.53, 4))
        clouds = _smoothstep(1.0 - cover - 0.10, 1.0 - cover + 0.04, cloud_field)
        shadow = np.roll(np.roll(clouds, 8, axis=0), 10, axis=1) * (clouds < 0.15)
        for alias in bands:
            if alias in ("elevation", "classification"):
                continue
            data = bands[alias].data * (1 - 0.75 * shadow)
            bright = 0.62 if alias != "lwir" else 250.0
            data = data * (1 - clouds) + clouds * bright
            bands[alias] = np.ma.masked_array(data.astype("float32"),
                                              mask=bands[alias].mask)
        # Mask everything the cloud touched, not just the solid centres --
        # a real scene classification flags thin edges and shadows too, and
        # leaving them in would streak any multi-date composite.
        cloudy = (clouds > 0.15) | (shadow > 0.15)
        cloud_fraction = float(cloudy.mean())
        if mask_clouds:
            for alias in bands:
                bands[alias] = np.ma.masked_array(bands[alias].data, mask=cloudy)

    return bands, cloud_fraction


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def _rank01(field: np.ndarray) -> np.ndarray:
    flat = field.ravel()
    step = max(1, flat.size // 200_000)
    quantiles = np.percentile(flat[::step], np.linspace(0, 100, 33))
    quantiles = np.maximum.accumulate(quantiles) + np.arange(33) * 1e-6
    return np.interp(field, quantiles, np.linspace(0, 1, 33)).astype("float32")
