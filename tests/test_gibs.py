"""NASA GIBS as a family of live layers: picking one layer per subject from
the catalogue, how its days are asked for, and its tile proxy."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, copernicus, gibs  # noqa: E402

NOW = dt.datetime(2026, 11, 9, 12, tzinfo=dt.timezone.utc)


def layer(name, title, end="2026-11-08", start="2014-09-06"):
    return (f"<Layer><Name>{name}</Name><Title>{title}</Title>"
            f'<Dimension name="time" units="ISO8601" default="{end}">'
            f"{start}/{end}/P1D</Dimension></Layer>")


def catalogue(*layers):
    return ('<WMS_Capabilities xmlns="http://www.opengis.net/wms" version="1.3.0">'
            "<Capability><Layer>" + "".join(layers) + "</Layer></Capability></WMS_Capabilities>")


XML = catalogue(
    layer("OCO-2_Carbon_Dioxide_Total_Column_Average",
          "Carbon Dioxide (Total Column Average, OCO-2)", end="2026-10-20"),
    layer("OCO-2_Carbon_Dioxide_Global_Mean_Difference",
          "Carbon Dioxide (Difference from Global Mean, OCO-2)"),
    layer("MODIS_Terra_Land_Surface_Temp_Day", "Land Surface Temperature (Day, MODIS, Terra)"),
    layer("MODIS_Terra_Land_Surface_Temp_Night", "Land Surface Temperature (Night, MODIS, Terra)"),
    layer("MODIS_Terra_Land_Surface_Temp_Day_Monthly", "Land Surface Temperature (Day, Monthly)"),
    layer("VIIRS_NOAA20_Thermal_Anomalies_375m_All",
          "Fires and Thermal Anomalies (Day and Night, VIIRS, NOAA-20)"),
    layer("AIRS_L2_Carbon_Monoxide_500hPa_Volume_Mixing_Ratio_Day",
          "Carbon Monoxide (L2, 500 hPa, Day, AIRS)"),
    layer("Old_Methane", "Methane (AIRS)", end="2016-01-01"),
    layer("Coastlines", "Coastlines"),
)


def test_one_layer_per_subject_carbon_dioxide_first():
    fam = gibs.sort_layers(XML, now=NOW)
    ids = [l["id"] for l in fam["layers"]]
    assert ids[0] == "OCO-2_Carbon_Dioxide_Total_Column_Average"
    assert "OCO-2_Carbon_Dioxide_Global_Mean_Difference" not in ids
    assert "MODIS_Terra_Land_Surface_Temp_Day" in ids
    assert "MODIS_Terra_Land_Surface_Temp_Night" in ids
    assert "MODIS_Terra_Land_Surface_Temp_Day_Monthly" not in ids
    assert "VIIRS_NOAA20_Thermal_Anomalies_375m_All" in ids
    assert "Coastlines" not in ids


def test_a_product_that_stopped_years_ago_is_not_offered():
    ids = [l["id"] for l in gibs.sort_layers(XML, now=NOW)["layers"]]
    assert "Old_Methane" not in ids


def test_oco2_weeks_behind_is_still_current():
    fam = gibs.sort_layers(XML, now=NOW)
    co2 = next(l for l in fam["layers"] if l["subject"] == "co2")
    assert co2["time_default"] == "2026-10-20"
    assert co2["frames"][-1] == {"label": "2026-10-20", "at": "2026-10-20", "time": "2026-10-20"}


def test_days_are_asked_for_as_plain_dates():
    fam = gibs.sort_layers(XML, now=NOW)
    for entry in fam["layers"]:
        assert all(len(f["time"]) == 10 for f in entry["frames"])
        assert len(entry["frames"]) == gibs.DAYS_OFFERED


def test_the_panel_shows_the_main_five_and_keeps_the_rest_behind_all_products():
    fam = gibs.sort_layers(XML, now=NOW)
    everyday = [l["subject"] for l in fam["layers"] if l["everyday"]]
    assert everyday[:3] == ["co2", "lst_day", "fires"]
    assert len(everyday) <= copernicus.MOST_EVERYDAY


def test_the_family_carries_its_own_tile_address():
    fam = gibs.sort_layers(XML, now=NOW)
    assert fam["wms"] == "/api/gibs/wms" and fam["key"] == "nasa"
    assert fam["attribution"] == gibs.ATTRIBUTION


def test_an_area_loop_of_a_nasa_layer_asks_for_dates_through_its_proxy():
    fam = gibs.sort_layers(XML, now=NOW)
    found = {"families": [fam], "attribution": "x"}
    plan = copernicus.history(found, "MODIS_Terra_Land_Surface_Temp_Day", 48, days=5)
    assert plan["daily"] and plan["wms"] == "/api/gibs/wms"
    assert [f["time"] for f in plan["frames"]][-1] == "2026-11-08"
    assert all("/" not in f["time"] for f in plan["frames"])
    assert plan["attribution"] == gibs.ATTRIBUTION


def test_an_unreachable_catalogue_gives_an_empty_family_that_says_why(monkeypatch):
    monkeypatch.setattr(gibs, "_sorted", None)

    def fail(refresh=False):
        raise gibs.GibsError("NASA GIBS could not be reached: timed out")

    monkeypatch.setattr(gibs, "capabilities", fail)
    fam = gibs.family(refresh=True)
    assert fam["layers"] == [] and "could not be reached" in fam["problem"]


class Resp:
    def __init__(self, status=200, body=b"\x89PNG..", kind="image/png"):
        self.status_code, self.content = status, body
        self.ok = status < 400
        self.headers = {"Content-Type": kind}


def test_tiles_are_fetched_once_and_held(monkeypatch):
    copernicus.forget_tiles()
    calls = []

    def get(url, params=None, timeout=None):
        calls.append((url, dict(params)))
        return Resp()

    monkeypatch.setattr(gibs._session, "get", get)
    asked = {"layers": "OCO-2_Carbon_Dioxide_Total_Column_Average", "time": "2026-10-20",
             "bbox": "0,0,1,1", "width": "256", "height": "256", "sld": "http://evil"}
    gibs.tile(asked)
    body, kind, hit = gibs.tile(asked)
    assert hit and kind == "image/png" and len(calls) == 1
    assert calls[0][0] == gibs.WMS and "sld" not in calls[0][1]


def test_a_refusal_or_a_page_instead_of_a_picture_is_an_error(monkeypatch):
    copernicus.forget_tiles()
    monkeypatch.setattr(gibs._session, "get", lambda *a, **k: Resp(500))
    with pytest.raises(gibs.GibsError, match="500"):
        gibs.tile({"layers": "x", "time": "2026-10-01"})
    monkeypatch.setattr(gibs._session, "get", lambda *a, **k: Resp(kind="text/xml"))
    with pytest.raises(gibs.GibsError, match="rather than an image"):
        gibs.tile({"layers": "y", "time": "2026-10-01"})


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        return TestClient(app)

    def test_the_demo_catalogue_has_a_nasa_family(self, client, monkeypatch):
        monkeypatch.setattr(config, "DEMO_MODE", True)
        got = client.get("/api/copernicus").json()
        nasa = next(f for f in got["families"] if f["key"] == "nasa")
        assert nasa["layers"][0]["subject"] == "co2"

    def test_nasa_still_shows_when_eumetsat_is_down(self, client, monkeypatch):
        monkeypatch.setattr(config, "DEMO_MODE", False)

        def down(refresh=False):
            raise copernicus.CopernicusError("EUMETSAT View could not be reached")

        monkeypatch.setattr(copernicus, "layers", down)
        monkeypatch.setattr(gibs, "family", lambda refresh=False: gibs.sort_layers(XML, now=NOW))
        got = client.get("/api/copernicus").json()
        assert [f["key"] for f in got["families"]] == ["nasa"]

    def test_both_down_is_an_error(self, client, monkeypatch):
        monkeypatch.setattr(config, "DEMO_MODE", False)

        def down(refresh=False):
            raise copernicus.CopernicusError("EUMETSAT View could not be reached")

        monkeypatch.setattr(copernicus, "layers", down)
        monkeypatch.setattr(gibs, "family",
                            lambda refresh=False: {**gibs.FAMILY, "layers": [], "stale": 0})
        assert client.get("/api/copernicus").status_code == 502
