"""Object detection on satellite imagery using a local Ollama qwen2.5vl model.

The image is split into tiles (objects like boats are only a handful of
pixels at Sentinel-2's 10 m resolution), each tile is upscaled and sent to
the vision model, and returned bounding boxes are mapped back to full-image
pixel coordinates.
"""

import base64
import io
import json
import os
import re

import requests
from PIL import Image

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("SENT2_MODEL", "qwen2.5vl:7b")

TILE = 512       # tile size in source pixels
UPSCALE = 2      # tiles are upscaled before being sent to the model
OVERLAP = 64     # tile overlap so objects on seams aren't missed

DEFAULT_TARGETS = ["boat", "ship", "airplane"]

PROMPT = """You are analysing a satellite image tile ({w}x{h} pixels, ~10 m per pixel).
Find every instance of: {targets}.
Respond with ONLY JSON, no other text, in this exact shape:
{{"detections": [{{"label": "<one of: {targets}>", "bbox": [x1, y1, x2, y2]}}]}}
bbox is in pixel coordinates of this {w}x{h} image (x1,y1 = top-left, x2,y2 = bottom-right).
If there are none: {{"detections": []}}"""

PLAN_PROMPT = """You configure a Sentinel-2 satellite imagery app (10 m/pixel resolution).
The user says what they want to find: "{query}"
Choose settings and respond with ONLY JSON in this exact shape:
{{"targets": ["<1-4 short singular object nouns to detect>"],
  "size_km": <area size in km, 2-20: small (2-5) for a single harbour/airport, large (10-20) for coastline or region scans>,
  "max_cloud": <max cloud cover %, 0-100: low (10-20) normally, higher only if the user cares more about recency than clarity>,
  "reason": "<one short sentence explaining your choices>"}}"""


def _installed_models() -> list[str]:
    r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=5)
    r.raise_for_status()
    return [m.get("name", "") for m in r.json().get("models", [])]


def resolve_model() -> str:
    """Match the configured model to the tag actually installed in Ollama
    (e.g. 'qwen2.5vl:7b' requested but 'qwen2.5vl:latest' pulled)."""
    models = _installed_models()
    if MODEL in models:
        return MODEL
    base = MODEL.split(":")[0]
    for m in models:
        if m.split(":")[0] == base:
            return m
    return MODEL


def ollama_available() -> tuple[bool, str]:
    try:
        models = _installed_models()
        if any(m.split(":")[0] == MODEL.split(":")[0] for m in models):
            return True, "ok"
        return False, f"Ollama is running but model '{MODEL}' is not pulled. Run: ollama pull {MODEL}"
    except requests.RequestException:
        return False, f"Cannot reach Ollama at {OLLAMA_URL}. Is it running?"


def _chat(model: str, prompt: str, images: list[str] | None = None) -> str:
    msg: dict = {"role": "user", "content": prompt}
    if images:
        msg["images"] = images
    r = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "stream": False,
            "format": "json",
            "messages": [msg],
            "options": {"temperature": 0},
        },
        timeout=300,
    )
    if not r.ok:
        raise RuntimeError(f"Ollama error {r.status_code} for model '{model}': {r.text[:300]}")
    return r.json().get("message", {}).get("content", "")


def plan_query(query: str) -> dict:
    """Ask the model to turn 'what am I looking for' into fetch/detect settings."""
    plan = {"targets": DEFAULT_TARGETS, "size_km": 8, "max_cloud": 30,
            "reason": "Default settings."}
    try:
        text = _chat(resolve_model(), PLAN_PROMPT.format(query=query.strip()[:300]))
        raw = json.loads(text)
    except (RuntimeError, requests.RequestException, json.JSONDecodeError):
        return plan
    targets = [str(t).lower().strip() for t in raw.get("targets", []) if str(t).strip()][:4]
    if targets:
        plan["targets"] = targets
    try:
        plan["size_km"] = min(20.0, max(2.0, float(raw["size_km"])))
        plan["max_cloud"] = min(100.0, max(0.0, float(raw["max_cloud"])))
    except (KeyError, TypeError, ValueError):
        pass
    if isinstance(raw.get("reason"), str):
        plan["reason"] = raw["reason"][:200]
    return plan


def _query_tile(tile_img: Image.Image, model: str, targets: list[str]) -> list[dict]:
    w, h = tile_img.size
    buf = io.BytesIO()
    tile_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    text = _chat(model, PROMPT.format(w=w, h=h, targets=", ".join(targets)), images=[b64])
    return _parse_detections(text, w, h)


def _parse_detections(text: str, w: int, h: int) -> list[dict]:
    raw = None
    try:
        obj = json.loads(text)
        raw = obj.get("detections") if isinstance(obj, dict) else obj
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                raw = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            label = str(d["label"]).lower()
            x1, y1, x2, y2 = (float(v) for v in d["bbox"])
        except (KeyError, TypeError, ValueError):
            continue
        # Some models answer in 0-1000 normalised coords; rescale if so.
        if max(x1, x2) <= 1000 and w > 1000:
            x1, x2 = x1 * w / 1000, x2 * w / 1000
            y1, y2 = y1 * h / 1000, y2 * h / 1000
        x1, x2 = sorted((max(0, min(w, x1)), max(0, min(w, x2))))
        y1, y2 = sorted((max(0, min(h, y1)), max(0, min(h, y2))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        out.append({"label": label, "bbox": [x1, y1, x2, y2]})
    return out


def _dedupe(dets: list[dict], iou_thresh: float = 0.45) -> list[dict]:
    """Drop overlapping duplicates from tile overlap regions."""
    kept: list[dict] = []
    for d in sorted(dets, key=lambda d: -(d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1])):
        ax1, ay1, ax2, ay2 = d["bbox"]
        dup = False
        for k in kept:
            bx1, by1, bx2, by2 = k["bbox"]
            ix = max(0, min(ax2, bx2) - max(ax1, bx1))
            iy = max(0, min(ay2, by2) - max(ay1, by1))
            inter = ix * iy
            union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
            if union > 0 and inter / union > iou_thresh:
                dup = True
                break
        if not dup:
            kept.append(d)
    return kept


def detect(image_path: str, targets: list[str] | None = None, progress=None) -> list[dict]:
    """Run detection over the whole image. Returns detections in full-image
    pixel coordinates: [{"label", "bbox": [x1, y1, x2, y2]}, ...]."""
    targets = targets or DEFAULT_TARGETS
    model = resolve_model()
    img = Image.open(image_path).convert("RGB")
    W, H = img.size
    step = TILE - OVERLAP
    xs = list(range(0, max(1, W - OVERLAP), step))
    ys = list(range(0, max(1, H - OVERLAP), step))
    total = len(xs) * len(ys)

    detections: list[dict] = []
    done = 0
    for oy in ys:
        for ox in xs:
            tile = img.crop((ox, oy, min(ox + TILE, W), min(oy + TILE, H)))
            tw, th = tile.size
            sent = tile.resize((tw * UPSCALE, th * UPSCALE), Image.LANCZOS)
            for d in _query_tile(sent, model, targets):
                x1, y1, x2, y2 = d["bbox"]
                detections.append({
                    "label": d["label"],
                    "bbox": [ox + x1 / UPSCALE, oy + y1 / UPSCALE,
                             ox + x2 / UPSCALE, oy + y2 / UPSCALE],
                })
            done += 1
            if progress:
                progress(done, total)
    return _dedupe(detections)
