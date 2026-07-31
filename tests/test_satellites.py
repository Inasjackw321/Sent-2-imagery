"""Multi-satellite reading and the image-quality tools.

Landsat, radar and elevation sources are stood in by local GeoTIFFs written to
match each satellite's real storage convention -- Landsat's scale/offset, its
QA_PIXEL bitfield, radar amplitude, metres of elevation -- so the conversion
and masking code is exercised for real rather than mocked.
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

from backend import composite, config, enhance, raster, service, sources  # noqa: E402
from backend.geo import Grid, circle_to_polygon, geometry_bounds  # noqa: E402

UTM = CRS.from_epsg(32630)
ORIGIN_X, ORIGIN_Y = 500000.0, 5700000.0
EXTENT = 60000.0


def write(path: Path, value_fn, resolution: float, dtype="uint16"):
    size = int(EXTENT / resolution)
    rows, cols = np.mgrid[0:size, 0:size]
    data = np.asarray(value_fn(cols / size, rows / size)).astype(dtype)
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1, dtype=dtype,
        crs=UTM, transform=Affine(resolution, 0, ORIGIN_X, 0, -resolution, ORIGIN_Y),
        nodata=0, tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(data, 1)
    return str(path)


def _lonlat(x, y):
    from rasterio.warp import transform

    lons, lats = transform(UTM, CRS.from_epsg(4326), [x], [y])
    return lons[0], lats[0]


@pytest.fixture(scope="module")
def aoi():
    lon, lat = _lonlat(ORIGIN_X + EXTENT / 2, ORIGIN_Y - EXTENT / 2)
    return circle_to_polygon(lon, lat, 4000)


# ── Landsat ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def landsat(tmp_path_factory):
    """Landsat Collection 2: DN * 0.0000275 - 0.2, 30 m bands, 15 m pan."""
    root = tmp_path_factory.mktemp("landsat")

    def dn(reflectance):
        return (reflectance + 0.2) / 2.75e-5

    assets = {
        "red": write(root / "red.tif", lambda x, y: np.full_like(x, dn(0.25)), 30, "uint16"),
        "green": write(root / "green.tif", lambda x, y: np.full_like(x, dn(0.20)), 30, "uint16"),
        "blue": write(root / "blue.tif", lambda x, y: np.full_like(x, dn(0.15)), 30, "uint16"),
        "nir08": write(root / "nir.tif", lambda x, y: np.full_like(x, dn(0.40)), 30, "uint16"),
        "swir16": write(root / "swir.tif", lambda x, y: np.full_like(x, dn(0.30)), 30, "uint16"),
        # Fine stripes only the 15 m pan band can resolve.
        "pan": write(root / "pan.tif",
                     lambda x, y: dn(0.22 + 0.12 * np.sin(x * 240)), 15, "uint16"),
        # QA_PIXEL: bit 3 (cloud) set over the top third.
        "qa_pixel": write(root / "qa.tif",
                          lambda x, y: np.where(y < 0.33, 1 << 3, 1 << 6), 30, "uint16"),
    }
    return {
        "id": "LC09_TEST", "source": "landsat-c2-l2", "date": "2023-07-04",
        "datetime": "2023-07-04T11:00:00Z", "cloud": 14.0, "platform": "landsat-9",
        "boa_offset_applied": False, "demo": False, "assets": assets,
    }


def test_landsat_scaling_gives_real_reflectance(landsat, aoi):
    grid = Grid(geometry_bounds(aoi), 256)
    bands, _ = raster.read_bands(landsat, grid, ["red", "green", "blue", "nir"],
                                 source=sources.get("landsat-c2-l2"))
    assert bands["red"].mean() == pytest.approx(0.25, abs=0.005)
    assert bands["green"].mean() == pytest.approx(0.20, abs=0.005)
    assert bands["blue"].mean() == pytest.approx(0.15, abs=0.005)
    # 'nir' is an alias onto Landsat's nir08 asset.
    assert bands["nir"].mean() == pytest.approx(0.40, abs=0.005)


def test_landsat_does_not_get_the_sentinel_offset(landsat, aoi):
    """The -1000 offset is a Sentinel-2 quirk; applying it to Landsat would wreck it."""
    grid = Grid(geometry_bounds(aoi), 128)
    bands, _ = raster.read_bands(landsat, grid, ["red"],
                                 source=sources.get("landsat-c2-l2"))
    assert bands["red"].mean() == pytest.approx(0.25, abs=0.005)


def test_qa_pixel_bitmask_masks_cloud(landsat):
    lon, lat = _lonlat(ORIGIN_X + EXTENT / 2, ORIGIN_Y - EXTENT * 0.33)
    grid = Grid(geometry_bounds(circle_to_polygon(lon, lat, 5000)), 256)
    bands, cloud = raster.read_bands(landsat, grid, ["red"],
                                     source=sources.get("landsat-c2-l2"),
                                     mask_clouds=True)
    assert 0.2 < cloud < 0.8
    assert np.ma.getmaskarray(bands["red"]).mean() == pytest.approx(cloud, abs=1e-6)


def test_qa_pixel_ignores_bits_that_are_not_cloud(landsat, aoi):
    """Bit 6 (clear) is set everywhere outside the cloud band — it must not mask."""
    grid = Grid(geometry_bounds(aoi), 128)
    _, cloud = raster.read_bands(landsat, grid, ["red"],
                                 source=sources.get("landsat-c2-l2"), mask_clouds=True)
    assert cloud == 0.0


def test_pansharpening_adds_detail_from_the_pan_band(landsat, aoi):
    """The 15 m pan band carries stripes the 30 m bands cannot see."""
    grid = Grid(geometry_bounds(aoi), 512)
    source = sources.get("landsat-c2-l2")
    bands, _ = raster.read_bands(landsat, grid, ["red", "green", "blue"], source=source)
    pan, _ = raster.read_bands(landsat, grid, ["pan"], source=source)

    plain_variation = float(bands["red"].std())
    sharpened = enhance.pansharpen(bands, pan["pan"], ["red", "green", "blue"], strength=1.0)
    assert float(sharpened["red"].std()) > plain_variation * 5
    # Hue must survive: all three bands gain the same detail.
    assert float(sharpened["red"].mean()) == pytest.approx(float(bands["red"].mean()), abs=0.02)


def test_pansharpen_is_a_no_op_without_a_pan_band():
    bands = {"red": np.ma.masked_array(np.ones((8, 8), "float32"))}
    assert enhance.pansharpen(bands, None, ["red"]) is bands


# ── Radar ──────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def radar(tmp_path_factory):
    root = tmp_path_factory.mktemp("radar")
    assets = {
        # Sentinel-1 RTC stores power; -10 dB is a power of 0.1.
        "vv": write(root / "vv.tif", lambda x, y: np.full_like(x, 0.1), 10, "float32"),
        "vh": write(root / "vh.tif", lambda x, y: np.full_like(x, 0.01), 10, "float32"),
    }
    return {
        "id": "S1_TEST", "source": "sentinel-1-rtc", "date": "2023-05-05",
        "datetime": "2023-05-05T06:00:00Z", "cloud": None, "platform": "sentinel-1a",
        "demo": False, "assets": assets,
    }


def test_radar_is_converted_to_decibels(radar, aoi):
    grid = Grid(geometry_bounds(aoi), 128)
    bands, _ = raster.read_bands(radar, grid, ["vv", "vh"],
                                 source=sources.get("sentinel-1-rtc"))
    assert bands["vv"].mean() == pytest.approx(-10.0, abs=0.1)
    assert bands["vh"].mean() == pytest.approx(-20.0, abs=0.1)


def test_radar_ratio_channel_is_derived(radar, aoi):
    result = service.render({
        "aoi": aoi, "scene": radar, "source": "sentinel-1-rtc",
        "mode": "composite", "preset": "radar", "size": 128,
    })
    assert result["meta"]["label"].startswith("Radar")
    assert result["bytes"][:4] == b"\x89PNG"


# ── Elevation ──────────────────────────────────────────────────


def test_hillshade_responds_to_slope():
    x = np.linspace(0, 1, 64)
    ramp = np.tile(x, (64, 1)) * 500.0
    elevation = np.ma.masked_array(ramp.astype("float32"))
    lit = enhance.hillshade(elevation, 30.0, azimuth=270, altitude=45)
    dim = enhance.hillshade(elevation, 30.0, azimuth=90, altitude=45)
    # A west-facing slope is bright from the west and dark from the east.
    assert lit.mean() > dim.mean()
    assert 0.0 <= lit.min() and lit.max() <= 1.0


def test_flat_ground_is_evenly_lit():
    flat = np.ma.masked_array(np.full((32, 32), 100.0, dtype="float32"))
    shade = enhance.hillshade(flat, 30.0, altitude=45)
    assert shade.std() == pytest.approx(0.0, abs=1e-6)


# ── Compositing ────────────────────────────────────────────────


def _scene_with_hole(value: float, hole: slice) -> dict:
    data = np.full((32, 32), value, dtype="float32")
    mask = np.zeros((32, 32), dtype=bool)
    mask[hole] = True
    return {"red": np.ma.masked_array(data, mask=mask)}


def test_median_composite_fills_the_gaps():
    """Clouds in different places on different dates cancel out."""
    stacks = [
        _scene_with_hole(0.2, slice(0, 10)),
        _scene_with_hole(0.2, slice(10, 20)),
        _scene_with_hole(0.2, slice(20, 32)),
    ]
    merged = enhance.composite(stacks, "median")
    assert not np.ma.getmaskarray(merged["red"]).any()
    assert merged["red"].mean() == pytest.approx(0.2, abs=1e-6)


def test_composite_report_counts_the_rescue():
    stacks = [_scene_with_hole(0.2, slice(0, 16)), _scene_with_hole(0.2, slice(16, 32))]
    report = enhance.composite_report(stacks, "red")
    assert report["scenes"] == 2
    assert report["best_single_pct"] == pytest.approx(50.0, abs=0.1)
    assert report["combined_pct"] == pytest.approx(100.0, abs=0.1)


def test_first_valid_composite_keeps_the_earliest_clear_pixel():
    stacks = [_scene_with_hole(0.1, slice(0, 16)), _scene_with_hole(0.9, slice(0, 0))]
    merged = enhance.composite(stacks, "first")
    assert merged["red"][20, 0] == pytest.approx(0.1)   # kept from the first scene
    assert merged["red"][0, 0] == pytest.approx(0.9)    # gap filled from the second


def test_single_scene_composite_is_returned_untouched():
    stack = _scene_with_hole(0.3, slice(0, 4))
    assert enhance.composite([stack]) is stack


# ── Image quality ──────────────────────────────────────────────


def test_dark_object_subtraction_removes_the_haze_floor():
    """Haze lifts the whole band; subtracting the floor should bring it back."""
    clean = np.linspace(0.0, 0.5, 4096).reshape(64, 64).astype("float32")
    hazy = {"blue": np.ma.masked_array(clean + 0.08)}
    fixed = enhance.dark_object_subtraction(hazy, percentile=1.0, strength=1.0)
    assert fixed["blue"].min() == pytest.approx(0.0, abs=0.005)
    assert float(fixed["blue"].std()) == pytest.approx(float(clean.std()), rel=0.02)


def test_haze_removal_never_goes_negative():
    band = {"blue": np.ma.masked_array(np.full((16, 16), 0.05, dtype="float32"))}
    out = enhance.dark_object_subtraction(band, strength=1.0)
    assert out["blue"].min() >= 0.0


def test_clahe_lifts_a_low_contrast_image():
    rng = np.random.default_rng(0)
    flat = (0.45 + rng.normal(0, 0.02, (128, 128))).clip(0, 1).astype("float32")
    out = enhance.clahe(flat, clip_limit=3.0, tiles=8)
    assert out.std() > flat.std() * 3
    assert 0.0 <= out.min() and out.max() <= 1.0


def test_clahe_is_locally_adaptive():
    """A dark half and a bright half should each be stretched on their own terms.

    The input has to be peaked rather than a ramp: equalising an already-uniform
    histogram is meant to be a no-op.
    """
    rng = np.random.default_rng(7)
    image = np.zeros((128, 128), dtype="float32")
    image[:, :64] = np.clip(rng.normal(0.15, 0.02, (128, 64)), 0, 1)
    image[:, 64:] = np.clip(rng.normal(0.85, 0.02, (128, 64)), 0, 1)
    out = enhance.clahe(image, clip_limit=4.0, tiles=8)
    # Judge single tiles: a global std would just measure the dark/bright step.
    for region in (slice(0, 16), slice(80, 96)):
        assert out[16:32, region].std() > image[16:32, region].std() * 2


def test_clahe_off_is_a_no_op():
    image = np.full((32, 32), 0.5, dtype="float32")
    assert np.array_equal(enhance.clahe(image, clip_limit=0), image)


def test_clahe_rgb_keeps_hue():
    rng = np.random.default_rng(1)
    rgb = np.clip(rng.normal(0.4, 0.05, (64, 64, 3)), 0, 1).astype("float32")
    rgb[..., 0] *= 1.4                                  # a red cast
    out = enhance.apply_clahe_rgb(rgb, clip_limit=3.0)
    before = (rgb[..., 0] / np.maximum(rgb[..., 1], 1e-6)).mean()
    after = (out[..., 0] / np.maximum(out[..., 1], 1e-6)).mean()
    assert after == pytest.approx(before, rel=0.05)


def test_white_balance_neutralises_a_colour_cast():
    rgb = np.zeros((32, 32, 3), dtype="float32")
    rgb[..., 0], rgb[..., 1], rgb[..., 2] = 0.6, 0.4, 0.2
    out = enhance.white_balance(rgb, strength=1.0)
    assert out[..., 0].mean() == pytest.approx(out[..., 2].mean(), abs=0.02)


def test_unsharp_sharpens_an_edge_without_clipping():
    image = np.zeros((32, 32, 3), dtype="float32")
    image[:, 16:] = 0.6
    out = enhance.unsharp(image, amount=0.8)
    assert out[:, 15].mean() < image[:, 15].mean() + 1e-6      # darker just before
    assert out[:, 16].mean() > image[:, 16].mean()             # brighter just after
    assert out.min() >= 0.0 and out.max() <= 1.0


def test_denoise_removes_speckle():
    rng = np.random.default_rng(2)
    clean = np.full((64, 64), 0.4, dtype="float32")
    noisy = clean.copy()
    salt = rng.random((64, 64)) > 0.9
    noisy[salt] = 1.0
    out = enhance.denoise({"vv": np.ma.masked_array(noisy)}, strength=1.0)
    assert float(out["vv"].std()) < float(noisy.std()) / 3


def test_enhancements_are_reported_in_metadata():
    scene = {"id": "demo-2024-06-01-1-0-sentinel-2-l2a", "date": "2024-06-01",
             "cloud": 0.0, "demo": True, "source": "sentinel-2-l2a"}
    result = service.render({
        "aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene, "size": 128,
        "preset": "true_color", "haze_removal": 1.0, "adaptive_contrast": 2.0,
        "sharpen": 0.3, "white_balance": 0.5,
    })
    applied = result["meta"]["enhancements"]
    assert "haze removal" in applied
    assert "adaptive contrast" in applied
    assert "detail" in applied
    assert "white balance" in applied


# ── The source registry ────────────────────────────────────────


def test_every_source_declares_a_usable_band_set():
    for source in sources.SOURCES.values():
        assert source.bands, f"{source.key} has no bands"
        assert source.provider in sources.PROVIDERS
        assert source.scale in sources.SCALES
        if source.cloud_mask:
            assert source.cloud_mask in sources.CLOUD_MASKS
        for alias, spec in source.bands.items():
            assert alias in config.BANDS, f"{source.key}: unknown alias {alias}"
            assert sources.resolve_band(source, alias)[1] >= 1


def test_band_aliases_resolve_to_assets_and_indices():
    landsat = sources.get("landsat-c2-l2")
    assert sources.resolve_band(landsat, "nir") == ("nir08", 1)
    naip = sources.get("naip")
    assert sources.resolve_band(naip, "nir") == ("image", 4)


def test_sources_only_offer_visualisations_they_can_render():
    radar = sources.get("sentinel-1-rtc")
    assert not sources.supports(radar, config.COMPOSITES["true_color"]["bands"])
    assert sources.supports(radar, ["vv", "vh"])
    s2 = sources.get("sentinel-2-l2a")
    assert sources.supports(s2, config.COMPOSITES["true_color"]["bands"])


def test_asking_a_satellite_for_a_band_it_lacks_fails_clearly():
    scene = {"id": "x", "source": "sentinel-1-rtc", "date": "2023-01-01",
             "demo": False, "assets": {}}
    grid = Grid((-0.1, 51.4, 0.0, 51.5), 64)
    with pytest.raises(raster.BandReadError, match="red"):
        raster.read_bands(scene, grid, ["red"], source=sources.get("sentinel-1-rtc"))


def test_render_rejects_an_unsupported_visualisation():
    scene = {"id": "demo-2024-06-01-1-0-sentinel-1-rtc", "date": "2024-06-01",
             "cloud": None, "demo": True, "source": "sentinel-1-rtc"}
    with pytest.raises(service.RenderError, match="does not carry"):
        service.render({"aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene,
                        "source": "sentinel-1-rtc", "preset": "true_color", "size": 64})


def test_land_cover_renders_classes_with_areas():
    scene = {"id": "demo-2024-06-01-1-0-esa-worldcover", "date": "2024-06-01",
             "cloud": None, "demo": True, "source": "esa-worldcover"}
    result = service.render({"aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene,
                             "source": "esa-worldcover", "size": 128})
    legend = result["meta"]["legend"]
    assert legend["type"] == "categorical"
    assert legend["classes"], "expected at least one land-cover class"
    assert sum(c["percent"] for c in legend["classes"]) == pytest.approx(100, abs=1.0)


def test_thermal_render_reports_kelvin():
    scene = {"id": "demo-2024-06-01-1-0-landsat-thermal", "date": "2024-06-01",
             "cloud": 0.0, "demo": True, "source": "landsat-thermal"}
    result = service.render({"aoi": {"bbox": [-0.1, 51.4, 0.0, 51.5]}, "scene": scene,
                             "source": "landsat-thermal", "mode": "index",
                             "index": "surface_temp", "size": 128})
    assert result["meta"]["legend"]["unit"] == "K"
    assert 200 < result["meta"]["stats"]["mean"] < 350


def test_coarse_satellites_still_fill_the_grid():
    scene = {"id": "demo-2024-06-01-1-0-modis-surface-reflectance", "date": "2024-06-01",
             "cloud": 0.0, "demo": True, "source": "modis-surface-reflectance"}
    result = service.render({"aoi": {"bbox": [-0.5, 51.0, 0.5, 52.0]}, "scene": scene,
                             "source": "modis-surface-reflectance", "size": 256})
    grid = result["meta"]["grid"]
    assert max(grid["width"], grid["height"]) == 256
    assert result["meta"]["valid_pct"] == pytest.approx(100.0, abs=0.1)
