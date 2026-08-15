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

let map = null;
let layer = null;
let enabled = false;
let source = 'viirs-noaa20';
let day = today();
let opacity = 0.7;
let steppedBack = false;

export function initClouds(leafletMap) {
  map = leafletMap;
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
    + 'One pass a day, and none of the night side.';
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

function paint() {
  layer?.remove();
  layer = null;
  if (!enabled) {
    rebuild();
    return;
  }
  const spec = SOURCES[source];
  layer = L.tileLayer(TILES, {
    layer: spec.layer,
    matrix: MATRIX,
    date: day,
    fmt: 'jpg',
    opacity,
    maxNativeZoom: NATIVE_ZOOM,
    maxZoom: 19,
    // Under the drawn area and the imagery, over the basemap: it is context,
    // not the subject.
    zIndex: 200,
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
const missing = debounce(() => {
  if (steppedBack || day !== today()) return;
  steppedBack = true;
  day = shift(day, -1);
  paint();
  rebuild();
  toast(`Today's mosaic is not published yet — showing ${day}`);
}, 400);
