"""SEN2SR: a trained network redrawing one Sentinel-2 date at 2.5 m.

Most of this runs without PyTorch. The network is one function -- a 128-pixel
tile of four bands in, a 512-pixel tile out -- and everything around it is
plain arithmetic that can be checked with a stand-in for it: the tiling, the
padding, the masking, the native read, the placing on the map, the refusals,
the install and the download. The one test that needs the real classes is
skipped where they are not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import transform_bounds

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config, neural, raster, service  # noqa: E402
from backend.geo import WGS84  # noqa: E402


def nearest_x4(tile: np.ndarray) -> np.ndarray:
    """A stand-in for the network that is exactly predictable: each pixel x4."""
    assert tile.shape[1:] == (neural.TILE, neural.TILE), tile.shape
    return np.repeat(np.repeat(tile, neural.SCALE, axis=1), neural.SCALE, axis=2)


def four(shape, seed=0):
    rng = np.random.default_rng(seed)
    return {b: np.ma.masked_array(rng.random(shape, dtype="float32") * 0.4)
            for b in neural.BANDS}


# ── Tiling ─────────────────────────────────────────────────────


class TestTiling:
    @pytest.mark.parametrize("length", [128, 129, 200, 224, 300, 1024])
    def test_every_pixel_is_in_a_tile_and_none_runs_off(self, length):
        got = neural.starts(length)
        covered = np.zeros(length, bool)
        for at in got:
            assert 0 <= at <= length - neural.TILE
            covered[at:at + neural.TILE] = True
        assert covered.all()
        assert got[-1] == length - neural.TILE

    def test_neighbours_overlap_by_at_least_the_overlap(self):
        got = neural.starts(1000)
        assert all(b - a <= neural.TILE - neural.OVERLAP for a, b in zip(got, got[1:]))

    @pytest.mark.parametrize("shape", [(128, 128), (300, 170), (170, 300), (129, 450)])
    def test_the_seams_are_invisible_on_any_shape(self, shape):
        # The released tiler sized its output from the height on both axes,
        # so a picture that was not square came out wrong. A stand-in that
        # repeats pixels has exactly one right answer to compare against.
        stack = np.random.default_rng(1).random((4, *shape), dtype="float32")
        got = neural.tiled(stack, nearest_x4)
        assert got.shape == (4, shape[0] * 4, shape[1] * 4)
        assert np.array_equal(got, nearest_x4_whole(stack))

    @pytest.mark.parametrize("shape", [(300, 170), (170, 300), (400, 400)])
    def test_each_seam_keeps_the_inside_of_both_tiles(self, shape):
        """A network is least sure at a tile's edge, which is why tiles overlap.

        This stand-in marks every pixel within half the overlap of its tile's
        edge. Kept only where that edge is the picture's own edge, so nothing
        marked may survive anywhere inside.
        """
        skip = neural.OVERLAP * neural.SCALE // 2

        def edgy(tile):
            out = nearest_x4(tile).copy()
            out[:, :skip, :] += 9
            out[:, -skip:, :] += 9
            out[:, :, :skip] += 9
            out[:, :, -skip:] += 9
            return out

        stack = np.random.default_rng(2).random((4, *shape), dtype="float32")
        got = neural.tiled(stack, edgy)
        inside = (slice(None), slice(skip, -skip), slice(skip, -skip))
        assert np.array_equal(got[inside], nearest_x4_whole(stack)[inside])

    def test_a_tile_of_the_wrong_scale_is_refused(self):
        with pytest.raises(neural.NeuralError):
            neural.tiled(np.zeros((4, 128, 128), "float32"), lambda t: t)


def nearest_x4_whole(stack):
    return np.repeat(np.repeat(stack, 4, axis=1), 4, axis=2)


# ── Enhancing ──────────────────────────────────────────────────


class TestEnhance:
    def test_four_times_finer_and_the_same_values(self):
        bands = four((200, 150))
        got = neural.enhance(bands, run=nearest_x4)
        assert set(got) == set(neural.BANDS)
        for b in neural.BANDS:
            assert got[b].shape == (800, 600)
            assert np.allclose(got[b].data, nearest_x4_whole(bands[b].data[None])[0])

    def test_an_area_under_one_tile_is_padded_then_cut_back(self):
        # Under 128 pixels the network's constraint does not fit at all.
        bands = four((60, 90))
        got = neural.enhance(bands, run=nearest_x4)
        assert got["red"].shape == (240, 360)
        assert np.allclose(got["red"].data, nearest_x4_whole(bands["red"].data[None])[0])

    def test_holes_are_masked_again_at_the_finer_size(self):
        bands = four((130, 130))
        for b in neural.BANDS:
            bands[b][10:20, 30:40] = np.ma.masked
        got = neural.enhance(bands, run=nearest_x4)
        mask = np.ma.getmaskarray(got["green"])
        assert mask[40:80, 120:160].all()
        assert mask.sum() == 10 * 10 * 16

    def test_a_hole_in_one_band_is_a_hole_in_all(self):
        for one in neural.BANDS:
            bands = four((130, 130))
            bands[one][0:5, 0:5] = np.ma.masked
            got = neural.enhance(bands, run=nearest_x4)
            for b in neural.BANDS:
                assert np.ma.getmaskarray(got[b])[:20, :20].all(), (one, b)

    def test_the_network_never_sees_a_black_hole(self):
        # Filled with zero, a cloud hole is a black wall the network sharpens
        # into a dark halo. It is filled from its neighbours instead.
        bands = {b: np.ma.masked_array(np.full((130, 130), 0.3, "float32"))
                 for b in neural.BANDS}
        for b in neural.BANDS:
            bands[b][50:70, 50:70] = np.ma.masked
            bands[b].data[50:70, 50:70] = 0.0
        seen = []

        def run(tile):
            seen.append(tile.min())
            return nearest_x4(tile)

        neural.enhance(bands, run=run)
        assert min(seen) == pytest.approx(0.3)

    def test_too_large_an_area_is_refused_in_kilometres(self):
        n = neural.MOST_NATIVE + 1
        with pytest.raises(neural.NeuralError, match="km"):
            neural.enhance(four((n, 10)), run=nearest_x4)

    def test_a_missing_band_is_named(self):
        bands = four((130, 130))
        del bands["nir"]
        with pytest.raises(neural.NeuralError, match="nir"):
            neural.enhance(bands, run=nearest_x4)


# ── Reading the satellite's own grid ───────────────────────────

UTM = CRS.from_epsg(32630)
X0, Y0 = 500000.0, 5700000.0
SIDE = 1200


@pytest.fixture(scope="module")
def native_scene(tmp_path_factory):
    root = tmp_path_factory.mktemp("native")
    rows, cols = np.mgrid[0:SIDE, 0:SIDE]
    assets = {}
    for i, band in enumerate(neural.BANDS):
        data = (1000 + (rows * 7 + cols * 13 + i * 101) % 6000).astype("uint16")
        path = root / f"{band}.tif"
        with rasterio.open(path, "w", driver="GTiff", height=SIDE, width=SIDE,
                           count=1, dtype="uint16", crs=UTM, nodata=0,
                           transform=Affine(10, 0, X0, 0, -10, Y0)) as dst:
            dst.write(data, 1)
        assets[band] = str(path)
    return {"id": "NATIVE", "date": "2023-06-15", "datetime": "2023-06-15T11:00:00Z",
            "platform": "sentinel-2a", "boa_offset_applied": False, "demo": False,
            "satellite": "sentinel-2", "assets": assets}


def box_around(x, y, half):
    return transform_bounds(UTM, WGS84, x - half, y - half, x + half, y + half)


class TestNativeRead:
    def test_every_pixel_is_the_files_own(self, native_scene):
        # No resampling at all: the network was trained on pixels as the
        # satellite delivers them.
        bounds = box_around(X0 + 6003, Y0 - 5997, 1500)
        got, grid, _ = raster.read_native(native_scene, bounds, list(neural.BANDS))
        assert grid.crs == UTM
        a = grid.transform
        assert (a.a, a.e) == (10, -10)
        # Snapped to the file's own pixel edges.
        assert (a.c - X0) % 10 == 0 and (Y0 - a.f) % 10 == 0
        col, row = int((a.c - X0) / 10), int((Y0 - a.f) / 10)
        with rasterio.open(native_scene["assets"]["red"]) as src:
            raw = src.read(1)[row:row + grid.height, col:col + grid.width]
        offset = raster.stac.boa_offset(native_scene)
        expected = (raw.astype("float32") + offset) * config.REFLECTANCE_SCALE
        assert np.allclose(got["red"].data, expected)

    def test_it_covers_the_area_asked_for(self, native_scene):
        bounds = box_around(X0 + 6003, Y0 - 5997, 1500)
        _, grid, _ = raster.read_native(native_scene, bounds, list(neural.BANDS))
        west, south, east, north = transform_bounds(WGS84, UTM, *bounds)
        a = grid.transform
        # All of it, and by less than a pixel more on any side.
        assert a.c <= west < a.c + 10
        assert a.f >= north > a.f - 10
        assert a.c + grid.width * 10 >= east > a.c + (grid.width - 1) * 10
        assert a.f - grid.height * 10 <= south < a.f - (grid.height - 1) * 10
        # A box 3 km across, as near as the warp from degrees allows.
        assert 295 <= grid.width <= 310 and 295 <= grid.height <= 310

    def test_an_area_off_the_tile_is_refused(self, native_scene):
        bounds = box_around(X0 - 50000, Y0 + 50000, 1000)
        with pytest.raises(raster.BandReadError, match="cover"):
            raster.read_native(native_scene, bounds, list(neural.BANDS))


# ── Through the render ─────────────────────────────────────────


@pytest.fixture
def fake_network(monkeypatch):
    monkeypatch.setattr(neural, "_torch_runner", lambda: nearest_x4)
    service._neural_cache = service._LRU(capacity=1)


def demo_request(**extra):
    from backend.geo import circle_to_polygon
    scene = {"id": "demo-2024-06-01", "date": "2024-06-01", "demo": True,
             "satellite": "sentinel-2", "cloud": 3.0, "assets": {}}
    return {"aoi": circle_to_polygon(30.33, 50.42, 1500), "scene": scene,
            "satellite": "sentinel-2", "mode": "composite", "preset": "true_color",
            "size": 1024, "mask_clouds": False, "clip": True, **extra}


class TestTheRender:
    def test_a_sen2sr_render_is_at_two_and_a_half_metres(self, fake_network):
        got = service.render(demo_request(sen2sr=True))
        meta = got["meta"]
        assert meta["effective_res_m"] == neural.RES_M
        assert meta["grid"]["ground_res_m"] == pytest.approx(neural.RES_M, rel=0.02)
        assert meta["sen2sr"]["scale"] == 4
        assert meta["sen2sr"]["credit"].startswith("SEN2SR")
        assert any("SEN2SR" in step for step in meta["enhancements"])

    def test_without_the_flag_nothing_changes(self, fake_network, monkeypatch):
        def never(*a, **kw):
            raise AssertionError("SEN2SR ran without being asked")
        monkeypatch.setattr(neural, "enhance", never)
        meta = service.render(demo_request())["meta"]
        assert meta["sen2sr"] is None
        assert meta["effective_res_m"] == 10.0

    def test_ndvi_can_be_drawn_from_it(self, fake_network):
        meta = service.render(demo_request(sen2sr=True, mode="index",
                                           index="ndvi"))["meta"]
        assert meta["sen2sr"] and meta["index"] == "ndvi"

    @pytest.mark.parametrize("extra, said", [
        ({"preset": "swir"}, "red, green, blue and near-infrared"),
        ({"satellite": "landsat"}, "Sentinel-2 only"),
    ])
    def test_what_it_cannot_do_is_said(self, fake_network, extra, said):
        req = demo_request(sen2sr=True, **extra)
        if "satellite" in extra:
            req["scene"] = {**req["scene"], "satellite": extra["satellite"]}
        with pytest.raises(service.RenderError, match=said):
            service.render(req)

    def test_one_date_only(self, fake_network):
        req = demo_request(sen2sr=True)
        req["scenes"] = [req["scene"], {**req["scene"], "id": "demo-2024-06-06",
                                        "date": "2024-06-06"}]
        with pytest.raises(service.RenderError, match="one date"):
            service.render(req)

    def test_not_installed_says_how_to_fix_it(self, monkeypatch):
        service._neural_cache = service._LRU(capacity=1)
        monkeypatch.setattr(neural, "missing", lambda: ["torch"])
        with pytest.raises(service.RenderError, match="press the SEN2SR button"):
            service.render(demo_request(sen2sr=True))


# ── Installing and downloading ─────────────────────────────────


class TestInstall:
    def test_linux_without_a_card_gets_the_cpu_build_first(self, monkeypatch):
        monkeypatch.setattr(neural.platform, "system", lambda: "Linux")
        monkeypatch.setattr(neural, "_has_nvidia", lambda: False)
        first, then = neural.install_command(["torch", "safetensors", "sen2sr"])
        assert first[-2:] == ["--index-url", neural.CPU_INDEX]
        assert "torch" in first and "sen2sr" not in " ".join(first)
        assert neural.NEEDS["sen2sr"] in then and "torch" not in then

    def test_with_a_card_it_is_one_ordinary_install(self, monkeypatch):
        monkeypatch.setattr(neural.platform, "system", lambda: "Linux")
        monkeypatch.setattr(neural, "_has_nvidia", lambda: True)
        (only,) = neural.install_command(["torch", "sen2sr"])
        assert "--index-url" not in only and "torch" in only

    def test_only_what_is_missing(self):
        (only,) = neural.install_command(["safetensors"])
        assert only[-1] == "safetensors" and "torch" not in only
        assert neural.install_command([]) == []

    def test_a_failed_install_is_reported(self, monkeypatch):
        monkeypatch.setattr(neural, "missing", lambda: ["torch"])

        class Done:
            returncode, stdout, stderr = 1, "", "ERROR: no matching distribution"

        assert neural.install(run=lambda *a, **kw: Done()) is False
        assert "no matching distribution" in neural.status()["install_error"]

    def test_success_is_checked_not_assumed(self, monkeypatch):
        monkeypatch.setattr(neural, "missing", lambda: ["torch"])

        class Done:
            returncode, stdout, stderr = 0, "", ""

        # pip said yes and torch is still not importable: that is a failure.
        assert neural.install(run=lambda *a, **kw: Done()) is False


class FakeResponse:
    def __init__(self, body, ok=True, status=200):
        self.content, self.ok, self.status_code = body, ok, status


class TestWeights:
    def test_both_files_arrive_once(self, tmp_path):
        asked = []

        def get(url, **kw):
            asked.append(url)
            name = url.rsplit("/", 1)[1]
            return FakeResponse(b"x" * neural.FILES[name])

        neural.fetch_weights(tmp_path, get=get)
        assert neural.have_weights(tmp_path)
        assert sorted(u.rsplit("/", 1)[1] for u in asked) == sorted(neural.FILES)
        assert all(u.startswith(neural.BASE) for u in asked)
        neural.fetch_weights(tmp_path, get=get)
        assert len(asked) == 2

    def test_a_short_download_is_not_kept(self, tmp_path):
        with pytest.raises(neural.NeuralError, match="bytes"):
            neural.fetch_weights(tmp_path, get=lambda url, **kw: FakeResponse(b"short"))
        assert not any(tmp_path.iterdir())

    def test_a_refusal_is_said(self, tmp_path):
        with pytest.raises(neural.NeuralError, match="404"):
            neural.fetch_weights(tmp_path, get=lambda url, **kw: FakeResponse(
                b"", ok=False, status=404))


class TestTheRealNetwork:
    def test_the_published_layout_loads_and_is_four_times(self, tmp_path):
        """With the real classes, built the way the project's load.py does.

        The weights are stand-ins of the same layout -- the real ones are
        fetched on first use -- and their files come out at exactly the sizes
        published for the real ones, which is the check that this is the
        same network.
        """
        torch = pytest.importorskip("torch")
        pytest.importorskip("sen2sr")
        import safetensors.torch
        from sen2sr.models.opensr_baseline.cnn import CNNSR

        net = CNNSR(4, 4, 24, 4, True, False, 6)
        safetensors.torch.save_file(
            {k: v.contiguous() for k, v in net.state_dict().items()},
            str(tmp_path / "model.safetensor"))
        safetensors.torch.save_file(
            {"weights": torch.ones(512, 512)}, str(tmp_path / "hard_constraint.safetensor"))
        assert neural.have_weights(tmp_path)

        model, device = neural._build(tmp_path)
        with torch.inference_mode():
            out = model(torch.rand(1, 4, 128, 128))
        assert tuple(out.shape) == (1, 4, 512, 512)


# ── The routes ─────────────────────────────────────────────────


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        return TestClient(app)

    def test_status_says_what_the_page_needs(self, client):
        got = client.get("/api/sen2sr").json()
        for key in ("installed", "missing", "weights", "installing", "command",
                    "credit", "most_km"):
            assert key in got

    @pytest.fixture
    def started(self, monkeypatch):
        calls = []
        monkeypatch.setattr(neural, "missing", lambda: ["torch"])
        monkeypatch.setattr(neural, "install_in_background",
                            lambda: calls.append(1) or True)
        return calls

    def test_the_page_can_install_it(self, client, started):
        assert client.post("/api/sen2sr/install", json={}).status_code == 200
        assert started == [1]

    def test_another_website_cannot(self, client, started):
        got = client.post("/api/sen2sr/install", json={},
                          headers={"origin": "https://elsewhere.example"})
        assert got.status_code == 403 and started == []

    def test_nor_a_post_with_no_body_and_no_origin(self, client, started):
        # The one shape of request that slips past both a preflight and the
        # body parser: nothing in it at all.
        assert client.post("/api/sen2sr/install").status_code == 403
        assert started == []

    def test_nor_can_a_form_that_needs_no_preflight(self, client, started):
        got = client.post("/api/sen2sr/install", content=b"{}",
                          headers={"content-type": "text/plain"})
        assert got.status_code >= 400 and started == []
