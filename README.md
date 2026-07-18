# Sent-2 Imagery

Grab the **latest Sentinel-2 satellite imagery** for anywhere on Earth, view it on an
interactive map, and let a local **Ollama qwen2.5vl** vision model highlight boats,
ships and aircraft.

- Imagery comes from the free [Earth Search STAC API](https://earth-search.aws.element84.com/v1)
  (Sentinel-2 L2A Cloud-Optimized GeoTIFFs on AWS) — **no API key needed**.
- Only the window you ask for is downloaded, not whole 100 km scenes.
- AI detection runs fully locally through Ollama.

## Run

**Just double-click `app.py`** (or run `python app.py`). On first launch it
installs any missing dependencies automatically, then opens the app in your
browser.

For AI detection (optional but the fun part), also pull the model once:

```bash
ollama pull qwen2.5vl:7b
``` Then:

1. **Click** anywhere on the map (harbours and airports are the good stuff).
2. **Fetch latest imagery** — pulls the newest low-cloud Sentinel-2 scene and overlays it.
3. **Detect boats & planes (AI)** — tiles the image through qwen2.5vl and draws
   highlight boxes on the map; click a box for its label.

## Notes

- Sentinel-2 resolution is 10 m/pixel, so a boat is only a few pixels — the app
  upscales tiles before sending them to the model, but expect it to catch large
  vessels and airliners more reliably than dinghies.
- Config via environment variables: `OLLAMA_HOST` (default `http://localhost:11434`),
  `SENT2_MODEL` (default `qwen2.5vl:7b`), `PORT` (default `8642`).
- Downloaded scenes land in `data/` — safe to delete anytime.
