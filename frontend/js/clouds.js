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
  // ── Geostationary: actually live ──────────────────────────────
  //
  // These are the ones that make this layer worth calling live. A satellite
  // parked over one longitude photographs its whole disc every ten minutes,
  // so the cloud you see is the cloud that is there now, and it is there at
  // night too. The polar mosaics below are one strip per orbit stitched into
  // a day -- excellent for a picture of the whole planet, useless for asking
  // what the sky is doing this afternoon.
  //
  // Each only sees its own third of the world, which is why there are three
  // and why the panel says which one covers where you are looking.
  'goes-east': {
    layer: 'GOES-East_ABI_GeoColor',
    label: 'GOES-East · live', kind: 'live',
    covers: 'the Americas and the Atlantic', centre: -75, reach: 70,
    stepMinutes: 10, matrix: 'GoogleMapsCompatible_Level7', native: 7,
  },
  'goes-west': {
    layer: 'GOES-West_ABI_GeoColor',
    label: 'GOES-West · live', kind: 'live',
    covers: 'the Pacific and western North America', centre: -137, reach: 70,
    stepMinutes: 10, matrix: 'GoogleMapsCompatible_Level7', native: 7,
  },
  himawari: {
    layer: 'Himawari_AHI_Geocolor',
    label: 'Himawari · live', kind: 'live',
    covers: 'Asia, Australia and the western Pacific', centre: 140, reach: 70,
    stepMinutes: 10, matrix: 'GoogleMapsCompatible_Level7', native: 7,
  },

  // ── Polar: one pass a day, but everywhere ─────────────────────
  //
  // Three VIIRS instruments and two MODIS, which is more redundancy than it
  // looks. They cross at two different local times, so Terra is a morning
  // picture and the rest an early-afternoon one, and when one satellite has a
  // bad day the others have not.
  'viirs-noaa21': {
    layer: 'VIIRS_NOAA21_CorrectedReflectance_TrueColor',
    label: 'VIIRS · NOAA-21', when: 'about 13:30 local', metres: 250,
  },
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

  // ── The same satellites, looking at something else ────────────
  //
  // True colour is what the eye would see, which means it stops working at
  // sunset and cannot tell cloud from snow. These two are the same instruments
  // read differently, and each answers a question true colour cannot.
  'viirs-night': {
    // Moonlight and firelight. City lights, gas flares, fishing fleets, and
    // the burning edge of a wildfire -- on the half of the planet where every
    // other layer here has nothing at all.
    layer: 'VIIRS_SNPP_DayNightBand_At_Sensor_Radiance',
    label: 'VIIRS · night lights', when: 'about 01:30 local', metres: 500,
    fmt: 'png',
    // It is a picture of light in the dark, so the cloud mask -- which keeps
    // what is bright and colourless -- would keep the cities and throw away
    // the cloud. Shown as it comes.
    raw: true,
  },
  'modis-bands721': {
    // Short-wave infrared in the red channel: burn scars go red, active fire
    // glows, cloud stays white and snow turns blue -- the one thing true
    // colour cannot do, since snow and cloud are both just white to it.
    layer: 'MODIS_Terra_CorrectedReflectance_Bands721',
    label: 'MODIS · fire and snow', when: 'about 10:30 local', metres: 250,
    raw: true,
  },
};

const isLive = (key) => SOURCES[key]?.kind === 'live';

// GIBS names its tile grids by how deep they go, and 250 m imagery stops at
// level 9. Past that the tiles are stretched rather than withheld, so the
// layer stays on screen when you zoom into an area instead of vanishing.
const MATRIX = 'GoogleMapsCompatible_Level9';
const NATIVE_ZOOM = 9;

// How long after an observation the tiles actually appear. Asking for the
// slot that has just begun gets nothing back, which looks like a dead layer
// rather than one that is a few minutes behind the world.
const PUBLISH_LAG_MINUTES = 20;

// How far back to walk, one slot or one day at a time, before giving up and
// saying so. The old version stepped back exactly once and then gave up
// silently, so a satellite having a bad morning looked like a broken app.
const MOST_STEPS_BACK = 6;

// How often to fetch a newer frame from a geostationary source.
const LIVE_REFRESH_MS = 5 * 60 * 1000;

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

// Every answer cloudiness() can give, worked out once.
//
// The judgement above reads three channels but only ever uses two numbers from
// them -- the brightest and the dimmest -- so there are 256 x 256 possible
// answers, not sixteen million. A tile is sixty-five thousand pixels and a
// screenful is a couple of dozen tiles, so the same few hundred answers were
// being recomputed a million and a half times per pan, on the thread that
// draws. Computing the table instead costs one tile's worth of work, once per
// sensitivity, and every pixel after that is an array lookup.
//
// This is arithmetic, not approximation: the table holds exactly what the
// loop used to produce, and a test compares the two pixel by pixel.
let table = null;
let tableFor = -1;

function lookup(sensitivity) {
  if (table && tableFor === sensitivity) return table;
  table = new Uint8Array(256 * 256);
  for (let high = 0; high < 256; high += 1) {
    for (let low = 0; low <= high; low += 1) {
      table[(high << 8) | low] =
        Math.round(255 * cloudiness(high, low, low, sensitivity));
    }
  }
  tableFor = sensitivity;
  return table;
}

/** Rewrite a tile's alpha so only its cloud survives. */
export function maskToCloud(pixels, sensitivity) {
  const data = pixels.data;
  const answers = lookup(sensitivity);
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i];
    const g = data[i + 1];
    const b = data[i + 2];
    let high = r;
    let low = r;
    if (g > high) high = g; else if (g < low) low = g;
    if (b > high) high = b; else if (b < low) low = b;
    data[i + 3] = answers[(high << 8) | low];
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
// How many slots (live) or days (polar) behind the present the layer is
// showing, because what was asked for first came back empty.
let stepsBack = 0;
let liveTimer = null;
// Why the layer is empty, when it is. Said rather than left to be guessed at.
let problem = '';

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

/**
 * The timestamp to ask a geostationary source for.
 *
 * Rounded down to the satellite's own slot and set back by the publishing
 * lag, then by however many slots have already come back empty. GIBS wants an
 * exact slot boundary: a time in between returns nothing at all, which is
 * indistinguishable from the layer being broken.
 */
function liveStamp(spec, back = 0) {
  const step = spec.stepMinutes * 60000;
  const when = new Date(
    Math.floor((Date.now() - PUBLISH_LAG_MINUTES * 60000 - back * step) / step) * step);
  return when.toISOString().replace(/\.\d+Z$/, 'Z');
}

/** How far the map's centre is from the middle of a satellite's disc. */
function reach(spec) {
  if (spec.centre == null) return { away: 0, inside: true };
  const away = Math.abs(((map.getCenter().lng - spec.centre + 540) % 360) - 180);
  return { away, inside: away <= spec.reach };
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
      el('div', { class: 'cloud-days', id: 'cloudDays' },
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
        onchange: (e) => {
          source = e.target.value;
          stepsBack = 0;
          problem = '';
          paint();
          rebuild();
        },
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
      // "Catch" slides how much haze counts as cloud, so it means nothing for
      // a source that is not being masked at all. Hidden rather than left
      // sitting there doing nothing when you drag it.
      el('label', { class: 'cloud-fade', id: 'cloudCatch',
                    hidden: !!SOURCES[source].raw }, 'Catch',
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
  const live = isLive(source);
  const when = $('#cloudDate');
  const lines = [];

  if (live) {
    // The age of the frame actually on screen, which is the only number worth
    // showing for something that calls itself live.
    const minutes = PUBLISH_LAG_MINUTES + stepsBack * spec.stepMinutes;
    when.textContent = `${minutes} min ago`;
    const view = reach(spec);
    lines.push(`Every ${spec.stepMinutes} minutes, day and night, over `
      + `${spec.covers}.`);
    if (!view.inside) {
      lines.push('<b>Over its horizon here</b> — this satellite cannot see '
        + 'where you are looking. Try another, or a daily mosaic.');
    }
  } else {
    when.textContent = day === today() ? `${day} · today` : day;
    lines.push(`Crosses ${spec.when}, published within about three hours.`);
    lines.push(spec.raw && source === 'viirs-night'
      ? 'One pass a night — this is the half of the planet the others cannot see.'
      : 'One pass a day, and none of the night side.');
  }
  // Say which of the two things is on screen. A layer drawn whole and a layer
  // with everything but the cloud cut out of it look nothing alike, and the
  // panel is the only place that can tell you which you are looking at.
  if (!spec.raw) {
    lines.push('<b>Cloud only</b> — the ground is cut out. Snow reads as cloud.');
  } else if (source === 'viirs-night') {
    lines.push('<b>Drawn whole</b> — light at night: cities, gas flares, '
      + 'fishing fleets, and the burning edge of a wildfire. Moonlit cloud '
      + 'shows as haze.');
  } else {
    lines.push('<b>Drawn whole</b> — short-wave infrared: burn scars red, '
      + 'active fire glowing, cloud white, snow and ice blue. The one view '
      + 'here that tells snow from cloud.');
  }
  if (problem) lines.push(`<b>${problem}</b>`);
  $('#cloudNote').innerHTML = lines.join('<br>');

  const catcher = $('#cloudCatch');
  if (catcher) catcher.hidden = !!spec.raw;

  // The day controls mean nothing for a source that publishes every ten
  // minutes, so they go rather than sitting there doing nothing.
  const days = $('#cloudDays');
  if (days) days.hidden = live;

  const shown = store.image?.meta?.scene?.date;
  const match = $('#cloudMatch');
  match.hidden = live || !shown || shown === day;
  if (shown) match.textContent = `Match ${fmt.date(shown)}`;
  const now = $('#cloudToday');
  if (now) now.hidden = live;
}

function toggle() {
  enabled = !enabled;
  $('#cloudToggle').classList.toggle('is-on', enabled);
  $('#cloudBody').hidden = !enabled;
  if (!enabled) {
    // A geostationary source keeps fetching a new frame every five minutes.
    // Left running with the layer switched off, that is somebody else's
    // bandwidth spent on tiles nobody is looking at.
    clearInterval(liveTimer);
    liveTimer = null;
  }
  paint();
}

function setDay(next) {
  // Nothing has been photographed tomorrow yet.
  day = next > today() ? today() : next;
  stepsBack = 0;
  problem = '';
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
  const live = isLive(source);
  // Most of these are pictures of the whole Earth with the cloud cut out of
  // them. Two are not: night lights and the fire-and-snow band are the point
  // of themselves rather than a way of seeing the sky, and the mask -- which
  // keeps what is bright and colourless -- would keep the cities and throw
  // away everything else. Those are drawn as they come, by a plain tile layer
  // that never touches a canvas.
  const Tiles = spec.raw ? L.TileLayer : CloudTiles;
  layer = new Tiles(TILES, {
    layer: spec.layer,
    matrix: live ? spec.matrix : MATRIX,
    // A geostationary source is asked for an instant; a polar mosaic for a
    // day. Same URL shape, and GIBS accepts either in the same slot.
    date: live ? liveStamp(spec, stepsBack) : day,
    // The day-night band is published as PNG; the reflectance mosaics as JPEG.
    fmt: spec.fmt ?? 'jpg',
    sensitivity,
    opacity,
    maxNativeZoom: live ? spec.native : NATIVE_ZOOM,
    maxZoom: 19,
    // Cloud sits above the ground, and now that only the cloud is drawn it can
    // sit above the imagery too without hiding any of it.
    pane: 'clouds',
    bounds: [[-85, -180], [85, 180]],
    attribution: 'NASA EOSDIS GIBS',
  });
  layer.on('tileerror', missing);
  // A frame that arrived means the walk back is over, whatever it cost.
  layer.on('tileload', () => { walking = false; });
  layer.addTo(map);
  rebuild();

  // Geostationary sources move on their own. Nothing else here does, so the
  // timer only exists while one is chosen.
  clearInterval(liveTimer);
  liveTimer = live ? setInterval(() => {
    if (!enabled || document.hidden) return;
    stepsBack = 0;
    paint();
  }, LIVE_REFRESH_MS) : null;
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

let walking = false;

/**
 * Nothing came back, so step further back and try again.
 *
 * The old version stepped back exactly one day and then gave up, silently, so
 * a satellite having a bad morning looked identical to a broken app -- and it
 * could only ever be a day, which is the wrong unit entirely for a source
 * that publishes every ten minutes.
 *
 * Now it walks: one slot at a time for a geostationary source, one day at a
 * time for a polar mosaic, up to a handful, and then says plainly that the
 * source is not answering rather than leaving an empty layer to be puzzled
 * over.
 */
const missing = debounce(() => {
  if (!enabled || walking) return;
  const live = isLive(source);

  if (!live && day !== today()) {
    // A day deliberately chosen rather than today's, and it has nothing. That
    // is a fact about that day, not something to paper over by showing a
    // different one -- somebody asked for this date.
    problem = `Nothing published for ${day} from ${SOURCES[source].label}.`;
    rebuild();
    return;
  }

  if (stepsBack >= MOST_STEPS_BACK) {
    problem = live
      ? `${SOURCES[source].label} has published nothing in the last `
        + `${Math.round((MOST_STEPS_BACK * SOURCES[source].stepMinutes
          + PUBLISH_LAG_MINUTES) / 60 * 10) / 10} h.`
      : `Nothing from ${SOURCES[source].label} in the last ${MOST_STEPS_BACK} days.`;
    rebuild();
    return;
  }

  walking = true;
  stepsBack += 1;
  if (!live) day = shift(today(), -stepsBack);
  paint();
  rebuild();
}, 400);
