"""The Electron shell, from the Python side: its rules, the serve mode it
drives, and the template's tests -- all run by one command.

The shell's own tests are plain Node and need no Electron installed; the
serve-mode test starts the real engine the way the shell does.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("suite", ["electron/test/shell.test.js", "tests/filmtemplate.test.mjs"])
def test_node_suites(suite) -> None:
    done = subprocess.run([NODE, "--test", suite], cwd=ROOT, capture_output=True,
                          text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# The second is Windows: a pipe gets the old cp1252 code page there, which
# has no arrow in it, and the first message app.py printed has one. It
# crashed on that line, before serving anything -- the very first thing the
# Electron app did on Windows. The encoding is forced here so that the
# failure is reproduced on any machine, not only on Windows.
@pytest.mark.parametrize("encoding", [None, "cp1252"])
def test_serve_mode_prints_its_address_and_serves(tmp_path, encoding):
    """What electron/main.js does, done from here: start app.py --serve, read
    the one line, ask the address for its health, stop it."""
    port = free_port()
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "SENT2_NO_AUTO_NATIVE": "1",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("PYTHONUTF8", None)
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    proc = subprocess.Popen([sys.executable, str(ROOT / "app.py"), "--serve", "--demo",
                             "--port", str(port)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        url = None
        until = time.time() + 60
        while time.time() < until and url is None:
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith("EARTHVIEWER_URL="):
                url = line.split("=", 1)[1].strip()
        assert url == f"http://localhost:{port}", (url, proc.stdout.read()
                                                  if url is None else "")
        health = None
        while time.time() < until:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health",
                                            timeout=2) as resp:
                    health = json.loads(resp.read())
                    break
            except OSError:
                time.sleep(0.25)
        assert health and health["ok"] and health["demo"] is True
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    # And it stops when asked: the shell ends the engine by ending the process.
    assert proc.returncode is not None


def test_the_minimap_land_ships_and_is_sane():
    got = json.loads((ROOT / "frontend" / "vendor" / "naturalearth" / "land.json")
                     .read_text(encoding="utf-8"))
    rings = got["rings"]
    assert len(rings) > 300
    for ring in rings:
        assert len(ring) % 2 == 0 and len(ring) >= 8
        assert all(-180 <= v <= 180 for v in ring[0::2])
        assert all(-90 <= v <= 90 for v in ring[1::2])


class TestWhereIs:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient
        from backend.app import app
        return TestClient(app)

    def test_a_known_place(self, client, monkeypatch):
        from backend import config, gazetteer
        monkeypatch.setattr(config, "DEMO_MODE", False)
        monkeypatch.setattr(gazetteer, "region_at", lambda lat, lon: "Southern District")
        assert client.get("/api/whereis", params={"lat": 30.7, "lon": 35.0}).json() == {
            "region": "Southern District"}

    def test_a_gazetteer_that_cannot_answer_is_no_name_not_an_error(self, client, monkeypatch):
        from backend import config, gazetteer
        monkeypatch.setattr(config, "DEMO_MODE", False)

        def down(lat, lon):
            raise gazetteer.GazetteerError("rate limiting")

        monkeypatch.setattr(gazetteer, "region_at", down)
        got = client.get("/api/whereis", params={"lat": 30.7, "lon": 35.0})
        assert got.status_code == 200 and got.json() == {"region": None}

    def test_the_demo_asks_nobody(self, client, monkeypatch):
        from backend import config, gazetteer
        monkeypatch.setattr(config, "DEMO_MODE", True)
        monkeypatch.setattr(gazetteer, "region_at",
                            lambda lat, lon: pytest.fail("the demo reached the gazetteer"))
        assert client.get("/api/whereis", params={"lat": 1, "lon": 2}).json() == {"region": None}

    def test_nonsense_coordinates_are_refused(self, client):
        assert client.get("/api/whereis", params={"lat": 95, "lon": 0}).status_code == 422
