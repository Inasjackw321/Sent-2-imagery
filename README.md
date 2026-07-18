# Sent-2 Change Monitor

Automatically pull **Sentinel-2 satellite imagery** for a set of watched locations,
compare two dates from the last month, and use a local **Ollama qwen2.5vl** model to
describe what changed — blast/impact damage, aircraft & vehicle movement, new
construction, and more.

- Imagery comes from the free [Earth Search STAC API](https://earth-search.aws.element84.com/v1)
  (Sentinel-2 L2A Cloud-Optimized GeoTIFFs on AWS) — **no API key needed**.
- Two clear scenes per site are aligned pixel-for-pixel; a change map finds *where*
  the ground changed, and the AI says *what* changed.
- Everything AI runs fully locally through Ollama.

## Run

**Just double-click `app.py`** (or run `python app.py`). On first launch it installs
any missing dependencies automatically, then opens the app in your browser.

For the AI change descriptions, pull the model once:

```bash
ollama pull qwen2.5vl:7b
```

## Monitor tab (main feature)

1. Choose a **window** (e.g. last 30 days) and a **max cloud** limit.
2. Click **Scan all locations**. For each site the app finds the newest and oldest
   clear scene in the window, aligns them, and computes a change map.
3. Each site lists its two dates and the changes found, tagged by type
   (🔴 damage · 🔵 movement · 🟡 construction · 🟣 other).
4. Click a site to inspect it: the map shows change regions with the AI's description
   in each popup, and a **Before / After / Change** toggle flips the overlay so you can
   eyeball the difference yourself.

Watched locations are defined in `LOCATIONS` near the top of `app.py` — edit that list
to monitor your own coordinates.

## Explore tab (manual)

Click a point or drag a box, **Fetch** the latest scene, and optionally **Detect** —
tiles the image through qwen2.5vl to highlight objects you describe
(e.g. "jets at this airport").

## Notes

- Sentinel-2 is 10 m/pixel: change detection reliably flags things roughly a pixel or
  larger (craters, damaged buildings, groups of vehicles/aircraft, ships). Expect some
  false positives from clouds, shadows and seasonal change — the AI description helps
  you triage them, and the Before/After toggle lets you confirm by eye.
- Config via environment variables: `OLLAMA_HOST` (default `http://localhost:11434`),
  `SENT2_MODEL` (default `qwen2.5vl:7b`), `PORT` (default `8642`).
- Downloaded imagery lands in `data/` — safe to delete anytime.
