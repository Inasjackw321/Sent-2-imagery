"""Two satellites over one area: radar alongside optical, and overpass timing.

Sentinel-1 is a different physical measurement from Sentinel-2 -- backscatter
in decibels rather than reflectance -- so most of what is tested here is that
the app keeps the two apart where it must and treats them alike where it can.

    python -m pytest tests/test_satellites.py -q
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import composite, config, passes, raster, service, stac  # noqa: E402
from backend.geo import Grid, circle_to_polygon, geometry_bounds  # noqa: E402

UTM = CRS.from_epsg(32630)
ORIGIN_X, ORIGIN_Y = 500000.0, 5700000.0
TILE_M = 2048 * 10


@pytest.fixture(scope="module")
def radar_scene(tmp_path_factory):
    """A Sentinel-1 scene whose 'remote' assets are local amplitude GeoTIFFs."""
    root = tmp_path_factory.mktemp("s1")

    def write(path, value):
        size = TILE_M // 10
        data = np.full((size, size), value, dtype="uint16")
        # A darker left half, so the decibel conversion has something to prove.
        data[:, : size // 2] = value // 4
        with rasterio.open(
            path, "w", driver="GTiff", height=size, width=size, count=1,
            dtype="uint16", crs=UTM, nodata=0,
            transform=Affine(10, 0, ORIGIN_X, 0, -10, ORIGIN_Y),
            tiled=True, blockxsize=256, blockysize=256, compress="deflate",
        ) as dst:
            dst.write(data, 1)
        return str(path)

    return {
        "id": "S1A_IW_GRDH_TEST",
        "satellite": "sentinel-1",
        "date": "2024-05-04",
        "datetime": "2024-05-04T17:52:11Z",
        "cloud": None,
        "platform": "sentinel-1a",
        "demo": False,
        "assets": {"vv": write(root / "vv.tif", 1000), "vh": write(root / "vh.tif", 400)},
    }


@pytest.fixture(scope="module")
def aoi():
    from rasterio.warp import transform

    lons, lats = transform(UTM, CRS.from_epsg(4326),
                           [ORIGIN_X + TILE_M / 2], [ORIGIN_Y - TILE_M / 2])
    return circle_to_polygon(lons[0], lats[0], 1500)


# ── The catalogue of satellites ────────────────────────────────


def test_both_satellites_are_fully_described():
    for key, sat in config.SATELLITES.items():
        assert sat["key"] == key
        assert sat["collection"] and sat["short"] and sat["platform"]
        assert sat["resolution"] > 0 and sat["repeat_days"] > 0
        assert sat["default_composite"] in config.COMPOSITES
        assert config.COMPOSITES[sat["default_composite"]]["sat"] == key
        assert isinstance(sat["can_superres"], bool)


def test_a_composite_only_ever_uses_its_own_satellites_bands():
    for key, spec in config.COMPOSITES.items():
        for band in spec["bands"]:
            assert config.BANDS[band]["sat"] == spec["sat"], f"{key} reaches across satellites"
    for key, spec in config.INDICES.items():
        for band in spec["bands"]:
            assert config.BANDS[band]["sat"] == spec["sat"], f"{key} reaches across satellites"


def test_a_collection_maps_back_to_its_satellite():
    for key, sat in config.SATELLITES.items():
        assert config.satellite_for_collection(sat["collection"]) == key
    # Anything unrecognised falls back rather than failing a search.
    assert config.satellite_for_collection("landsat-c2-l2") == config.DEFAULT_SATELLITE


# ── Reading radar ──────────────────────────────────────────────


def test_amplitude_is_read_as_decibels(radar_scene, aoi):
    grid = Grid(geometry_bounds(aoi), 256)
    bands, cloud = raster.read_bands(radar_scene, grid, ["vv", "vh"])

    # 10 * log10(1000^2) = 60 dB; a quarter of the amplitude is 12 dB down.
    assert bands["vv"][:, -60:].mean() == pytest.approx(60.0, abs=0.1)
    assert bands["vv"][:, :60].mean() == pytest.approx(60.0 - 12.04, abs=0.1)
    # VV over VH, 1000 against 400, is 7.96 dB whichever half it is measured in.
    assert (bands["vv"] - bands["vh"]).mean() == pytest.approx(7.96, abs=0.05)
    assert cloud == 0.0


def test_the_ratio_band_is_worked_out_not_downloaded(radar_scene, aoi):
    grid = Grid(geometry_bounds(aoi), 192)
    bands, _ = raster.read_bands(radar_scene, grid, ["vv", "vh", "vvvh"])
    assert set(bands) == {"vv", "vh", "vvvh"}
    assert np.allclose(bands["vvvh"].compressed(),
                       (bands["vv"] - bands["vh"]).compressed(), atol=1e-4)


def test_asking_the_wrong_satellite_for_a_band_says_so(radar_scene, aoi):
    grid = Grid(geometry_bounds(aoi), 64)
    with pytest.raises(raster.BandReadError, match="not a Sentinel-1 band"):
        raster.read_bands(radar_scene, grid, ["red"])


def test_radar_reads_only_the_bands_it_was_asked_for(radar_scene, aoi):
    """The ratio needs both polarisations, but VV alone must not drag VH in."""
    grid = Grid(geometry_bounds(aoi), 64)
    bands, _ = raster.read_bands(radar_scene, grid, ["vv"])
    assert set(bands) == {"vv"}


# ── Rendering radar ────────────────────────────────────────────


def test_a_radar_composite_renders(radar_scene, aoi):
    result = service.render({"aoi": aoi, "scene": radar_scene, "mode": "composite",
                             "preset": "radar_color", "size": 192})
    meta = result["meta"]
    assert result["bytes"][:8] == b"\x89PNG\r\n\x1a\n"
    assert meta["satellite"] == "sentinel-1"
    assert meta["source"]["kind"] == "radar"
    assert meta["native_res_m"] == 20
    assert meta["band_labels"] == ["VV backscatter", "VH backscatter", "VV − VH ratio"]


def test_the_radar_ratio_index_is_a_difference_in_decibels(radar_scene, aoi):
    grid = Grid(geometry_bounds(aoi), 128)
    bands, _ = raster.read_bands(radar_scene, grid, ["vv", "vh"])
    ratio = composite.compute_index(bands, "radar_ratio")
    assert ratio.mean() == pytest.approx(7.96, abs=0.05)


def test_an_optical_composite_is_refused_for_radar(radar_scene, aoi):
    with pytest.raises(service.RenderError, match="needs Sentinel-2"):
        service.render({"aoi": aoi, "scene": radar_scene, "preset": "true_color", "size": 64})


def test_the_two_satellites_are_never_merged_into_each_other():
    optical = {"id": "a", "satellite": "sentinel-2", "date": "2024-05-01"}
    radar = {"id": "b", "satellite": "sentinel-1", "date": "2024-05-02"}
    assert service.satellite_of([optical])["key"] == "sentinel-2"
    assert service.satellite_of([radar])["key"] == "sentinel-1"
    with pytest.raises(service.RenderError, match="One satellite at a time"):
        service.satellite_of([optical, radar])


def test_a_scene_with_no_satellite_is_taken_as_sentinel_2():
    """Older callers, and every test written before radar existed."""
    assert service.satellite_of([{"id": "x"}])["key"] == "sentinel-2"


def test_merging_radar_averages_the_speckle_instead_of_sharpening():
    """The honest promise for radar: cleaner, not finer.

    Sentinel-1 is delivered on a 10 m grid but resolves about 20 m, so it is
    already over-sampled and there is nothing between its samples to recover.
    """
    grid = Grid((-0.05, 51.45, 0.05, 51.55), 2048)
    radar = [{"id": f"r{i}", "satellite": "sentinel-1"} for i in range(6)]
    optical = [{"id": f"o{i}", "satellite": "sentinel-2"} for i in range(6)]

    radar_plan = service.merge_plan({}, grid, radar)
    optical_plan = service.merge_plan({}, grid, optical)

    assert radar_plan["sharpening"] is False
    assert radar_plan["despeckling"] is True
    assert radar_plan["resolves"] == 1.0
    # The same grid and the same number of dates does sharpen the optical.
    assert optical_plan["sharpening"] is True
    assert optical_plan["resolves"] > 1.0


def test_averaging_radar_passes_actually_reduces_the_speckle(aoi):
    """Six passes of the same ground, and the grain should visibly drop."""
    grid = Grid(geometry_bounds(aoi), 256)
    scenes = [dict(stac._demo_scene(dt.date(2024, 5, 1) + dt.timedelta(days=6 * i),
                                    0.0, 4242, "sentinel-1")) for i in range(6)]
    stacks = [raster.read_bands(s, grid, ["vv"])[0] for s in scenes]

    from backend import enhance

    one = stacks[0]["vv"]
    merged = enhance.composite(stacks, "mean")["vv"]
    # Speckle is what is left after the ground itself is smoothed away.
    grain = lambda b: float(np.std(b - _blur(b)))  # noqa: E731
    assert grain(merged) < grain(one) * 0.6


def _blur(band):
    from scipy import ndimage

    return ndimage.uniform_filter(np.ma.filled(band, 0.0).astype("float32"), 7)


# ── The catalogue search ───────────────────────────────────────


def test_a_radar_item_is_flattened_with_its_track():
    item = {
        "id": "S1A_IW_GRDH_1SDV_20240504T175211",
        "collection": "sentinel-1-grd",
        "properties": {
            "datetime": "2024-05-04T17:52:11Z",
            "platform": "sentinel-1a",
            "sar:instrument_mode": "IW",
            "sat:orbit_state": "descending",
            "sat:relative_orbit": 59,
        },
        "assets": {"vv": {"href": "s3://x/vv.tiff"}, "vh": {"href": "s3://x/vh.tiff"}},
    }
    summary = stac.scene_summary(item)
    assert summary["satellite"] == "sentinel-1"
    assert summary["cloud"] is None          # radar has none, and must not invent one
    assert summary["orbit"] == 59
    assert summary["tile"] == "IW DES #59"
    assert summary["assets"]["vv"].startswith("https://")


def test_a_search_can_be_narrowed_to_one_satellite_or_widened_to_both():
    assert stac.normalise_satellites(None) == list(config.SATELLITES)
    assert stac.normalise_satellites("both") == list(config.SATELLITES)
    assert stac.normalise_satellites("sentinel-1") == ["sentinel-1"]
    assert stac.normalise_satellites(["sentinel-2"]) == ["sentinel-2"]
    with pytest.raises(stac.SceneSearchError):
        stac.normalise_satellites(["sentinel-9"])


def test_the_offline_catalogue_flies_both_satellites():
    from backend.geo import normalise_aoi

    found = stac.search_scenes(normalise_aoi({"bbox": [-0.2, 51.4, 0.0, 51.6]}),
                               "2024-01-01", "2024-06-01", max_cloud=100,
                               limit=40, demo=True)["scenes"]
    kinds = {s["satellite"] for s in found}
    assert kinds == {"sentinel-1", "sentinel-2"}
    # Newest first, whichever satellite it came from.
    assert found == sorted(found, key=lambda s: s["datetime"], reverse=True)
    for scene in found:
        assert stac.get_scene(scene["id"])["satellite"] == scene["satellite"]


# ── When the satellites next come over ─────────────────────────


def _times(start: str, days: float, count: int) -> list[dt.datetime]:
    first = dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc)
    return [first + dt.timedelta(days=days * i) for i in range(count)]


def test_the_repeat_interval_comes_from_the_passes_themselves():
    """A gap in the record must not be mistaken for a slower satellite."""
    times = _times("2024-05-01T10:31:00", 5, 6)
    del times[3]                                   # one pass never archived
    period, measured = passes._interval(times, dt.timedelta(days=10))
    assert measured is True
    assert period == dt.timedelta(days=5)


def test_one_pass_filed_twice_is_not_an_interval():
    times = _times("2024-05-01T10:31:00", 5, 3)
    times.insert(1, times[0] + dt.timedelta(minutes=4))
    period, _ = passes._interval(times, dt.timedelta(days=10))
    assert period == dt.timedelta(days=5)


def test_a_single_pass_falls_back_to_the_nominal_cycle():
    period, measured = passes._interval(_times("2024-05-01T10:31:00", 5, 1),
                                        dt.timedelta(days=12))
    assert measured is False
    assert period == dt.timedelta(days=12)


def test_the_next_pass_is_always_in_the_future_and_keeps_the_time_of_day():
    last = dt.datetime(2024, 5, 1, 10, 31, tzinfo=dt.timezone.utc)
    now = dt.datetime(2024, 5, 20, 14, 0, tzinfo=dt.timezone.utc)
    nxt = passes._project(last, dt.timedelta(days=5), now)
    assert nxt > now
    assert nxt - now < dt.timedelta(days=5)
    assert (nxt.hour, nxt.minute) == (10, 31)


def test_the_soonest_track_wins(monkeypatch):
    """Two ground tracks cross one point; the answer is whichever is next."""
    now = dt.datetime(2024, 6, 1, 12, 0, tzinfo=dt.timezone.utc)

    def fake(sat, lon, lat, when):
        return [
            *[{"when": t, "orbit": 59, "platform": "sentinel-1a", "orbit_state": "ascending"}
              for t in _times("2024-05-10T06:00:00", 12, 2)],
            *[{"when": t, "orbit": 132, "platform": "sentinel-1a", "orbit_state": "descending"}
              for t in _times("2024-05-14T17:52:00", 12, 2)],
        ]

    monkeypatch.setattr(passes, "_recent_passes", fake)
    out = passes.satellite_passes(config.satellite("sentinel-1"), 2.35, 48.86, now)

    assert out["orbits_seen"] == 2
    assert out["measured"] is True
    assert out["next"]["period_days"] == 12.0
    # Track 132 last flew on 26 May, so it comes round on 7 June -- ahead of
    # track 59, which flew on 22 May and is not back until 3 June... which is
    # sooner, so that is the one to report.
    assert out["next"]["orbit"] == 59
    assert out["next"]["datetime"].startswith("2024-06-03T06:00")
    assert out["last"]["datetime"].startswith("2024-05-26")


def test_a_point_no_satellite_has_crossed_says_so(monkeypatch):
    monkeypatch.setattr(passes, "_recent_passes", lambda *a: [])
    out = passes.satellite_passes(config.satellite("sentinel-2"), 0.0, 0.0)
    assert out["next"] is None
    assert "No Sentinel-2 pass" in out["note"]


def test_both_satellites_are_answered_soonest_first():
    out = passes.next_passes(2.35, 48.86, demo=True)
    assert {s["satellite"] for s in out["satellites"]} == set(config.SATELLITES)
    aways = [s["next"]["hours_away"] for s in out["satellites"]]
    assert aways == sorted(aways)
    assert all(a > 0 for a in aways)
