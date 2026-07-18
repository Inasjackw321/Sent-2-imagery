"""Sent-2 Imagery — grab the latest Sentinel-2 scene for any spot on Earth,
map it, and let a local Ollama qwen2.5vl model highlight boats, ships and
aircraft.

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
              "numpy": "numpy", "PIL": "Pillow"}
    missing = [pkg for mod, pkg in needed.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print(f"First run — installing dependencies: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure_deps()

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageDraw, ImageFont

import detector
import sentinel

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# In-memory state for the current scene and any running detection job.
scenes: dict[str, dict] = {}
jobs: dict[str, dict] = {}

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


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/data/<path:name>")
def data_file(name):
    return send_from_directory(DATA_DIR, name)


@app.post("/api/fetch")
def api_fetch():
    p = request.get_json(force=True)
    lat, lon = float(p["lat"]), float(p["lon"])
    size_km = float(p.get("size_km", 8))
    max_cloud = float(p.get("max_cloud", 30))
    targets = [str(t).lower() for t in p.get("targets", []) if str(t).strip()] or detector.DEFAULT_TARGETS

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
