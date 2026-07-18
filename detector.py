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

TILE = 320       # tile size in source pixels
SENT_PX = 1000   # each tile is upscaled so its long side is ~this many px
OVERLAP = 48     # tile overlap so objects on seams aren't missed
MAX_BOX_FRAC = 0.6   # boxes wider/taller than this share of the tile are junk
MAX_PER_TILE = 40    # sanity cap on detections returned for one tile

# Targets the planner must never pick: huge linear / area features that a
# per-object detector cannot meaningfully box.
BANNED_TARGETS = {"runway", "runways", "road", "roads", "highway", "coastline",
                  "coast", "water", "sea", "ocean", "river", "lake", "forest",
                  "field", "fields", "land", "ground", "building area", "city",
                  "airport", "harbour", "harbor", "port", "terminal", "apron"}

DEFAULT_TARGETS = ["ship", "boat", "airplane"]

PROMPT = """This is a {w}x{h} pixel Sentinel-2 satellite image tile at 10 m per pixel.
At this resolution a car is ~1 px, an aircraft ~7 px, a large ship ~30 px — objects are TINY.
Look carefully for these small objects: {targets}.

Rules:
- Box every instance you can actually identify, but only real, visible objects — do not guess wildly on empty ground.
- Each box must tightly wrap ONE object. Never return a box that covers most of the tile or a large stretch of ground, water, runway or apron.
- If you genuinely see none, return an empty list.

Respond with ONLY JSON:
{{"detections": [{{"label": "<one of: {targets}>", "box": [x1, y1, x2, y2]}}]}}
Coordinates are normalized 0 to 1000 (0,0 = top-left corner; 1000,1000 = bottom-right corner).
If nothing is found: {{"detections": []}}"""

PLAN_PROMPT = """You configure a Sentinel-2 satellite imagery search. Resolution is 10 m/pixel,
so only objects larger than about 20 m are visible as small blobs.
The user wants to find: "{query}"

Respond with ONLY JSON in this exact shape:
{{"targets": ["<1-3 small, discrete, COUNTABLE objects, singular nouns, e.g. ship, boat, airplane, tanker, truck>"],
  "size_km": <area size in km, 2-20: small (2-4) for one airport or harbour, large (10-20) for coastline or region scans>,
  "max_cloud": <max cloud cover %, 0-100: use 10-20 for clear imagery>,
  "reason": "<one short sentence explaining your choices>"}}

Only choose objects that show up individually as small blobs. NEVER choose large linear or area
features such as runway, road, coastline, water, forest, building or airport itself."""


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
    targets = [str(t).lower().strip() for t in raw.get("targets", []) if str(t).strip()]
    targets = [t for t in targets if t not in BANNED_TARGETS][:3]
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


CHANGE_PROMPT = """These are two Sentinel-2 satellite crops of the SAME location (10 m/pixel).
The FIRST image is earlier ({d1}). The SECOND image is later ({d2}).
Describe what CHANGED from the first to the second image, in a few words.
Look for: new crater or blast damage, a destroyed or damaged building, new construction,
aircraft or vehicles that appeared or left, ships that moved, new ground scarring or burn marks.
Respond with ONLY JSON:
{{"change": "<short phrase>", "category": "damage" | "movement" | "construction" | "other" | "none"}}
If the two crops look essentially the same, respond {{"change": "no significant change", "category": "none"}}."""


def _img_b64(img: Image.Image, upscale_to: int = 384) -> str:
    w, h = img.size
    if max(w, h) < upscale_to:
        s = upscale_to / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def describe_change(before_img: Image.Image, after_img: Image.Image,
                    d1: str, d2: str, model: str | None = None) -> dict:
    """Ask the vision model what changed between two aligned crops."""
    model = model or resolve_model()
    imgs = [_img_b64(before_img), _img_b64(after_img)]
    try:
        text = _chat(model, CHANGE_PROMPT.format(d1=d1, d2=d2), images=imgs)
        obj = json.loads(text)
        change = str(obj.get("change", "")).strip() or "change detected"
        category = str(obj.get("category", "other")).lower().strip()
    except (RuntimeError, requests.RequestException, json.JSONDecodeError, AttributeError):
        return {"change": "change detected (AI description unavailable)", "category": "other"}
    if category not in ("damage", "movement", "construction", "other", "none"):
        category = "other"
    return {"change": change, "category": category}


def _query_tile(tile_img: Image.Image, model: str, targets: list[str]) -> list[dict]:
    w, h = tile_img.size
    buf = io.BytesIO()
    tile_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    text = _chat(model, PROMPT.format(w=w, h=h, targets=", ".join(targets)), images=[b64])
    dets = _parse_detections(text, w, h, targets)

    # Drop hallucinated boxes that swallow most of the tile.
    kept = [d for d in dets
            if (d["bbox"][2] - d["bbox"][0]) < MAX_BOX_FRAC * w
            and (d["bbox"][3] - d["bbox"][1]) < MAX_BOX_FRAC * h]
    return kept[:MAX_PER_TILE]


def _to_pixels(v: float, dim: int) -> float:
    """Interpret a model coordinate. We ask for normalised 0-1000, but stay
    robust to models that answer in 0-1 fractions or raw pixels."""
    if v <= 1.5:            # fraction of the image
        return v * dim
    if v <= 1000:           # normalised 0-1000 (our requested convention)
        return v / 1000.0 * dim
    return v                # already pixels


def _parse_detections(text: str, w: int, h: int, targets: list[str] | None = None) -> list[dict]:
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
    allowed = {t.lower() for t in targets} if targets else None
    out = []
    for d in raw if isinstance(raw, list) else []:
        try:
            label = str(d["label"]).lower()
            coords = d.get("box", d.get("bbox"))
            x1, y1, x2, y2 = (float(v) for v in coords)
        except (KeyError, TypeError, ValueError):
            continue
        x1, x2 = _to_pixels(x1, w), _to_pixels(x2, w)
        y1, y2 = _to_pixels(y1, h), _to_pixels(y2, h)
        x1, x2 = sorted((max(0, min(w, x1)), max(0, min(w, x2))))
        y1, y2 = sorted((max(0, min(h, y1)), max(0, min(h, y2))))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        # Keep the label to a requested target if we can match one.
        if allowed and label not in allowed:
            label = next((t for t in allowed if t in label or label in t), label)
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
            # Upscale each tile so its long side is ~SENT_PX; small (tightly
            # cropped) areas get more zoom, making objects easier to see.
            up = max(2, min(6, round(SENT_PX / max(tw, th))))
            sent = tile.resize((tw * up, th * up), Image.LANCZOS)
            for d in _query_tile(sent, model, targets):
                x1, y1, x2, y2 = d["bbox"]
                detections.append({
                    "label": d["label"],
                    "bbox": [ox + x1 / up, oy + y1 / up,
                             ox + x2 / up, oy + y2 / up],
                })
            done += 1
            if progress:
                progress(done, total)
    return _dedupe(detections)
