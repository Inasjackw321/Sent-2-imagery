"""Active fire detection: the Schroeder et al. (2016) tests on Sentinel-2 /
Landsat bands, and the picture and report made from them."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import activefire, service  # noqa: E402

from tests.test_neural import demo_request  # noqa: E402

# Green vegetation: bright in the near infrared, dark in SWIR.
GROUND = {"coastal": 0.05, "red": 0.05, "nir08": 0.35, "swir16": 0.20, "swir22": 0.10}


def scene(shape=(80, 80), spots=None):
    """Bands of ordinary ground, with some pixels set to other reflectances."""
    out = {k: np.full(shape, v, dtype="float32") for k, v in GROUND.items()}
    for (y, x), values in (spots or {}).items():
        for k, v in values.items():
            out[k][y, x] = v
    return {k: np.ma.masked_array(v, mask=np.zeros(shape, bool)) for k, v in out.items()}


FLAMES = {"nir08": 0.20, "swir16": 0.60, "swir22": 0.90}           # unambiguous
SATURATED = {"coastal": 0.10, "nir08": 0.45, "swir16": 0.90, "swir22": 0.95}
SMOULDER = {"nir08": 0.15, "swir16": 0.17, "swir22": 0.35}         # potential
BRIGHT_SOIL = {"nir08": 0.30, "swir16": 0.40, "swir22": 0.35}


def test_flames_are_unambiguous():
    found = activefire.detect(scene(spots={(40, 40): FLAMES}))
    assert found["unambiguous"][40, 40] and found["fire"].sum() == 1


def test_the_hottest_fires_near_saturation_are_caught():
    found = activefire.detect(scene(spots={(10, 10): SATURATED}))
    assert found["unambiguous"][10, 10]


def test_a_weaker_fire_standing_out_from_its_ground_is_potential():
    found = activefire.detect(scene(spots={(40, 40): SMOULDER}))
    assert found["potential"][40, 40] and not found["unambiguous"][40, 40]


def test_bright_soil_and_plain_ground_are_not_fire():
    found = activefire.detect(scene(spots={(20, 20): BRIGHT_SOIL}))
    assert found["fire"].sum() == 0


def test_a_whole_field_of_candidates_has_no_ground_to_stand_out_from():
    # Every pixel at the potential level -- a vast bright roof, say. Nothing
    # to compare with, so nothing is confirmed.
    bands = scene((40, 40))
    for k, v in SMOULDER.items():
        bands[k] = np.ma.masked_array(np.full((40, 40), v, "float32"), mask=np.zeros((40, 40), bool))
    found = activefire.detect(bands)
    assert found["potential"].sum() == 0


def test_masked_pixels_are_never_fire():
    bands = scene(spots={(5, 5): FLAMES})
    for k in bands:
        bands[k].mask[5, 5] = True
    assert activefire.detect(bands)["fire"].sum() == 0


def test_separate_fires_are_separate_clusters_biggest_first():
    spots = {(10, 10): FLAMES, (10, 11): FLAMES, (11, 10): FLAMES, (60, 60): FLAMES}
    found = activefire.detect(scene(spots=spots))
    groups = activefire.clusters(found["fire"], np.ma.filled(scene(spots=spots)["swir22"], np.nan))
    assert [g["pixels"] for g in groups] == [3, 1]
    assert groups[0]["peak_swir22"] == pytest.approx(0.9)


def test_the_window_is_about_two_kilometres_and_odd():
    assert activefire.window_for(30) == 61
    assert activefire.window_for(20) % 2 == 1
    assert activefire.window_for(1) == activefire.MOST_WINDOW
    assert activefire.window_for(500) == activefire.LEAST_WINDOW


def test_the_picture_dims_the_ground_and_paints_the_fire():
    found = activefire.detect(scene(spots={(40, 40): FLAMES, (20, 20): SMOULDER}))
    rgb = np.full((80, 80, 3), 200, dtype="uint8")
    out = activefire.paint(rgb, found)
    assert tuple(out[40, 40]) == activefire.UNAMBIGUOUS
    assert tuple(out[20, 20]) == activefire.POTENTIAL
    assert tuple(out[40, 41]) == activefire.HALO
    assert tuple(out[0, 0]) == (110, 110, 110)


# ── Through the render ─────────────────────────────────────────


def test_the_render_reports_what_it_found():
    meta = service.render(demo_request(preset="active_fire"))["meta"]
    fire = meta["fire"]
    for key in ("pixels", "unambiguous", "potential", "clusters", "area_ha", "where", "method"):
        assert key in fire
    assert any("active fire" in step for step in meta["enhancements"])


def test_it_looks_at_one_pass_only():
    req = demo_request(preset="active_fire")
    req["scenes"] = [req["scene"], {**req["scene"], "id": "demo-2024-06-06", "date": "2024-06-06"}]
    with pytest.raises(service.RenderError, match="one pass"):
        service.render(req)


def test_the_cloud_mask_is_switched_off_and_said():
    meta = service.render(demo_request(preset="active_fire", mask_clouds=True))["meta"]
    assert any("cloud mask off" in step for step in meta["enhancements"])


def test_a_fire_in_the_bands_is_found_and_located(monkeypatch):
    """A real fire placed in the demo scene's bands comes out in the report,
    at the right place on the map."""
    real_gather = service._gather

    def with_fire(scenes, geometry, grid, names, req, sat):
        bands, cloud, comp, sr, g = real_gather(scenes, geometry, grid, names, req, sat)
        y, x = g.height // 2, g.width // 2
        for k, v in {**GROUND, **FLAMES}.items():
            if k in bands:
                arr = bands[k].copy()
                arr[y - 1:y + 2, x - 1:x + 2] = v
                bands[k] = arr
        return bands, cloud, comp, sr, g

    monkeypatch.setattr(service, "_gather", with_fire)
    req = demo_request(preset="active_fire", clip=False)
    fire = service.render(req)["meta"]["fire"]
    assert fire["unambiguous"] >= 9 and fire["clusters"] >= 1
    biggest = fire["where"][0]
    assert biggest["pixels"] >= 9
    assert biggest["lat"] == pytest.approx(50.42, abs=0.01)
    assert biggest["lon"] == pytest.approx(30.33, abs=0.01)
