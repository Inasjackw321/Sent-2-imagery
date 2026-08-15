// Today's cloud, from NASA's near-real-time imagery service.
//
// GIBS republishes every polar overpass as map tiles within about three hours
// of the satellite taking it, so the most recent daily pass is the closest
// thing to a live global picture of where the cloud is. It is not a
// geostationary loop: it is one strip per orbit, stitched into a whole Earth
// once a day, and the night side of the planet is simply not there.
//
// Which makes it exactly the right companion to the imagery. A Sentinel-2
// scene is only as good as the sky over it, and this says what the sky was
// doing -- today, or on the day of the scene you are looking at.

import { store, on } from './store.js';
import { $, el, toast, fmt, debounce } from './ui.js';

const TILES = 'https://gibs.earthdata.nasa.gov/wmts/epsg3857/best'
  + '/{layer}/default/{date}/{matrix}/{z}/{y}/{x}.{fmt}';

// Corrected reflectance: what the eye would see, with the atmosphere's own
// scattering taken out of the land but left in the cloud. Each satellite
// crosses at a fixed local time, so which one you pick is really a choice of
// what hour of the day you are looking at.
const SOURCES = {
  'viirs-noaa20': {
    layer: 'VIIRS_NOAA20_CorrectedReflectance_TrueColor',
    label: 'VIIRS · NOAA-20', when: 'about 13:30 local', metres: 250,
  },
  'viirs-snpp': {
    layer: 'VIIRS_SNPP_CorrectedReflectance_TrueColor',
    label: 'VIIRS · Suomi-NPP', when: 'about 13:30 local', metres: 250,
  },
  'modis-terra': {
    layer: 'MODIS_Terra_CorrectedReflectance_TrueColor',
    label: 'MODIS · Terra', when: 'about 10:30 local', metres: 250,
  },
  'modis-aqua': {
    layer: 'MODIS_Aqua_CorrectedReflectance_TrueColor',
    label: 'MODIS · Aqua', when: 'about 13:30 local', metres: 250,
  },
};

// GIBS names its tile grids by how deep they go, and 250 m imagery stops at
// level 9. Past that the tiles are stretched rather than withheld, so the
// layer stays on screen when you zoom into an area instead of vanishing.
const MATRIX = 'GoogleMapsCompatible_Level9';
const NATIVE_ZOOM = 9;

// How cloud is told apart from ground. The tiles are a picture of the whole
// Earth -- land, sea and cloud together -- and only the cloud is wanted, so
// every pixel is judged and the rest is made transparent.
//
// Two things separate them, and it takes both. Cloud is bright, but so is
// desert and so is sand. Cloud is also close to colourless, because it
// reflects every visible wavelength about equally, while bright ground almost
// never is -- sand is orange, water is blue, vegetation is green. Bright *and*
// grey is cloud; bright and coloured is ground.
//
// Snow is the honest exception: it is bright and grey too, and no rule written
// on a true-colour picture can tell it from cloud. The panel says so.
const SATURATION_FLOOR = 0.20;   // below this, colourless enough to be cloud
const SATURATION_CEILING = 0.50; // above this, too coloured to be cloud

function smoothstep(edge0, edge1, x) {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

/**
 * How much of a pixel is cloud, from 0 to 1.
 *
 * Exported so it can be checked against known colours rather than by eye.
 */
export function cloudiness(r, g, b, sensitivity = 0.5) {
  const high = Math.max(r, g, b);
  const low = Math.min(r, g, b);
  const value = high / 255;
  const saturation = high === 0 ? 0 : (high - low) / high;

  // Sensitivity slides the brightness a pixel needs: turn it up and thin haze
  // starts to count, turn it down and only solid cloud does.
  const floor = 0.62 - 0.42 * sensitivity;
  const bright = smoothstep(floor, floor + 0.26, value);
  const colourless = 1 - smoothstep(SATURATION_FLOOR, SATURATION_CEILING, saturation);
  return bright * colourless;
}

/** Rewrite a tile's alpha so only its cloud survives. */
export function maskToCloud(pixels, sensitivity) {
  const data = pixels.data;
  for (let i = 0; i < data.length; i += 4) {
    data[i + 3] = Math.round(255 * cloudiness(data[i], data[i + 1], data[i + 2],
                                              sensitivity));
  }
  return pixels;
}

let map = null;
let layer = null;
let enabled = false;
let source = 'viirs-noaa20';
let day = today();
let opacity = 0.85;
// Tuned against known ground colours: thick and thin cloud at full strength,
// haze most of the way, a grey city only faintly.
let sensitivity = 0.42;
let steppedBack = false;

export function initClouds(leafletMap) {
  map = leafletMap;
  // Its own pane, above the imagery: cloud is over the ground, not under it.
  map.createPane('clouds').style.zIndex = 450;
  map.getPane('clouds').style.pointerEvents = 'none';
  buildDock();
  // A new render is a new date to offer to match.
  on('image', () => paint());
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function shift(date, days) {
  const when = new Date(`${date}T12:00:00Z`);
  when.setUTCDate(when.getUTCDate() + days);
  return when.toISOString().slice(0, 10);
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#cloudDock');
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'cloud-toggle', id: 'cloudToggle', onclick: toggle },
      el('span', { class: 'cloud-mark' }, '☁'), 'Live clouds'),
    el('div', { class: 'cloud-body', id: 'cloudBody', hidden: true },
      el('div', { class: 'cloud-days' },
        el('button', { class: 'cloud-step', title: 'The day before',
          onclick: () => setDay(shift(day, -1)) }, '‹'),
        el('span', { class: 'cloud-date', id: 'cloudDate' }, day),
        el('button', { class: 'cloud-step', title: 'The day after',
          onclick: () => setDay(shift(day, 1)) }, '›')),
      el('div', { class: 'cloud-jump' },
        el('button', { class: 'cloud-link', id: 'cloudToday',
          onclick: () => setDay(today()) }, 'Today'),
        el('button', { class: 'cloud-link', id: 'cloudMatch',
          onclick: matchImagery }, 'Match the imagery')),
      el('select', {
        class: 'cloud-source', id: 'cloudSource',
        onchange: (e) => { source = e.target.value; paint(); rebuild(); },
      }, ...Object.entries(SOURCES).map(([key, spec]) =>
        el('option', { value: key, selected: key === source }, spec.label))),
      el('label', { class: 'cloud-fade' }, 'Fade',
        el('input', {
          type: 'range', min: 10, max: 100, value: Math.round(opacity * 100),
          oninput: (e) => {
            opacity = e.target.valueAsNumber / 100;
            layer?.setOpacity(opacity);
          },
        })),
      el('label', { class: 'cloud-fade' }, 'Catch',
        el('input', {
          type: 'range', min: 0, max: 100, value: Math.round(sensitivity * 100),
          oninput: (e) => {
            sensitivity = e.target.valueAsNumber / 100;
            queueRepaint();
          },
        })),
      el('div', { class: 'cloud-note', id: 'cloudNote' })),
  );
  rebuild();
}

/** Keep the panel saying what is actually on the map. */
function rebuild() {
  const spec = SOURCES[source];
  $('#cloudDate').textContent = day === today() ? `${day} · today` : day;
  $('#cloudNote').innerHTML =
    `Crosses ${spec.when}, published within about three hours.<br>`
    + 'One pass a day, and none of the night side.<br>'
    + '<b>Cloud only</b> — the ground is cut out. Snow reads as cloud.';
  const shown = store.image?.meta?.scene?.date;
  const match = $('#cloudMatch');
  match.hidden = !shown || shown === day;
  if (shown) match.textContent = `Match ${fmt.date(shown)}`;
}

function toggle() {
  enabled = !enabled;
  $('#cloudToggle').classList.toggle('is-on', enabled);
  $('#cloudBody').hidden = !enabled;
  paint();
}

function setDay(next) {
  // Nothing has been photographed tomorrow yet.
  day = next > today() ? today() : next;
  steppedBack = false;
  paint();
  rebuild();
}

/** Show the sky on the day of the imagery underneath. */
function matchImagery() {
  const shown = store.image?.meta?.scene?.date;
  if (!shown) {
    toast('Show some imagery first, then its date can be matched');
    return;
  }
  setDay(shown);
  if (!enabled) toggle();
}

// ── The layer ──────────────────────────────────────────────────

/**
 * A tile layer that keeps only the cloud out of each tile.
 *
 * GIBS serves a picture of the whole Earth, so the tile arrives with land and
 * sea in it as well. Each one is drawn into a canvas, judged pixel by pixel and
 * handed back with everything that is not cloud made transparent -- so what
 * lands on the map is weather over your imagery rather than a second basemap
 * on top of the first.
 */
const CloudTiles = L.TileLayer.extend({
  createTile(coords, done) {
    const canvas = L.DomUtil.create('canvas', 'cloud-tile');
    const size = this.getTileSize();
    canvas.width = size.x;
    canvas.height = size.y;

    const image = new Image();
    // Required before the pixels can be read back; GIBS allows it.
    image.crossOrigin = 'anonymous';
    image.onload = () => {
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(image, 0, 0, size.x, size.y);
      try {
        const pixels = ctx.getImageData(0, 0, size.x, size.y);
        ctx.putImageData(maskToCloud(pixels, this.options.sensitivity), 0, 0);
      } catch {
        // The browser refused to let the pixels be read, so the mask cannot
        // be worked out. Blending is a poorer substitute -- it drops dark
        // ground but keeps bright ground -- and it is better than either
        // hiding the layer or covering the map with a second basemap.
        blendInstead(canvas);
      }
      done(null, canvas);
    };
    image.onerror = (err) => done(err, canvas);
    image.src = this.getTileUrl(coords);
    return canvas;
  },
});

let warnedAboutBlending = false;

function blendInstead(canvas) {
  canvas.classList.add('is-blended');
  if (warnedAboutBlending) return;
  warnedAboutBlending = true;
  toast('This browser will not let the cloud be cut out exactly — '
    + 'blending instead, so bright ground may show through');
}

function paint() {
  layer?.remove();
  layer = null;
  if (!enabled) {
    rebuild();
    return;
  }
  const spec = SOURCES[source];
  layer = new CloudTiles(TILES, {
    layer: spec.layer,
    matrix: MATRIX,
    date: day,
    fmt: 'jpg',
    sensitivity,
    opacity,
    maxNativeZoom: NATIVE_ZOOM,
    maxZoom: 19,
    // Cloud sits above the ground, and now that only the cloud is drawn it can
    // sit above the imagery too without hiding any of it.
    pane: 'clouds',
    bounds: [[-85, -180], [85, 180]],
    attribution: 'NASA EOSDIS GIBS',
  });
  layer.on('tileerror', missing);
  layer.addTo(map);
  rebuild();
}

/**
 * Today's mosaic is not finished the moment the day starts in UTC.
 *
 * Ask for it too early and the tiles come back empty, which would look like a
 * broken layer rather than a day that has not happened yet -- so the first
 * time that happens, step back to yesterday and say why.
 */
// Dragging the catch slider would otherwise rebuild every tile per pixel of
// travel; one repaint once the thumb settles is enough.
const queueRepaint = debounce(() => { if (enabled) paint(); }, 180);

const missing = debounce(() => {
  if (steppedBack || day !== today()) return;
  steppedBack = true;
  day = shift(day, -1);
  paint();
  rebuild();
  toast(`Today's mosaic is not published yet — showing ${day}`);
}, 400);
