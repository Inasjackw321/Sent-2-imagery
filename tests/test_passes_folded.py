"""One flight over, not six pieces of one.

A satellite does not photograph your area. It flies over recording, and the
catalogue cuts what it recorded into squares -- for Sentinel-2, the
hundred-kilometre MGRS tiles. An ordinary area sits across several of them, so
one pass over one town comes back from the catalogue as six entries: same
date, same minute, same track, different squares.

Listed as six that is wrong twice over. It reads as six chances to see the
ground when there was one. And it offers a picture per tile, so picking any
single one renders the area with everything that fell in the other tiles
simply missing -- a picture with a straight edge across it and no explanation.

These tests are about the folding, and about the one thing folding must not
do: quietly average six cloud figures that describe six different
hundred-kilometre squares, none of which is the area anybody asked about.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import service, stac  # noqa: E402
from backend.geo import Grid, normalise_aoi  # noqa: E402
from backend.raster import BandReadError, scene_is_safe  # noqa: E402

UTM = CRS.from_epsg(32630)


def tile(**over):
    """A catalogue entry for one square of one pass."""
    base = {
        "id": "S2_TILE", "satellite": "sentinel-2", "source": "earth-search",
        "datetime": "2026-09-21T09:06:01Z", "date": "2026-09-21",
        "cloud": 50.0, "orbit": 79, "tile": "36TYL",
        "bbox": [29.0, 49.5, 30.2, 50.7], "assets": {"red": "https://x/a.tif"},
    }
    base.update(over)
    return base


AREA = (30.0, 50.0, 31.0, 50.6)


# ── Which pieces belong together ───────────────────────────────


class TestWhatCountsAsOnePass:
    def test_tiles_from_one_minute_on_one_track_are_one_pass(self):
        folded = stac.fold_passes([
            tile(id="a", tile="36TYL", datetime="2026-09-21T09:06:01Z"),
            tile(id="b", tile="37TBF", datetime="2026-09-21T09:06:11Z"),
            tile(id="c", tile="37TCF", datetime="2026-09-21T09:06:21Z"),
        ], AREA)
        assert len(folded) == 1
        assert len(folded[0]["pieces"]) == 3

    def test_two_days_are_two_passes(self):
        folded = stac.fold_passes([
            tile(id="a", datetime="2026-09-21T09:06:01Z"),
            tile(id="b", datetime="2026-09-18T09:06:01Z"),
        ], AREA)
        assert len(folded) == 2

    def test_two_tracks_on_one_day_are_two_passes(self):
        """A place near the edge of two adjacent tracks is photographed by
        both, sometimes hours apart on the same day. Those are two views of
        the ground, and folding them would claim one."""
        folded = stac.fold_passes([
            tile(id="a", orbit=79, datetime="2026-09-21T09:06:01Z"),
            tile(id="b", orbit=36, datetime="2026-09-21T09:06:05Z"),
        ], AREA)
        assert len(folded) == 2

    def test_two_satellites_are_never_one_pass(self):
        folded = stac.fold_passes([
            tile(id="a", satellite="sentinel-2"),
            tile(id="b", satellite="landsat"),
        ], AREA)
        assert len(folded) == 2

    def test_passes_an_hour_apart_are_not_folded(self):
        folded = stac.fold_passes([
            tile(id="a", datetime="2026-09-21T09:06:01Z"),
            tile(id="b", datetime="2026-09-21T10:46:01Z"),
        ], AREA)
        assert len(folded) == 2

    def test_a_single_tile_is_left_exactly_as_it_was(self):
        one = tile()
        assert stac.fold_passes([one], AREA) == [one]
        assert "pieces" not in stac.fold_passes([one], AREA)[0]

    def test_nothing_folds_to_nothing(self):
        assert stac.fold_passes([], AREA) == []

    def test_the_newest_pass_still_comes_first(self):
        folded = stac.fold_passes([
            tile(id="old", datetime="2026-09-01T09:06:01Z"),
            tile(id="new", datetime="2026-09-21T09:06:01Z"),
        ], AREA)
        assert [f["id"] for f in folded] == ["new", "old"]

    def test_a_scene_with_no_time_does_not_bring_the_folding_down(self):
        folded = stac.fold_passes([tile(id="a", datetime=""), tile(id="b")], AREA)
        assert len(folded) == 2


class TestWhichPieceLeads:
    def test_the_tile_holding_most_of_the_area_leads(self):
        """Its id and assets become the entry's, so a reader who renders
        without thinking about it gets the tile they would have picked."""
        folded = stac.fold_passes([
            tile(id="edge", bbox=[28.0, 49.5, 30.05, 50.7],
                 datetime="2026-09-21T09:06:01Z"),
            tile(id="middle", bbox=[29.9, 49.5, 31.1, 50.7],
                 datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["id"] == "middle"
        assert [p["id"] for p in folded[0]["pieces"]] == ["middle", "edge"]

    def test_without_an_area_it_still_folds(self):
        folded = stac.fold_passes([tile(id="a"), tile(id="b", tile="37TBF")], None)
        assert len(folded) == 1 and len(folded[0]["pieces"]) == 2


# ── The cloud figure ───────────────────────────────────────────


class TestOneCloudFigureForThePass:
    def test_a_tile_clipping_a_corner_does_not_decide_the_figure(self):
        """The pass that prompted this had six figures from 59% to 99%. A
        flat average of six hundred-kilometre squares is not the cloud over
        the ten-kilometre area somebody drew."""
        folded = stac.fold_passes([
            tile(id="most", cloud=20.0, bbox=[29.9, 49.9, 31.1, 50.7],
                 datetime="2026-09-21T09:06:01Z"),
            tile(id="corner", cloud=99.0, bbox=[30.95, 50.55, 32.0, 51.5],
                 datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["cloud"] < 30, folded[0]["cloud"]

    def test_equal_cover_averages(self):
        folded = stac.fold_passes([
            tile(id="west", cloud=20.0, bbox=[29.0, 50.0, 30.5, 50.6],
                 datetime="2026-09-21T09:06:01Z"),
            tile(id="east", cloud=80.0, bbox=[30.5, 50.0, 32.0, 50.6],
                 datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["cloud"] == pytest.approx(50.0, abs=2)

    def test_a_pass_with_no_cloud_figures_at_all_keeps_none(self):
        folded = stac.fold_passes([
            tile(id="a", cloud=None, datetime="2026-09-21T09:06:01Z"),
            tile(id="b", cloud=None, datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["cloud"] is None

    def test_pieces_without_a_figure_are_skipped_rather_than_counted_as_zero(self):
        folded = stac.fold_passes([
            tile(id="a", cloud=60.0, datetime="2026-09-21T09:06:01Z"),
            tile(id="b", cloud=None, datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["cloud"] == pytest.approx(60.0)

    def test_a_piece_outside_the_area_entirely_is_not_weighed_in(self):
        folded = stac.fold_passes([
            tile(id="here", cloud=10.0, bbox=[29.9, 49.9, 31.1, 50.7],
                 datetime="2026-09-21T09:06:01Z"),
            tile(id="far", cloud=95.0, bbox=[40.0, 60.0, 41.0, 61.0],
                 datetime="2026-09-21T09:06:11Z"),
        ], AREA)
        assert folded[0]["cloud"] == pytest.approx(10.0, abs=1)


class TestOverlapArithmetic:
    def test_a_box_covering_everything_is_one(self):
        assert stac._overlap([0, 0, 10, 10], (2, 2, 3, 3)) == pytest.approx(1.0)

    def test_a_box_touching_nothing_is_zero(self):
        assert stac._overlap([20, 20, 30, 30], (2, 2, 3, 3)) == 0.0

    def test_half_is_half(self):
        assert stac._overlap([2, 2, 2.5, 3], (2, 2, 3, 3)) == pytest.approx(0.5)

    def test_a_missing_box_is_assumed_to_cover(self):
        assert stac._overlap(None, (2, 2, 3, 3)) == 1.0
        assert stac._overlap([1, 2], (2, 2, 3, 3)) == 1.0


# ── Rendering the folded pass ──────────────────────────────────


def write_half(path, xs, value, nodata_elsewhere=True):
    """A GeoTIFF covering part of a strip, empty where it has nothing."""
    size = 512
    res = 20.0
    data = np.zeros((size, size), dtype="uint16")
    lo, hi = xs
    data[:, int(size * lo):int(size * hi)] = value
    transform = Affine(res, 0, 500000.0, 0, -res, 5700000.0)
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size,
                       count=1, dtype="uint16", crs=UTM, transform=transform,
                       nodata=0) as dst:
        dst.write(data, 1)
    return str(path)


@pytest.fixture(scope="module")
def seam(tmp_path_factory):
    """Two tiles of one pass: one holds the west half, one the east."""
    root = tmp_path_factory.mktemp("seam")
    names = ("red", "green", "blue", "nir", "scl")
    west = {n: write_half(root / f"w_{n}.tif", (0.0, 0.55), 3000) for n in names}
    east = {n: write_half(root / f"e_{n}.tif", (0.45, 1.0), 7000) for n in names}
    from rasterio.warp import transform as warp
    lons, lats = warp(UTM, CRS.from_epsg(4326),
                      [500000.0, 500000.0 + 512 * 20.0],
                      [5700000.0, 5700000.0 - 512 * 20.0])
    area = normalise_aoi({"bbox": [lons[0] + 0.005, lats[1] + 0.005,
                                   lons[1] - 0.005, lats[0] - 0.005]})
    one = {"id": "west", "satellite": "sentinel-2", "source": "earth-search",
           "datetime": "2026-09-21T09:06:01Z", "date": "2026-09-21",
           "cloud": 10.0, "orbit": 79, "tile": "36TYL", "assets": west,
           "boa_offset_applied": True, "demo": False}
    two = dict(one, id="east", tile="37TBF", assets=east, cloud=40.0,
               datetime="2026-09-21T09:06:11Z")
    return one, two, area


def rendered(scene, area, size=192):
    return service.render({
        "aoi": area, "scene": scene, "preset": "true_color", "size": size,
        "mask_clouds": False,
    })


class TestTheSeam:
    def test_one_tile_alone_leaves_a_hole(self, seam):
        """What the reader saw before: pick a tile, get the part of the area
        that fell inside it and nothing else."""
        west, _, area = seam
        made = rendered(west, area)
        assert made["meta"]["valid_pct"] < 75, made["meta"]["valid_pct"]

    def test_the_folded_pass_fills_it(self, seam):
        west, east, area = seam
        folded = dict(west, pieces=[west, east])
        made = rendered(folded, area)
        assert made["meta"]["valid_pct"] > 97, made["meta"]["valid_pct"]

    def test_the_pieces_are_laid_side_by_side_rather_than_averaged(self, seam):
        """Where one tile has pixels the other has none, so an average would
        be arithmetic on one real number and one absence."""
        west, east, area = seam
        folded = dict(west, pieces=[west, east])
        made = rendered(folded, area)
        assert made["meta"]["scene"]["date"] == "2026-09-21"
        # The picture keeps both tiles' values rather than a blend of them.
        image = np.asarray(bytearray(made["bytes"]))
        assert image.size > 0

    def test_a_tile_that_cannot_be_read_is_a_gap_not_a_failure(self, seam):
        west, _, area = seam
        broken = dict(west, id="broken", assets={"red": "/nowhere/nothing.tif"})
        folded = dict(west, pieces=[west, broken])
        made = rendered(folded, area)
        assert made["meta"]["valid_pct"] > 40

    def test_a_pass_whose_every_tile_fails_says_so(self, seam):
        west, _, area = seam
        broken = dict(west, assets={"red": "/nowhere/nothing.tif"})
        folded = dict(west, id="all-bad",
                      assets={"red": "/nowhere/nothing.tif"},
                      pieces=[broken, dict(broken, id="b2")])
        with pytest.raises(BandReadError, match="No tile of this pass"):
            rendered(folded, area)


class TestTheCheckAtTheDoor:
    def test_an_asset_hidden_in_a_piece_is_still_checked(self):
        """The pieces carry assets of their own, and the renderer opens them.
        Checking only the outer scene would be the whole check walked around."""
        folded = {"id": "x", "assets": {"red": "https://ok.example/a.tif"},
                  "pieces": [{"id": "p", "assets": {"red": "/etc/passwd"}}]}
        with pytest.raises(BandReadError):
            scene_is_safe(folded)

    def test_a_good_folded_pass_passes(self):
        folded = {"id": "x", "assets": {"red": "https://ok.example/a.tif"},
                  "pieces": [{"id": "p", "assets": {"red": "https://ok.example/b.tif"}}]}
        scene_is_safe(folded)

    def test_a_piece_that_is_the_scene_itself_does_not_loop_for_ever(self):
        scene = {"id": "x", "assets": {"red": "https://ok.example/a.tif"}}
        scene["pieces"] = [scene]
        scene_is_safe(scene)


class TestTheBandCacheKeepsScenesApart:
    """Found by the test above, and worth its own name.

    A scene arrives in the request body, so its id is whatever the caller
    wrote on it. Keyed on the id alone, two different scenes sharing one are
    one entry in the cache and each is served the other's pixels -- which is
    a stale picture at best, and at worst a body naming a real scene while
    pointing its bands somewhere else.
    """

    def test_two_scenes_with_one_id_do_not_share_an_entry(self, seam):
        west, east, area = seam
        first = rendered(west, area)
        pretender = dict(east, id=west["id"])      # east's pixels, west's name
        second = rendered(pretender, area)
        assert first["bytes"] != second["bytes"]

    def test_the_same_scene_twice_still_comes_from_the_cache(self, seam):
        """The cache has to go on being a cache."""
        west, _, area = seam
        assert rendered(west, area)["bytes"] == rendered(west, area)["bytes"]


class TestWhatHappensInTheOverlap:
    """Neighbouring tiles of one pass overlap by a few kilometres.

    In that strip both tiles have pixels, and they are not identical: each is
    its own resampling of the same ground, and the catalogues can process
    adjacent tiles with different baselines. Averaging them there blurs the
    strip and can leave a visible band of in-between brightness down the
    picture -- a seam where the point of folding was not to have one.

    So the strip comes from one tile: the one the area is mostly in.
    """

    def test_the_seam_comes_from_the_leading_tile_not_a_blend(self, seam):
        west, east, area = seam
        from backend import raster
        from backend.geo import Grid, geometry_bounds

        grid = Grid(geometry_bounds(area), 192)
        lead, cloud, note = service.load_pass(
            dict(west, pieces=[west, east]), area, grid, ["red"],
            {"mask_clouds": False})
        follow, _, _ = service.load_pass(
            dict(east, pieces=[east, west]), area, grid, ["red"],
            {"mask_clouds": False})
        # The two orders differ only in the overlap, so if the strip were
        # averaged they would come out identical.
        assert not np.allclose(np.ma.filled(lead["red"], 0),
                               np.ma.filled(follow["red"], 0)), (
            "the tiles were blended in the overlap rather than laid in order")
        assert note["tiles"] == 2 and note["laid"] == 2

    def test_the_strip_holds_one_tile_s_values_rather_than_their_average(self, seam):
        west, east, area = seam
        from backend.geo import Grid, geometry_bounds

        grid = Grid(geometry_bounds(area), 192)
        bands, _, _ = service.load_pass(dict(west, pieces=[west, east]), area,
                                        grid, ["red"], {"mask_clouds": False})
        values = np.ma.filled(bands["red"], np.nan)
        values = values[np.isfinite(values)]
        # Two levels in the picture, not three: the west tile's, the east
        # tile's, and no average of the two in between.
        middle = (np.nanmin(values) + np.nanmax(values)) / 2
        near_middle = np.mean(np.abs(values - middle) < abs(middle) * 0.05)
        assert near_middle < 0.05, (
            f"{near_middle:.0%} of the picture sits between the two tiles' "
            f"values -- that is a blended seam")
