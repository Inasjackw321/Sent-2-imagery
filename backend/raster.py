"""Reading Sentinel-2 bands onto the output grid."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom
from scipy import ndimage

from . import config, stac
from .geo import WGS84, Grid


class BandReadError(RuntimeError):
    pass


# How wide a neighbourhood the VV/VH ratio is averaged over. Five cells is
# 50 m on Sentinel-1's grid -- well inside a field, and enough to take the
# ratio's compounded speckle down by the same factor.
RATIO_LOOKS = 5


def sampling_for(grid: Grid, merging: bool, resolution: float | None = None) -> Resampling:
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
    native = config.SATELLITE["resolution"] if resolution is None else resolution
    if grid.ground_res_m >= native:
        return Resampling.bilinear                  # downsampling: average
    return Resampling.nearest if merging else Resampling.cubic


def _resolve_assets(scene: dict, bands: list[str]) -> tuple[list[str], dict[str, str]]:
    """Which bands must be read, and where each one lives.

    A derived band is not on the satellite: it is arithmetic on two that are,
    so its inputs get read in its place and it is worked out afterwards.
    """
    sat = config.satellite(scene.get("satellite"))
    unknown = [b for b in bands if b not in config.BANDS]
    if unknown:
        raise BandReadError(f"{sat['short']} has no {', '.join(unknown)} band")

    wrong = [b for b in bands if sat["key"] not in config.BANDS[b]["sat"]]
    if wrong:
        raise BandReadError(
            f"{', '.join(wrong)} is not a {sat['short']} band")

    needed: list[str] = []
    for band in bands:
        needed.extend(config.BANDS[band].get("derive") or (band,))
    needed = list(dict.fromkeys(needed))

    assets = scene.get("assets", {})
    targets = {}
    for band in needed:
        spec = config.BANDS[band]
        # The same wavelength is called different things by different missions.
        name = spec.get("assets", {}).get(sat["key"], spec["asset"])
        # Catalogues are not always consistent about case, and a radar scene
        # naming its polarisation VV rather than vv should not be a failure.
        href = assets.get(name) or _case_insensitive(assets, name)
        if not href:
            have = ", ".join(sorted(assets)[:8]) or "none"
            raise BandReadError(
                f"Scene {scene['id']} has no {name} asset (it offers: {have})")
        targets[band] = stac.sign_href(scene, href)
    return needed, targets


def _case_insensitive(assets: dict, name: str) -> str | None:
    wanted = name.lower()
    for key, href in assets.items():
        if key.lower() == wanted:
            return href
    return None


def _add_derived(out: dict, bands: list[str]) -> dict:
    for band in bands:
        parts = config.BANDS[band].get("derive")
        if not parts or band in out:
            continue
        first, second = (out[p] for p in parts)
        ratio = (first.data - second.data).astype("float32")
        # Multi-looked, and it has to be. Speckle in the two polarisations is
        # independent, so their ratio carries about half again as much of it as
        # either channel does -- while spanning a far narrower range, which
        # makes it three times noisier on screen and paints the grain in
        # colour. A ratio is in any case a property of a patch of ground
        # rather than of one resolution cell, so averaging over a small
        # neighbourhood is what the quantity actually means.
        out[band] = np.ma.masked_array(
            ndimage.uniform_filter(ratio, RATIO_LOOKS),
            mask=np.ma.getmaskarray(first) | np.ma.getmaskarray(second))
    return {band: out[band] for band in bands}


def read_bands(scene: dict, grid: Grid, bands: list[str], mask_clouds: bool = False,
               merging: bool = False):
    """Return {band: masked float32 array} on the grid, in physical units.

    Each band is read only over the area asked for, straight out of the
    cloud-optimised GeoTIFF, and reprojected onto the output grid as it is
    read -- so a 20 m band and a 10 m one land on the same pixels. Sentinel-2
    comes back as surface reflectance and Sentinel-1 as decibels. The fraction
    of pixels dropped by cloud masking comes back alongside.
    """
    bands = list(dict.fromkeys(bands))
    sat = config.satellite(scene.get("satellite"))
    sampling = sampling_for(grid, merging, sat["resolution"])

    if scene.get("demo"):
        return _demo_bands(scene, grid, bands, mask_clouds, sampling)

    needed, targets = _resolve_assets(scene, bands)

    want_mask = mask_clouds and config.SCL_ASSET in scene.get("assets", {})

    with rasterio.Env(**config.GDAL_ENV):
        with ThreadPoolExecutor(max_workers=min(8, len(targets) + 1)) as pool:
            futures = {band: pool.submit(_read_one, href, grid, sampling)
                       for band, href in targets.items()}
            # Classes must never be averaged with their neighbours.
            mask_future = (pool.submit(_read_one, scene["assets"][config.SCL_ASSET],
                                       grid, Resampling.nearest) if want_mask else None)

            out = {band: _to_physical(fut.result(), scene, band)
                   for band, fut in futures.items()}
            scl = mask_future.result() if mask_future else None

    out = _add_derived(out, bands)

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
            _check_georeferencing(src, href)
            nodata = 0 if src.nodata is None else src.nodata
            with _placed(src, nodata) as placed, WarpedVRT(
                placed,
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
        raise BandReadError(_explain(href, exc)) from exc


@contextmanager
def _placed(src, nodata):
    """The source as something that knows where it is on the Earth.

    Sentinel-1 GRD carries no map transform. It is georeferenced by a grid of
    ground control points, because the geometry of a radar pass is not a
    rectangle laid on the ground and no single affine transform describes it.

    Warping straight onto the output grid does not honour those points -- GDAL
    only builds a GCP transformer when it is left to work the extent out for
    itself, and asking for an explicit destination grid in the same breath
    quietly loses them. The result reads without error and contains no ground,
    which is worse than a failure.

    So it is done in two steps: let the GCP warp settle first, then resample
    that onto the grid we actually want. A file that already has a transform
    skips this and is handed straight through.
    """
    if src.crs is not None and src.transform and not src.transform.is_identity:
        yield src
        return

    _gcps, gcp_crs = src.gcps
    with WarpedVRT(src, src_crs=gcp_crs, crs=gcp_crs,
                   src_nodata=nodata, nodata=nodata) as inner:
        yield inner


def _check_georeferencing(src, href: str) -> None:
    """Refuse a file that cannot be placed at all, rather than warping nonsense.

    Warping a file with no georeferencing of any kind does not raise anything --
    it silently produces a smooth smear with no ground in it, which looks like
    imagery and is not. Better to say so. Ground control points are not that
    case: those can be placed, and `_placed` does it.
    """
    if src.crs is not None and src.transform and not src.transform.is_identity:
        return
    if src.gcps and src.gcps[0] and src.gcps[1] is not None:
        return
    raise BandReadError(
        f"The file served by {_host(href)} carries no map projection and no "
        "ground control points, so there is no way to say where on Earth it "
        "belongs. Try a different source for this satellite.")


def _explain(href: str, exc: Exception) -> str:
    """Turn a GDAL failure into something a person can act on."""
    text = str(exc)
    where = _host(href)
    if "403" in text or "AccessDenied" in text or "Forbidden" in text:
        return (f"{where} refused the request (403). Sentinel-1 on AWS is a "
                "requester-pays bucket, which cannot be read anonymously; the "
                "Planetary Computer copy can.")
    if "404" in text or "NoSuchKey" in text:
        return f"{where} no longer has that file (404) — the catalogue is ahead of the data."
    if "signature" in text.lower() or "401" in text:
        return f"{where} rejected the signature — it may have expired; try again."
    return f"Could not read {href.rsplit('/', 1)[-1].split('?')[0]} from {where}: {exc}"


def _host(href: str) -> str:
    without = href.split("://", 1)[-1]
    return without.split("/", 1)[0] or "the source"


def _to_physical(raw, scene: dict, band: str | None = None) -> np.ma.MaskedArray:
    """Stored numbers to the units the band is actually in."""
    sat = config.satellite(scene.get("satellite"))
    if sat["kind"] == "radar":
        return _to_decibels(raw)

    # Sentinel-2 stores reflectance in ten-thousandths, with a baseline offset
    # on newer scenes. Landsat Collection 2 scales and shifts it instead. A
    # band that is not reflectance at all -- Landsat's thermal one is kelvin --
    # overrides both, because the satellite's own figures would be nonsense
    # for it and nothing would look broken while it happened.
    spec = config.BANDS.get(band or "", {})
    key = sat["key"]
    scale = spec.get("scale", {}).get(key, sat.get("scale", config.REFLECTANCE_SCALE))
    offset = spec.get("offset", {}).get(key, sat.get("offset", 0.0))
    thermal = "scale" in spec

    data = np.ma.filled(raw.astype("float32"), 0.0)
    if not thermal:
        data = data + stac.boa_offset(scene)
    # A stored zero is no data, not absolute zero, and offsetting it would put
    # a plausible-looking temperature where there is no measurement.
    mask = np.ma.getmaskarray(raw) | (raw.data == 0) if thermal else np.ma.getmaskarray(raw)
    return np.ma.masked_array((data * scale + offset).astype("float32"), mask=mask)


def _to_decibels(raw) -> np.ma.MaskedArray:
    """Sentinel-1 GRD amplitude to decibels.

    The stored number is amplitude; squaring it gives power, and radar is
    always looked at on a log scale because backscatter spans four orders of
    magnitude between still water and a city. Working in decibels is also what
    makes averaging dates behave: speckle is multiplicative noise, so in the
    log it is additive and simply averages out.
    """
    amplitude = np.ma.filled(raw.astype("float32"), 0.0)
    zero = amplitude <= 0                      # no data, not a silent target
    power = np.maximum(amplitude, config.S1_FLOOR_DN) ** 2
    db = (10.0 * np.log10(power)).astype("float32")
    return np.ma.masked_array(db, mask=np.ma.getmaskarray(raw) | zero)


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

# The same ground seen by radar, in decibels, at figures Sentinel-1 really
# returns. Still water reflects the pulse away from the satellite and comes
# back almost black; buildings line their corners up with it and come back
# brightest of all.
#
# The gap between the two polarisations matters as much as the levels, because
# it is what gives a radar picture its colour. VH only comes back from things
# that scatter in a volume, so vegetation returns relatively much more of it
# than bare ground does -- a small VV-VH gap for foliage, a wide one for soil.
# Get that ordering wrong and the false colour is merely decorative.
# What each kind of ground reads at on a thermal band, in kelvin, on a mild
# day. These are the differences that make a thermal picture worth looking at:
# water barely moves because it takes so much energy to warm, tarmac and roofs
# run far above the fields beside them, and snow pins itself near freezing.
_THERMAL_K = {
    "water": 288.0, "veg": 295.0, "soil": 303.0, "urban": 310.0, "snow": 272.0,
}

# Cloud top, on a thermal band. Around -23 C, which is what a few kilometres
# of altitude does.
_CLOUD_TOP_K = 250.0

_RADAR_ENDMEMBERS = {
    "water": dict(vv=-22.5, vh=-30.5),      # ratio 8.0 -- near black
    "veg": dict(vv=-8.5, vh=-13.5),         # ratio 5.0 -- volume scatter, green
    "soil": dict(vv=-11.5, vh=-20.5),       # ratio 9.0 -- surface scatter, mauve
    "urban": dict(vv=-3.0, vh=-11.0),       # ratio 8.0 -- bright, near white
    "snow": dict(vv=-15.0, vh=-22.0),       # ratio 7.0 -- dull grey
}

# Sentinel-1's noise-equivalent backscatter for the wide swath mode: nothing
# fainter than this can be told apart from the instrument's own noise.
NOISE_FLOOR = 10.0 ** (-24.0 / 10.0)


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


def _as_the_satellite_saw_it(field: np.ndarray, grid: Grid, phase: float,
                             sampling: Resampling,
                             resolution: float | None = None) -> np.ndarray:
    """Put a synthetic field through the satellite's sampling.

    Without this the demo would generate its imagery straight onto whatever
    grid was asked for, at whatever fineness -- which is a world where the
    satellite has no pixel size, nothing is ever hiding between its samples,
    and merging dates therefore has nothing to find. Averaging over a 10 m
    footprint and holding that value across the pixels it covers is what makes
    the demo behave like the real thing, including the part where each date
    lands its footprint in a slightly different place.
    """
    native = config.SATELLITE["resolution"] if resolution is None else resolution
    span = native / max(grid.ground_res_m, 1e-6)
    if span <= 1.2:                        # grid already coarser than the sensor
        return field

    h, w = field.shape
    offset = (phase % 1.0) * span
    # Which footprint each output pixel falls in, offset by this date's phase.
    rows = np.clip(((np.arange(h) + offset) // span).astype("int32"), 0, None)
    cols = np.clip(((np.arange(w) + offset) // span).astype("int32"), 0, None)
    # Average within each footprint, then hold it across the pixels it covers.
    sums = np.zeros((rows[-1] + 1, cols[-1] + 1), dtype="float32")
    counts = np.zeros_like(sums)
    np.add.at(sums, (rows[:, None], cols[None, :]), field)
    np.add.at(counts, (rows[:, None], cols[None, :]), 1.0)
    held = (sums / np.maximum(counts, 1))[rows[:, None], cols[None, :]]

    # A single date is read with a smooth enlargement rather than held in
    # blocks, so the demo has to enlarge it the same way or it would look
    # coarser on screen than the real thing ever does.
    if sampling != Resampling.nearest:
        held = ndimage.uniform_filter(held, max(int(round(span)), 1))
    return held


def _demo_bands(scene: dict, grid: Grid, bands: list[str], mask_clouds: bool,
                sampling: Resampling = Resampling.cubic):
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

    # Every date views the ground with its footprint in a slightly different
    # place, which is the whole reason merging them can recover detail.
    phase = (date_seed % 97) / 97.0

    if config.satellite(scene.get("satellite"))["kind"] == "radar":
        return _demo_radar(fractions, grid, phase, sampling, bands, date_seed), 0.0

    # A thermal band is a temperature, not a reflectance, so it is mixed from
    # its own endmembers and swings with the season rather than with the sun
    # angle. Without this it would fall through to the reflectance default and
    # read as roughly absolute zero.
    warmth = 273.15 + (season - 0.5) * 18.0

    out = {}
    for band in bands:
        acc = np.zeros((h, w), dtype="float32")
        thermal = config.BANDS.get(band, {}).get("unit") == "K"
        table = _THERMAL_K if thermal else None
        for name, frac in fractions.items():
            acc += frac * (table[name] if table else _ENDMEMBERS[name].get(band, 0.1))
        if thermal:
            # Seasonal swing about the endmembers, and a gentler grain: a
            # thermal sensor is coarser than the bands beside it.
            acc = acc + (warmth - 273.15) + 0.6 * (grain - 1.0) * 100.0
        else:
            acc = acc * grain
        acc = _as_the_satellite_saw_it(acc, grid, phase, sampling)
        out[band] = np.ma.masked_array(acc.astype("float32"),
                                       mask=np.zeros((h, w), dtype=bool))

    cloud_fraction = 0.0
    cover = float(scene.get("cloud") or 0.0) / 100.0
    if cover > 0.01:
        cloud_field = _rank01(_fbm(X * 0.8 + 31, Y * 0.8 + 13, seed + date_seed * 0.53, 4))
        clouds = _smoothstep(1.0 - cover - 0.10, 1.0 - cover + 0.04, cloud_field)
        shadow = np.roll(np.roll(clouds, 8, axis=0), 10, axis=1) * (clouds < 0.15)
        for band in out:
            thermal = config.BANDS.get(band, {}).get("unit") == "K"
            data = out[band].data
            if thermal:
                # Cloud is cold on a thermal band, not bright: the sensor sees
                # the top of it, several kilometres up. Blending toward a
                # reflectance value here would put 0.6 kelvin on the map.
                data = data * (1 - clouds) + clouds * _CLOUD_TOP_K
            else:
                data = data * (1 - 0.75 * shadow)
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


def _looks(grid: Grid) -> float:
    """How many radar resolution cells one output pixel averages together."""
    cells = grid.ground_res_m / config.satellite("sentinel-1")["resolution"]
    return max(cells * cells, 1.0)


def _demo_radar(fractions: dict, grid: Grid, phase: float, sampling: Resampling,
                bands: list[str], date_seed: int) -> dict:
    """The same synthetic ground, measured by radar instead of photographed.

    The part that matters for the demo is the speckle. Radar illuminates the
    ground with a coherent pulse, so scatterers inside one resolution cell
    interfere and every pixel comes back multiplied by a random factor -- the
    grain that makes a single radar image hard to read. It is independent from
    one pass to the next, which is why merging dates cleans it up, and the demo
    has to have it or merging Sentinel-1 would look pointless.
    """
    h, w = grid.shape
    out = {}
    rng = np.random.default_rng(date_seed)
    for band in ("vv", "vh"):
        # Mixed in power, not in decibels. Half a pixel of water and half of
        # city returns the average of what each sent back, and decibels are a
        # logarithm -- averaging those would put the mixture nowhere near where
        # the radar would actually read it.
        power = np.zeros((h, w), dtype="float32")
        for name, frac in fractions.items():
            power += frac * (10.0 ** (_RADAR_ENDMEMBERS[name][band] / 10.0))
        # Every measurement also carries the instrument's own thermal noise,
        # and that floor is why radar images look the way they do over water:
        # calm sea returns less than the noise, so what comes back is mostly
        # noise in both polarisations, and their ratio collapses towards one.
        # Without this the sea would have the *highest* VV/VH ratio in the
        # scene and come out vivid blue instead of black.
        power += NOISE_FLOOR
        acc = (10.0 * np.log10(power)).astype("float32")
        # Radar resolves about 20 m however fine the grid it is delivered on,
        # which is exactly why merging its passes cannot sharpen it.
        acc = _as_the_satellite_saw_it(acc, grid, phase, sampling,
                                       config.satellite("sentinel-1")["resolution"])
        # Multi-looked speckle: near-Gaussian in the log, about 1.5 dB wide on
        # a single resolution cell. Every output pixel that covers more than
        # one cell is already an average of that many independent draws, so the
        # grain falls off as the square root of them -- which is why a radar
        # scene looks noisy zoomed in and clean zoomed out.
        acc = acc + rng.normal(0.0, 1.5 / math.sqrt(_looks(grid)),
                               size=(h, w)).astype("float32")
        acc = acc + _demo_interference(grid, band, date_seed)
        out[band] = np.ma.masked_array(acc.astype("float32"),
                                       mask=np.zeros((h, w), dtype=bool))
    return _add_derived(out, bands)


# Roughly one demo pass in three carries interference, which is about how often
# it turns up over a busy coast. Anything more and it would stop looking like
# something worth finding.
_RFI_ODDS = 3


def _demo_interference(grid: Grid, band: str, date_seed: int):
    """Streaks from a ground radar, on some of the synthetic passes.

    Without this the interference view has nothing to show offline, and a
    feature nobody can try is a feature nobody trusts. The shape is the real
    one: a few long straight bands at a shared angle, brighter in the
    cross-polarised channel because genuine VH return is so much weaker that
    the same injected power stands proud of it.
    """
    if date_seed % _RFI_ODDS:
        return 0.0

    h, w = grid.shape
    rng = np.random.default_rng(date_seed + 4242)
    angle = math.radians(rng.uniform(0, 180))
    yy, xx = np.mgrid[0:h, 0:w]
    across = (xx - w / 2) * math.sin(angle) - (yy - h / 2) * math.cos(angle)

    # A handful of bands, unevenly spaced, as a radar's sweep leaves them.
    field = np.zeros((h, w), dtype="float32")
    spacing = max(18.0, min(h, w) / rng.uniform(4.0, 8.0))
    for k in range(-4, 5):
        offset = (k + rng.uniform(-0.25, 0.25)) * spacing
        width = rng.uniform(1.4, 3.2)
        field += np.exp(-((across - offset) / width) ** 2).astype("float32")

    # Interference arrives without having made the round trip to the ground,
    # so it does not care what the surface is -- but it stands out far more
    # against VH, whose real return is weak to begin with.
    strength = 11.0 if band == "vh" else 6.0
    return field * strength


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / (edge1 - edge0 + 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def _rank01(field: np.ndarray) -> np.ndarray:
    flat = field.ravel()
    step = max(1, flat.size // 200_000)
    quantiles = np.percentile(flat[::step], np.linspace(0, 100, 33))
    quantiles = np.maximum.accumulate(quantiles) + np.arange(33) * 1e-6
    return np.interp(field, quantiles, np.linspace(0, 1, 33)).astype("float32")
