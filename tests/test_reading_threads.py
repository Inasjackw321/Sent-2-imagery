"""Reading imagery must not stop the rest of the app.

The app window and this backend share one Python process, so anything that
holds Python's global lock while it waits on the network freezes the window.
Clicking the imagery for its measurements did exactly that: the bands are read
on a pool of threads, a rasterio.Env belongs only to the thread that entered
it, and a remote file opened with no Env in its thread holds the lock for a
whole round trip. Half a second of frozen map per click, measured.

These serve a small GeoTIFF over HTTP from this machine, slowly on purpose,
and read it the way a render and a measurement do.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from rasterio.crs import CRS

from backend import config

ROOT = Path(__file__).resolve().parent.parent

DELAY = 0.4
GIVE_UP = 30.0
BANDS = ("blue", "green", "red", "nir", "swir16")


class Slow(http.server.BaseHTTPRequestHandler):
    root: Path

    def log_message(self, *a):
        pass

    def _body(self):
        path = self.root / self.path.lstrip("/").split("?")[0]
        if not path.is_file():
            # GDAL asks for sidecar files and directory listings unless it
            # is told not to, which is part of what is being tested.
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return path.read_bytes()

    def do_HEAD(self):
        body = self._body()
        if body is None:
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()

    def do_GET(self):
        time.sleep(DELAY)
        body = self._body()
        if body is None:
            return
        asked = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        if asked:
            a = int(asked.group(1))
            b = min(int(asked.group(2) or len(body) - 1), len(body) - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{len(body)}")
            body = body[a:b + 1]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("slow")
    for band in BANDS:
        data = np.random.default_rng(0).integers(1000, 6000, (1024, 1024)).astype("uint16")
        with rasterio.open(root / f"{band}.tif", "w", driver="GTiff", height=1024,
                           width=1024, count=1, dtype="uint16", nodata=0,
                           crs=CRS.from_epsg(32636), tiled=True, blockxsize=256,
                           blockysize=256, transform=Affine(10, 0, 600000, 0, -10, 3500000)) as dst:
            dst.write(data, 1)
    handler = type("Handler", (Slow,), {"root": root})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


# The reading happens in a child process. Without the fix the lock is held
# for minutes on end, and a check running in the same process as the read
# could not run to notice -- it would hang rather than fail.
CHILD = r"""
import json, sys, threading, time
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform
from backend import raster, service

base, bands = sys.argv[1], sys.argv[2].split(",")
raster.scene_is_safe = lambda *a, **k: None
seen = []
real = rasterio.open
def opened(*a, **k):
    seen.append(rasterio.env.getenv().get("GDAL_DISABLE_READDIR_ON_OPEN"))
    return real(*a, **k)
rasterio.open = opened

scene = {"id": "SLOW", "date": "2024-06-01", "datetime": "2024-06-01T08:00:00Z",
         "platform": "sentinel-2a", "boa_offset_applied": True, "demo": False,
         "satellite": "sentinel-2", "assets": {b: f"{base}/{b}.tif" for b in bands}}
lons, lats = transform(CRS.from_epsg(32636), CRS.from_epsg(4326), [605000], [3495000])
done = threading.Event()
def work():
    service.probe({"lon": lons[0], "lat": lats[0], "scene": scene})
    done.set()
threading.Thread(target=work, daemon=True).start()
worst, last = 0.0, time.perf_counter()
while not done.is_set():
    time.sleep(0.01)
    now = time.perf_counter()
    worst, last = max(worst, now - last), now
print(json.dumps({"stall": worst, "settings": seen}))
"""


@pytest.fixture(scope="module")
def reading(served):
    env = {**os.environ, "NO_PROXY": "127.0.0.1,localhost",
           "no_proxy": "127.0.0.1,localhost", "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, served, ",".join(BANDS)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=GIVE_UP)
    except subprocess.TimeoutExpired:
        # Without its settings GDAL goes looking for sidecar files and
        # directory listings one slow request at a time, holding the lock
        # through each: minutes of a frozen process.
        pytest.fail(f"reading one point took over {GIVE_UP:.0f} s")
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_reading_the_measurements_does_not_stall_the_app(reading):
    # Every request here takes DELAY. Holding the lock across one of them is
    # a stall of at least that; anything well under it is scheduling noise.
    assert reading["stall"] < DELAY * 0.6, \
        f"the app stood still for {reading['stall']:.2f} s"


def test_every_reading_thread_has_the_apps_gdal_settings(reading):
    assert reading["settings"]
    assert set(reading["settings"]) == {config.GDAL_ENV["GDAL_DISABLE_READDIR_ON_OPEN"]}
