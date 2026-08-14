# Sent-2 · Sentinel-2 imagery

Look at any place on Earth in free Sentinel-2 imagery — and, because one pass
is never the best picture available, merge several dates into one that is
sharper than any single pass and has the cloud taken out.

That is the whole app. One screen: a map, the dates over your area, and the
imagery.

It is a desktop app: run one Python file and it opens in its own window.

```bash
python app.py
```

That's it — `app.py` installs anything missing on first run, starts the local
engine and opens Sent-2 in a desktop window. No account, no API key, no
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

## The satellite

**Sentinel-2**, and only Sentinel-2 — the best free optical imagery there is,
and the reason the app is called Sent-2.

| | |
| --- | --- |
| Resolution | 10 m visible and near-infrared, 20 m red-edge and short-wave infrared |
| Revisit | About every five days, everywhere on Earth |
| Archive | June 2015 to this morning |
| Bands | Twelve, from coastal aerosol at 443 nm to short-wave infrared at 2190 nm |
| Level | L2A surface reflectance, atmospherically corrected |
| Cost | Free, no account, no key |

It comes from [Earth Search](https://earth-search.aws.element84.com/v1), a
public catalogue in front of the Sentinel-2 archive on AWS Open Data. Only the
bands and the pixels your area needs are read, by HTTP range request straight
out of the cloud-optimised GeoTIFFs, so a small area is quick even though a
source tile runs to a gigabyte.

Stored numbers become surface reflectance on the way in, including the -1000
offset that scenes from January 2022 onwards carry, so the values behind every
picture are physically correct.

## Making the imagery look better

Satellite imagery rarely looks its best raw. The **Make it look better** panel
does the work that matters, in the right order and in the right units.

**Merging dates** — the big one, and the only thing here that adds detail
rather than presenting existing detail better. See
[below](#one-date-or-many-merged).

**Haze removal** — dark-object subtraction. Deep shadow and clear water should
read near zero; whatever they actually read is atmosphere. Subtracting it per
band lifts the blue-grey veil without shifting colour.

**Adaptive contrast** — contrast-limited histogram equalisation, applied to
brightness only. A single stretch has to compromise between a bright desert and
a dark forest in one frame; this equalises within tiles and blends them, so both
read properly and no seams show.

Plus **denoise** (median filtering, edges intact), **white balance**,
**vibrance**, and **detail** with overshoot clamping so strong settings do not
draw halos around coastlines. Five one-click presets — Off, Balanced, Punchy,
Hazy day, Natural — set sensible combinations.

Everything applied is recorded in the imagery's metadata and reported when it
appears, so a picture always says how it was made.

---

## One date, or many merged

The first choice in the app, because the two answer different questions.

**One date** is what the satellite saw on a day: a single pass, exactly as
recorded, cloud and all. Pick the date and look at it. This is what you want
when the day itself matters — a flood, a fire, the state of a field last
Tuesday.

**Merge dates** is what the ground looks like, put together from several
passes. It is better in two ways at once, which is why it is one button and
not two:

- **Higher definition.** The merge is sampled two to four times finer than the
  satellite's own 10 m, with detail measured off the ground rather than
  invented by a model.
- **More of the ground.** Cloud sitting over one date is taken out by the
  others, so the picture is clearer than any single pass and often has no gaps
  at all.

How much finer follows from how many dates you ticked — 2 earn 2×, 5 earn 3×,
9 earn 4× — and the button says which you are getting before you press it:
*"Merge 6 dates → 3×"*.

**Why there is anything to recover.** A satellite never samples the same ground
twice in the same place. Orbits repeat to within a few tens of metres and each
pass lands its pixel grid at a different sub-pixel phase, so every date reports
a *different average* of the same ground. Ten dates are ten different equations
about what lies under one pixel. Solve them together and the answer is finer
than any one of them could be.

**How it works.** Nothing is upscaled and then sharpened. Every date is read
straight onto the finer grid from its own native pixels, so each arrives
carrying its own sampling phase, and then:

| Step | What happens |
| --- | --- |
| **Register** | Sub-pixel alignment by phase correlation with an upsampled DFT — accurate to about a twentieth of a pixel. Only frequencies below the native Nyquist are compared, because above it the phase turns with the sampling grid rather than with the ground. A peak that does not stand clear of its rivals is refused: the dates arrive georeferenced, so leaving one where the satellite put it beats moving it on a guess. |
| **Fuse** | Each pixel is the mean of the dates that agree about it, centred on the median and rejecting anything beyond a few robust deviations — the noise reduction of a mean with the cloud immunity of a median. |
| **Restore** | Van Cittert deconvolution of the sampling blur, clamped into the local range of the input on every pass, so edges sharpen and do not ring. |

**What it reports.** How big the result is, how much finer it reads, how much
of the noise went, and how much of the frame came out clear:
*"3× merge of 6 dates — 1536×1116 px at 3.6 m/px · +31% detail · 12% less
noise · 100% clear"*.

**Getting the best out of it.** Dates close together work best: the method
assumes every date saw the same ground, so a year of crop growth averages into
*less* detail, not more — and the report says so plainly when that is what
happened, rather than claiming a gain it did not make. More dates always help
the cloud, even when they are too few to earn the next multiplier. The
multiplier is capped so the merged grid stays within 4096 px, so a large area
at 2048 px gets 2× rather than 3×.

It composes with everything else — haze removal, adaptive contrast and the
rest run afterwards on the merged image, in reflectance.

---

## Where the imagery comes from

[Earth Search](https://earth-search.aws.element84.com/v1), Element 84's free
STAC API in front of the `sentinel-2-l2a` collection on AWS Open Data. No
account, no key, no token — a search is one anonymous HTTP request, and so is
every band read after it.

The imagery is licensed for any use, including commercially, with attribution:
*Contains modified Copernicus Sentinel data*. That line rides along with every
render's metadata.

## The screen

One map, one sidebar, three steps down it, and a button at the bottom that is
always in reach and always says exactly what it will do.

**1 · Pick a place.** Type a place name to fly there, then pick a shape —
**box**, **circle**, **lasso** or **polygon** — and drag it out on the map.

The map is yours the rest of the time: dragging pans, the wheel zooms, and
nothing is ever drawn by accident. A shape tool has to be picked deliberately,
it lights the hint bar orange while it is armed, and it disarms itself the
moment you finish a shape — so the drag after the one that drew your area pans
the map like any other.

Drawing an area is a clear enough request for the dates over it, so the search
runs on its own.

**2 · One date, or many merged.** The choice
[described above](#one-date-or-many-merged), made before anything else so that
the list below it means the right thing: radio buttons for one date, tick boxes
for a merge. Every Sentinel-2 pass over your area is listed, newest first, with
how cloudy each was; the clearest is chosen for you, or the six clearest ticked,
and the list scrolls to show you which. The dates searched and the cloud ceiling
fold away into a line you can open when you want to change them.

**3 · How it looks.** Nine band combinations and eight indices:

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

Indices come with a colour scale and a legend on the map. Also here: cloud and
shadow masking from the scene classification band, clipping to the exact shape
you drew, and sizes up to 4096 px before a merge multiplies them. **Fine
tuning** folds away the rest — tone mapping, gamma, haze removal, adaptive
contrast, denoise, detail, vibrance, white balance, and five presets — and says
how many of them you have moved.

**The button.** It sits below the panel where it cannot scroll away, with a
line above it saying what pressing it will produce: *"6 dates → 3× detail ·
1536 px · 9 dates would reach 4×"*. Press it and the imagery lands on the map,
which flies to it. Once it is showing, the button reads **Showing this now**
and greys out until you change something — so it always tells you whether what
you are looking at is what your settings say. Save the result as a PNG or as a
georeferenced **GeoTIFF** that drops straight into QGIS.

## Demo mode

`python run.py --demo` replaces the catalogue with synthetic dates generated
from noise fields: plausible water, vegetation, soil, urban and snow cover, a
seasonal cycle, slow urban growth over the years, and clouds matching each
date's stated cover. It is there so the interface can be explored, tested and
demonstrated offline.

It is not real imagery, and the app says so everywhere it could matter: a badge
in the header, a second badge in place of the collection name, a warning when
it starts, and a flag on every render's metadata.

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
  enhance.py     compositing, haze removal, CLAHE, denoise, white balance
  superres.py    sub-pixel registration, multi-frame fusion, deconvolution
  app.py         FastAPI routes and static hosting
  config.py      the satellite, band table, composites, indices, colour maps
  stac.py        catalogue search, plus synthetic scenes for demo mode
  raster.py      windowed COG reads, reprojection, cloud masking, demo bands
  composite.py   stretches, indices, colour maps, statistics, encoding
  service.py     render orchestration: merging, enhancement, caching
  geo.py         AOI handling, geodesic area, output grid
  launcher.py    port choice, app windows, desktop shortcuts
frontend/
  index.html, css/app.css, manifest.webmanifest, sw.js
  js/            map, imagery, store, ui, api, install
  icons/         app icons, favicon.ico and a macOS .icns
  vendor/        Leaflet 1.9.4 (BSD-2-Clause), vendored — no CDN needed
launchers/       double-clickable launchers for macOS, Linux and Windows
tools/           icon generator
tests/
  test_pipeline.py   reading, reprojection, cloud masking, indices, export
  test_superres.py   registration, fusion, restoration, the resolution gain
  test_enhance.py    compositing and the image-quality tools
  test_launcher.py   ports, windows, shortcuts, manifest and service worker
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

The suite writes GeoTIFFs that match how Sentinel-2 really stores itself — the
DN offset from baseline 04.00, 10 m and 20 m bands side by side, the scene
classification layer — and runs the live (non-demo) reading path over them.
That covers reprojection onto the output grid, mixed band resolutions,
reflectance conversion, cloud masking, shape clipping, index maths, stretch
modes, GeoTIFF export and STAC parsing.

Merging gets its own file. The scenes there are one fixed patch of
ground, held on a grid four times finer than any date samples it, with each
date averaging that ground over its own footprint a quarter of a pixel from the
last — which is what makes the tests able to ask the only question that
matters: is the fused image closer to the ground than any single date was? They
also check that registration recovers a known sub-pixel shift, ignores a
difference in brightness, refuses an implausible or ambiguous match, that the
fusion throws out a cloud the mask missed but keeps a pixel any date saw, and
that the deconvolution sharpens an edge without ringing around it — and that
merging two or more dates sharpens by default, at the multiplier the number of
dates has earned.

It also covers the image-quality tools (compositing fills the gaps, haze
removal finds the right floor, adaptive contrast really is local, denoise
removes speckle, white balance neutralises a cast) and the desktop-app plumbing
(port selection, window arguments, generated shortcuts, the web-app manifest
and service worker).

## Requirements

Python 3.10+. Everything else installs itself on first run: `rasterio` ships
GDAL in its wheels, and `pywebview` uses the window toolkit your OS already has
(WebView2 on Windows, WebKit on macOS, GTK or Qt on Linux). If no native
toolkit is available Sent-2 falls back to a chrome-less browser window.

Internet access is needed for imagery, map tiles and place search — but not in
`--demo` mode.
