"""The Detect fires family: every fire product from every satellite, in one
place at the top of the live panel, each still fetched from where it lives."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, copernicus, gibs  # noqa: E402

from tests.test_gibs import FIRE_XML, NOW, XML  # noqa: E402


def families():
    """The demo's EUMETSAT families and NASA's, before gathering."""
    eumetsat = copernicus.demo()["families"]
    nasa = gibs.sort_layers(FIRE_XML, now=NOW)
    return [*eumetsat, nasa]


def test_the_fire_family_comes_first_with_every_fire_product():
    out = copernicus.gather_fires(families())
    fires = out[0]
    assert fires["key"] == "fires" and fires["short"] == "🔥 Detect fires"
    titles = [l["title"] for l in fires["layers"]]
    assert titles == [
        "Meteosat MTG Fire Temperature · 10 min",
        "VIIRS NOAA-20 fires · 375 m",
        "VIIRS Suomi-NPP fires · 375 m",
        "VIIRS NOAA-21 fires · 375 m",
        "MODIS Terra fires · 1 km",
        "MODIS Aqua fires · 1 km",
        "Sentinel-3A SLSTR fire power · 1 km",
    ]


def test_they_leave_the_families_they_came_from():
    out = copernicus.gather_fires(families())
    for family in out[1:]:
        assert not any(copernicus.is_fire(l) for l in family["layers"]), family["key"]
    # And nothing else is lost.
    before = sum(len(f["layers"]) for f in families())
    assert sum(len(f["layers"]) for f in out) == before


def test_each_keeps_where_its_tiles_come_from_and_how_its_days_are_named():
    fires = copernicus.gather_fires(families())[0]["layers"]
    meteosat, viirs = fires[0], fires[1]
    assert meteosat["wms"] == copernicus.TILES and meteosat["animates"] is True
    assert meteosat["spans_days"] is False and meteosat["time_as_date"] is False
    assert viirs["wms"] == gibs.TILES and viirs["time_as_date"] is True
    assert viirs["spans_days"] is True and viirs["attribution"] == gibs.ATTRIBUTION
    assert viirs["full_title"].startswith("Fires and Thermal Anomalies")


def test_meteosat_still_opens_with_the_map():
    fires = copernicus.gather_fires(families())[0]["layers"]
    assert fires[0]["pinned"] and not any(l["pinned"] for l in fires[1:])
    assert all(l["everyday"] for l in fires)


def test_playback_of_a_nasa_fire_layer_is_14_days_of_dates_through_nasa():
    found = {"families": copernicus.gather_fires(families()), "attribution": "x"}
    plan = copernicus.history(found, "MODIS_Terra_Thermal_Anomalies_All", 48)
    assert plan["daily"] and len(plan["frames"]) == copernicus.AREA_DAYS
    assert plan["wms"] == gibs.TILES and "/" not in plan["frames"][-1]["time"]


def test_playback_of_meteosat_is_48_hours_of_ten_minute_frames():
    found = {"families": copernicus.gather_fires(families()), "attribution": "x"}
    plan = copernicus.history(found, "mtg_fd:rgb_firetemperature", 48)
    assert not plan["daily"] and plan["wms"] == copernicus.TILES
    assert len(plan["frames"]) > 100


def test_sentinel_3_fire_power_plays_back_as_days_of_ranges_through_eumetsat():
    found = {"families": copernicus.gather_fires(families()), "attribution": "x"}
    plan = copernicus.history(found, "copernicus:sentinel3a_slstr_level2_frp", 48)
    assert plan["daily"] and plan["wms"] == copernicus.TILES
    assert "/" in plan["frames"][-1]["time"]


def test_no_fire_products_is_an_empty_family_not_an_error():
    out = copernicus.gather_fires([gibs.sort_layers(XML, now=NOW)])
    assert out[0]["key"] == "fires"
    # XML's only fire layer is VIIRS NOAA-20.
    assert [l["id"] for l in out[0]["layers"]] == ["VIIRS_NOAA20_Thermal_Anomalies_375m_All"]
    assert copernicus.gather_fires([])[0]["layers"] == []


def test_the_live_panel_answer_has_it_first(monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import app
    monkeypatch.setattr(config, "DEMO_MODE", True)
    got = TestClient(app).get("/api/copernicus").json()
    assert got["families"][0]["key"] == "fires"
    assert got["families"][0]["layers"][0]["id"] == "mtg_fd:rgb_firetemperature"
