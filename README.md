# Sent-2 · satellite imagery studio

Circle a region on a map, pull imagery from fourteen free satellites, clean it
up, edit it, and turn it into timelapse GIFs and annotated informative graphics.

It is a desktop app: run one Python file and it opens in its own window.

```bash
python app.py
```

That's it — `app.py` installs anything missing on first run, starts the local
engine and opens the studio in a desktop window. No account, no API key, no
terminal needed afterwards. Add `--demo` to explore the interface offline with
synthetic imagery (see [Demo mode](#demo-mode)).

## Opening it as an app

**Double-click `app.py`.** On most systems that is all it takes. If your
computer opens `.py` files in an editor instead, use the launchers:
`launchers/Sent-2.command` on macOS and Linux, `launchers/Sent-2.bat` on
Windows — they set up a private `.venv` on first run.

**Add it to your applications.** One command puts it where you'd expect:

```bash
python app.py --install-shortcut
```

- macOS — a real `Sent-2.app` in `~/Applications`, with its own icon, ready to
  drag to the Dock
- Windows — shortcuts on the Desktop and in the Start menu, launched via
  `pythonw` so no console window appears
- Linux — a `.desktop` entry in your applications menu

**Install from the browser.** Sent-2 is also a progressive web app: open it in
a tab and use the **Install app** button in the header. You get a dock/taskbar
icon and a standalone window, and the interface is cached so it starts
instantly — and still opens if you're offline.

The window itself comes from, in order of preference: a real OS window
(pywebview, installed on demand), a chrome-less browser window, or an ordinary
tab. Force one with `--tab`, or `--no-native` to skip pywebview.

| Command | What it does |
| --- | --- |
| `python app.py` | Desktop window, live imagery |
| `python app.py --demo` | Desktop window, synthetic imagery, works offline |
| `python app.py --tab` | Open in an ordinary browser tab |
| `python app.py --install-shortcut` | Add to applications / Dock / Start menu |
| `python run.py --headless` | Serve only; open the URL yourself |

It picks a free port if 8000 is taken, and launching it a second time brings
the running copy up instead of starting another. Closing the window quits.

---

## The satellites

Fourteen sources, all free and open, reached through two public catalogues that
need no account. Every one of them is read the same way — only the parts your
area needs, straight out of cloud-optimised GeoTIFFs.

| Satellite | Resolution | Since | Revisit | Good for |
| --- | --- | --- | --- | --- |
| **Sentinel-2** L2A | 10 m | 2015 | ~5 days | The default: best free optical imagery |
| Sentinel-2 L1C | 10 m | 2015 | ~5 days | Top-of-atmosphere, when L2A misbehaves |
| **Landsat 4–9** C2 L2 | 30 m (15 m pan) | **1982** | ~8 days | Four decades of history; pan-sharpens to 15 m |
| Landsat thermal | 30 m | 2013 | ~8 days | Ground temperature in kelvin |
| Harmonised L–S (S30/L30) | 30 m | 2013 | 2–3 days | Landsat and Sentinel on one grid |
| **Sentinel-1** radar (RTC/GRD) | 10 m | 2014 | 6–12 days | Sees through cloud and at night; floods |
| **NAIP** aerial | 0.6 m | 2010 | 2–3 years | Sub-metre detail (United States) |
| MODIS surface reflectance | 500 m | 2000 | Daily | Continent-scale change since 2000 |
| ASTER | 15 m | 2000 | On request | Multispectral with thermal |
| Copernicus DEM | 30 m | static | — | Terrain, elevation tints and hillshade |
| ESA WorldCover | 10 m | 2020 | yearly | Eleven land-cover classes with areas |

Pick one from the **Satellite** menu and the rest of the interface adapts: only
the band combinations and indices that satellite can actually produce are
offered, cloud controls disappear for radar, and the resolution default follows
the sensor. Each source declares its own unit conversion (Sentinel-2's
reflectance offset, Landsat's scale and offset, radar's decibels) so numbers are
physically correct whichever one you use.

## Making the imagery look better

Satellite imagery rarely looks its best raw. The **Make it look better** panel
does the work that matters, in the right order and in the right units.

**Cloud-free composite** — the big one. Tick several dates and Sent-2 masks the
cloud out of each, then takes the median through the stack. A pixel only has to
be clear in half the scenes, so a place that is never cloud-free on any single
day comes out clean. The result reports how much it rescued: *"8 scenes —
100% clear (best single date: 84%)"*.

**Pan-sharpening** — Landsat carries a 15 m panchromatic band alongside its 30 m
colour bands. Sent-2 injects that band's detail into the colour ones, weighted
so hue survives. Genuinely twice the detail, not just sharpening.

**Haze removal** — dark-object subtraction. Deep shadow and clear water should
read near zero; whatever they actually read is atmosphere. Subtracting it per
band lifts the blue-grey veil without shifting colour.

**Adaptive contrast** — contrast-limited histogram equalisation, applied to
brightness only. A single stretch has to compromise between a bright desert and
a dark forest in one frame; this equalises within tiles and blends them, so both
read properly and no seams show.

Plus **denoise** (median filtering — radar is unreadable until despeckled),
**white balance**, **vibrance**, and **detail** with overshoot clamping so
strong settings do not draw halos around coastlines. Six one-click presets
(Balanced, Punchy, Hazy day, Radar, Natural) set sensible combinations.

Everything applied is recorded and shown in the graphic's statistics panel, so a
figure always says how its imagery was processed.

---

## Where the imagery comes from

Two public STAC catalogues, both free and neither needing an account:

- **[Earth Search](https://earth-search.aws.element84.com/v1)** (Element 84) —
  Sentinel-2, Sentinel-1 and Copernicus DEM on AWS Open Data, entirely anonymous.
- **[Planetary Computer](https://planetarycomputer.microsoft.com)** (Microsoft) —
  Landsat, HLS, MODIS, NAIP, ASTER and WorldCover. Assets are signed with an
  anonymous token that Sent-2 fetches and refreshes for you.

Only the bands and the pixels your area needs are read, by HTTP range request
straight out of the cloud-optimised GeoTIFFs, so a small area is quick even
though a source tile can be over a gigabyte.

Each satellite carries its own licence and attribution — Copernicus for the
Sentinels, USGS for Landsat, and so on — and the graphic composer writes the
right line for whatever went into the figure.

## The four tabs

### 1 · Capture

**Draw an area.** Four tools: freehand **lasso** (drag to circle a region),
**circle**, **box** and **polygon**. Or type a place name to fly there. The
panel shows the area in km², the ground extent and the centre coordinates.

**Pick a satellite,** then **find imagery**: set a date range and a cloud-cover
ceiling, and the scene list shows every pass over your area.

**Render.** Eleven band combinations and eleven indices, filtered to what the
chosen satellite supports:

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

Radar adds a VV/VH composite and a flood view; Landsat adds pan-sharpened true
colour; the DEM adds elevation tints with hillshade; WorldCover adds classified
land cover with the area of every class.

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
| Surface temperature | Ground temperature in kelvin (Landsat thermal) |
| Elevation | Height in metres (Copernicus DEM) |
| Backscatter | Radar brightness in decibels (Sentinel-1) |

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
app.py           the desktop app — double-click this
run.py           the same thing with more command-line options
backend/
  sources.py     the satellite catalogue: bands, scaling, cloud masks
  enhance.py     compositing, haze, pan-sharpening, CLAHE, denoise, hillshade
  app.py         FastAPI routes and static hosting
  config.py      band table, composites, indices, colour maps
  stac.py        catalogue search and asset signing, plus synthetic scenes
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

The suite writes GeoTIFFs that match each satellite's real storage convention —
Sentinel-2's DN offset, Landsat's scale and offset and QA_PIXEL bitfield, radar
power, metres of elevation — and runs the live (non-demo) reading path over
them. That covers reprojection onto the output grid, mixed band resolutions,
unit conversion, cloud masking, shape clipping, index maths, stretch modes,
GeoTIFF export, change detection and STAC parsing.

It also covers the image-quality tools (compositing fills gaps, haze removal
finds the right floor, pan-sharpening adds real detail without shifting hue,
CLAHE is locally adaptive, hillshade lights the correct slopes) and the
desktop-app plumbing (port selection, window arguments, generated shortcuts,
the web-app manifest and service worker).

## Requirements

Python 3.10+. Everything else installs itself on first run: `rasterio` ships
GDAL in its wheels, and `pywebview` uses the window toolkit your OS already has
(WebView2 on Windows, WebKit on macOS, GTK or Qt on Linux). If no native
toolkit is available the studio falls back to a chrome-less browser window.

Internet access is needed for imagery, map tiles and place search — but not in
`--demo` mode.
