"""Render orchestration: area + dates + options -> image bytes and metadata."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
from collections import OrderedDict

import numpy as np

from . import composite, config, enhance, raster, sar, stac, superres
from .geo import (Grid, circle_to_polygon, geodesic_area_km2, geometry_bounds,
                  normalise_aoi)


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


def _named(keys: list[str]) -> str:
    """The satellites a preset works on, read out for a person."""
    names = [config.satellite(k)["short"] for k in keys]
    if len(names) == 1:
        return names[0]
    return " or ".join([", ".join(names[:-1]), names[-1]])


def change_band(scenes: list[dict]) -> str:
    """Which channel two passes are compared in.

    The co-polarised one -- VV over land, HH over ice and ocean -- because it
    carries most of the return and sits furthest above the instrument's noise
    floor. A difference taken in the crossed channel over water would be a
    difference of two noise floors.
    """
    carried = [set(sar.polarisations(s)) for s in scenes]
    for band in config.CHANGE["bands"]:
        want = config.BAND_POLARISATION[band]
        if all(not pol or want in pol for pol in carried):
            return band
    pairs = " and ".join(sorted(sar.pair_of(p) or "unknown" for p in carried))
    raise RenderError(
        f"These two passes have no channel in common ({pairs}), so there is "
        "nothing to compare between them.")


def _needed_bands(mode: str, preset: str, index: str, sat: dict,
                  scenes: list[dict] | None = None) -> list[str]:
    if mode == "change":
        return [change_band(scenes or [])]
    if mode == "index":
        spec = config.INDICES.get(index)
        if not spec:
            raise RenderError(f"Unknown index {index!r}")
        if sat["key"] not in spec["sat"]:
            raise RenderError(f"{spec['label']} needs {_named(spec['sat'])}")
        return list(spec["bands"])
    spec = config.COMPOSITES.get(preset)
    if not spec:
        raise RenderError(f"Unknown composite {preset!r}")
    if sat["key"] not in spec["sat"]:
        raise RenderError(f"{spec['label']} needs {_named(spec['sat'])}")
    return list(dict.fromkeys(spec["bands"]))


def satellite_of(scenes: list[dict]) -> dict:
    """The satellite a render is about, refusing to mix two of them.

    An optical picture and a radar measurement cannot be averaged together:
    they are different physical quantities in different units, and the result
    would mean nothing. Merging is always within one satellite.
    """
    keys = {s.get("satellite") or config.DEFAULT_SATELLITE for s in scenes}
    if len(keys) > 1:
        names = ", ".join(sorted(config.satellite(k)["short"] for k in keys))
        raise RenderError(f"One satellite at a time: this mixes {names}")
    return config.satellite(next(iter(keys), None))


def prepare(req: dict):
    geometry = normalise_aoi(req.get("aoi") or req.get("geometry"))
    grid = Grid(geometry_bounds(geometry), int(req.get("size") or config.DEFAULT_SIZE))
    return geometry, grid


# ── Band loading, with enhancement applied in physical units ───


def load_bands(scene: dict, geometry: dict, grid: Grid, names: list[str], req: dict,
               merging: bool = False):
    # The addresses as well as the id. A scene arrives in the request body,
    # so its id is whatever the caller wrote on it: keyed on the id alone,
    # two different scenes sharing one -- a mistake, a stale copy, or a
    # crafted body naming a real scene and pointing its bands somewhere else
    # -- are one entry in this cache, and each would be served the other's
    # pixels.
    key = _cache_key("bands", scene["id"],
                     sorted((scene.get("assets") or {}).items()),
                     grid.bounds3857, grid.shape,
                     sorted(names), bool(req.get("mask_clouds")), bool(req.get("clip")),
                     merging, geometry if req.get("clip") else None)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    bands, cloud_fraction = raster.read_bands(
        scene, grid, names, mask_clouds=bool(req.get("mask_clouds")), merging=merging)
    if req.get("clip"):
        bands = raster.apply_clip(bands, raster.aoi_mask(geometry, grid))

    _cache.put(key, (bands, cloud_fraction))
    return bands, cloud_fraction


def auto_scale(dates: int) -> int:
    """How much finer than the satellite this many dates can honestly resolve."""
    for needed, scale in config.SUPERRES_STEPS:
        if dates >= needed:
            return scale
    return 1


def oversampling(grid: Grid, resolution: float | None = None) -> float:
    """How many output pixels the satellite's own resolution cell spans.

    This is what decides whether there is anything to super-resolve. Detail
    recoverable by merging lives *between* the satellite's samples, so it only
    exists where the output grid is finer than that sampling. Render a wide
    area small and the grid is coarser than 10 m -- several dates can still
    clear the cloud, but there is no finer detail to find. Render it large and
    each 10 m measurement covers several output pixels, which is exactly the
    gap the merge fills in.
    """
    native = config.SATELLITE["resolution"] if resolution is None else resolution
    return native / max(grid.ground_res_m, 1e-6)


def merge_plan(req: dict, grid: Grid, scenes: list[dict], sat: dict | None = None) -> dict:
    """What merging these dates onto this grid can and cannot do.

    The output is always the size that was asked for. Merging makes that size
    *real* rather than making it bigger: the same picture, resolving detail
    that one pass could not, instead of the same detail spread over more
    pixels.

    Radar is the exception, and it is a physical one. Sentinel-1 GRD arrives on
    a 10 m grid but resolves about 20 m, so it is already over-sampled: there
    is nothing hiding between its samples to solve for. What merging radar
    dates does instead is average out the speckle, which is the thing that
    actually stops a single radar image being readable.
    """
    if sat is None:
        sat = satellite_of(scenes) if scenes else config.satellite()
    over = oversampling(grid, sat["resolution"])
    supported = auto_scale(len(scenes)) if len(scenes) > 1 else 1
    asked = req.get("superres", "auto")
    # `is True` rather than `== True`: 1 equals True in Python, and a caller
    # asking for 1x means "leave it alone", not "choose for me".
    if not (asked is None or asked is True or asked == "auto"):
        supported = min(max(int(asked), 1), config.MAX_SUPERRES)

    # Below this the grid is at or coarser than the satellite's own sampling,
    # so the dates have nothing finer to contribute and a plain composite is
    # the honest answer.
    sharpening = (sat["can_superres"] and len(scenes) > 1
                  and supported > 1 and over > 1.2)
    return {
        "sharpening": sharpening,
        "oversampling": round(over, 2),
        "supported": supported if sat["can_superres"] else 1,
        # Merging radar still buys something worth saying out loud.
        "despeckling": not sat["can_superres"] and len(scenes) > 1,
        # What the result can actually resolve: the finer of what the grid can
        # hold and what the number of dates justifies.
        "resolves": min(over, supported) if sharpening else 1.0,
    }


def _gather(scenes: list[dict], geometry, grid, names, req, sat=None):
    """Read every scene the render needs, merging them if there is more than one.

    Two ways of merging, and they answer different questions. The composite
    asks *what is under the cloud* and answers it by taking the middle of the
    stack. Super-resolution asks *what is smaller than a pixel* and answers it
    by reading every date onto a finer grid, where each one lands its samples
    at a slightly different place, and solving for the detail they jointly saw.
    """
    plan = merge_plan(req, grid, scenes, sat)

    stacks = []
    clouds = []
    for scene in scenes:
        bands, cloud = load_bands(scene, geometry, grid, names, req,
                                  merging=plan["sharpening"])
        stacks.append(bands)
        clouds.append(cloud)

    cloud_fraction = min(clouds) if clouds else 0.0
    report = enhance.composite_report(stacks, names[0]) if len(stacks) > 1 else None

    if plan["sharpening"]:
        merged, sr_report = superres.fuse(
            stacks,
            # The blur to undo is the satellite's own footprint measured in
            # output pixels, which is what the oversampling is.
            scale=plan["oversampling"],
            resolves=plan["resolves"],
            restore=float(req.get("superres_restore", 0.75)),
            register=req.get("superres_register", True) is not False,
            dates=[s.get("date") for s in scenes],
        )
        return merged, cloud_fraction, report, sr_report, grid

    # One date, or a grid no finer than the satellite sampled it: nothing to
    # sharpen, so the middle of the stack is the best answer available.
    #
    # Radar takes the mean instead. Speckle is a random multiplier on every
    # pixel, which in decibels is an additive error with no bias -- averaging
    # divides it by the square root of the number of dates, where the median
    # would only throw away the extremes. There is no cloud to reject, so
    # nothing is lost by preferring the average.
    method = "mean" if plan.get("despeckling") else "median"
    merged = enhance.composite(stacks, method) if len(stacks) > 1 else stacks[0]
    return merged, cloud_fraction, report, None, grid


def change_pair(scenes: list[dict], geometry, grid, req: dict):
    """Two passes, and what is different between them. Returns (dB, band, pair).

    The difference in decibels, newer less older, one number per pixel. In
    decibels because backscatter spans six orders of magnitude and the
    question is "how many times brighter", which on a logarithmic scale is a
    subtraction; done in linear power, the bright half of the scene would
    swamp every change in the dark half.

    REFUSED rather than drawn where the two passes are not comparable, which
    is the one place in this app where a render says no. Everywhere else a
    mismatch is said out loud and the picture is drawn anyway, because
    somebody may want the average and being told is enough. Not here: the
    difference between an ascending and a descending pass, or between two
    tracks, is a picture of the terrain and the geometry rather than of
    anything that happened. Every hillside is a change and nothing that
    changed stands out of them. There is no reading of that image that is
    worth having, so it is not offered.
    """
    if len(scenes) != 2:
        raise RenderError(
            f"A change needs exactly two passes; {len(scenes)} "
            f"{'was' if len(scenes) == 1 else 'were'} chosen.")
    older, newer = sorted(scenes, key=lambda s: s.get("datetime") or s.get("date") or "")
    if not sar.comparable(older, newer):
        raise RenderError(
            "These two passes cannot be subtracted: "
            f"{sar.label(older)} against {sar.label(newer)}. Radar brightness "
            "depends on the angle the pulse arrives at, so a difference "
            "between two geometries is a picture of the hills rather than of "
            "what changed. Pick two dates on the same track and direction.")

    band = change_band([older, newer])
    first, _ = load_bands(older, geometry, grid, [band], req)
    second, _ = load_bands(newer, geometry, grid, [band], req)
    # Masked in either pass is masked in the difference: a pixel one of them
    # did not see has no difference to state, and filling it would draw the
    # edge of a swath as a change.
    out = second[band] - first[band]
    return (np.ma.masked_array(np.ma.filled(out, 0.0),
                               mask=np.ma.getmaskarray(out)),
            band, (older, newer))


def _enhance_bands(bands: dict, req: dict, applied: list[str],
                   optical: bool = True) -> dict:
    """Corrections that belong in reflectance, before any stretch."""
    # Haze is an atmospheric effect on light. Radar goes straight through the
    # atmosphere, so subtracting a dark object from a decibel figure would not
    # be removing anything -- it would just be moving the numbers.
    if req.get("haze_removal") and optical:
        bands = enhance.dark_object_subtraction(
            bands, percentile=float(req.get("haze_percentile", 1.0)),
            strength=float(req.get("haze_removal")))
        applied.append("haze removal")

    if float(req.get("denoise") or 0) > 0:
        bands = enhance.denoise(bands, float(req["denoise"]))
        applied.append("denoise")

    return bands


def _enhance_rgb(rgb: np.ndarray, req: dict, applied: list[str],
                 scale: int = 1, valid: np.ndarray | None = None) -> np.ndarray:
    """Corrections on the normalised 0-1 image, after the stretch.

    `scale` is how much finer than the satellite the image was sampled, and it
    matters to the sharpening: on a 3x merge the real structure sits about
    three pixels across, so sharpening at one pixel works on the interpolation
    rather than on the ground. Tying the radius to the scale sharpens what was
    actually recovered -- measured against known ground truth it adds 41% more
    fine detail than the fixed radius, at the same fidelity.
    """
    image = rgb.astype("float32") / 255.0
    # Everything below either measures the whole picture or spreads one pixel
    # into the next, and both go wrong on ground the satellite never saw.
    image = enhance.fill_invalid(image, valid)

    clip_limit = float(req.get("adaptive_contrast") or 0)
    if clip_limit > 0:
        image = enhance.apply_clahe_rgb(image, clip_limit=clip_limit,
                                        tiles=int(req.get("adaptive_tiles", 8)),
                                        strength=float(req.get("adaptive_strength", 1.0)),
                                        valid=valid)
        applied.append("adaptive contrast")

    if req.get("white_balance"):
        image = enhance.white_balance(image, float(req.get("white_balance")))
        applied.append("white balance")

    sharpen = float(req.get("sharpen") or 0)
    if sharpen > 0:
        radius = float(req.get("sharpen_radius") or max(1.2, 0.9 * scale))
        image = enhance.unsharp(image, amount=sharpen, radius=radius)
        applied.append("detail")

    vib = float(req.get("vibrance") or 0)
    if vib:
        image = enhance.vibrance(image, vib)
        applied.append("vibrance")

    return (np.clip(image, 0, 1) * 255).round().astype("uint8")


# ── Main render ────────────────────────────────────────────────


def render(req: dict) -> dict:
    geometry, grid = prepare(req)

    scenes = req.get("scenes") or [
        req.get("scene")
        or stac.get_scene(req["scene_id"], req.get("satellite"))
    ]
    if not scenes:
        raise RenderError("No date selected")

    sat = satellite_of(scenes)
    mode = req.get("mode", "composite")
    # A radar scene opens on a picture it can make. The satellite's default is
    # a VV/VH one, and over sea ice the instrument transmits HH instead -- so
    # for those the default used to be a picture out of bands the scene does
    # not carry, and the first thing anybody saw was "has no vv asset".
    preset = req.get("preset") or (
        sar.default_composite(scenes[0]) if sat["kind"] == "radar"
        else sat["default_composite"])
    index_name = req.get("index") or ("radar_ratio" if sat["kind"] == "radar" else "ndvi")
    names = _needed_bands(mode, preset, index_name, sat, scenes)
    # Checked here, against the scene, rather than discovered as a missing
    # asset four layers down. "This pass is HH+HV; radar colour needs VV" is
    # something a reader can act on; "Scene S1A_... has no vv asset" is not.
    # A picture the pass cannot draw is SWAPPED, not refused.
    #
    # It used to raise, and that was wrong twice over. The choice came from a
    # menu this app had put in front of somebody, so refusing it blamed them
    # for taking what was offered -- and it left them with no picture at all
    # when a perfectly good one of the same ground was one substitution away.
    # Seven identical red toasts and an empty panel is what that looked like.
    #
    # Said out loud, because a silent swap is a different lie: the picture
    # would not be the one that was asked for and nothing would say so.
    swapped = ""
    if sat["kind"] == "radar" and mode == "composite":
        short = sar.missing_for(scenes[0], names)
        if short:
            instead = sar.default_composite(scenes[0])
            swapped = (
                f"This pass carries {sar.pair_of(sar.polarisations(scenes[0]))},"
                f" so {config.COMPOSITES.get(preset, {}).get('label', preset)}"
                f" could not be drawn — shown as"
                f" {config.COMPOSITES.get(instead, {}).get('label', instead)}.")
            preset = instead
            names = _needed_bands(mode, preset, index_name, sat, scenes)

    applied: list[str] = []

    # Two passes and the difference between them, which is neither a picture
    # of one date nor an average of several. It does not go through _gather
    # at all: that exists to fold several dates into one, and folding is the
    # opposite of what this asks for -- the two have to stay apart to be
    # subtracted.
    if mode == "change":
        change, change_band_name, pair = change_pair(scenes, geometry, grid, req)
        rgb, valid, legend = composite.render_ramp(
            change, f"{config.CHANGE['label']} · {change_band_name.upper()}",
            config.CHANGE["range"], config.CHANGE["colormap"], req)
        if not valid.any():
            raise RenderError(
                "Neither pass covers this area — a radar swath is a slanted "
                "strip, so a catalogue can offer a date whose bounding box "
                "includes your shape while the strip itself misses it.")
        applied.append(f"{pair[1].get('date')} less {pair[0].get('date')}, "
                       f"in {change_band_name.upper()}")
        return _change_result(change, rgb, valid, legend, pair, change_band_name,
                              grid, geometry, sat, req, applied)

    bands, cloud_fraction, composite_report, sr_report, grid = _gather(
        scenes, geometry, grid, names, req, sat)
    if sr_report:
        applied.append(f"{sr_report['scale']}× merge of {sr_report['scenes']} dates")
    elif len(scenes) > 1:
        applied.append(
            f"speckle-averaged over {len(scenes)} passes" if sat["kind"] == "radar"
            else f"median merge of {len(scenes)} dates")

    bands = _enhance_bands(bands, req, applied, optical=sat["kind"] == "optical")

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
        rgb = _enhance_rgb(rgb, req, applied,
                           scale=sr_report["scale"] if sr_report else 1, valid=valid)

    # A radar swath is a slanted strip, so the catalogue can offer a pass whose
    # bounding box covers the area while the strip itself misses it. Saying so
    # beats handing back a picture of nothing.
    if not valid.any():
        raise RenderError(
            f"That {sat['short']} pass does not cover this area — nothing it "
            "measured falls inside your shape. Try another date.")

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
        "satellite": sat["key"],
        "source": satellite_meta(sat["key"], scenes[0].get("source")),
        "grid": grid.as_dict(),
        "mode": mode,
        "preset": preset if mode == "composite" else None,
        "index": index_name if mode == "index" else None,
        "label": label,
        "bands": names,
        "band_labels": [config.BANDS[b]["label"] for b in names],
        # What this pass actually was: which way it looked, which track, which
        # mode, which polarisations. Empty for anything that is not radar.
        "sar": sar.describe(scenes[0]),
        # And what is wrong with averaging these particular passes, if
        # anything. Said rather than refused -- somebody may want it anyway,
        # and being told is the difference between a choice and a surprise.
        "sar_merge": sar.merge_trouble(scenes),
        # What was shown instead of what was asked for, if anything.
        "sar_swapped": swapped,
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
        "native_res_m": sat["resolution"],
        "effective_res_m": round(
            sat["resolution"] / (sr_report["scale"] if sr_report else 1), 2),
        "cloud_masked_pct": round(cloud_fraction * 100, 2) if req.get("mask_clouds") else 0.0,
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "scene_area_km2": round(grid.width * grid.height * pixel_area / 1e6, 4),
        "demo": bool(scenes[0].get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


def _change_result(change, rgb, valid, legend, pair, band, grid, geometry,
                   sat, req, applied) -> dict:
    """The answer for a change render, in the shape every other render has.

    Its own function because a change has its own things to say -- which two
    dates, in which channel, how much of the frame moved -- and threading
    those through the shared metadata as a pile of Nones would make the
    ordinary path harder to read for the sake of this one.
    """
    older, newer = pair
    rgba = composite.to_rgba(rgb, valid)
    fmt = req.get("format", "png")
    if fmt == "geotiff":
        payload, media = composite.encode_geotiff(rgba, grid), "image/tiff"
    elif fmt == "float_geotiff":
        # The decibels themselves, for anybody who wants to measure rather
        # than look. The picture is a ramp over them and throws the number
        # away.
        payload, media = composite.encode_float_geotiff(change, grid), "image/tiff"
    elif fmt == "jpeg":
        payload, media = composite.encode_jpeg(rgba), "image/jpeg"
    else:
        payload, media = composite.encode_png(rgba), "image/png"

    span = config.CHANGE["range"][1]
    moved = float((np.abs(np.ma.filled(change, 0.0)) > span / 2)[valid].mean()) \
        if valid.any() else 0.0
    days = None
    try:
        days = abs((dt.date.fromisoformat(str(newer.get("date"))[:10])
                    - dt.date.fromisoformat(str(older.get("date"))[:10])).days)
    except (TypeError, ValueError):
        pass

    meta = {
        "scene": {k: v for k, v in newer.items() if k != "assets"},
        "scenes": [{"id": s["id"], "date": s.get("date"), "cloud": s.get("cloud")}
                   for s in (older, newer)],
        "satellite": sat["key"],
        "source": satellite_meta(sat["key"], newer.get("source")),
        "grid": grid.as_dict(),
        "mode": "change",
        "preset": None,
        "index": None,
        "label": f"{config.CHANGE['label']} · {band.upper()}",
        "bands": [band],
        "band_labels": [config.BANDS[band]["label"]],
        "sar": sar.describe(newer),
        # Nothing to warn about: a change between passes that are not
        # comparable is refused outright in change_pair, so by here they are.
        "sar_merge": "",
        "sar_swapped": "",
        "stretch": None,
        "legend": legend,
        "stats": composite.array_stats(change),
        "histogram": composite.histogram(change, span=config.CHANGE["range"]),
        "enhancements": applied,
        "composite_report": None,
        "superres": None,
        # What the two dates were and how far apart, because "brighter than
        # before" means nothing without "before when".
        "change": {
            "older": older.get("date"),
            "newer": newer.get("date"),
            "days": days,
            "band": band,
            "hint": config.CHANGE["hint"],
            # How much of the frame moved by more than half the ramp. Speckle
            # alone will not do that, so it is a fair headline number -- and
            # it is what somebody watching one town wants to know before
            # looking at anything else.
            "moved_pct": round(moved * 100, 2),
            # And what "moved" meant, so the figure can be read rather than
            # trusted. It is off the fixed ramp, not off the slider: a person
            # widening the scale is changing what they can see, not what
            # counts as a change.
            "moved_above_db": span / 2,
        },
        "native_res_m": sat["resolution"],
        "effective_res_m": sat["resolution"],
        "cloud_masked_pct": 0.0,
        "valid_pct": round(float(valid.mean()) * 100, 2),
        "aoi_area_km2": round(geodesic_area_km2(geometry), 4),
        "scene_area_km2": round(
            grid.width * grid.height * grid.ground_res_m ** 2 / 1e6, 4),
        "demo": bool(newer.get("demo")),
    }
    return {"bytes": payload, "media_type": media, "meta": meta}


# ── Reading one point ──────────────────────────────────────────

# What is worth reading at a point, per satellite. Not every band -- twelve
# HTTP reads to answer one click would be slow and most of them would say
# nothing new -- but enough to work out what is on the ground there.
PROBE_BANDS = {
    "optical": ["blue", "green", "red", "nir", "swir16"],
    "radar": ["vv", "vh"],
}

PROBE_INDICES = {
    "optical": ["ndvi", "ndwi", "ndbi"],
    "radar": ["radar_ratio"],
}


def probe(req: dict) -> dict:
    """What the satellite actually measured at one point.

    The picture on screen is a rendering: stretched, curved and coloured. This
    goes back to the numbers behind it, so a place that looks green can be
    asked how green, and in what units. It reads a small window around the
    point rather than a single pixel, which costs the same in HTTP range
    requests and lets it say how uniform the neighbourhood is.
    """
    lon, lat = float(req["lon"]), float(req["lat"])
    scenes = req.get("scenes") or [req.get("scene") or
                                   stac.get_scene(req["scene_id"], req.get("satellite"))]
    sat = satellite_of(scenes)

    # A window a few hundred metres across: wide enough to be a neighbourhood,
    # small enough to be one read.
    around = circle_to_polygon(lon, lat, max(sat["resolution"] * 15, 200))
    grid = Grid(geometry_bounds(around), config.MIN_SIZE)

    names = [b for b in PROBE_BANDS[sat["kind"]] if sat["key"] in config.BANDS[b]["sat"]]
    stacks = [raster.read_bands(s, grid, names)[0] for s in scenes]
    bands = enhance.composite(stacks, "median") if len(stacks) > 1 else stacks[0]

    row, col = grid.height // 2, grid.width // 2
    unit = "dB" if sat["kind"] == "radar" else "reflectance"
    values = []
    for name in names:
        patch = bands[name]
        here = patch[row, col]
        if here is np.ma.masked:
            values.append({"band": name, "label": config.BANDS[name]["label"],
                           "value": None, "unit": unit})
            continue
        values.append({
            "band": name,
            "label": config.BANDS[name]["label"],
            "value": round(float(here), 4),
            # How much the neighbourhood varies: a lone bright pixel and a
            # uniform field read the same on their own and are not the same.
            "spread": round(float(np.ma.std(patch)), 4),
            "unit": unit,
        })

    indices = []
    for name in PROBE_INDICES[sat["kind"]]:
        spec = config.INDICES[name]
        if any(b not in bands for b in spec["bands"]):
            continue
        arr = composite.compute_index(bands, name)
        here = arr[row, col]
        indices.append({
            "index": name,
            "label": spec["label"],
            "value": None if here is np.ma.masked else round(float(here), 4),
        })

    return {
        "point": [round(lon, 6), round(lat, 6)],
        "satellite": sat["key"],
        "source": satellite_meta(sat["key"], scenes[0].get("source")),
        "date": scenes[0].get("date"),
        "scenes": len(scenes),
        "ground_res_m": sat["resolution"],
        "unit": unit,
        "bands": values,
        "indices": indices,
        "demo": bool(scenes[0].get("demo")),
    }


def satellite_meta(key: str | None = None, source: str | None = None) -> dict:
    """Who took the picture — carried on every render for the credit line."""
    sat = config.satellite(key)
    meta = {k: sat[k] for k in
            ("key", "short", "label", "kind", "platform", "resolution",
             "units", "attribution", "provider")}
    # Radar can come from more than one catalogue, so credit the one it did.
    catalogue = config.SOURCES.get(source or "")
    if catalogue:
        meta["provider"] = catalogue["label"]
        meta["catalogue"] = catalogue["key"]
    return meta
