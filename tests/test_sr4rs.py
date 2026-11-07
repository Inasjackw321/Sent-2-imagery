"""SR4RS: the tiling its worker does, its runtime, its model, its render.

TensorFlow is not needed for any of this. The network is stood in for by the
same trick as SEN2SR's tests -- every coarse pixel becomes four by four of the
same value -- trimmed at the edges exactly as the real export trims them, so
a tile placed one pixel wrong shows up as a wrong value.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import neural, service, sr4rs, sr4rs_worker as worker  # noqa: E402

from tests.test_neural import demo_request  # noqa: E402


def trimmed_x4(batch: np.ndarray) -> np.ndarray:
    """What output_64 is: x4, then 64 fine pixels cut off every side."""
    big = np.repeat(np.repeat(batch, 4, axis=1), 4, axis=2)
    return big[:, worker.PAD_FINE:-worker.PAD_FINE, worker.PAD_FINE:-worker.PAD_FINE, :]


def stack_of(shape, seed=0):
    return np.random.default_rng(seed).uniform(0, 3000, (4, *shape)).astype("float32")


# ── The worker's tiling ────────────────────────────────────────


class TestTheTiles:
    def test_the_fields_are_the_projects_own(self):
        # sr.py: rfield = (efield + 2 * pad) / 4, efield 512, pad 64.
        assert worker.TILE_IN == (worker.TILE_OUT + 2 * worker.PAD_FINE) // 4 == 160
        assert worker.OUTPUT == "output_64:0" and worker.INPUT == "lr_input:0"

    @pytest.mark.parametrize("shape", [(128, 128), (37, 300), (130, 129), (1, 1), (257, 64)])
    def test_every_fine_pixel_comes_from_the_right_coarse_one(self, shape):
        stack = stack_of(shape)
        got = worker.enhance(stack, trimmed_x4)
        want = np.repeat(np.repeat(stack, 4, axis=1), 4, axis=2)
        assert got.shape == (4, shape[0] * 4, shape[1] * 4)
        np.testing.assert_array_equal(got, want)

    def test_it_is_handed_channel_last_tiles_of_160(self):
        seen = []

        def run(batch):
            seen.append(batch.shape)
            return trimmed_x4(batch)

        worker.enhance(stack_of((300, 300)), run, batch=4)
        assert all(s[1:] == (160, 160, 4) for s in seen)
        assert sum(s[0] for s in seen) == 9 and max(s[0] for s in seen) <= 4

    def test_the_edge_has_mirrored_context_not_zeros(self):
        stack = np.full((4, 20, 20), 500.0, dtype="float32")
        firsts = []

        def run(batch):
            firsts.append(batch[0])
            return trimmed_x4(batch)

        worker.enhance(stack, run)
        assert (firsts[0] == 500.0).all()

    def test_a_tile_of_the_wrong_size_is_refused(self):
        with pytest.raises(ValueError):
            worker.enhance(stack_of((10, 10)), lambda b: b)


# ── enhance(), around the worker ───────────────────────────────


def bands_of(shape, holes=None):
    rng = np.random.default_rng(1)
    out = {}
    for b in sr4rs.BANDS:
        data = rng.uniform(0.0, 0.3, shape).astype("float32")
        out[b] = np.ma.masked_array(data, mask=holes if holes is not None else False)
    return out


def whole_x4(stack):
    return np.repeat(np.repeat(stack, 4, axis=1), 4, axis=2)


class TestEnhance:
    def test_it_hands_over_digital_numbers_and_gives_back_reflectance(self):
        bands = bands_of((20, 30))
        seen = {}

        def run(stack):
            seen["max"] = float(stack.max())
            return whole_x4(stack)

        got = sr4rs.enhance(bands, run)
        assert seen["max"] > 100                   # x 10 000, not 0..1
        np.testing.assert_allclose(got["red"][:4, :4], bands["red"][0, 0], rtol=1e-5)
        assert got["nir"].shape == (80, 120)

    def test_holes_are_masked_again_at_the_finer_size(self):
        holes = np.zeros((10, 10), dtype=bool)
        holes[2, 3] = True
        got = sr4rs.enhance(bands_of((10, 10), holes), whole_x4)
        assert got["red"].mask[8:12, 12:16].all()
        assert got["red"].mask.sum() == 16

    def test_a_wrong_shape_back_is_refused(self):
        with pytest.raises(neural.NeuralError):
            sr4rs.enhance(bands_of((10, 10)), lambda s: s)

    def test_too_large_an_area_is_refused_in_kilometres(self):
        with pytest.raises(neural.NeuralError, match="km"):
            sr4rs.enhance(bands_of((1100, 10)), whole_x4)

    def test_a_missing_band_is_named(self):
        bands = bands_of((10, 10))
        del bands["nir"]
        with pytest.raises(neural.NeuralError, match="nir"):
            sr4rs.enhance(bands, whole_x4)

    def test_without_its_runtime_it_says_so(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        with pytest.raises(neural.NotInstalled):
            sr4rs.enhance(bands_of((10, 10)))


# ── The runtime ────────────────────────────────────────────────


class TestTheRuntime:
    def test_a_python_tensorflow_supports_is_used_as_it_is(self):
        steps = sr4rs.install_steps(have_uv=True, version=(3, 12))
        venv = steps[0][1]
        assert venv[venv.index("--python") + 1] == sys.executable

    def test_one_too_new_gets_a_python_uv_fetches(self):
        steps = sr4rs.install_steps(have_uv=True, version=(3, 14))
        venv = steps[0][1]
        assert venv[venv.index("--python") + 1] == sr4rs.RUNTIME_PYTHON

    def test_uv_is_installed_first_when_missing(self):
        steps = sr4rs.install_steps(have_uv=False, version=(3, 14))
        assert "uv" in " ".join(steps[0][1])
        assert len(steps) == 4

    def test_tensorflow_goes_into_the_runtime_not_the_app(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        install = sr4rs.install_steps(have_uv=True)[1][1]
        assert install[install.index("--python") + 1] == str(sr4rs.runtime_python())
        assert any(part.startswith("tensorflow") for part in install)

    def test_it_is_ready_only_once_tensorflow_imported(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        ran = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def run(command, **_):
            ran.append(command)
            sr4rs.runtime_python().parent.mkdir(parents=True, exist_ok=True)
            sr4rs.runtime_python().write_text("")
            return Done()

        assert sr4rs.install(run=run) is True
        assert sr4rs.runtime_ready()
        assert ran[-1][-1] == "import numpy, tensorflow"

    def test_a_failed_step_is_said_and_nothing_is_marked_ready(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))

        class Failed:
            returncode, stdout, stderr = 1, "", "ERROR: no matching distribution\n"

        assert sr4rs.install(run=lambda c, **_: Failed()) is False
        assert "no matching distribution" in sr4rs.status()["install_error"]
        assert not sr4rs.runtime_ready()

    def test_a_half_made_runtime_is_cleared_first(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        stale = sr4rs.runtime_dir() / "leftover"
        stale.parent.mkdir(parents=True)
        stale.write_text("x")

        class Done:
            returncode, stdout, stderr = 0, "", ""

        sr4rs.install(run=lambda c, **_: Done())
        assert not stale.exists()


# ── The model ──────────────────────────────────────────────────


def model_zip(names: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in names.items():
            zf.writestr(name, body)
    return buf.getvalue()


GOOD = {"sr4rs_savedmodel/saved_model.pb": b"graph",
        "sr4rs_savedmodel/variables/variables.index": b"i",
        "sr4rs_savedmodel/variables/variables.data-00000-of-00001": b"d"}


class TestTheModel:
    def test_the_published_layout_unpacks(self, tmp_path):
        archive = tmp_path / "m.zip"
        archive.write_bytes(model_zip(GOOD))
        sr4rs.unpack(archive, tmp_path / "model")
        found = sr4rs.saved_model(tmp_path / "model")
        assert found.name == "sr4rs_savedmodel" and sr4rs.have_weights(tmp_path / "model")

    @pytest.mark.parametrize("names, said", [
        ({**GOOD, "../evil.py": b"x"}, "outside"),
        ({"/etc/evil": b"x", **GOOD}, "outside"),
        ({"readme.txt": b"x"}, "saved_model.pb"),
        ({"m/saved_model.pb": b"g"}, "variables"),
    ])
    def test_anything_else_is_refused_and_nothing_kept(self, tmp_path, names, said):
        archive = tmp_path / "m.zip"
        archive.write_bytes(model_zip(names))
        with pytest.raises(neural.NeuralError, match=said):
            sr4rs.unpack(archive, tmp_path / "model")
        assert not (tmp_path / "model").exists()
        assert not (tmp_path / "model.part").exists()

    def test_not_a_zip_at_all(self, tmp_path):
        archive = tmp_path / "m.zip"
        archive.write_bytes(b"<html>not found</html>")
        with pytest.raises(neural.NeuralError, match="not a zip"):
            sr4rs.unpack(archive, tmp_path / "model")

    def test_it_is_downloaded_once(self, tmp_path):
        body = model_zip(GOOD)
        calls = []

        class Resp:
            ok, status_code = True, 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def iter_content(self, n):
                yield body

        def get(url, **kw):
            calls.append(url)
            return Resp()

        where = tmp_path / "model"
        sr4rs.fetch_weights(where, get=get)
        sr4rs.fetch_weights(where, get=get)
        assert calls == [sr4rs.MODEL_URL]

    def test_a_refusal_is_said(self, tmp_path):
        class Resp:
            ok, status_code = False, 404

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with pytest.raises(neural.NeuralError, match="404"):
            sr4rs.fetch_weights(tmp_path / "model", get=lambda u, **k: Resp())


# ── The render ─────────────────────────────────────────────────


@pytest.fixture
def fake_sr4rs(monkeypatch):
    monkeypatch.setattr(sr4rs, "_worker_runner", lambda: whole_x4)
    service._neural_cache = service._LRU(capacity=1)


class TestTheRender:
    def test_an_sr4rs_render_is_at_two_and_a_half_metres(self, fake_sr4rs):
        meta = service.render(demo_request(sr4rs=True))["meta"]
        assert meta["effective_res_m"] == sr4rs.RES_M
        assert meta["neural"]["name"] == "SR4RS"
        assert meta["neural"]["credit"].startswith("SR4RS")
        assert meta["sen2sr"] is None
        assert any("SR4RS" in step for step in meta["enhancements"])

    def test_it_is_not_mistaken_for_sen2sr_in_the_cache(self, fake_sr4rs, monkeypatch):
        service.render(demo_request(sr4rs=True))
        from tests.test_neural import nearest_x4
        monkeypatch.setattr(neural, "_torch_runner", lambda: nearest_x4)
        meta = service.render(demo_request(sen2sr=True))["meta"]
        assert meta["neural"]["name"] == "SEN2SR"

    def test_two_networks_at_once_is_refused(self, fake_sr4rs):
        with pytest.raises(service.RenderError, match="one network"):
            service.render(demo_request(sr4rs=True, sen2sr=True))

    def test_its_limits_are_said_in_its_name(self, fake_sr4rs):
        with pytest.raises(service.RenderError, match="SR4RS"):
            service.render(demo_request(sr4rs=True, preset="swir"))

    def test_not_installed_says_which_button(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        service._neural_cache = service._LRU(capacity=1)
        with pytest.raises(service.RenderError, match="SR4RS button"):
            service.render(demo_request(sr4rs=True))


class TestTheRoutes:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        return TestClient(app)

    def test_status(self, client):
        got = client.get("/api/sr4rs").json()
        for key in ("installed", "weights", "installing", "step", "command", "credit"):
            assert key in got

    def test_only_this_page_can_install_it(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("SR4RS_DIR", str(tmp_path))
        calls = []
        monkeypatch.setattr(sr4rs, "install_in_background", lambda: calls.append(1))
        assert client.post("/api/sr4rs/install", json={},
                           headers={"origin": "https://elsewhere.example"}).status_code == 403
        assert client.post("/api/sr4rs/install").status_code == 403
        assert calls == []
        assert client.post("/api/sr4rs/install", json={}).status_code == 200
        assert calls == [1]
