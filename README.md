# Sent-2 · Sentinel imagery

Look at any place on Earth in free Sentinel imagery — and, because one pass is
never the best picture available, merge several dates into one that is sharper
than any single pass and has the cloud taken out.

Two satellites cover the same ground and answer different questions.
**Sentinel-2** photographs it in daylight, when there is no cloud in the way.
**Sentinel-1** measures it with radar, through cloud and at night. Either can
be shown, and both can sit on the map at once so you can fade between them.

Right-click anywhere to find out when each of them next flies over. Switch on
**live clouds** for today's sky and **active fires** for every thermal
detection NASA has published in the last day. Highlight part of the imagery to
take it away as a picture, marked `@Kaldockhi`.

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

## The satellites

**Sentinel-2** is the reason the app is called Sent-2 — the best free optical
imagery there is. **Sentinel-1** is what you turn to when Sentinel-2 cannot
help: it is radar, so cloud, smoke and darkness make no difference to it.

| | Sentinel-2 | Sentinel-1 |
| --- | --- | --- |
| What it is | Optical — a photograph | C-band radar — a measurement of roughness |
| Resolution | 10 m visible and near-infrared, 20 m red-edge and short-wave infrared | ~20 m, delivered on a 10 m grid |
| Revisit | About every five days | About every 6–12 days |
| Archive | June 2015 to this morning | October 2014 to this morning |
| Bands | Twelve, 443 nm to 2190 nm | VV and VH polarisation |
| Level | L2A surface reflectance, atmospherically corrected | GRD amplitude, shown in decibels |
| Sees through cloud | No | Yes |
| Works at night | No | Yes |
| Cost | Free, no account, no key | Free, no account, no key |

Sentinel-2 comes from [Earth Search](https://earth-search.aws.element84.com/v1),
a public catalogue in front of the archive on AWS Open Data. Only the bands and
the pixels your area needs are read, by HTTP range request straight out of the
cloud-optimised GeoTIFFs, so a small area is quick even though a source tile
runs to a gigabyte.

**Sentinel-1 needs more than one home, and the reason is worth knowing.** The
GRD products in the AWS `sentinel-s1-l1c` bucket are requester-pays, so an
anonymous read is refused outright — and the measurement files inside them are
georeferenced by ground control points rather than by a map transform. Warping
one of those without honouring its GCPs does not fail; it quietly produces a
smooth smear with no ground in it, which looks like imagery and is not.

So radar is asked for from [Microsoft's Planetary
Computer](https://planetarycomputer.microsoft.com/) first, which republishes
the same acquisitions as projected cloud-optimised GeoTIFFs and hands out a
free, anonymous signature for reading them — no account, no key. Earth Search
stays behind it as a fallback, and each catalogue is tried in turn until one
answers. Whichever did is recorded on the scene and credited on the render.
Pin one with `S1_SOURCE=planetary-computer` or `S1_SOURCE=earth-search` if you
would rather not have the choice made for you.

When a read does fail, it says what failed and where: a refusal names the host
and explains requester-pays, a file with no map projection is refused rather
than warped into a smear, and a pass whose swath misses your shape says so
instead of handing back a blank.

Stored numbers become physical units on the way in. For Sentinel-2 that is
surface reflectance, including the -1000 offset that scenes from January 2022
onwards carry. For Sentinel-1 it is decibels — amplitude squared is power, and
radar is always read on a log scale because backscatter spans four orders of
magnitude between still water and a city.

**Reading a radar picture.** Brightness is roughness and geometry, not colour.
Still water reflects the pulse away and comes back black; buildings line their
corners up with the satellite and come back brightest of all. VH only returns
from things that scatter in a volume — foliage, mostly — so the standard
false colour (VV red, VH green, their ratio blue) separates towns, crops and
water on its own: **black water, white towns, green vegetation, violet bare
ground**. Three ways to look at it: **radar colour**, **VV only**, and **water
& flood**, which puts the cross-polarised channel first so still water goes to
near black.

**Radar is displayed in linear power, on fixed windows, and both of those
matter more than they sound.**

Decibels are right for measuring and for merging — that is the scale on which
backscatter means something and on which speckle averages out — but wrong for
looking at. A logarithm gives water, soil, vegetation and concrete roughly
equal shares of the histogram, so a stretch in decibels comes out looking like
a poster. Converting back to power puts the scene where radar actually lives:
dark, with a long bright tail.

Fixed windows matter more still. VV and VH measure the same ground twice and
agree to within about a percent — 0.993 correlation — so red and green move
together and all the colour rides on their ratio, whose real spread is two or
three decibels. Stretch each channel to its own percentiles, as an optical
composite sensibly does, and that two-decibel wiggle is amplified to full
scale: every scene collapses onto a single garish red-to-cyan axis, flat ground
acquires structure that was never there, and no two dates are comparable.
Anchoring each channel to a window in physical units is what makes a radar
picture mean the same thing twice.

Two smaller things follow from the physics. The VV/VH ratio is **multi-looked**
over a 50 m neighbourhood, because speckle in the two polarisations is
independent — so their ratio carries half again as much of it while spanning a
far narrower range, which would otherwise paint the grain in colour. And a
ratio is a property of a patch of ground rather than of one resolution cell, so
averaging is what the quantity actually means. Second, the sea is black because
of the **noise floor**: calm water returns less than the instrument's own
thermal noise, so both polarisations read mostly noise and their ratio
collapses towards one. Model the water without that floor and it has the widest
polarisation gap in the scene — and the blue channel lights the sea up brighter
than the land.

The two satellites are never merged into each other. Reflectance and
backscatter are different physical quantities in different units, and averaging
them together would mean nothing — so a render is always of one satellite, and
ticking a date from the other one starts a fresh selection. Putting both on the
map at the same time is a different matter, and that is what the layer dock in
the corner is for.

## Making the imagery look better

Satellite imagery rarely looks its best raw, and there are two quite different
places to improve it.

**In the reflectance, before the picture exists.** These need the numbers the
satellite measured, so they happen server-side and changing one fetches the
imagery again. This is where the corrections that have a physically right
answer belong.

**Highlight roll-off** — always on, and the reason bright ground has colour at
all. A plain linear stretch clips everything above its window to pure white:
on a sandy or built-up scene that was 61% of the frame, and a pixel with all
three channels pinned at 255 has lost its texture and its hue together.
Compressing the top of the range instead of walling it off keeps both. Measured
on a scene of sand, roads and scrub, it takes clipping from 61% to none and
lifts saturation from 8.6 to 15.3 on its own.

**Haze removal is on by default** — dark-object subtraction, and the other
half of the colour. Raw Sentinel-2 does not look like the ground; it looks like
the ground through fifty kilometres of atmosphere. Deep shadow and clear water
should read near zero, so whatever they actually read is scattering, and it is
strongest in blue — which is exactly what makes untreated imagery read milky
and blue-grey. Subtracting it per band takes saturation from 15 to 34 and turns
flat grey sand back into sand, without shifting hue. The app opens on the
**Balanced** preset (haze removal, a little adaptive contrast, detail and
vibrance) so the first picture already looks right; **Off** gives you the
untouched stretch.

**Merging dates** — the big one, and the only thing here that adds detail
rather than presenting existing detail better. See
[below](#one-date-or-many-merged).

**Adaptive contrast** — contrast-limited histogram equalisation, applied to
brightness only. A single stretch has to compromise between a bright desert and
a dark forest in one frame; this equalises within tiles and blends them, so both
read properly and no seams show.

Plus **denoise** (median filtering, edges intact), **white balance**,
**vibrance**, and **detail** with overshoot clamping so strong settings do not
draw halos around coastlines. Five one-click presets — Off, Balanced, Punchy,
Detail, Hazy day, Natural — set sensible combinations.

**On the finished picture, in the browser.** Ordinary photographic
adjustments — **contrast**, exposure, saturation, clarity, highlights, shadows,
warmth, tint, midtones and vignette — applied to the imagery already on the
map. They run on the pixels in front of you, so they take effect as you drag
the slider with nothing to re-fetch and nothing to undo. Nine one-click looks
(Natural, Punchy, Soft, Vivid, Crisp, Mono, Cold, Warm, Faded) are starting
points — **Aerial** is the one that reads most like commercial aerial
photography: warm sand, deep shadows, hard edges and colour that is actually
present. **Back to the original** returns the
untouched render.

Both are non-destructive. The processing applied in the render is recorded in
the imagery's metadata and reported when it appears, and the adjustments live
only in the view until you save.

**Saving.** *Save PNG* writes what you are looking at, at full render size,
adjustments included. *GeoTIFF* writes the georeferenced measurement as the
satellite had it — adjustments are a way of looking at data, not a change to
it, and a file destined for QGIS should not carry them.

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

- **Higher definition.** The picture stays the size you asked for; what
  changes is what it resolves — two to four times finer than the satellite's
  own 10 m, with detail measured off the ground rather than invented by a
  model. A merge that handed back a bigger file would spread the same detail
  over more pixels and look no better beside a single date, which is the whole
  point of the exercise.
- **More of the ground.** Cloud sitting over one date is taken out by the
  others, so the picture is clearer than any single pass and often has no gaps
  at all.

How much finer follows from two things at once, and the button says which you
are getting before you press it: *"Merge 6 dates → 2.5× finer"*.

**The dates** set the ceiling — 2 earn 2×, 5 earn 3×, 9 earn 4×.

**The size** decides whether any of it is reachable. Detail recoverable by
merging hides *between* the satellite's samples, so it only exists where the
output grid is finer than those samples. Render a wide area at 512 px and each
pixel covers more ground than a whole Sentinel-2 measurement: several dates
still clear the cloud, but there is nothing finer to find, and the panel says
so rather than pretending. Raise the size and the gap opens up for the merge to
fill. The panel tells you which of the two is currently the limit — *"a larger
size would resolve more"* or *"more dates would resolve more"*.

**Why there is anything to recover.** A satellite never samples the same ground
twice in the same place. Orbits repeat to within a few tens of metres and each
pass lands its pixel grid at a different sub-pixel phase, so every date reports
a *different average* of the same ground. Ten dates are ten different equations
about what lies under one pixel. Solve them together and the answer is finer
than any one of them could be.

**How it works.** Nothing is upscaled and then sharpened. Every date is read
straight onto the finer grid from its own native pixels, **as measured** —
nearest-neighbour, not interpolated. That detail decides most of the result:
interpolating a date on the way in averages neighbouring measurements
together, and those differences between neighbours are exactly what the merge
is about to solve for. Measured against known ground truth, reading the dates
interpolated recovers 60% of the true fine detail and beats one date by 17%;
reading them as measured recovers 80% and beats one date by 38%. So each date
arrives carrying its own sampling phase intact, and then:

| Step | What happens |
| --- | --- |
| **Register** | Sub-pixel alignment by phase correlation with an upsampled DFT — accurate to about a twentieth of a pixel. Only frequencies below the native Nyquist are compared, because above it the phase turns with the sampling grid rather than with the ground. A peak that does not stand clear of its rivals is refused: the dates arrive georeferenced, so leaving one where the satellite put it beats moving it on a guess. |
| **Fuse** | Each pixel is the mean of the dates that agree about it, centred on the median and rejecting anything beyond a few robust deviations — the noise reduction of a mean with the cloud immunity of a median. |
| **Restore** | Van Cittert deconvolution of the sampling blur, clamped into the local range of the input on every pass, so edges sharpen and do not ring. |

**What it reports.** How big the result is, what it can actually resolve, how
much sharper it reads, how much of the noise went, and how much of the frame
came out clear: *"3× merge of 6 dates — 1536×1116 px · ~3.3 m detail · +31%
sharper · 12% less noise · 100% clear"*.

**Pixel size is not resolution, and Sent-2 will not pretend otherwise.** Ask
for a small area at 2048 px and the pixels come out under a metre across — but
they are interpolated from 10 m data and cannot show anything Sentinel-2 did
not see. So the figure reported is the honest one: the satellite's 10 m divided
by what the merge won. Four dates give ~5 m, nine give ~2.5 m, and that is the
floor. Imagery showing individual cars and fence posts is 0.3–0.5 m, six times
finer again, and it comes from commercial satellites (Maxar, Planet SkySat,
Airbus Pléiades) or aircraft — no amount of processing gets Sentinel-2 there,
because the detail was never recorded.

**A merge is never softer than the date it started from.** That is the one
outcome nobody would accept, and without guarding against it, it happens:
ground changes between passes, and the satellite's own pointing error varies
across a frame in a way a single shift cannot correct. Averaging things that
disagree blurs them. Measured against a single date, merging six passes with a
pixel of drifting misregistration came back at 0.79× the fine detail — visibly
worse than not merging at all.

So the result is measured against what one date would have looked like, and
where the fusion has not recovered more than the averaging cost, the merge's
own fine structure is lifted to cover the difference — its high frequencies,
which have the noise averaged out of them, rather than a single date's noisier
ones. Across misregistration, changed ground and both together, the merge now
comes out between 1.05× and 1.14× the detail of one date instead of 0.79×.

**Merging radar is a different bargain, and the app says which one you are
getting.** Sentinel-1 arrives on a 10 m grid but resolves about 20 m, so it is
already over-sampled: there is nothing hiding between its samples for a merge
to solve for, and no honest way to sharpen it. What merging radar passes does
instead is kill the speckle — the grain that makes a single radar image hard
to read at all. Speckle is a random multiplier on every pixel, independent from
one pass to the next, so in decibels it is an unbiased additive error that
simply averages away: six passes come back with about 59% less of it. The
button says *"Average 6 radar passes"* rather than promising a sharpness it
cannot deliver, and the merge takes the mean rather than the median, because
there is no cloud to reject and the mean removes more noise.

**Getting the best out of it.** Dates close together work best: the method
assumes every date saw the same ground, so a year of crop growth averages into
*less* detail, not more — and the report says so plainly when that is what
happened, rather than claiming a gain it did not make. More dates always help
the cloud, even when they are too few to earn the next multiplier. The
multiplier is capped so the merged grid stays within 4096 px, so a large area
at 2048 px gets 2× rather than 3×.

It composes with everything else — haze removal, adaptive contrast and the
rest run afterwards on the merged image, in reflectance. The **Detail** slider
knows about the merge too: on a 3× merge the recovered structure is about three
pixels across, so sharpening at one pixel would work on the interpolation
rather than on the ground. Tying its radius to the merge scale adds 41% more
fine detail at the same fidelity to the truth, which is the difference between
sharpening the picture and sharpening the grain.

---

## When does it next fly over?

**Right-click anywhere on the map.** A panel says when each satellite last
crossed that point and when it will next be back: *"Sentinel-1 — in 2 d 6 h ·
Tue, 18 Aug, 17:52. Last pass 3 d ago · track 59, repeating every 12 days."*

There is no public "next overpass" service, but there is something better: the
catalogue of every pass already flown. Both satellites are held on a repeating
ground track — Sentinel-2 retraces its own every 10 days, Sentinel-1 every 12 —
and each track is numbered, so a place is visited by the same numbered orbit at
the same local time, over and over.

So the prediction is measurement, not modelling. The app asks the catalogue
what has crossed that point in the last 45 days, groups those passes by the
track that took them, works out the interval each track actually repeats on,
and steps the most recent one forward until it lands in the future. Whichever
track comes round soonest is the answer, and it inherits the real time of day.

Two things it is careful about. A missed or unarchived acquisition can only
make a gap bigger, never smaller, so the interval is the *smallest* gap on the
track rather than the average — a patchy record does not make the satellite
look slow. And one overflight is often filed as several products minutes apart;
those are collapsed to a single pass, or the prediction would creep a few
minutes early every cycle. Where there is only one pass on record it falls back
to the nominal repeat cycle and says so rather than inventing precision.

## Live clouds

Switch on **Live clouds** in the corner of the map and today's sky appears
under the imagery — the whole planet, as the polar orbiters last saw it.

The tiles are [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/), which
republishes every overpass as map tiles within about three hours of the
satellite taking it. Corrected reflectance: what the eye would see, with the
atmosphere's own scattering taken out of the land but left in the cloud, which
is precisely what makes it a cloud picture.

Be clear about what "live" means here, because it is not a geostationary loop.
Each satellite crosses a given place once a day at a fixed local time, so
choosing between **VIIRS on NOAA-20 or Suomi-NPP** (about 13:30 local) and
**MODIS on Terra** (about 10:30) or **Aqua** (13:30) is really choosing what
hour of the day you are looking at. One pass a day, and none of the night side.
Ask for today's mosaic before it has been assembled and the app steps back a
day and says why, rather than showing you an empty layer.

The useful trick is **Match the imagery**: set the cloud layer to the date of
the Sentinel scene you are looking at, and you can see the weather that pass
was flying through — why a scene is hazy on one side, or what the cloud your
merge just removed actually looked like. Fade it against the imagery with the
slider; it draws under your drawn area and under the render, because it is
context rather than the subject.

## What is on fire

Switch on **Active fires** in the corner of the map and every thermal
detection NASA has published over the visible area appears on it, over the last
24 hours, 48 hours or 7 days.

The data is [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/) — the Fire
Information for Resource Management System, which publishes every hot spot the
polar-orbiting satellites have seen, worldwide, within about three hours of the
overpass. VIIRS resolves 375 m and catches much smaller fires than MODIS's
1 km, so it leads; MODIS is kept because it fills the gaps between the VIIRS
overpasses.

**A detection is not a fire.** It is one satellite pixel, a few hundred metres
across, that came back hot at a known minute — so it is drawn as a dot at a
point, never as a burnt area, and the app says so on the panel. What it tells
you is *where something was burning and when*, which is exactly the question a
satellite image of a fire raises. The dot is sized and coloured by fire
radiative power in megawatts, which is the closest thing to "how bad": a few MW
is a field being cleared, hundreds is a fire front. Low-confidence detections
are drawn fainter rather than hidden — weaker evidence is still evidence.

It pairs with the imagery on purpose. Sentinel-2 shows the smoke and the scar;
Sentinel-1 sees the ground through the smoke; the detections say which part of
what you are looking at was alight, and at what hour.

**No account needed.** The public FIRMS archive files are open, so the layer
works out of the box — one global file per sensor, fetched once and held for
fifteen minutes rather than pulled down again on every pan. If you have a free
[FIRMS MAP_KEY](https://firms.modaps.eosdis.nasa.gov/api/area/), put it in
`FIRMS_MAP_KEY` and the app will ask only for the rectangle you are looking at
instead, which is far smaller and a little fresher. A continent's worth of
detections would bury the browser, so when there are too many the fiercest are
kept and the panel says how many were left out.

## Taking a picture away

**Copy a region** (or **Save region**) arms a highlight tool: drag a box over
the part of the imagery you want, and it lands on your clipboard as a PNG
marked **@Kaldockhi**.

The crop is taken from the rendered imagery itself, not from the screen, so it
comes out at the full resolution that was fetched rather than at whatever size
the map happened to be showing — and it carries the adjustments you are looking
at, because the point is to take away what you can see. The corners are
converted through Web Mercator rather than treated as latitudes, which is what
stops the crop shearing. Under the mark sits a small credit line naming the
satellite and the date, which is both useful and what the Copernicus licence
asks for when the imagery is published.

Writing an image to the clipboard needs a secure context and the browser's
permission, and neither is guaranteed; if it is refused the picture is saved as
a file instead and the app says so, so it is never lost to a permission prompt.

## Where the imagery comes from

Sentinel-2 from [Earth Search](https://earth-search.aws.element84.com/v1),
Element 84's free STAC API in front of `sentinel-2-l2a` on AWS Open Data;
Sentinel-1 from the [Planetary
Computer](https://planetarycomputer.microsoft.com/), with Earth Search as its
fallback — see [The satellites](#the-satellites) for why radar needs both.
Clouds from [NASA GIBS](https://nasa-gibs.github.io/gibs-api-docs/) and fires
from [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/). No account, no key,
no token anywhere: a search is one anonymous HTTP request, and so is every band
read after it.

The two Sentinel collections are searched separately, because a cloud-cover
filter would throw away every radar scene: radar has no such property, and
filtering it by one would make it look as though it never flew.

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

Right-clicking is never drawing: it asks when the satellites next come over.

**2 · Which satellite, and when.** **Sentinel-2**, **Sentinel-1**, or **Both** —
then the choice [described above](#one-date-or-many-merged), made before
anything else so that the list below it means the right thing: radio buttons
for one date, tick boxes for a merge. Every pass over your area is listed,
newest first, each with a dot saying which satellite took it and how cloudy it
was — or `radar`, for the ones cloud cannot touch. The clearest is chosen for
you, or the six best ticked, and the list scrolls to show you which. The dates
searched and the cloud ceiling fold away into a line you can open when you want
to change them.

Picking a radar date changes everything downstream to suit it: the
visualisations on offer, the tuning presets, and what a merge is for.

**3 · How it looks.** Nine Sentinel-2 band combinations and eight indices, or
three radar composites and the VV/VH ratio:

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

| Radar composite | What it shows |
| --- | --- |
| Radar colour | The standard false colour: towns pink, crops green, water black |
| Radar (VV only) | Plain backscatter — bright is rough or metal, black is smooth water |
| Radar water & flood | Cross-polarised first, so still water goes to near black |

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
| VV/VH | Radar ratio — low where the ground scatters in a volume: forest, dense crops |

Indices come with a colour scale and a legend on the map. Also here: cloud and
shadow masking from the scene classification band, clipping to the exact shape
you drew, and sizes up to 4096 px before a merge multiplies them. **Fine
tuning** folds away the rest — tone mapping, gamma, haze removal, adaptive
contrast, denoise, detail, vibrance, white balance, and five presets — and says
how many of them you have moved. On radar it offers its own presets and drops
haze removal and white balance entirely: those are corrections to light, and
radar is not light, so leaving them in doing nothing would be a lie about what
the controls do.

Two more layers live in the corner of the map rather than in the sidebar,
because they are context for the imagery rather than part of building it:
**Live clouds** and **Active fires**, each with its own fade, time window and
panel saying exactly what it is showing.

**4 · Adjust the picture.** Contrast, exposure, saturation, clarity,
highlights, shadows, warmth, tint, midtones and vignette, plus nine looks.
Unlike step 3 these work on the picture already on the map, so they apply as
you drag.

**The button.** It sits below the panel where it cannot scroll away, with a
line above it saying what pressing it will produce: *"6 dates → 3× detail ·
1536 px · 9 dates would reach 4×"*. Press it and the imagery lands on the map,
which flies to it. Once it is showing, the button reads **Showing this now**
and greys out until you change something — so it always tells you whether what
you are looking at is what your settings say. Save the result as a PNG or as a
georeferenced **GeoTIFF** that drops straight into QGIS, or highlight part of
it and take that away marked.

**The layer dock.** Each render stays on the map as its own layer, one per
satellite, with its own fade. That is what lets you put radar and optical over
the same ground and cross-fade between them — the cloud-free measurement
underneath, the photograph on top. `×` takes a layer off again.

## Demo mode

`python run.py --demo` replaces the catalogue with synthetic dates generated
from noise fields: plausible water, vegetation, soil, urban and snow cover, a
seasonal cycle, slow urban growth over the years, and clouds matching each
date's stated cover. Both satellites fly, on their own cadences and at their
own times of day, so a radar pass lands on a different date from the optical
one. The synthetic radar carries speckle — independent every pass, because
without it merging Sentinel-1 offline would look pointless when it is not. It
is there so the interface can be explored, tested and demonstrated offline.

The synthetic radar carries the two things that make a radar picture look like
one: the instrument's thermal noise floor, which is why the sea comes out
black, and speckle that thins out as you zoom away, because an output pixel
covering many resolution cells is already an average of them. Active fires get
synthetic detections too, clustered along a front with a tail rather than
sprinkled evenly.

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
  config.py      the satellites, band table, composites, indices, colour maps
  stac.py        catalogue search over both collections, plus synthetic scenes
  passes.py      when each satellite last flew over a point, and when it next will
  fires.py       NASA FIRMS active fire detections
  raster.py      windowed COG reads, reprojection, cloud masking, demo bands
  composite.py   stretches, indices, colour maps, statistics, encoding
  service.py     render orchestration: merging, enhancement, caching
  geo.py         AOI handling, geodesic area, output grid
  launcher.py    port choice, app windows, desktop shortcuts
frontend/
  index.html, css/app.css, manifest.webmanifest, sw.js
  js/            map, imagery, fires, clouds, capture, adjust, store, ui, api, install
  icons/         app icons, favicon.ico and a macOS .icns
  vendor/        Leaflet 1.9.4 (BSD-2-Clause), vendored — no CDN needed
launchers/       double-clickable launchers for macOS, Linux and Windows
tools/           icon generator
tests/
  test_pipeline.py   reading, reprojection, cloud masking, indices, export
  test_superres.py   registration, fusion, restoration, the resolution gain
  test_enhance.py    compositing and the image-quality tools
  test_launcher.py   ports, windows, shortcuts, manifest and service worker
  test_satellites.py radar reading and colour, the two satellites apart, overpasses
  test_fires.py      FIRMS parsing, the bounding box, the time window, the cap
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

Sentinel-1 has its own file, and most of what is in it is about the boundary
between the two satellites: that amplitude becomes the right number of
decibels, that the VV/VH ratio is worked out rather than downloaded, that a
band or a composite belonging to one satellite is refused to the other, that
the two are never merged into each other, and that merging radar averages the
speckle away instead of claiming a sharpness it cannot deliver. The overpass
prediction is tested where it is easy to get wrong: a gap in the record must
not be read as a slower satellite, one pass filed twice must not be read as an
interval, and the answer must always be in the future at the right time of
day.

## Requirements

Python 3.10+. Everything else installs itself on first run: `rasterio` ships
GDAL in its wheels, and `pywebview` uses the window toolkit your OS already has
(WebView2 on Windows, WebKit on macOS, GTK or Qt on Linux). If no native
toolkit is available Sent-2 falls back to a chrome-less browser window.

Internet access is needed for imagery, map tiles, place search, overpass
prediction and fire data — but not in `--demo` mode.

Two optional settings, both with sensible defaults. `FIRMS_MAP_KEY` — a free
key from NASA — makes the fire layer ask for just the rectangle on screen
instead of the global file; the layer works without it. `S1_SOURCE` pins which
catalogue Sentinel-1 comes from (`planetary-computer` or `earth-search`)
instead of trying each in turn.
