"""S2DR3: the price before anything is spent, the key kept in memory, the
job followed, the product drawn.

Gamma Earth's own client (gamma-earth-s2dr) is used for real, talking to a
stand-in of their API: a session that answers by method and address and
records every request, the way the client's own tests do it. So what is
checked is what this app asks of the real client, and where the key went.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import s2dr3, service  # noqa: E402

from tests.test_neural import demo_request  # noqa: E402

gamma = pytest.importorskip("gamma_earth_s2dr")

API = "https://api.gamma.earth"
KEY = "k" * 32
PID = "T36UUA-945f7dd92-20240601"
SCENE = "S2B_T36UUA_20240601T085603_L2A"
SIGNED = "https://d111.cloudfront.net/x/TCI.tif?Expires=1&Signature=s"
DAY = "2024-06-01"
BBOX = [30.2, 50.3, 30.5, 50.5]          # round the demo request's circle


class Response:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code, self._body = status, body
        self.headers, self._content = headers or {}, content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body

    def iter_content(self, size):
        yield self._content

    def close(self):
        pass


class Session:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        answer = self.routes[(method, url)]
        return answer.pop(0) if isinstance(answer, list) else answer


def tci_bytes(tmp_path) -> bytes:
    """A small 3-band uint8 true-colour GeoTIFF over BBOX, as S2DR3 sends."""
    path = tmp_path / "made.tif"
    rows, cols = 200, 300
    data = np.zeros((3, rows, cols), dtype="uint8")
    data[0], data[1], data[2] = 200, 120, 40
    with rasterio.open(path, "w", driver="GTiff", width=cols, height=rows, count=3,
                       dtype="uint8", crs="EPSG:4326",
                       transform=from_bounds(*BBOX, cols, rows)) as dst:
        dst.write(data)
    return path.read_bytes()


def scene_feature(credits=4, processable=True):
    f = {"type": "Feature", "stac_version": "1.0.0", "id": SCENE,
         "collection": "sentinel-2-c1-l2a", "bbox": BBOX,
         "geometry": {"type": "Polygon", "coordinates": [[[30.2, 50.3], [30.5, 50.3],
                                                          [30.5, 50.5], [30.2, 50.5],
                                                          [30.2, 50.3]]]},
         "properties": {"datetime": f"{DAY}T08:56:03Z", "s2dr3:status": "unprocessed",
                        "s2dr3:processable": processable},
         "assets": {}, "links": []}
    if processable:
        f["links"].append({"rel": "s2dr3:process", "href": f"{API}/jobs/{{api_key}}",
                           "method": "POST",
                           "body": {"aoi": "30.31 50.40 30.35 50.44", "date": DAY,
                                    "date_range": 0, "max_clouds": 100}})
        if credits is not None:
            f["properties"]["s2dr3:estimated_credits"] = credits
    return f


def product_feature():
    return {"type": "Feature", "stac_version": "1.0.0", "id": PID, "collection": "s2dr3",
            "bbox": BBOX, "links": [],
            "geometry": scene_feature()["geometry"],
            "properties": {"datetime": f"{DAY}T08:56:03Z"},
            "assets": {"tci": {"href": f"{API}/stac/assets/{PID}/tci"},
                       "ms": {"href": f"{API}/stac/assets/{PID}/ms"}}}


def found(*features):
    return {"type": "FeatureCollection", "features": list(features), "links": []}


@pytest.fixture
def api(monkeypatch, tmp_path):
    """The stand-in API, the real client on it, products into tmp_path."""
    monkeypatch.setenv("S2DR3_DIR", str(tmp_path / "products"))
    monkeypatch.delenv("GE_API_KEY", raising=False)
    tif = tci_bytes(tmp_path)
    routes = {
        ("POST", f"{API}/stac/search"): Response(200, found(scene_feature())),
        ("POST", f"{API}/jobs/{KEY}"): Response(200, {
            "job_id": "j1", "pid": PID, "stac_job": f"{API}/stac/jobs/j1"}),
        ("GET", f"{API}/stac/jobs/j1"): Response(200, found(product_feature())),
        ("GET", f"{API}/stac/assets/{PID}/tci"): Response(302, headers={"Location": SIGNED}),
        ("GET", SIGNED): Response(200, content=tif, headers={"Content-Length": str(len(tif))}),
    }
    session = Session(routes)
    monkeypatch.setattr(s2dr3, "_client",
                        lambda: gamma.Client(API, KEY, session=session))
    s2dr3._quotes.clear()
    s2dr3._job.clear()
    s2dr3._job["state"] = "idle"
    yield session
    s2dr3.set_key(None)


def run_now(entry):
    """start() with the job followed on this thread, so the test can look."""
    s2dr3._follow(entry)


class _Inline:
    def __init__(self, target, args, **_):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setattr(s2dr3.threading, "Thread", _Inline)


BOUNDS = (30.31, 50.40, 30.35, 50.44)


# ── The price ──────────────────────────────────────────────────


class TestTheQuote:
    def test_a_price_is_given_and_nothing_is_spent(self, api):
        got = s2dr3.quote(BOUNDS, DAY)
        assert got["credits"] == 4 and got["owned"] is False and got["token"]
        assert not [c for c in api.calls if "/jobs/" in c[1]]

    def test_a_product_already_owned_is_free(self, api):
        api.routes[("POST", f"{API}/stac/search")] = Response(
            200, found(product_feature(), scene_feature()))
        got = s2dr3.quote(BOUNDS, DAY)
        assert got["owned"] is True and got["credits"] == 0

    def test_no_scene_that_day_is_said(self, api):
        api.routes[("POST", f"{API}/stac/search")] = Response(200, found())
        with pytest.raises(s2dr3.S2dr3Error, match="no Sentinel-2 scene"):
            s2dr3.quote(BOUNDS, DAY)

    def test_too_large_an_area_is_said(self, api):
        api.routes[("POST", f"{API}/stac/search")] = Response(
            200, found(scene_feature(processable=False)))
        with pytest.raises(s2dr3.S2dr3Error, match="too large"):
            s2dr3.quote(BOUNDS, DAY)

    def test_a_scene_with_no_price_is_not_offered(self, api):
        api.routes[("POST", f"{API}/stac/search")] = Response(
            200, found(scene_feature(credits=None)))
        with pytest.raises(s2dr3.S2dr3Error, match="no price"):
            s2dr3.quote(BOUNDS, DAY)

    def test_the_key_goes_in_the_header_and_nowhere_else(self, api):
        s2dr3.quote(BOUNDS, DAY)
        method, url, kwargs = api.calls[0]
        assert kwargs["headers"] == {"X-API-Key": KEY} and KEY not in url

    @pytest.mark.parametrize("bounds, date", [
        ((1, 2, 3), DAY), ((10, 5, 5, 10), DAY), (BOUNDS, "yesterday"), (BOUNDS, "2024-13-01"),
    ])
    def test_nonsense_is_refused_before_asking(self, api, bounds, date):
        with pytest.raises(s2dr3.S2dr3Error):
            s2dr3.quote(bounds, date)
        assert api.calls == []


# ── The job ────────────────────────────────────────────────────


class TestTheJob:
    def test_only_the_quoted_price_is_accepted(self, api, inline):
        quote = s2dr3.quote(BOUNDS, DAY)
        with pytest.raises(s2dr3.S2dr3Error, match="not the price"):
            s2dr3.start(quote["token"], 5)
        # The quote is used up by trying: a second go has to ask again.
        with pytest.raises(s2dr3.S2dr3Error, match="expired"):
            s2dr3.start(quote["token"], 4)

    def test_an_unknown_token_spends_nothing(self, api, inline):
        with pytest.raises(s2dr3.S2dr3Error, match="expired"):
            s2dr3.start("made-up", 4)
        assert not [c for c in api.calls if "/jobs/" in c[1]]

    def test_agreed_it_runs_downloads_and_says_so(self, api, inline):
        quote = s2dr3.quote(BOUNDS, DAY)
        state = s2dr3.start(quote["token"], quote["credits"])
        job = state["job"]
        assert job["state"] == "done", job
        posted = [kw for m, u, kw in api.calls if m == "POST" and "/jobs/" in u]
        assert len(posted) == 1 and posted[0]["json"]["date"] == DAY
        product = Path(job["product"]["path"])
        assert product.is_file() and product.name == f"{PID}_tci.tif"
        # The signed download address never sees the key.
        signed = [kw for m, u, kw in api.calls if u == SIGNED][0]
        assert KEY not in str(signed)

    def test_an_owned_product_is_downloaded_without_a_job(self, api, inline):
        api.routes[("POST", f"{API}/stac/search")] = Response(200, found(product_feature()))
        quote = s2dr3.quote(BOUNDS, DAY)
        state = s2dr3.start(quote["token"], 0)
        assert state["job"]["state"] == "done"
        assert not [c for c in api.calls if c[0] == "POST" and "/jobs/" in c[1]]

    def test_a_failure_ends_the_job_and_is_said(self, api, inline):
        api.routes[("POST", f"{API}/jobs/{KEY}")] = Response(402, {"error": "no credits left"})
        quote = s2dr3.quote(BOUNDS, DAY)
        job = s2dr3.start(quote["token"], 4)["job"]
        assert job["state"] == "failed" and "no credits left" in job["error"]
        assert KEY not in job["error"]


# ── The key ────────────────────────────────────────────────────


class TestTheKey:
    def test_it_is_held_and_never_shown(self, monkeypatch):
        monkeypatch.delenv("GE_API_KEY", raising=False)
        s2dr3.set_key(KEY)
        try:
            state = s2dr3.status()
            assert state["has_key"] is True and KEY not in str(state)
        finally:
            s2dr3.set_key(None)
        assert s2dr3.status()["has_key"] is False

    @pytest.mark.parametrize("bad", ["short", "has space in it", "x" * 300, "naïve-key-123"])
    def test_nonsense_is_refused(self, bad):
        with pytest.raises(s2dr3.S2dr3Error):
            s2dr3.set_key(bad)

    def test_it_is_never_written_down(self, monkeypatch, tmp_path):
        monkeypatch.setenv("S2DR3_DIR", str(tmp_path))
        s2dr3.set_key(KEY)
        s2dr3.set_key(None)
        assert list(tmp_path.iterdir()) == []

    def test_without_one_it_asks(self, monkeypatch):
        monkeypatch.delenv("GE_API_KEY", raising=False)
        s2dr3.set_key(None)
        with pytest.raises(s2dr3.NoKey):
            s2dr3._client()


# ── Drawing it ─────────────────────────────────────────────────


class TestTheRender:
    def test_the_product_is_drawn_at_one_metre(self, api, inline):
        quote = s2dr3.quote(BOUNDS, DAY)
        s2dr3.start(quote["token"], 4)
        req = demo_request(s2dr3=True, clip=False)
        got = service.render(req)
        meta = got["meta"]
        assert meta["s2dr3"]["name"] == "S2DR3" and meta["effective_res_m"] == 1.0
        assert meta["grid"]["ground_res_m"] == pytest.approx(1.0, rel=0.05) \
            or meta["grid"]["width"] == 4096 or meta["grid"]["height"] == 4096
        assert got["media_type"] == "image/png" and meta["valid_pct"] > 99

    def test_it_is_found_again_after_a_restart(self, api, inline):
        quote = s2dr3.quote(BOUNDS, DAY)
        s2dr3.start(quote["token"], 4)
        s2dr3._job.clear()                       # what a new run starts with
        found_again = s2dr3.product_for(BOUNDS, DAY)
        assert found_again and Path(found_again["path"]).is_file()

    def test_none_yet_says_which_button(self, api):
        with pytest.raises(service.RenderError, match="S2DR3 button"):
            service.render(demo_request(s2dr3=True))

    def test_the_geotiff_comes_from_the_same_picture(self, api, inline):
        quote = s2dr3.quote(BOUNDS, DAY)
        s2dr3.start(quote["token"], 4)
        got = service.render(demo_request(s2dr3=True, format="geotiff"))
        assert got["media_type"] == "image/tiff" and got["bytes"][:4] in (b"II*\x00", b"MM\x00*")


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        return TestClient(app)

    def test_status_never_carries_the_key(self, client):
        s2dr3.set_key(KEY)
        try:
            got = client.get("/api/s2dr3")
            assert got.json()["has_key"] is True and KEY not in got.text
        finally:
            s2dr3.set_key(None)

    @pytest.mark.parametrize("path, body", [
        ("/api/s2dr3/key", {"key": KEY}),
        ("/api/s2dr3/quote", {"aoi": None, "scene": {}}),
        ("/api/s2dr3/start", {"token": "t", "credits": 1}),
        ("/api/s2dr3/install", {}),
    ])
    def test_another_website_can_do_none_of_it(self, client, path, body):
        got = client.post(path, json=body, headers={"origin": "https://elsewhere.example"})
        assert got.status_code == 403
        assert s2dr3.status()["has_key"] is False or path != "/api/s2dr3/key"

    def test_a_form_with_no_preflight_cannot_start_a_job(self, client):
        got = client.post("/api/s2dr3/start", content=b'{"token":"t","credits":1}',
                          headers={"content-type": "text/plain"})
        assert got.status_code >= 400 and s2dr3.status()["job"].get("state") != "running"

    def test_the_page_can_set_and_forget_the_key(self, client, monkeypatch):
        monkeypatch.delenv("GE_API_KEY", raising=False)
        assert client.post("/api/s2dr3/key", json={"key": KEY}).json()["has_key"] is True
        assert client.post("/api/s2dr3/key", json={"key": ""}).json()["has_key"] is False
