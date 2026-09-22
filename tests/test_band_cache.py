"""What the band cache is allowed to treat as the same read.

A scene arrives in the request body: the page hands back the one it got from
the catalogue, so everything on it -- including its id -- is whatever the
caller wrote there. A cache keyed on the id alone therefore treats two
different scenes sharing an id as one entry, and serves each of them the
other's pixels. That is a stale picture at best, and at worst a body naming a
real scene while pointing its bands somewhere else.
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

from backend import service  # noqa: E402
from backend.geo import normalise_aoi  # noqa: E402

UTM = CRS.from_epsg(32630)


def write_flat(path, value):
    """A small GeoTIFF of one value, standing in for a remote asset."""
    size, res = 256, 20.0
    data = np.full((size, size), value, dtype="uint16")
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size,
                       count=1, dtype="uint16", crs=UTM, nodata=0,
                       transform=Affine(res, 0, 500000.0, 0, -res, 5700000.0)) as dst:
        dst.write(data, 1)
    return str(path)


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    """Two scenes whose pixels differ, over the same ground."""
    root = tmp_path_factory.mktemp("cache")
    names = ("red", "green", "blue", "nir", "scl")
    dark = {n: write_flat(root / f"d_{n}.tif", 2000) for n in names}
    bright = {n: write_flat(root / f"b_{n}.tif", 8000) for n in names}
    from rasterio.warp import transform as warp

    lons, lats = warp(UTM, CRS.from_epsg(4326),
                      [500000.0, 500000.0 + 256 * 20.0],
                      [5700000.0, 5700000.0 - 256 * 20.0])
    area = normalise_aoi({"bbox": [lons[0] + 0.005, lats[1] + 0.005,
                                   lons[1] - 0.005, lats[0] - 0.005]})
    one = {"id": "SCENE", "satellite": "sentinel-2", "source": "earth-search",
           "datetime": "2026-09-21T09:06:01Z", "date": "2026-09-21",
           "cloud": 10.0, "tile": "36TYL", "assets": dark,
           "boa_offset_applied": True, "demo": False}
    two = dict(one, assets=bright)
    return one, two, area


def rendered(scene, area, size=128):
    return service.render({"aoi": area, "scene": scene, "preset": "true_color",
                           "size": size, "mask_clouds": False})


def test_two_scenes_with_one_id_do_not_share_an_entry(pair):
    dark, bright, area = pair
    first = rendered(dark, area)
    second = rendered(bright, area)      # same id, different pixels
    assert dark["id"] == bright["id"]
    assert first["bytes"] != second["bytes"]


def test_the_same_scene_twice_still_comes_from_the_cache(pair):
    """The cache has to go on being a cache."""
    dark, _, area = pair
    assert rendered(dark, area)["bytes"] == rendered(dark, area)["bytes"]


def test_a_scene_with_no_assets_at_all_does_not_break_the_key(pair):
    _, _, area = pair
    with pytest.raises(Exception):
        rendered({"id": "empty", "satellite": "sentinel-2", "assets": {},
                  "date": "2026-09-21"}, area)
