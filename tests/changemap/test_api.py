"""The change map through EarthViewer's API, in the demo build (synthetic
results, nothing downloaded) and with the offline archive of the original
tests (the real engine on synthetic Sentinel-1 GeoTIFFs)."""

from __future__ import annotations

import io
import json
import time
import zipfile

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from PIL import Image

from backend import config
from backend.app import app
from backend.changemap import engine, jobs
from tests.changemap import conftest

BODY = {"bounds": list(conftest.AOI), "start": "2026-07-04", "end": "2026-08-04", "images": 3,
        "label": "Irpin"}


@pytest.fixture
def client():
    return TestClient(app)


def wait_done(client, timeout=120):
    until = time.time() + timeout
    while time.time() < until:
        got = client.get("/api/change/job").json()
        if got["state"] != "running":
            return got
        time.sleep(0.1)
    raise AssertionError("the change map never finished")


@pytest.fixture
def demo(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEMO_MODE", True)
    monkeypatch.setattr(engine, "CACHE_DIR", tmp_path / "cache")
    jobs._held.clear()


def test_the_plan_says_which_images_and_how_much(client, demo):
    got = client.post("/api/change/plan", json=BODY).json()
    assert len(got["before"]) == 3 and len(got["after"]) == 3
    assert got["download_mb"] > 0 and got["params"]["label"] == "Irpin"
    assert not got["saved"]


@pytest.mark.parametrize("change, said", [
    ({"start": "2026-08-04", "end": "2026-07-04"}, "after the start"),
    ({"end": "2099-01-01"}, "future"),
    ({"start": "2010-01-01"}, "October 2014"),
    ({"bounds": None}, "Draw an area"),
])
def test_a_bad_request_is_said_plainly(client, demo, change, said):
    got = client.post("/api/change/plan", json={**BODY, **change})
    assert got.status_code == 400 and said in got.json()["detail"]


def test_a_tiny_area_is_enlarged_and_says_so(client, demo):
    got = client.post("/api/change/plan", json={**BODY, "bounds": [30.2, 50.5, 30.2001, 50.5001]}).json()
    assert "Enlarged" in got["note"]


def test_run_result_presets_saved_and_files(client, demo):
    started = client.post("/api/change/run", json=BODY)
    assert started.status_code == 200
    done = wait_done(client)
    assert done["state"] == "done", done
    key = done["key"]

    balanced = client.get(f"/api/change/result/{key}").json()
    assert balanced["overlay"].startswith("data:image/png")
    assert balanced["summary"]["spots"] >= 3 and balanced["detection"]["name"] == "Balanced"
    assert balanced["summary"]["noise"]["level"] in ("good", "fair", "poor")
    assert {"before", "after"} <= set(balanced["layers"])
    strict = client.get(f"/api/change/result/{key}?preset=Strict").json()
    assert strict["detection"]["min_db"] == 6.0
    custom = client.get(f"/api/change/result/{key}?preset=Custom&alpha=0.001&min_db=1&min_area_m2=100").json()
    assert custom["detection"]["name"] == "Custom"

    saved = client.get("/api/change/saved").json()["saved"]
    assert [s["key"] for s in saved] == [key]

    png = client.get(f"/api/change/export/{key}?kind=png")
    assert png.headers["content-type"] == "image/png"
    assert "attachment" in png.headers["content-disposition"]
    assert max(Image.open(io.BytesIO(png.content)).size) >= 1400
    csv = client.get(f"/api/change/export/{key}?kind=csv").text
    assert csv.startswith("latitude,longitude") and len(csv.splitlines()) >= 4
    geo = json.loads(client.get(f"/api/change/export/{key}?kind=geojson").text)
    assert geo["type"] == "FeatureCollection" and geo["features"][0]["geometry"]["type"] == "Point"
    tif = client.get(f"/api/change/export/{key}?kind=tif").content
    with rasterio.MemoryFile(tif) as mem, mem.open() as src:
        assert src.count == 2 and src.crs.to_epsg() == 3857
        kinds = src.read(1)
        assert {1.0, 2.0} <= set(np.unique(kinds[np.isfinite(kinds)]).tolist())
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/change/export/{key}?kind=zip").content)) as zf:
        names = zf.namelist()
        assert "README.txt" in names and sum(n.endswith((".png", ".csv", ".geojson", ".tif"))
                                             for n in names) == 4

    assert client.delete(f"/api/change/saved/{key}").json()["saved"] == []
    jobs._held.clear()
    assert client.get(f"/api/change/result/{key}").status_code == 400


def test_a_key_that_is_not_one_is_refused(client, demo):
    assert client.get("/api/change/result/../../etc").status_code in (400, 404)
    assert client.get("/api/change/export/zzzz?kind=csv").status_code == 400
    assert client.get("/api/change/export/0123456789abcdef?kind=exe").status_code == 400


def test_another_website_cannot_start_one(client, demo):
    got = client.post("/api/change/run", json=BODY, headers={"origin": "https://elsewhere.example"})
    assert got.status_code == 403


def test_the_real_engine_through_the_api(client, offline, monkeypatch):
    """The engine of the original app, on its synthetic archive, end to end."""
    monkeypatch.setattr(config, "DEMO_MODE", False)
    jobs._held.clear()
    plan = client.post("/api/change/plan", json=BODY).json()
    assert plan["orbit"] == 36 and len(plan["before"]) == 3
    client.post("/api/change/run", json=BODY)
    done = wait_done(client)
    assert done["state"] == "done", done
    got = client.get(f"/api/change/result/{done['key']}").json()
    # The patch that lost 7 dB and the "aircraft" that came and went.
    assert got["summary"]["spots"] == 2
    kinds = sorted(s["kind"] for s in got["spots"])
    assert kinds == ["decrease", "increase"]
    assert got["summary"]["noise"]["level"] == "good"
