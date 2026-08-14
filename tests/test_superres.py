"""Multi-frame super-resolution: registration, fusion, restoration, plumbing.

The end-to-end tests stand local GeoTIFFs in for the remote assets, one per
"date", each written on a grid nudged by a fraction of a pixel -- which is what
repeat passes of the same orbit really look like, and the only reason fusing
several dates can recover detail that none of them carries alone.

    python -m pytest tests/test_superres.py -q
"""

from __future__ import annotations

import functools
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from scipy import ndimage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, service, superres  # noqa: E402
from backend.geo import Grid, circle_to_polygon  # noqa: E402

UTM = CRS.from_epsg(32630)
ORIGIN_X, ORIGIN_Y = 500000.0, 5700000.0
RESOLUTION = 10
SIZE = 512                                   # 5.12 km of synthetic tile


SUB = 4                                      # sub-samples per pixel, per axis


@functools.lru_cache(maxsize=1)
def ground_truth() -> np.ndarray:
    """The ground itself, on a grid four times finer than any date samples it.

    Textured rather than patterned: real land cover is, and a repeating pattern
    would make the alignment ambiguous in a way that says nothing about how the
    fusion behaves on imagery. The detail runs finer than one satellite pixel,
    which is what gives super-resolution something to find.
    """
    rng = np.random.default_rng(4)
    span = (SIZE + 2) * SUB
    field = ndimage.gaussian_filter(rng.random((span, span)).astype("float32"), 1.3)
    field += 0.6 * ndimage.gaussian_filter(rng.random((span, span)).astype("float32"), 22.0)
    field -= field.min()
    return (0.04 + 0.32 * field / field.max()).astype("float32")


def write_date(path: Path, offset_x: float, offset_y: float, seed: int) -> str:
    """One date's band: the same ground, sampled a fraction of a pixel away.

    Each pixel is the average of the ground over its own footprint, because
    that is what a detector measures -- and it is why moving the footprint by
    a quarter of a pixel returns a genuinely different number for the same
    ground, which is the whole basis of multi-frame super-resolution. The
    offsets are whole sub-samples, so every date is an exact view of one fixed
    ground truth rather than an interpolation of it.
    """
    oy, ox = int(round(offset_y * SUB)), int(round(offset_x * SUB))
    window = ground_truth()[oy:oy + SIZE * SUB, ox:ox + SIZE * SUB]
    data = window.reshape(SIZE, SUB, SIZE, SUB).mean(axis=(1, 3))
    rng = np.random.default_rng(seed)
    data = data + rng.normal(0, 0.004, data.shape).astype("float32")

    raw = np.clip(data / 1e-4, 1, 65000).astype("uint16")
    transform = Affine(RESOLUTION, 0, ORIGIN_X + offset_x * RESOLUTION,
                       0, -RESOLUTION, ORIGIN_Y - offset_y * RESOLUTION)
    with rasterio.open(
        path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1,
        dtype="uint16", crs=UTM, transform=transform, nodata=0,
        tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(raw, 1)
    return str(path)


@pytest.fixture(scope="module")
def dates(tmp_path_factory):
    """Six passes over the same ground, each landing on its own sub-pixel phase."""
    root = tmp_path_factory.mktemp("superres")
    offsets = [(0.0, 0.0), (0.5, 0.25), (0.25, 0.5), (0.75, 0.75), (0.5, 0.0), (0.0, 0.5)]
    scenes = []
    for i, (dx, dy) in enumerate(offsets):
        assets = {
            band: write_date(root / f"{band}_{i}.tif", dx, dy, seed=100 + i)
            for band in ("red", "green", "blue")
        }
        scenes.append({
            "id": f"DATE_{i}",
            "date": f"2023-0{i + 1}-15",
            "datetime": f"2023-0{i + 1}-15T11:00:00Z",
            "cloud": 0.0,
            "platform": "sentinel-2a",
            "boa_offset_applied": True,
            "demo": False,
            "assets": assets,
        })
    return scenes


@pytest.fixture(scope="module")
def aoi():
    from rasterio.warp import transform

    span = SIZE * RESOLUTION
    lons, lats = transform(UTM, CRS.from_epsg(4326),
                           [ORIGIN_X + span / 2], [ORIGIN_Y - span / 2])
    return circle_to_polygon(lons[0], lats[0], 900)


def _masked(array: np.ndarray) -> np.ma.MaskedArray:
    return np.ma.masked_array(array.astype("float32"),
                              mask=np.zeros(array.shape, dtype=bool))


# ── Registration ───────────────────────────────────────────────


@pytest.fixture(scope="module")
def textured():
    rng = np.random.default_rng(7)
    return ndimage.gaussian_filter(rng.random((192, 192)).astype("float32"), 2.0)


@pytest.mark.parametrize("shift", [(0.0, 0.0), (1.4, -2.35), (-0.6, 0.3), (3.0, 2.0)])
def test_registration_recovers_a_known_sub_pixel_shift(textured, shift):
    moved = ndimage.shift(textured, shift, order=3, mode="wrap")
    dy, dx = superres.estimate_shift(_masked(textured), _masked(moved), cutoff=0.2)
    # The answer is the correction, so applying it undoes the offset.
    assert dy == pytest.approx(-shift[0], abs=0.15)
    assert dx == pytest.approx(-shift[1], abs=0.15)


def test_registration_ignores_a_difference_in_brightness(textured):
    """Two dates are never exposed the same; only the geometry may be compared."""
    hazier = textured * 0.7 + 0.05
    moved = ndimage.shift(hazier, (0.8, -1.2), order=3, mode="wrap")
    dy, dx = superres.estimate_shift(_masked(textured), _masked(moved), cutoff=0.2)
    assert (dy, dx) == pytest.approx((-0.8, 1.2), abs=0.15)


def test_registration_refuses_an_implausible_answer(textured):
    """A date that does not match must contribute unshifted, not smeared."""
    moved = ndimage.shift(textured, (20.0, 20.0), order=3, mode="wrap")
    assert superres.estimate_shift(_masked(textured), _masked(moved),
                                   max_shift=4.0) == (0.0, 0.0)


def test_alignment_is_measured_below_the_native_nyquist(textured):
    """Aliasing turns with the sampling phase, not the ground: it must be ignored.

    Both frames here are honest resamplings of the same ground onto the same
    grid, differing only in the sub-pixel phase they were sampled at. Anything
    that chases the aliased high frequencies reports a shift between them; the
    truth is that there is none.
    """
    def sample(offset):
        blurred = ndimage.gaussian_filter(textured, 0.9)
        coarse = ndimage.map_coordinates(
            blurred, np.meshgrid(np.arange(0, 192, 2) + offset,
                                 np.arange(0, 192, 2) + offset, indexing="ij"),
            order=1, mode="nearest")
        fine = (np.arange(192) - offset) / 2.0
        return ndimage.map_coordinates(coarse, np.meshgrid(fine, fine, indexing="ij"),
                                       order=1, mode="nearest").astype("float32")

    dy, dx = superres.estimate_shift(_masked(sample(0.0)), _masked(sample(0.9)),
                                     cutoff=0.08)
    assert (dy, dx) == pytest.approx((0.0, 0.0), abs=0.15)


def test_shifting_a_band_moves_its_mask_with_it():
    data = np.zeros((32, 32), dtype="float32")
    mask = np.zeros((32, 32), dtype=bool)
    mask[10:14, 10:14] = True
    moved = superres.shift_band(np.ma.masked_array(data, mask), 3.0, 2.0)
    assert np.ma.getmaskarray(moved)[13:17, 12:16].all()
    assert not np.ma.getmaskarray(moved)[10:12, 10:12].any()


# ── Fusion ─────────────────────────────────────────────────────


def test_robust_mean_rejects_a_date_the_cloud_mask_missed():
    clear = [np.ma.masked_array(np.full((16, 16), 0.20, dtype="float32"),
                                np.zeros((16, 16), bool)) for _ in range(4)]
    cloudy = np.ma.masked_array(np.full((16, 16), 0.85, dtype="float32"),
                                np.zeros((16, 16), bool))
    merged, kept = superres.robust_mean(np.ma.stack([*clear, cloudy]))
    assert float(np.mean(merged)) == pytest.approx(0.20, abs=1e-3)
    assert int(np.median(kept)) == 4


def test_robust_mean_averages_the_dates_that_agree():
    """Agreement is the point: N honest looks beat one by root N in noise."""
    rng = np.random.default_rng(11)
    layers = np.ma.stack([
        np.ma.masked_array((0.30 + rng.normal(0, 0.05, (64, 64))).astype("float32"),
                           np.zeros((64, 64), bool))
        for _ in range(9)
    ])
    merged, kept = superres.robust_mean(layers)
    assert float(np.std(merged)) < float(np.std(layers[0])) / 2
    assert int(np.median(kept)) >= 7


def test_a_pixel_survives_if_any_single_date_saw_it():
    frames = []
    for i in range(3):
        mask = np.zeros((8, 8), dtype=bool)
        mask[i, :] = True                     # each date loses a different row
        mask[7, :] = True                     # and all of them lose the last
        frames.append(np.ma.masked_array(np.full((8, 8), 0.4, dtype="float32"), mask))
    merged, _ = superres.robust_mean(np.ma.stack(frames))
    assert not np.ma.getmaskarray(merged)[0:3, :].any()
    assert np.ma.getmaskarray(merged)[7, :].all()


def test_deconvolution_sharpens_without_ringing():
    edge = np.zeros((64, 64), dtype="float32")
    edge[:, 32:] = 1.0
    blurred = ndimage.gaussian_filter(edge, 1.5)
    restored = superres.deconvolve(blurred, sigma=1.5, amount=0.8, iterations=4)

    def edge_width(img):
        profile = img[32]
        return float(np.sum((profile > 0.05) & (profile < 0.95)))

    assert edge_width(restored) < edge_width(blurred)
    # The overshoot clamp is what separates recovered detail from a drawn-on halo.
    assert restored.min() > -0.3 and restored.max() < 1.3


def test_deconvolution_of_a_flat_field_changes_nothing():
    flat = np.full((32, 32), 0.25, dtype="float32")
    assert superres.deconvolve(flat, sigma=1.0, amount=0.8) == pytest.approx(flat)


def test_fusing_one_scene_is_a_no_op():
    bands = {"red": _masked(np.full((8, 8), 0.2))}
    fused, report = superres.fuse([bands], scale=2)
    assert fused is bands and report == {}


# ── Does it actually recover detail? ───────────────────────────


def _simulate(scale: int, offsets, size: int = 192):
    """Frames of one scene, each sampled at its own sub-pixel phase.

    This mirrors what the reader does for real: every date is warped onto the
    same fine grid from its own coarser native grid, so each arrives already
    positioned but carrying a different set of ground samples.
    """
    rng = np.random.default_rng(5)
    truth = ndimage.gaussian_filter(rng.random((size, size)).astype("float32"), 1.5)
    frames = []
    for oy, ox in offsets:
        blurred = ndimage.gaussian_filter(truth, 0.45 * scale)
        coarse = ndimage.map_coordinates(
            blurred, np.meshgrid(np.arange(0, size, scale) + oy,
                                 np.arange(0, size, scale) + ox, indexing="ij"),
            order=1, mode="nearest")
        coarse = coarse + rng.normal(0, 0.004, coarse.shape)
        fine_y = (np.arange(size) - oy) / scale
        fine_x = (np.arange(size) - ox) / scale
        frames.append({"red": _masked(ndimage.map_coordinates(
            coarse, np.meshgrid(fine_y, fine_x, indexing="ij"),
            order=1, mode="nearest"))})
    return truth, frames


@pytest.mark.parametrize("scale", [2, 3])
def test_fusion_is_closer_to_the_truth_than_any_single_date(scale):
    offsets = [(0, 0), (0.9, 0.4), (0.5, 1.3), (1.4, 1.7), (0.2, 1.1), (1.1, 0.2)]
    truth, frames = _simulate(scale, offsets)
    fused, report = superres.fuse(frames, scale=scale)

    def error(image):
        inner = slice(16, -16)
        return float(np.sqrt(np.mean(
            (np.ma.filled(image, 0.0)[inner, inner] - truth[inner, inner]) ** 2)))

    assert error(fused["red"]) < error(frames[0]["red"]) * 0.9
    assert report["sharpness_gain_pct"] > 0
    assert report["noise_drop_pct"] > 0


def test_fusion_reports_what_it_did():
    offsets = [(0, 0), (0.9, 0.4), (0.5, 1.3), (1.4, 1.7)]
    _, frames = _simulate(2, offsets)
    _, report = superres.fuse(frames, scale=2,
                              dates=["2023-01-01", "2023-02-01", "2023-03-01", "2023-04-01"])
    assert report["scale"] == 2 and report["scenes"] == 4
    assert report["frames_wanted"] == 4 and report["well_supported"] is True
    assert [s["date"] for s in report["shifts"]][0] == "2023-01-01"
    assert report["shifts"][0]["reference"] is True
    assert report["samples_per_pixel"] > 1


def test_a_thin_stack_is_reported_as_under_supported():
    _, frames = _simulate(3, [(0, 0), (0.5, 0.5)])
    _, report = superres.fuse(frames, scale=3)
    assert report["frames_wanted"] == 9
    assert report["well_supported"] is False


# ── Grid and clamping ──────────────────────────────────────────


def test_a_refined_grid_covers_the_same_ground_more_finely():
    grid = Grid((-0.1, 51.4, 0.1, 51.6), 512)
    fine = grid.refined(3)
    assert (fine.width, fine.height) == (grid.width * 3, grid.height * 3)
    assert fine.bounds3857 == grid.bounds3857
    assert fine.ground_res_m == pytest.approx(grid.ground_res_m / 3, rel=1e-6)
    assert grid.refined(1) is grid


def test_the_scale_is_clamped_to_what_the_output_can_hold():
    scenes = [{"id": "a"}, {"id": "b"}]
    small = Grid((-0.1, 51.4, 0.1, 51.6), 512)
    assert service.superres_scale({"superres": 4}, small, scenes) == 4
    big = Grid((-0.1, 51.4, 0.1, 51.6), config.MAX_SIZE // 2)
    assert service.superres_scale({"superres": 4}, big, scenes) == 2
    # Beyond the honest limit, and with nothing to fuse, it stays switched off.
    assert service.superres_scale({"superres": 9}, small, scenes) == config.MAX_SUPERRES
    assert service.superres_scale({"superres": 3}, small, scenes[:1]) == 1
    assert service.superres_scale({}, small, scenes) == 1


# ── End to end ─────────────────────────────────────────────────


def test_render_fuses_the_dates_onto_a_finer_grid(dates, aoi):
    result = service.render({
        "aoi": aoi, "scenes": dates, "scene": dates[0],
        "preset": "true_color", "size": 256, "superres": 2, "clip": False,
    })
    meta = result["meta"]
    assert meta["grid"]["width"] == 512                    # 256 requested, 2x fused
    assert meta["grid"]["ground_res_m"] < RESOLUTION
    report = meta["superres"]
    assert report["scale"] == 2 and report["scenes"] == len(dates)
    assert report["sub_pixel_dates"] >= 1                  # the passes really differ
    assert any("super-resolution" in e for e in meta["enhancements"])
    assert result["media_type"] == "image/png"


def test_render_without_super_resolution_keeps_the_requested_grid(dates, aoi):
    result = service.render({
        "aoi": aoi, "scenes": dates, "scene": dates[0],
        "preset": "true_color", "size": 256, "clip": False,
    })
    assert result["meta"]["grid"]["width"] == 256
    assert result["meta"]["superres"] is None
    assert result["meta"]["composite_report"]["scenes"] == len(dates)


def test_fused_imagery_resolves_more_of_the_ground_than_one_date(dates, aoi):
    """The point of the whole exercise, measured against the pattern on the ground."""
    from backend.geo import geometry_bounds

    grid = Grid(geometry_bounds(aoi), 256).refined(2)
    request = {"aoi": aoi, "mask_clouds": False}
    single, _ = service.load_bands(dates[0], aoi, grid, ["red"], request)
    fused, _ = superres.fuse(
        [service.load_bands(scene, aoi, grid, ["red"], request)[0] for scene in dates],
        scale=2)

    inner = (slice(16, -16), slice(16, -16))
    detail = lambda band: float(np.std(              # noqa: E731
        np.ma.filled(band, 0.0)[inner]
        - ndimage.gaussian_filter(np.ma.filled(band, 0.0), 1.0)[inner]))
    assert detail(fused["red"]) > detail(single["red"]) * 1.1
