// Leaflet map: panning and zooming by default, drawing only when a tool is
// armed, plus the imagery overlay.
//
// The map belongs to the mouse. Nothing is drawn unless a tool has been picked
// deliberately, and the tool disarms itself the moment a shape is finished --
// so a drag is a pan, a wheel is a zoom, and the only way to plot an area is
// to have just asked for one.

import { api } from './api.js';
import { store, emit, on } from './store.js';
import { $, $$, toast, fmt, el } from './ui.js';
import { copyRegion, saveRegion, WATERMARK } from './capture.js';
import { initFires, POPUP } from './fires.js';
import { initClouds } from './clouds.js';
import { initRadar } from './radar.js';
import { initVessels } from './vessels.js';
import { initCams } from './cams.js';

let map;
let aoiLayer = null;
let sketchLayer = null;
// One overlay per satellite, so radar and optical can be looked at over the
// same ground and faded against each other.
const overlays = new Map();
let mode = 'none';
let drawing = null;

const AOI_STYLE = { color: '#4cc2ff', weight: 2, fillColor: '#4cc2ff', fillOpacity: 0.10, dashArray: null };
const SKETCH_STYLE = { color: '#ffd166', weight: 2, dashArray: '5,5', fill: false };
const CAPTURE_STYLE = { color: '#37e0a0', weight: 2, dashArray: '4,4', fill: true,
  fillColor: '#37e0a0', fillOpacity: 0.10 };

const HINTS = {
  none: 'Drag to pan, scroll to zoom. Right-click anywhere for the next satellite pass.',
  lasso: 'Drag to circle your region — Esc to cancel',
  circle: 'Drag out from the centre of your area — Esc to cancel',
  rect: 'Drag a box around your area — Esc to cancel',
  polygon: 'Click each corner, double-click to finish — Esc to cancel',
  capture: `Drag over the part you want — it is copied marked ${WATERMARK} — Esc to cancel`,
};

// Basemaps that need no key.
//
// The dark one used to come from CARTO, which started requiring an API key and
// stamped "API KEY REQUIRED" across every tile -- the map still drew, so
// nothing failed, it just went wrong in public. Everything here is keyless,
// and if one of them goes the same way the next in the list takes over rather
// than leaving a watermarked map on screen.
const BASEMAPS = [
  {
    key: 'dark', label: 'Dark',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    options: { maxNativeZoom: 16, maxZoom: 19, attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors' },
    // Place names, drawn over the imagery rather than under it.
    reference: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}',
  },
  {
    key: 'satellite', label: 'Satellite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    options: { maxNativeZoom: 19, maxZoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics' },
  },
  {
    key: 'ocean', label: 'Ocean',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
    options: { maxNativeZoom: 13, maxZoom: 19, attribution: 'Esri, GEBCO, NOAA, National Geographic' },
  },
  {
    key: 'streets', label: 'Streets',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    options: { maxZoom: 19, attribution: '© OpenStreetMap contributors' },
  },
];

// How many tiles have to fail before a basemap is judged unusable. One is
// noise -- a tile at the edge of the world, a dropped connection. A dozen in a
// row is the service saying no.
const DEAD_TILES = 12;

let basemapLayers = null;
// Falling through several basemaps in a row -- which is what happens with no
// connection at all -- should say one thing, not one thing per hop.
let fallbackReported = false;

/**
 * Put the basemaps on the map, and move on from one that stops working.
 *
 * A tile service that starts refusing usually does it by degrees -- a
 * watermark, a placeholder, an error -- so the fallback watches for outright
 * failures and takes the next one down the list. Whichever ends up on screen,
 * the layer control still offers all of them.
 */
function buildBasemaps() {
  basemapLayers = new Map();
  const named = {};

  for (const spec of BASEMAPS) {
    const layer = L.tileLayer(spec.url, spec.options);
    if (spec.reference) {
      // Base and labels move together, so they are one entry in the control.
      const labels = L.tileLayer(spec.reference, { ...spec.options, pane: 'shadowPane' });
      const group = L.layerGroup([layer, labels]);
      basemapLayers.set(spec.key, group);
      named[spec.label] = group;
    } else {
      basemapLayers.set(spec.key, layer);
      named[spec.label] = layer;
    }

    let failures = 0;
    layer.on('tileerror', () => {
      failures += 1;
      if (failures !== DEAD_TILES) return;
      const current = basemapLayers.get(spec.key);
      if (!map.hasLayer(current)) return;
      const next = BASEMAPS[BASEMAPS.indexOf(spec) + 1];
      if (!next) return;
      map.removeLayer(current);
      basemapLayers.get(next.key).addTo(map);
      if (!fallbackReported) {
        fallbackReported = true;
        toast(`The ${spec.label.toLowerCase()} basemap is not answering — falling back`);
      }
    });
  }

  basemapLayers.get('dark').addTo(map);
  L.control.layers(named, {}, { position: 'topright' }).addTo(map);
}

export function initMap() {
  map = L.map('map', { zoomControl: true, worldCopyJump: true, preferCanvas: true })
    .setView([48.86, 2.35], 11);

  buildBasemaps();

  L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

  bindDrawTools();
  bindPassLookup();
  initFires(map);
  initClouds(map);
  initRadar(map);
  initVessels(map);
  initCams(map);

  // Five panels in one column: opening the last one can leave its own button
  // below the fold. Bring whichever was just pressed back into view.
  $('.side-docks')?.addEventListener('click', (e) => {
    const button = e.target.closest('button');
    if (button?.id?.endsWith('Toggle')) {
      requestAnimationFrame(() => button.scrollIntoView({ block: 'nearest' }));
    }
  });

  on('image', showOverlay);
  return map;
}

export const getMap = () => map;

/** Swap an overlay's pixels in place — used while adjustments are dragged. */
export function updateOverlay(url, satellite = store.image?.meta?.satellite) {
  overlays.get(satellite ?? 'sentinel-2')?.layer.setUrl(url);
}

// ── Drawing ────────────────────────────────────────────────────

function bindDrawTools() {
  $$('[data-draw]').forEach((btn) => btn.addEventListener('click', () => {
    const next = btn.dataset.draw;
    const wants = btn.dataset.capture;
    // Copy and save arm the same tool with different endings, so switching
    // between them re-arms rather than putting the tool away.
    if (wants && next === mode && wants !== captureWants) {
      captureWants = wants;
      setMode(next);
      return;
    }
    if (wants) captureWants = wants;
    // Clicking the armed tool again puts the map back to navigating.
    setMode(next === mode ? 'none' : next);
  }));

  map.on('mousedown', onDown);
  map.on('mousemove', onMove);
  map.on('mouseup', onUp);
  map.on('click', onClick);
  map.on('dblclick', onDblClick);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') setMode('none');
  });

  $('#clearAoi').addEventListener('click', clearAoi);
  $('#zoomAoi').addEventListener('click', () => {
    if (aoiLayer) map.fitBounds(aoiLayer.getBounds(), { padding: [40, 40] });
  });
  setMode('none');
}

/** Arm a drawing tool, or 'none' to hand the map back to panning and zooming. */
function setMode(next) {
  cancelSketch();
  mode = next;
  const drawingNow = mode !== 'none';
  $$('[data-draw]').forEach((b) => b.classList.toggle(
    'is-active',
    b.dataset.draw === mode && (!b.dataset.capture || b.dataset.capture === captureWants)));
  $('#mapHint').textContent = mode === 'capture'
    ? `Drag over the part you want — ${captureWants === 'save' ? 'saved' : 'copied'} `
      + `with the ${WATERMARK} mark — Esc to cancel`
    : HINTS[mode];
  $('#mapHint').classList.toggle('is-armed', drawingNow);
  document.getElementById('map').classList.toggle('is-drawing', drawingNow);
  // While a tool is armed the drag belongs to it; otherwise it pans the map.
  map.dragging[drawingNow ? 'disable' : 'enable']();
  map.doubleClickZoom[mode === 'polygon' ? 'disable' : 'enable']();
}

function onDown(e) {
  if (mode === 'none' || mode === 'polygon' || drawing) return;
  drawing = { mode, start: e.latlng, points: [e.latlng], lastPoint: e.containerPoint };
  sketchLayer?.remove();
  sketchLayer = (mode === 'circle'
    ? L.circle(e.latlng, { radius: 1, ...SKETCH_STYLE })
    : mode === 'capture'
      ? L.polygon([e.latlng], CAPTURE_STYLE)
      : L.polyline([e.latlng], SKETCH_STYLE)).addTo(map);
}

function onMove(e) {
  if (!drawing) return;
  if (drawing.mode === 'circle') {
    sketchLayer.setRadius(Math.max(map.distance(drawing.start, e.latlng), 1));
  } else if (drawing.mode === 'rect' || drawing.mode === 'capture') {
    sketchLayer.setLatLngs(boxRing(drawing.start, e.latlng));
  } else if (drawing.mode === 'lasso') {
    // Decimate on screen so a fast drag does not produce thousands of vertices.
    if (e.containerPoint.distanceTo(drawing.lastPoint) < 6) return;
    drawing.lastPoint = e.containerPoint;
    drawing.points.push(e.latlng);
    sketchLayer.setLatLngs(drawing.points);
  }
}

function onUp(e) {
  if (!drawing) return;
  const sketch = drawing;
  // Finished or abandoned, the tool is done: the map goes back to navigating
  // so the next drag pans instead of drawing over the area just chosen.
  setMode('none');

  if (sketch.mode === 'capture') {
    if (map.distance(sketch.start, e.latlng) < 20) return;
    takeRegion(sketch.start, e.latlng);
  } else if (sketch.mode === 'circle') {
    const radius = map.distance(sketch.start, e.latlng);
    if (radius < 20) return;
    setAoi({ type: 'circle', lon: sketch.start.lng, lat: sketch.start.lat, radius });
  } else if (sketch.mode === 'rect') {
    if (map.distance(sketch.start, e.latlng) < 20) return;
    setAoi(ringToPolygon(boxRing(sketch.start, e.latlng)));
  } else if (sketch.mode === 'lasso') {
    if (sketch.points.length < 4) return;
    setAoi(ringToPolygon(sketch.points));
  }
}

function onClick(e) {
  // Nothing armed and imagery on the map: a click asks what is there.
  if (mode === 'none') {
    if (store.image) showProbe(e.latlng);
    return;
  }
  if (mode !== 'polygon') return;
  if (!drawing) {
    drawing = { mode: 'polygon', points: [] };
    sketchLayer = L.polyline([], SKETCH_STYLE).addTo(map);
  }
  drawing.points.push(e.latlng);
  sketchLayer.setLatLngs(drawing.points);
}

function onDblClick() {
  if (mode !== 'polygon' || !drawing) return;
  const points = drawing.points;
  setMode('none');
  if (points.length >= 3) setAoi(ringToPolygon(points));
}

function cancelSketch() {
  drawing = null;
  sketchLayer?.remove();
  sketchLayer = null;
}

const boxRing = (a, b) => [a, L.latLng(a.lat, b.lng), b, L.latLng(b.lat, a.lng), a];

function ringToPolygon(latlngs) {
  const ring = latlngs.map((p) => [round6(p.lng), round6(p.lat)]);
  const [fx, fy] = ring[0];
  const [lx, ly] = ring.at(-1);
  if (fx !== lx || fy !== ly) ring.push([fx, fy]);
  return { type: 'Polygon', coordinates: [ring] };
}

const round6 = (n) => Math.round(n * 1e6) / 1e6;

// ── AOI ────────────────────────────────────────────────────────

export async function setAoi(spec, { fit = false } = {}) {
  try {
    const info = await api.describeAoi(spec);
    store.aoi = info.geometry;
    store.aoiInfo = info;
    drawAoi(info.geometry);
    if (fit && aoiLayer) map.fitBounds(aoiLayer.getBounds(), { padding: [40, 40] });
    renderAoiSummary(info);
    emit('aoi', info);
  } catch (err) {
    toast(`Area could not be used: ${err.message}`, 'err');
  }
}

function drawAoi(geometry) {
  aoiLayer?.remove();
  aoiLayer = L.geoJSON(geometry, { style: AOI_STYLE }).addTo(map);
}

export function clearAoi() {
  aoiLayer?.remove();
  aoiLayer = null;
  store.aoi = null;
  store.aoiInfo = null;
  $('#aoiSummary').innerHTML = '<em>Pick a shape, then draw on the map.</em>';
  emit('aoi', null);
}

function renderAoiSummary(info) {
  const [w, s, e, n] = info.bounds;
  const widthM = map.distance(L.latLng(info.center[1], w), L.latLng(info.center[1], e));
  const heightM = map.distance(L.latLng(s, info.center[0]), L.latLng(n, info.center[0]));
  // The panel needs the ground extent to work out what a given output size
  // means in metres per pixel, and so what a merge can resolve.
  info.extent_m = [widthM, heightM];
  $('#aoiSummary').innerHTML = `
    <div>Area <b>${fmt.area(info.area_km2)}</b></div>
    <div>Extent <b>${fmt.distance(widthM)} × ${fmt.distance(heightM)}</b></div>
    <div>Centre <b>${fmt.coord(info.center[0], info.center[1])}</b></div>`;
}

// ── Overlays of the rendered imagery ───────────────────────────

/**
 * Put a render on the map, one layer per satellite.
 *
 * Radar and optical are kept separately rather than replacing each other, so
 * the same ground can be looked at both ways at once: Sentinel-2 for what it
 * looks like, Sentinel-1 for what is there under the cloud. Fading between the
 * two is the whole point, so each layer keeps its own opacity.
 */
function showOverlay(image) {
  if (!image?.meta?.grid) return;
  const key = image.meta.satellite ?? 'sentinel-2';
  const existing = overlays.get(key);
  const opacity = existing?.opacity ?? 1;
  existing?.layer.remove();

  const [w, s, e, n] = image.meta.grid.bounds;
  const layer = L.imageOverlay(image.src, [[s, w], [n, e]], {
    opacity,
    interactive: false,
    className: 'render-overlay',
  }).addTo(map);
  layer.bringToFront();

  overlays.set(key, { layer, opacity, visible: true, meta: image.meta });
  store.images.set(key, image);
  renderLayerDock();
  if (compare) applyCompare();
  drawMapLegend(image.meta);
  // Looking at the imagery is the whole point, so go and look at it.
  map.fitBounds([[s, w], [n, e]], { padding: [28, 28] });
}

/** A row per layer on the map: what it is, how strong, and a way to drop it. */
function renderLayerDock() {
  const dock = $('#layerDock');
  dock.innerHTML = '';
  if (!overlays.size) {
    dock.hidden = true;
    return;
  }
  for (const [key, entry] of overlays) {
    const sat = store.config.satellites?.[key] ?? {};
    const slider = el('input', {
      type: 'range', min: 0, max: 100, value: Math.round(entry.opacity * 100),
      oninput: (ev) => {
        entry.opacity = ev.target.valueAsNumber / 100;
        entry.visible = entry.opacity > 0;
        entry.layer.setOpacity(entry.opacity);
      },
    });
    dock.append(el('div', { class: 'layer-row' },
      el('span', { class: 'layer-dot', style: `background:${sat.colour ?? '#4cc2ff'}` }),
      el('span', { class: 'layer-name' }, sat.short ?? key,
        el('small', {}, fmt.date(entry.meta.scene?.date))),
      slider,
      el('button', {
        class: 'layer-x', title: 'Take this layer off the map',
        onclick: () => {
          entry.layer.remove();
          overlays.delete(key);
          store.images.delete(key);
          // Taking a layer off the map takes away the picture the adjustments
          // and the region copier were working on, so hand them another one --
          // set directly rather than announced, or the layer would come back.
          if (store.image?.meta?.satellite === key) {
            store.image = store.images.values().next().value ?? null;
          }
          if (compare && compare.keys.includes(key)) stopCompare();
          renderLayerDock();
          emit('layers', store.image);
        },
      }, '×')));
  }
  if (overlays.size > 1) {
    dock.append(el('button', {
      class: `compare-btn${compare ? ' is-on' : ''}`,
      onclick: toggleCompare,
    }, compare ? '✕ Stop comparing' : '⟺ Compare side by side'));
  }
  dock.hidden = false;
}

function drawMapLegend(meta) {
  const box = $('#mapLegend');
  const legend = meta.legend;
  if (!legend) {
    box.hidden = true;
    return;
  }
  const stops = legend.stops.map((stop) => `${stop.color} ${stop.pos * 100}%`).join(', ');
  box.innerHTML = '';
  box.append(
    el('div', {}, legend.label),
    el('div', { class: 'bar', style: `background: linear-gradient(90deg, ${stops})` }),
    el('div', { class: 'ends' },
      el('span', {}, legend.vmin.toFixed(2)),
      el('span', {}, legend.vmax.toFixed(2))),
  );
  box.hidden = false;
}

// ── Copying a highlighted region ───────────────────────────────

async function takeRegion(start, end) {
  const a = map.options.crs.project(start);
  const b = map.options.crs.project(end);
  const rect = { x0: a.x, y0: a.y, x1: b.x, y1: b.y };
  if (!store.image) {
    toast('Show some imagery first — there is nothing to copy yet', 'err');
    return;
  }
  await (captureWants === 'save' ? saveRegion(rect) : copyRegion(rect));
}

let captureWants = 'copy';

/** Arm the highlight-and-take tool: 'copy' to the clipboard, or 'save' a file. */
export function armCapture(what = 'copy') {
  captureWants = what;
  setMode(mode === 'capture' ? 'none' : 'capture');
}

// ── What is actually there ─────────────────────────────────────

/**
 * Read the measurements behind a point of the picture.
 *
 * The imagery on screen has been stretched, curved and coloured to be looked
 * at; underneath it are numbers with units. A click goes back to those, so
 * ground that looks green can be asked how green it is and in what -- which is
 * the difference between a picture of a place and a measurement of it.
 */
async function showProbe(latlng) {
  const request = store.image?.request;
  if (!request) return;

  const box = el('div', { class: 'probe' },
    el('div', { class: 'probe-wait' }, 'Reading the measurements…'));
  const popup = L.popup({ className: 'probe-popup', maxWidth: 290, ...POPUP })
    .setLatLng(latlng).setContent(box).openOn(map);

  try {
    const data = await api.probe({
      lon: latlng.lng, lat: latlng.lat,
      scene: request.scene, scenes: request.scenes,
    });
    box.innerHTML = '';
    box.append(
      el('div', { class: 'probe-head' },
        `${data.source.short} · ${fmt.date(data.date)}`,
        data.scenes > 1 ? el('span', { class: 'dim' }, ` · ${data.scenes} dates`) : null),
      el('div', { class: 'probe-coord' }, fmt.coord(...data.point)),
      ...data.bands.map((band) => el('div', { class: 'probe-row' },
        el('span', {}, band.label),
        el('b', {}, band.value == null ? '—'
          : data.unit === 'dB' ? `${band.value.toFixed(1)} dB` : band.value.toFixed(3)))),
      data.indices.length
        ? el('div', { class: 'probe-indices' },
          ...data.indices.map((index) => el('div', { class: 'probe-row' },
            el('span', {}, index.label.split(' - ')[0]),
            el('b', {}, index.value == null ? '—' : index.value.toFixed(3)))))
        : null,
      el('div', { class: 'probe-foot' },
        `${data.unit === 'dB' ? 'Backscatter' : 'Surface reflectance'}, `
        + `averaged over one ${data.ground_res_m} m cell`),
    );
    popup.update();
  } catch (err) {
    box.innerHTML = '';
    box.append(el('div', { class: 'probe-wait' }, `Could not read that: ${err.message}`));
    popup.update();
  }
}

// ── Comparing two layers ───────────────────────────────────────

let compare = null;

/**
 * A divider you drag across the map, with one layer either side of it.
 *
 * Two renders of the same ground fade over each other badly -- at 50% you are
 * looking at neither. A hard edge shows both at full strength and lets the eye
 * carry detail across it, which is what makes a difference obvious: the same
 * field before and after, or the radar answer beside the optical one.
 */
function toggleCompare() {
  if (compare) {
    stopCompare();
    return;
  }
  const keys = [...overlays.keys()];
  if (keys.length < 2) {
    toast('Show both satellites, or two dates, and they can be compared');
    return;
  }
  compare = { at: 0.5, keys: keys.slice(-2) };
  const handle = el('div', { class: 'compare-handle', id: 'compareHandle' },
    el('div', { class: 'compare-line' }),
    el('div', { class: 'compare-grip' }, '⟺'));
  document.querySelector('.stage').append(handle);
  handle.addEventListener('pointerdown', startDrag);
  map.on('move zoom viewreset', applyCompare);
  applyCompare();
  renderLayerDock();
}

function stopCompare() {
  map.off('move zoom viewreset', applyCompare);
  $('#compareHandle')?.remove();
  for (const entry of overlays.values()) {
    const img = entry.layer.getElement();
    if (img) img.style.clipPath = '';
  }
  compare = null;
  renderLayerDock();
}

function startDrag(event) {
  event.preventDefault();
  const stage = document.querySelector('.stage');
  const move = (e) => {
    const rect = stage.getBoundingClientRect();
    compare.at = Math.min(0.98, Math.max(0.02, (e.clientX - rect.left) / rect.width));
    applyCompare();
  };
  const up = () => {
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
  };
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

/**
 * Put the split where the handle is.
 *
 * The clip has to be worked out per layer rather than set once: it is measured
 * in the image's own coordinates, and the image is a fixed piece of ground
 * whose position and width on screen change with every pan and zoom.
 */
function applyCompare() {
  if (!compare) return;
  const stage = document.querySelector('.stage').getBoundingClientRect();
  const splitX = stage.left + compare.at * stage.width;
  const handle = $('#compareHandle');
  if (handle) handle.style.left = `${compare.at * 100}%`;

  const [left, right] = compare.keys;
  for (const [key, entry] of overlays) {
    const img = entry.layer.getElement();
    if (!img) continue;
    if (key !== left && key !== right) {
      img.style.clipPath = '';
      continue;
    }
    const rect = img.getBoundingClientRect();
    const cut = Math.min(1, Math.max(0, (splitX - rect.left) / (rect.width || 1)));
    img.style.clipPath = key === left
      ? `inset(0 ${(1 - cut) * 100}% 0 0)`
      : `inset(0 0 0 ${cut * 100}%)`;
  }
}

// ── When the satellites next come over ─────────────────────────

function bindPassLookup() {
  map.on('contextmenu', (e) => {
    // A right-click while a tool is armed would be an odd time to ask.
    if (mode !== 'none') return;
    showPasses(e.latlng);
  });
}

async function showPasses(latlng) {
  const box = el('div', { class: 'passes' },
    el('div', { class: 'passes-head' }, fmt.coord(latlng.lng, latlng.lat)),
    el('div', { class: 'passes-wait' }, 'Asking the catalogue…'));
  const popup = L.popup({ className: 'pass-popup', maxWidth: 320, ...POPUP })
    .setLatLng(latlng).setContent(box).openOn(map);

  try {
    const data = await api.passes(latlng.lng.toFixed(5), latlng.lat.toFixed(5));
    box.querySelector('.passes-wait').remove();
    for (const sat of data.satellites) box.append(passRow(sat));
    box.append(el('div', { class: 'passes-foot' },
      'Predicted from the passes already flown over this point, '
      + `stepping each ground track on by the interval it actually repeats on.`));
    popup.update();
  } catch (err) {
    box.querySelector('.passes-wait').textContent = `Could not look that up: ${err.message}`;
    popup.update();
  }
}

function passRow(sat) {
  const spec = store.config.satellites?.[sat.satellite] ?? {};
  const rows = [
    el('div', { class: 'pass-name' },
      el('span', { class: 'layer-dot', style: `background:${spec.colour ?? '#4cc2ff'}` }),
      sat.short),
  ];
  if (sat.next) {
    rows.push(el('div', { class: 'pass-next' },
      el('b', {}, `in ${fmt.duration(sat.next.hours_away)}`),
      ` · ${fmt.when(sat.next.datetime)}`));
    rows.push(el('div', { class: 'pass-last' },
      `Last pass ${fmt.duration(sat.last.hours_ago)} ago`
      + (sat.next.orbit != null ? ` · track ${sat.next.orbit}` : '')
      + (sat.measured ? `, repeating every ${sat.next.period_days} days`
        : ', on the nominal cycle')));
  } else {
    rows.push(el('div', { class: 'pass-last' }, sat.note ?? 'No passes on record here.'));
  }
  return el('div', { class: 'pass' }, ...rows);
}

// ── Place search ───────────────────────────────────────────────

export function initPlaceSearch() {
  const input = $('#placeSearch');
  const results = $('#placeResults');

  const run = async () => {
    const q = input.value.trim();
    if (q.length < 2) return;
    try {
      const { results: hits } = await api.geocode(q);
      results.innerHTML = '';
      if (!hits.length) {
        results.append(el('button', {}, 'Nothing found'));
      }
      for (const hit of hits) {
        results.append(el('button', {
          onclick: () => {
            results.hidden = true;
            input.value = hit.name.split(',')[0];
            if (hit.bbox) {
              const [s, n, w, e] = hit.bbox;
              map.fitBounds([[s, w], [n, e]], { padding: [30, 30] });
            } else {
              map.setView([hit.lat, hit.lon], 13);
            }
          },
        }, hit.name));
      }
      results.hidden = false;
    } catch (err) {
      toast(`Place search unavailable: ${err.message}`, 'err');
    }
  };

  $('#placeSearchBtn').addEventListener('click', run);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
  document.addEventListener('click', (e) => {
    if (!results.contains(e.target) && e.target !== input) results.hidden = true;
  });
}
