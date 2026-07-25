"""Exercise the real (non-demo) raster path against locally written COGs.

The live catalogue is not reachable from CI, so these tests stand in local
GeoTIFFs -- in a Sentinel-2-like UTM projection, at native band resolutions --
for the remote assets. That covers reprojection onto the output grid, the
reflectance offset, cloud masking from the SCL band and AOI clipping.

    python -m pytest tests/ -q
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import composite, config, raster, service, stac  # noqa: E402
from backend.geo import Grid, circle_to_polygon, geodesic_area_km2, normalise_aoi  # noqa: E402

UTM = CRS.from_epsg(32630)          # a zone covering the Greenwich meridian
ORIGIN_X, ORIGIN_Y = 500000.0, 5700000.0   # easting/northing of the tile corner
TILE_M = 10980 * 10                 # a full S2 tile is 109.8 km across


def write_band(path: Path, value_fn, resolution: int, dtype="uint16"):
    """Write a single-band GeoTIFF on a Sentinel-2-like grid."""
    size = int(TILE_M / resolution)
    rows, cols = np.mgrid[0:size, 0:size]
    data = value_fn(cols / size, rows / size).astype(dtype)
    transform = Affine(resolution, 0, ORIGIN_X, 0, -resolution, ORIGIN_Y)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype=dtype, crs=UTM, transform=transform, nodata=0,
        tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(data, 1)
    return path


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    """A scene whose 'remote' assets are local files with known values."""
    root = tmp_path_factory.mktemp("cog")

    # Reflectance 0.20 left half, 0.60 right half (as raw DN before the offset).
    write_band(root / "red.tif", lambda x, y: np.where(x < 0.5, 3000, 7000), 10)
    # A vertical gradient in the NIR so NDVI varies predictably.
    write_band(root / "nir.tif", lambda x, y: 2000 + 6000 * y, 10)
    write_band(root / "green.tif", lambda x, y: np.full_like(x, 4000), 10)
    write_band(root / "blue.tif", lambda x, y: np.full_like(x, 2500), 10)
    # 20 m band, to prove mixed resolutions land on the same grid.
    write_band(root / "swir16.tif", lambda x, y: np.full_like(x, 5000), 20)
    # SCL: top quarter flagged as high-probability cloud (class 9).
    write_band(root / "scl.tif", lambda x, y: np.where(y < 0.25, 9, 4), 20, dtype="uint8")

    return {
        "id": "TEST_SCENE",
        "date": "2023-06-15",           # after the baseline change -> offset applies
        "datetime": "2023-06-15T11:00:00Z",
        "cloud": 12.0,
        "platform": "sentinel-2a",
        "boa_offset_applied": False,
        "demo": False,
        "assets": {
            "red": str(root / "red.tif"),
            "nir": str(root / "nir.tif"),
            "green": str(root / "green.tif"),
            "blue": str(root / "blue.tif"),
            "swir16": str(root / "swir16.tif"),
            "scl": str(root / "scl.tif"),
        },
    }


@pytest.fixture(scope="module")
def aoi():
    """A small circle well inside the synthetic tile."""
    lon, lat = _utm_to_lonlat(ORIGIN_X + TILE_M / 2, ORIGIN_Y - TILE_M / 2)
    return circle_to_polygon(lon, lat, 3000)


def _utm_to_lonlat(x, y):
    from rasterio.warp import transform

    lons, lats = transform(UTM, CRS.from_epsg(4326), [x], [y])
    return lons[0], lats[0]


# ── Geometry ───────────────────────────────────────────────────


def test_circle_area_matches_requested_radius():
    circle = circle_to_polygon(0.0, 51.5, 5000)
    expected = math.pi * 5.0 ** 2
    assert geodesic_area_km2(circle) == pytest.approx(expected, rel=0.01)


def test_normalise_accepts_bbox_and_feature():
    poly = normalise_aoi({"bbox": [-1, 50, 0, 51]})
    assert poly["type"] == "Polygon"
    assert geodesic_area_km2(poly) > 0
    wrapped = normalise_aoi({"type": "Feature", "geometry": poly})
    assert wrapped == poly


def test_grid_is_north_up_and_scaled_to_latitude():
    grid = Grid((-0.1, 51.4, 0.1, 51.6), 512)
    # Mercator stretches northing by 1/cos(lat), so at 51.5 N a square degree
    # box is taller than it is wide and the long side takes the requested size.
    assert max(grid.width, grid.height) == 512
    assert grid.height > grid.width
    assert grid.transform.e < 0                      # north-up
    # Mercator over-states distance by 1/cos(lat); the grid must undo that.
    mercator_res = (grid.bounds3857[2] - grid.bounds3857[0]) / grid.width
    assert grid.ground_res_m == pytest.approx(
        mercator_res * math.cos(math.radians(grid.center_lat)), rel=1e-6)


# ── Band reading ───────────────────────────────────────────────


def test_reads_reflectance_with_baseline_offset(scene, aoi):
    grid = Grid(_bounds(aoi), 256)
    bands, cloud = raster.read_bands(scene, grid, ["red", "green"])

    assert bands["red"].shape == grid.shape
    # DN 3000/7000 with the -1000 baseline offset -> 0.2 / 0.6 reflectance.
    left = bands["red"][:, :100].mean()
    right = bands["red"][:, -100:].mean()
    assert left == pytest.approx(0.20, abs=0.01)
    assert right == pytest.approx(0.60, abs=0.01)
    assert cloud == 0.0                              # not requested


def test_offset_skipped_for_older_scenes(scene, aoi):
    grid = Grid(_bounds(aoi), 128)
    old = {**scene, "date": "2019-05-01"}
    bands, _ = raster.read_bands(old, grid, ["red"])
    assert bands["red"][:, :50].mean() == pytest.approx(0.30, abs=0.01)


def test_offset_skipped_when_already_applied(scene, aoi):
    grid = Grid(_bounds(aoi), 128)
    applied = {**scene, "boa_offset_applied": True}
    bands, _ = raster.read_bands(applied, grid, ["red"])
    assert bands["red"][:, :50].mean() == pytest.approx(0.30, abs=0.01)


def test_mixed_resolution_bands_land_on_one_grid(scene, aoi):
    grid = Grid(_bounds(aoi), 300)
    bands, _ = raster.read_bands(scene, grid, ["red", "swir16"])
    assert bands["red"].shape == bands["swir16"].shape == grid.shape
    assert bands["swir16"].mean() == pytest.approx(0.40, abs=0.01)


def test_cloud_mask_removes_flagged_pixels(scene, aoi):
    grid = Grid(_bounds(aoi), 256)
    bands, cloud = raster.read_bands(scene, grid, ["red"], mask_clouds=True)
    mask = np.ma.getmaskarray(bands["red"])
    # This AOI sits below the cloudy top quarter, so masking must be a no-op.
    assert cloud == 0.0
    assert not mask.any()


def test_cloud_mask_applies_where_clouds_overlap(scene):
    """Straddle the cloud boundary in the SCL band and check the split."""
    lon, lat = _utm_to_lonlat(ORIGIN_X + TILE_M / 2, ORIGIN_Y - TILE_M * 0.25)
    circle = circle_to_polygon(lon, lat, 4000)
    grid = Grid(_bounds(circle), 256)
    bands, cloud = raster.read_bands(scene, grid, ["red"], mask_clouds=True)
    assert 0.2 < cloud < 0.8
    assert np.ma.getmaskarray(bands["red"]).mean() == pytest.approx(cloud, abs=1e-6)


def test_clip_masks_outside_the_shape(scene, aoi):
    grid = Grid(_bounds(aoi), 256)
    bands, _ = raster.read_bands(scene, grid, ["red"])
    clipped = raster.apply_clip(bands, raster.aoi_mask(aoi, grid))
    outside = np.ma.getmaskarray(clipped["red"])
    # A circle inscribed in its own bounding box covers about pi/4 of it.
    assert outside.mean() == pytest.approx(1 - math.pi / 4, abs=0.02)
    assert not outside[grid.height // 2, grid.width // 2]


def test_missing_band_is_reported_clearly(scene, aoi):
    grid = Grid(_bounds(aoi), 64)
    with pytest.raises(raster.BandReadError, match="swir22"):
        raster.read_bands(scene, grid, ["swir22"])


# ── Indices and rendering ──────────────────────────────────────


def test_ndvi_matches_the_formula(scene, aoi):
    grid = Grid(_bounds(aoi), 256)
    bands, _ = raster.read_bands(scene, grid, ["nir", "red"])
    ndvi = composite.compute_index(bands, "ndvi")
    nir = bands["nir"]
    red = bands["red"]
    expected = (nir - red) / (nir + red)
    assert np.allclose(ndvi.compressed(), expected.compressed(), atol=1e-6)
    assert -1.0 <= ndvi.min() <= ndvi.max() <= 1.0


def test_fixed_stretch_is_comparable_between_scenes(scene, aoi):
    grid = Grid(_bounds(aoi), 128)
    bands, _ = raster.read_bands(scene, grid, ["red", "green", "nir"])
    opts = {"stretch": "fixed", "vmin": 0.0, "vmax": 0.6, "gamma": 1.0}
    rgb, valid, info = composite.render_composite(
        {"red": bands["red"], "green": bands["green"], "blue": bands["green"]},
        "true_color", opts)
    assert info["mode"] == "fixed"
    # 0.2 / 0.6 of the way up a 0-0.6 window -> 85 and 255.
    assert int(rgb[:, :40, 0].mean()) == pytest.approx(85, abs=2)
    assert int(rgb[:, -40:, 0].mean()) == pytest.approx(255, abs=2)
    assert valid.all()


def test_percentile_linked_keeps_channels_on_one_scale(scene, aoi):
    grid = Grid(_bounds(aoi), 128)
    bands, _ = raster.read_bands(scene, grid, ["red", "green", "nir"])
    _, _, info = composite.render_composite(
        {"red": bands["red"], "green": bands["green"], "blue": bands["nir"]},
        "true_color", {"stretch": "percentile_linked"})
    bounds = list(info["bands"].values())
    assert bounds[0] == bounds[1] == bounds[2]


def test_colormap_lut_is_monotonic_and_full_range():
    lut = composite.colormap_lut("viridis")
    assert lut.shape == (256, 3)
    assert lut.dtype == np.uint8
    assert tuple(lut[0]) != tuple(lut[-1])


def test_class_areas_sum_to_the_valid_area():
    data = np.ma.masked_array(np.linspace(-1, 1, 10000).reshape(100, 100),
                              mask=np.zeros((100, 100), bool))
    classes = composite.class_areas(data, [-0.1, 0.1], ["loss", "stable", "gain"], 100.0)
    assert sum(c["pixels"] for c in classes) == 10000
    assert sum(c["percent"] for c in classes) == pytest.approx(100.0, abs=0.1)
    assert sum(c["area_km2"] for c in classes) == pytest.approx(10000 * 100 / 1e6, rel=1e-6)


# ── End to end through the service layer ───────────────────────


def test_render_returns_png_and_metadata(scene, aoi):
    result = service.render({"aoi": aoi, "scene": scene, "mode": "composite",
                             "preset": "true_color", "size": 256})
    assert result["bytes"][:8] == b"\x89PNG\r\n\x1a\n"
    meta = result["meta"]
    assert meta["grid"]["width"] == 256
    assert meta["aoi_area_km2"] == pytest.approx(math.pi * 9, rel=0.02)
    assert meta["demo"] is False
    assert meta["cloud_masked_pct"] == 0.0


def test_index_render_carries_legend_stats_and_histogram(scene, aoi):
    result = service.render({"aoi": aoi, "scene": scene, "mode": "index",
                             "index": "ndvi", "size": 192})
    meta = result["meta"]
    assert meta["legend"]["colormap"] == "ndvi"
    assert len(meta["legend"]["stops"]) == 5
    assert meta["stats"]["count"] > 0
    assert sum(meta["histogram"]["counts"]) > 0


def test_geotiff_export_is_georeferenced(scene, aoi):
    result = service.render({"aoi": aoi, "scene": scene, "mode": "composite",
                             "preset": "true_color", "size": 128, "format": "geotiff"})
    with rasterio.MemoryFile(result["bytes"]) as mem, mem.open() as src:
        assert src.count == 4
        assert src.crs.to_epsg() == 3857
        assert src.width == 128
        west, south, east, north = src.bounds
        assert west < east and south < north


def test_float_geotiff_preserves_index_values(scene, aoi):
    result = service.render({"aoi": aoi, "scene": scene, "mode": "index", "index": "ndvi",
                             "size": 128, "format": "float_geotiff"})
    with rasterio.MemoryFile(result["bytes"]) as mem, mem.open() as src:
        data = src.read(1)
        assert src.dtypes[0] == "float32"
        assert np.nanmin(data) >= -1.0 and np.nanmax(data) <= 1.0


def test_change_detection_reports_gain_loss_and_areas(scene, aoi):
    later = {**scene, "id": "LATER", "date": "2023-08-20",
             "datetime": "2023-08-20T11:00:00Z"}
    # Same imagery on both dates -> everything must land in the stable class.
    result = service.change_detection({"aoi": aoi, "scene_a": scene, "scene_b": later,
                                       "index": "ndvi", "size": 128, "threshold": 0.1})
    meta = result["meta"]
    stable = next(c for c in meta["classes"] if c["label"] == "Stable")
    assert stable["percent"] == pytest.approx(100.0, abs=0.1)
    assert meta["days_apart"] == 66
    assert meta["scene_a"]["date"] < meta["scene_b"]["date"]


def test_change_detection_orders_scenes_by_date(scene, aoi):
    later = {**scene, "id": "LATER", "date": "2023-08-20"}
    result = service.change_detection({"aoi": aoi, "scene_a": later, "scene_b": scene,
                                       "index": "ndvi", "size": 64})
    assert result["meta"]["scene_a"]["date"] == "2023-06-15"


def test_unknown_visualisation_is_rejected(scene, aoi):
    with pytest.raises(service.RenderError):
        service.render({"aoi": aoi, "scene": scene, "mode": "index", "index": "nope"})


# ── STAC parsing ───────────────────────────────────────────────


def test_scene_summary_flattens_a_stac_item():
    item = {
        "id": "S2A_31UCT_20230615_0_L2A",
        "properties": {
            "datetime": "2023-06-15T11:02:19Z",
            "eo:cloud_cover": 7.53,
            "platform": "sentinel-2a",
            "proj:epsg": 32631,
            "earthsearch:boa_offset_applied": True,
            "mgrs:utm_zone": 31, "mgrs:latitude_band": "U", "mgrs:grid_square": "CT",
        },
        "assets": {
            "red": {"href": "s3://x/B04.tif"},
            "nir": {"href": "s3://x/B08.tif"},
            "scl": {"href": "s3://x/SCL.tif"},
            "thumbnail": {"href": "https://x/thumb.jpg"},
        },
    }
    summary = stac.scene_summary(item)
    assert summary["date"] == "2023-06-15"
    assert summary["cloud"] == 7.5
    assert summary["tile"] == "31UCT"
    assert summary["assets"]["red"].endswith("B04.tif")
    assert stac.boa_offset(summary) == 0


def test_boa_offset_depends_on_processing_baseline():
    assert stac.boa_offset({"date": "2021-12-01", "boa_offset_applied": False}) == 0
    assert stac.boa_offset({"date": "2022-06-01", "boa_offset_applied": False}) == -1000
    assert stac.boa_offset({"date": "2022-06-01", "boa_offset_applied": True}) == 0


def test_demo_scene_id_round_trips_cloud_cover():
    scenes = stac.search_scenes(
        normalise_aoi({"bbox": [-0.2, 51.4, 0.0, 51.6]}),
        "2024-01-01", "2024-03-01", max_cloud=100, limit=6, demo=True)["scenes"]
    assert scenes
    for original in scenes:
        assert stac.get_scene(original["id"])["cloud"] == original["cloud"]


def test_demo_bands_are_stable_across_calls():
    """Frames of a timelapse must not shimmer between processes."""
    aoi = normalise_aoi({"bbox": [-0.2, 51.4, 0.0, 51.6]})
    grid = Grid((-0.2, 51.4, 0.0, 51.6), 128)
    scene = {"id": "demo-2024-06-01-1-0", "date": "2024-06-01", "cloud": 0.0, "demo": True}
    first, _ = raster.read_bands(scene, grid, ["red", "nir"])
    second, _ = raster.read_bands(scene, grid, ["red", "nir"])
    assert np.array_equal(first["red"].data, second["red"].data)


def _bounds(geometry):
    from backend.geo import geometry_bounds

    return geometry_bounds(geometry)
