"""Sent-2 Imagery — automatic Sentinel-2 change monitoring.

For each watched location it pulls two clear scenes from the last month,
aligns them pixel-for-pixel, finds where the ground changed, and asks a
local Ollama qwen2.5vl model what changed (blast damage, aircraft/vehicle
movement, new construction, ...). Also keeps a manual single-scene
explore + object-detection mode.

Just double-click this file (or run:  python app.py) — missing
dependencies are installed automatically on first launch.
"""

import importlib.util
import os
import subprocess
import sys
import threading
import uuid
import webbrowser


def _ensure_deps():
    # module name -> pip package name
    needed = {"flask": "flask", "requests": "requests", "rasterio": "rasterio",
              "numpy": "numpy", "scipy": "scipy", "PIL": "Pillow"}
    missing = [pkg for mod, pkg in needed.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"First run — installing dependencies: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_deps()

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont

import changedetect
import detector
import sentinel

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# In-memory state for the current scene and any running detection job.
scenes: dict[str, dict] = {}
jobs: dict[str, dict] = {}

# Watched locations for the change monitor.
LOCATIONS = [
    {"lat": 24.24144368264759, "lon": 54.55047056726246},
    {"lat": 25.12623664498919, "lon": 51.315237164255485},
    {"lat": 25.91560435623897, "lon": 50.58833421929604},
    {"lat": 29.347814547437533, "lon": 47.52101408106427},
    {"lat": 24.062939360466512, "lon": 47.5636748686972},
    {"lat": 31.83106147022119, "lon": 36.79100266594385},
    {"lat": 29.936190445040744, "lon": 34.93934839063682},
    {"lat": 32.00582844314073, "lon": 34.887055498781876},
]
for _i, _loc in enumerate(LOCATIONS, 1):
    _loc["id"] = _i
    _loc.setdefault("name", f"Site {_i}")

monitor_job: dict = {"state": "idle", "done": 0, "total": len(LOCATIONS), "results": []}

PALETTE = ["#00e5ff", "#ff3d71", "#ffd400", "#7cff4f"]


def _color_map(targets: list[str]) -> dict[str, str]:
    return {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(targets)}


@app.post("/api/plan")
def api_plan():
    query = str(request.get_json(force=True).get("query", "")).strip()
    if not query:
        return jsonify({"error": "Empty query."}), 400
    plan = detector.plan_query(query)
    plan["colors"] = _color_map(plan["targets"])
    return jsonify(plan)


@app.get("/api/locations")
def api_locations():
    return jsonify(LOCATIONS)


@app.post("/api/monitor")
def api_monitor():
    if monitor_job["state"] == "running":
        return jsonify({"error": "A scan is already running."}), 409
    p = request.get_json(force=True, silent=True) or {}
    days = int(p.get("days", 30))
    box_km = float(p.get("box_km", 6))
    max_cloud = float(p.get("max_cloud", 20))
    ids = p.get("ids")  # optional subset
    locs = [l for l in LOCATIONS if not ids or l["id"] in ids]

    monitor_job.update(state="running", done=0, total=len(locs), results=[])

    def run():
        for loc in locs:
            try:
                monitor_job["results"].append(_scan_location(loc, days, box_km, max_cloud))
            except Exception as e:
                msg = str(e)
                if "ProxyError" in msg or "Max retries" in msg or "Failed to establish" in msg:
                    msg = "Could not reach the Sentinel-2 imagery service (no internet?)."
                monitor_job["results"].append({
                    "id": loc["id"], "name": loc["name"],
                    "lat": loc["lat"], "lon": loc["lon"],
                    "status": "error", "message": msg,
                })
            monitor_job["done"] += 1
        monitor_job["state"] = "finished"

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"started": True, "total": len(locs)})


@app.get("/api/monitor/status")
def api_monitor_status():
    return jsonify(monitor_job)


def _scan_location(loc: dict, days: int, box_km: float, max_cloud: float) -> dict:
    import numpy as np

    bbox = sentinel.bbox_around(loc["lat"], loc["lon"], box_km)
    base = {"id": loc["id"], "name": loc["name"], "lat": loc["lat"], "lon": loc["lon"],
            "bounds": sentinel.bounds_latlon(bbox)}

    items = sentinel.search_range(bbox, days=days, max_cloud=max_cloud)
    pair = sentinel.pick_before_after(items)
    if pair is None:
        return {**base, "status": "insufficient",
                "message": f"Fewer than 2 clear (<{max_cloud:.0f}% cloud) scenes in the last {days} days."}
    before_it, after_it = pair
    out_wh = sentinel.grid_size(bbox)
    before_raw = sentinel.read_raw(before_it, bbox, out_wh)
    after_raw = sentinel.read_raw(after_it, bbox, out_wh)
    before8, after8, mask = sentinel.render_pair(before_raw, after_raw)
    W, H = out_wh

    regions, diff = changedetect.change_regions(before8, after8, mask)

    key = uuid.uuid4().hex[:10]
    before_name = f"before_{key}.png"
    after_name = f"after_{key}.png"
    change_name = f"change_{key}.png"
    Image.fromarray(before8, "RGB").save(os.path.join(DATA_DIR, before_name))
    Image.fromarray(after8, "RGB").save(os.path.join(DATA_DIR, after_name))
    changedetect.heat_overlay(after8, diff, regions).save(os.path.join(DATA_DIR, change_name))

    d1 = str(before_it["properties"]["datetime"])[:10]
    d2 = str(after_it["properties"]["datetime"])[:10]

    ok, _ = detector.ollama_available()
    out_regions = []
    for r in regions[:6]:   # describe the strongest changes only
        info = {"change": "change detected", "category": "other"}
        if ok:
            bcrop, acrop = changedetect.crop_pair(before8, after8, r["bbox"])
            info = detector.describe_change(bcrop, acrop, d1, d2)
        if info["category"] == "none":
            continue
        out_regions.append({
            "bounds": changedetect.region_bounds_latlon(r["bbox"], bbox, W, H),
            "change": info["change"],
            "category": info["category"],
            "color": changedetect.CATEGORY_COLORS.get(info["category"], "#b388ff"),
            "intensity": round(r["intensity"], 1),
        })

    return {
        **base,
        "status": "ok",
        "before_date": d1, "after_date": d2,
        "before_cloud": round(before_it["properties"].get("eo:cloud_cover", 0), 1),
        "after_cloud": round(after_it["properties"].get("eo:cloud_cover", 0), 1),
        "before_url": f"/data/{before_name}",
        "after_url": f"/data/{after_name}",
        "change_url": f"/data/{change_name}",
        "regions": out_regions,
        "change_count": len(out_regions),
        "ai": ok,
    }


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/data/<path:name>")
def data_file(name):
    return send_from_directory(DATA_DIR, name)


@app.post("/api/fetch")
def api_fetch():
    p = request.get_json(force=True)
    max_cloud = float(p.get("max_cloud", 30))
    targets = [str(t).lower() for t in p.get("targets", []) if str(t).strip()] or detector.DEFAULT_TARGETS

    # Either an explicit drawn rectangle [west, south, east, north],
    # or a click point plus an area size.
    if p.get("bbox"):
        bbox = [float(v) for v in p["bbox"]]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return jsonify({"error": "Drawn area is empty — drag a larger box."}), 400
        if (bbox[2] - bbox[0]) > 0.6 or (bbox[3] - bbox[1]) > 0.6:
            return jsonify({"error": "Drawn area is too large (max ~60 km) — draw a tighter box."}), 400
    else:
        lat, lon = float(p["lat"]), float(p["lon"])
        size_km = float(p.get("size_km", 8))
        bbox = sentinel.bbox_around(lat, lon, size_km)
    try:
        items = sentinel.search_latest(bbox, max_cloud=max_cloud)
    except Exception as e:
        return jsonify({"error": f"STAC search failed: {e}"}), 502
    if not items:
        return jsonify({"error": "No recent scenes found — try raising the cloud limit."}), 404

    last_err = None
    for item in items:
        key = uuid.uuid4().hex[:12]
        png = f"scene_{key}.png"
        try:
            meta = sentinel.download_visual(item, bbox, os.path.join(DATA_DIR, png))
        except Exception as e:
            last_err = e
            continue
        meta["key"] = key
        meta["image_url"] = f"/data/{png}"
        meta["image_path"] = os.path.join(DATA_DIR, png)
        meta["targets"] = targets
        meta["colors"] = _color_map(targets)
        scenes[key] = meta
        public = {k: v for k, v in meta.items() if k != "image_path"}
        return jsonify(public)
    return jsonify({"error": f"Could not download any scene: {last_err}"}), 502


@app.post("/api/detect")
def api_detect():
    key = request.get_json(force=True).get("key")
    scene = scenes.get(key)
    if not scene:
        return jsonify({"error": "Unknown scene — fetch imagery first."}), 400

    ok, msg = detector.ollama_available()
    if not ok:
        return jsonify({"error": msg}), 503

    job = {"done": 0, "total": 1, "state": "running", "result": None, "error": None}
    jobs[key] = job

    def run():
        try:
            def progress(done, total):
                job["done"], job["total"] = done, total

            dets = detector.detect(scene["image_path"], targets=scene["targets"], progress=progress)
            annotated = _annotate(scene, dets)
            job["result"] = {
                "detections": _georef(scene, dets),
                "annotated_url": annotated,
                "count": len(dets),
            }
            job["state"] = "finished"
        except Exception as e:
            job["error"] = str(e)
            job["state"] = "failed"

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"started": True})


@app.get("/api/detect/status")
def api_detect_status():
    job = jobs.get(request.args.get("key"))
    if not job:
        return jsonify({"error": "No detection job for this scene."}), 404
    return jsonify(job)


def _annotate(scene: dict, dets: list[dict]) -> str:
    """Draw highlight boxes on a copy of the scene image; return its URL."""
    img = Image.open(scene["image_path"]).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    colors = scene.get("colors", {})
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        color = colors.get(d["label"], "#ffd400")
        draw.rectangle([x1, y1, x2, y2], outline=color, width=3)
        draw.text((x1, max(0, y1 - 18)), d["label"], fill=color, font=font)
    name = f"annotated_{scene['key']}.png"
    img.save(os.path.join(DATA_DIR, name))
    return f"/data/{name}"


def _georef(scene: dict, dets: list[dict]) -> list[dict]:
    """Convert pixel bboxes to lat/lon rectangles for the map."""
    (south, west), (north, east) = scene["bounds"]
    W, H = scene["width"], scene["height"]
    colors = scene.get("colors", {})
    out = []
    for d in dets:
        x1, y1, x2, y2 = d["bbox"]
        out.append({
            "label": d["label"],
            "color": colors.get(d["label"], "#ffd400"),
            "bounds": [
                [north - (y2 / H) * (north - south), west + (x1 / W) * (east - west)],
                [north - (y1 / H) * (north - south), west + (x2 / W) * (east - west)],
            ],
        })
    return out


def main():
    port = int(os.environ.get("PORT", 8642))
    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Sent-2 Imagery running at {url}\n")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Keep the console window open when launched by double-click,
        # so the error is readable instead of the window flashing closed.
        import traceback
        traceback.print_exc()
        input("\nSomething went wrong — press Enter to close...")
