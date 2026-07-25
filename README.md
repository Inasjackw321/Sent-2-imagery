# Sent-2 · satellite imagery studio

Circle a region on a map, download real Sentinel-2 imagery for it, edit the
picture, and turn it into timelapse GIFs and annotated informative graphics.

Everything runs locally: a small Python backend fetches and processes the
imagery, a browser front end does the drawing, editing and composition. No
account, no API key.

```bash
pip install -r requirements.txt
python run.py
```

It opens in its own app window — no address bar, no tabs. To try the interface
without downloading anything, `python run.py --demo` serves synthetic imagery
instead — see [Demo mode](#demo-mode).

## Opening it as an app

Three ways, pick whichever suits:

**Double-click a launcher.** `launchers/Sent-2.command` on macOS and Linux,
`launchers/Sent-2.bat` on Windows. First run creates a `.venv` and installs
dependencies; after that it just opens.

**Add it to your applications.** One command puts it where you'd expect:

```bash
python run.py --install-shortcut
```

- macOS — a real `Sent-2.app` in `~/Applications`, with its own icon, ready to
  drag to the Dock
- Windows — shortcuts on the Desktop and in the Start menu, launched via
  `pythonw` so no console window appears
- Linux — a `.desktop` entry in your applications menu

**Install from the browser.** Sent-2 is a progressive web app: open it in a tab
and use the **Install app** button in the header (or your browser's install
action). You get a dock/taskbar icon and a standalone window, and the interface
is cached so it starts instantly — and still opens if you're offline.

How the window is opened:

| Flag | Behaviour |
| --- | --- |
| *(none)* or `--app` | Stand-alone app window via an installed Chromium-family browser |
| `--native` | A native OS window — needs `pip install pywebview` |
| `--tab` | An ordinary browser tab |
| `--headless` | Serve only; open the URL yourself |

The app picks a free port if 8000 is taken, and launching it a second time
brings the running copy up instead of starting another. Closing the app window
shuts the server down. Set `SENT2_BROWSER` to force a particular browser.

---

## Where the imagery comes from

[Element 84's Earth Search](https://earth-search.aws.element84.com/v1) — a free
STAC catalogue in front of the Sentinel-2 Level-2A archive on AWS Open Data.
Level-2A is atmospherically corrected surface reflectance at 10–60 m, with a
revisit of about five days.

Only the bands and the pixels your area needs are read, straight out of the
cloud-optimised GeoTIFFs by HTTP range request, so a small area is quick even
though each source tile is over a gigabyte.

Imagery is Copernicus data, free to use with attribution — the graphic composer
adds the attribution line for you.

## The four tabs

### 1 · Capture

**Draw an area.** Four tools: freehand **lasso** (drag to circle a region),
**circle**, **box** and **polygon**. Or type a place name to fly there. The
panel shows the area in km², the ground extent and the centre coordinates.

**Find imagery.** Set a date range and a cloud-cover ceiling; the scene list
shows every pass over your area with its date and cloud percentage.

**Render.** Nine band combinations and eight spectral indices:

| Band combination | What it shows |
| --- | --- |
| True colour | What the eye would see |
| Colour infrared | Healthy vegetation glows red |
| Agriculture | Crop types and field boundaries |
| Urban / built-up | Concrete and bare rock vs vegetation |
| Geology | Lithology, faults, mineral alteration |
| Healthy vegetation | Vegetation stress and moisture |
| Short-wave infrared | Burn scars, active fire, soil moisture |
| Bathymetric | Shallow sea floor and sediment plumes |
| Atmospheric penetration | Sees through haze |

| Index | What it measures |
| --- | --- |
| NDVI | Green biomass and vigour |
| NDWI | Open water and flooding |
| NDMI | Canopy water content, drought stress |
| NDBI | Impervious surfaces, urban growth |
| NBR | Fire severity |
| NDSI | Snow and ice |
| EVI | Vegetation, resistant to soil and haze |
| SAVI | Vegetation over sparse or bright soils |

Indices come with a colour scale (twelve colour maps), statistics and a
histogram. Also on this tab: cloud and shadow masking from the scene
classification band, clipping to the exact shape you drew, resolution up to
4096 px, and export as PNG or as a georeferenced **GeoTIFF** that drops
straight into QGIS.

**Change detection.** Tick two dates and compare them: the index difference is
mapped on a red–green scale, with the gained, stable and lost areas quantified
in km² and percent.

### 2 · Edit

Non-destructive adjustments on the rendered image: exposure, contrast,
saturation, highlights, shadows, temperature, tint, gamma, clarity (unsharp
mask) and vignette, plus nine one-click looks. Rotate, flip and crop. Undo and
redo throughout. Save as PNG or JPEG, or push the same adjustments onto every
other image you have captured — handy before building a timelapse.

### 3 · Timelapse

Tick a run of scenes on the Capture tab and render them all as frames, then
scrub or play them back. Overlays are burnt into each frame: date stamp,
progress bar, scale bar and a title. Export as animated **GIF**, **WebP** or
**APNG** — with a single shared palette so colours do not shimmer between
frames — or as a contact sheet of all frames in a grid.

### 4 · Graphic

Compose a finished figure from one or more captures.

- **Layouts** — single, side by side, stacked, grid of four, before/after swipe
- **Themes** — dark, light, paper, or the bare image
- **Text** — title, subtitle, caption, credit line
- **Map furniture** — scale bar, north arrow (which follows any rotation you
  applied), coordinates, graticule, colour legend, statistics panel, histogram,
  date labels and a position-on-Earth diagram
- **Annotations** — text labels, arrows, rectangles, ellipses and lines, in any
  colour and weight; drag to reposition
- **Export** — PNG at 1×, 2× or 3×

Caption and information cards flow into two balanced columns, so the figure
stays tight no matter which features you switch on.

## Demo mode

`python run.py --demo` replaces the catalogue with synthetic scenes generated
from noise fields: plausible water, vegetation, soil, urban and snow cover, a
seasonal cycle, slow urban growth over the years, and clouds matching each
scene's stated cover. It is there so the interface can be explored, tested and
demonstrated offline.

It is not real imagery, and the app says so everywhere it could matter: a badge
in the header, a flag on every render, a diagonal watermark on exported
graphics and animation frames, and a footer line in place of the Copernicus
attribution.

## Details worth knowing

- **Projection.** Everything is rendered onto a Web Mercator (EPSG:3857) grid
  covering your area, so exports line up with web maps and GIS. Reported
  resolution is true ground metres per pixel, corrected for Mercator's latitude
  stretch — as are the scale bars.
- **Reflectance.** Digital numbers are converted to surface reflectance,
  including the −1000 offset that Sentinel-2 products carry from processing
  baseline 04.00 (25 January 2022) onwards.
- **Tone mapping.** True colour defaults to a fixed 0–0.30 reflectance window so
  two dates look comparable. Other options: percentiles pooled across the three
  bands (contrast without a colour shift), per-band percentiles (maximum
  contrast), or full range.
- **Mixed resolutions.** 10 m, 20 m and 60 m bands are resampled onto one grid,
  so any band can be combined with any other.
- **Caching.** Recently read band windows are kept in memory, so changing the
  colour map or stretch does not re-download anything.

## Layout

```
backend/
  app.py         FastAPI routes and static hosting
  config.py      band table, composites, indices, colour maps
  stac.py        catalogue search, plus the synthetic scene generator
  raster.py      windowed COG reads, reprojection, cloud masking, demo bands
  composite.py   stretches, indices, colour maps, statistics, encoding
  service.py     render and change-detection orchestration, caching
  animate.py     GIF / WebP / APNG assembly and contact sheets
  geo.py         AOI handling, geodesic area, output grid
  launcher.py    port choice, app windows, desktop shortcuts
frontend/
  index.html, css/app.css, manifest.webmanifest, sw.js
  js/            map, capture, adjust, editor, overlay, timelapse, graphic
  icons/         app icons, favicon.ico and a macOS .icns
  vendor/        Leaflet 1.9.4 (BSD-2-Clause), vendored — no CDN needed
launchers/       double-clickable launchers for macOS, Linux and Windows
tools/           icon generator
tests/           pytest suite over the real raster path and the launcher
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The suite writes Sentinel-2-like GeoTIFFs in a UTM projection and runs the real
(non-demo) reading path over them, covering reprojection onto the output grid,
mixed band resolutions, the reflectance offset, cloud masking, shape clipping,
index maths, stretch modes, GeoTIFF export, change detection and STAC parsing.
It also covers the desktop-app plumbing: port selection, app-window arguments,
the generated shortcuts, and the web-app manifest and service worker.

## Requirements

Python 3.10+ and a browser. `rasterio` ships GDAL in its wheels, so there is
nothing else to install. Internet access is needed for imagery, map tiles and
place search — but not in `--demo` mode, which only needs the map tiles.
