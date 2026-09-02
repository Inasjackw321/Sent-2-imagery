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
import { initSeismic } from './seismic.js';
import { initDayNight, sunBlock } from './daynight.js';

let map;
let aoiLayer = null;
let sketchLayer = null;
// One overlay per satellite, so radar and optical can be looked at over the
// same ground and faded against each other.
const overlays = new Map();
let mode = 'none';
let drawing = null;

// interactive: false on all three. A Leaflet path catches the pointer by
// default, and these are annotations rather than controls -- there is nothing
// to click on them. Left interactive, the area you have just drawn swallows
// every drag and click inside it: the map stops panning over its own selection
// and clicking the imagery stops answering, which reads as the mouse being
// stuck in drag mode over a large part of the screen.
const AOI_STYLE = { color: '#4cc2ff', weight: 2, fillColor: '#4cc2ff', fillOpacity: 0.10,
  dashArray: null, interactive: false };
const SKETCH_STYLE = { color: '#ffd166', weight: 2, dashArray: '5,5', fill: false,
  interactive: false };
const CAPTURE_STYLE = { color: '#37e0a0', weight: 2, dashArray: '4,4', fill: true,
  fillColor: '#37e0a0', fillOpacity: 0.10, interactive: false };

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
// The default is plain OpenStreetMap: the map most people have already read a
// thousand times, and the one whose place names are right. Esri's label layer
// was putting wrong titles on cities, which is worse than an ugly backdrop --
// a name you cannot trust makes the whole map suspect. OSM's names are edited
// by the people who live there and are baked into the tile rather than
// stacked on top of it, so there is no second layer to disagree with.
const BASEMAPS = [
  {
    key: 'streets', label: 'OpenStreetMap',
    url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    options: { maxZoom: 19, attribution: '© OpenStreetMap contributors' },
  },
  {
    key: 'satellite', label: 'Satellite',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    options: {
      maxNativeZoom: 19, maxZoom: 19,
      attribution: 'Esri, Maxar, Earthstar Geographics',
      // Taken down a little so the app's own overlays, markers and pins stay
      // the brightest thing on screen instead of competing with the backdrop.
      className: 'tiles-imagery',
    },
    // No label layer. Esri's is the one that was captioning cities with names
    // decades out of date -- Kiev, Kishinev -- and there is no keyless
    // alternative to draw over imagery. Imagery with no names beats imagery
    // with wrong ones, and every other basemap here carries correct ones.
  },
  {
    // Replaces a "Terrain" layer that did not work. It was Esri's hillshade,
    // which is relief drawn dark-on-white for a white page; inverted to suit a
    // dark interface, everything flat -- which is most of the world -- came out
    // black, so the map was a black rectangle with borders on it. OpenTopoMap
    // is a real topographic map with contours and its own correct labels.
    key: 'topo', label: 'Topographic',
    url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    options: {
      subdomains: 'abc', maxNativeZoom: 17, maxZoom: 19,
      attribution: '© OpenStreetMap contributors, SRTM · © OpenTopoMap (CC-BY-SA)',
    },
  },
  {
    key: 'humanitarian', label: 'Humanitarian',
    url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',
    options: {
      subdomains: 'abc', maxNativeZoom: 19, maxZoom: 19,
      attribution: '© OpenStreetMap contributors · Humanitarian OSM Team',
    },
  },
  {
    key: 'dark', label: 'Dark',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',
    options: {
      maxNativeZoom: 16, maxZoom: 19,
      attribution: 'Esri, HERE, Garmin, © OpenStreetMap contributors',
      // Esri's dark canvas is really a mid grey. Deepened here so it reads as
      // a background rather than as the subject.
      className: 'tiles-dark',
    },
  },
  {
    key: 'ocean', label: 'Ocean',
    url: 'https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
    options: { maxNativeZoom: 13, maxZoom: 19, attribution: 'Esri, GEBCO, NOAA, National Geographic' },
  },
];

// Which one is on screen at the start.
const DEFAULT_BASEMAP = 'streets';

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
    // Every basemap here is a single layer: the place names are drawn into
    // the tiles by whoever made them, rather than stacked on afterwards from
    // a separate gazetteer that can disagree with the map underneath.
    const layer = L.tileLayer(spec.url, spec.options);
    basemapLayers.set(spec.key, layer);
    named[spec.label] = layer;

    let failures = 0;
    layer.on('tileerror', () => {
      failures += 1;
      if (failures !== DEAD_TILES) return;
      const current = basemapLayers.get(spec.key);
      if (!map.hasLayer(current)) return;
      // Skip to a different provider. Most of this list is Esri, so falling to
      // the next entry when Esri itself is unreachable just fails again three
      // more times before landing anywhere useful.
      const host = new URL(spec.url).host;
      const next = BASEMAPS.slice(BASEMAPS.indexOf(spec) + 1)
        .find((other) => new URL(other.url).host !== host)
        ?? BASEMAPS[BASEMAPS.indexOf(spec) + 1];
      if (!next) return;
      map.removeLayer(current);
      basemapLayers.get(next.key).addTo(map);
      if (!fallbackReported) {
        fallbackReported = true;
        toast(`The ${spec.label.toLowerCase()} basemap is not answering — falling back`);
      }
    });
  }

  basemapLayers.get(DEFAULT_BASEMAP).addTo(map);
  L.control.layers(named, {}, { position: 'topright' }).addTo(map);
}

export function initMap() {
  map = L.map('map', { zoomControl: true, worldCopyJump: true, preferCanvas: true })
    .setView([48.86, 2.35], 11);

  buildBasemaps();

  L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

  bindDrawTools();
  bindPassLookup();
  initDayNight(map);
  initFires(map);
  initClouds(map);
  initRadar(map);
  initVessels(map);
  initSeismic(map);
  initCams(map);

  // Six panels in one column: opening the last one can leave its own button
  // below the fold. Bring whichever was just pressed back into view.
  $('.side-docks')?.addEventListener('click', (e) => {
    const button = e.target.closest('button');
    if (button?.id?.endsWith('Toggle')) {
      requestAnimationFrame(() => button.scrollIntoView({ block: 'nearest' }));
    }
  });

  map.on('mousemove', (e) => {
    lastPointer = e.latlng;
    if (showCoords) paintCoords();
  });
  map.on('mouseout', () => { lastPointer = null; if (showCoords) paintCoords(); });

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

  // Releasing the button anywhere but over the map -- on the sidebar, on a
  // panel, or outside the window entirely -- never reaches Leaflet, so the
  // sketch was never finished and the tool stayed armed. From then on the map
  // would not pan and would not answer a click, because every gesture was
  // still feeding a drawing nobody could see. Ending the gesture wherever the
  // button actually comes up is the fix.
  document.addEventListener('pointerup', looseEnd);
  document.addEventListener('pointercancel', looseEnd);
  // Alt-tabbing away mid-drag comes back with the button already released.
  window.addEventListener('blur', looseEnd);

  // Reaching for anything outside the map puts the tool away.
  //
  // An armed tool takes the drag away from the map, so a tool left armed is a
  // map that will not pan and will not answer a click -- and there was nothing
  // to end it but the Escape key. Arming a box and then opening a panel, or
  // clicking one corner of a polygon and wandering off, both left it that way.
  // Going somewhere else is as clear a statement that you have finished
  // drawing as putting the tool back would be.
  document.addEventListener('pointerdown', (e) => {
    if (mode === 'none' || drawing) return;
    if (e.target.closest('#map')) return;
    // Except the tool buttons themselves, which have their own meaning: they
    // swap tools, and disarming underneath them would fight that.
    if (e.target.closest('[data-draw]')) return;
    setMode('none');
  }, true);

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
  drawing = null;
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
  drawing = {
    mode, start: e.latlng, points: [e.latlng],
    lastPoint: e.containerPoint,
    // Where the pointer was last seen, so a gesture that ends off the map can
    // still be finished at the last place it was actually over.
    at: e.latlng,
  };
  sketchLayer?.remove();
  sketchLayer = (mode === 'circle'
    ? L.circle(e.latlng, { radius: 1, ...SKETCH_STYLE })
    : mode === 'capture'
      ? L.polygon([e.latlng], CAPTURE_STYLE)
      : L.polyline([e.latlng], SKETCH_STYLE)).addTo(map);
}

function onMove(e) {
  if (!drawing) return;
  drawing.at = e.latlng;
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

/**
 * The button came up somewhere the map never heard about.
 *
 * A polygon is not affected: it is built by clicking, not dragging, and has
 * no gesture to end here.
 */
function looseEnd() {
  if (!drawing || drawing.mode === 'polygon') return;
  finish(drawing.at ?? drawing.start);
}

function onUp(e) {
  finish(e.latlng);
}

function finish(at) {
  if (!drawing) return;
  const sketch = drawing;
  // Finished or abandoned, the tool is done: the map goes back to navigating
  // so the next drag pans instead of drawing over the area just chosen.
  setMode('none');

  if (sketch.mode === 'capture') {
    if (map.distance(sketch.start, at) < 20) return;
    takeRegion(sketch.start, at);
  } else if (sketch.mode === 'circle') {
    const radius = map.distance(sketch.start, at);
    if (radius < 20) return;
    setAoi({ type: 'circle', lon: sketch.start.lng, lat: sketch.start.lat, radius });
  } else if (sketch.mode === 'rect') {
    if (map.distance(sketch.start, at) < 20) return;
    setAoi(ringToPolygon(boxRing(sketch.start, at)));
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
  aoiLayer = L.geoJSON(geometry, { style: AOI_STYLE, interactive: false }).addTo(map);
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
/**
 * Keep the picture currently on the map when the next one replaces it.
 *
 * Overlays are keyed by satellite, so rendering a second date of Sentinel-2
 * used to drop the first -- which meant the comparison slider could only ever
 * put one satellite against another, and never the same ground a week apart.
 * That is the comparison anyone actually wants. Pinning copies the layer under
 * a key of its own so the next render has nothing to overwrite.
 */
let pins = 0;

function pinOverlay(key) {
  const entry = overlays.get(key);
  const image = store.images.get(key);
  if (!entry || !image) return;

  pins += 1;
  const pinKey = `pin:${pins}`;
  const [w, s, e, n] = entry.meta.grid.bounds;
  const layer = L.imageOverlay(image.src, [[s, w], [n, e]], {
    opacity: entry.opacity, interactive: false, className: 'render-overlay',
  }).addTo(map);
  overlays.set(pinKey, {
    layer, opacity: entry.opacity, visible: true, meta: entry.meta, pinned: key,
  });
  store.images.set(pinKey, image);
  renderLayerDock();
  toast('Pinned. Render another date and the two can be compared.');
}

// ── Where you are looking ──────────────────────────────────────
//
// A little readout beside the imagery: the corners of the picture, its centre,
// and wherever the pointer happens to be. Off by default, because most of the
// time the map is the answer and a coordinate is noise -- but the moment you
// want to write down where something is, nothing else in the interface will
// tell you.

let showCoords = false;
let lastPointer = null;

function toggleCoords() {
  showCoords = !showCoords;
  paintCoords();
  renderLayerDock();
}

function paintCoords() {
  const box = $('#coordPanel');
  if (!box) return;
  const entry = [...overlays.values()].at(-1);
  if (!showCoords || !entry) {
    box.hidden = true;
    return;
  }

  const [w, s, e, n] = entry.meta.grid.bounds;
  const rows = [
    ['Centre', fmt.coord((w + e) / 2, (s + n) / 2)],
    ['North-west', fmt.coord(w, n)],
    ['South-east', fmt.coord(e, s)],
  ];
  box.replaceChildren(
    el('div', { class: 'coord-head' },
      el('b', {}, 'Coordinates'),
      el('button', {
        class: 'coord-x', title: 'Hide', onclick: toggleCoords,
      }, '×')),
    el('dl', { class: 'coord-list' },
      ...rows.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)])),
    el('div', { class: 'coord-pointer' },
      lastPointer
        ? el('span', {}, fmt.coord(lastPointer.lng, lastPointer.lat))
        : el('span', { class: 'dim' }, 'Move over the map for a reading')),
    // The extent is the honest size of what is on screen, and it is the thing
    // people most often want alongside a position.
    el('div', { class: 'coord-span' },
      `${Math.abs(e - w).toFixed(4)}° × ${Math.abs(n - s).toFixed(4)}°`
      + (entry.meta.grid.width ? ` · ${entry.meta.grid.width}×${entry.meta.grid.height} px` : '')),
  );
  box.hidden = false;
}

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
  paintCoords();
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
    const sat = store.config.satellites?.[entry.pinned ?? key] ?? {};
    const slider = el('input', {
      type: 'range', min: 0, max: 100, value: Math.round(entry.opacity * 100),
      oninput: (ev) => {
        entry.opacity = ev.target.valueAsNumber / 100;
        entry.visible = entry.opacity > 0;
        entry.layer.setOpacity(entry.opacity);
      },
    });
    dock.append(el('div', { class: `layer-row${entry.pinned ? ' is-pinned' : ''}` },
      el('span', { class: 'layer-dot', style: `background:${sat.colour ?? '#4cc2ff'}` }),
      el('span', { class: 'layer-name' }, sat.short ?? key,
        el('small', {}, fmt.date(entry.meta.scene?.date)),
        // How much of the shape this picture actually covers. Shown only when
        // it is not most of it: a toast scrolls away, and the holes do not.
        entry.meta.valid_pct != null && entry.meta.valid_pct < 92
          ? el('em', { class: 'layer-thin', title: 'Share of your area with imagery' },
            `${Math.round(entry.meta.valid_pct)}%`)
          : null,
        entry.pinned ? el('em', { class: 'layer-pinned' }, 'kept') : null),
      slider,
      // Only live layers can be pinned: a pin is already a copy, and copying
      // a copy would fill the dock with the same picture.
      entry.pinned ? null : el('button', {
        class: 'layer-pin', title: 'Keep this picture when the next one is rendered',
        onclick: () => pinOverlay(key),
      }, '⊕'),
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
  } else {
    dock.append(el('div', { class: 'layer-tip' },
      'Press ⊕ to keep this picture, then render another date to compare them.'));
  }
  dock.append(el('button', {
    class: `coord-btn${showCoords ? ' is-on' : ''}`, onclick: toggleCoords,
  }, showCoords ? '✕ Hide coordinates' : '⌖ Show coordinates'));
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
  // The last two are the two most recently put on the map, which is what
  // somebody who has just rendered a second date is looking at.
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
  const lon = latlng.lng.toFixed(5);
  const lat = latlng.lat.toFixed(5);
  const sky = el('div', { class: 'wx' }, el('div', { class: 'passes-wait' }, 'Asking Open-Meteo…'));
  const box = el('div', { class: 'passes' },
    el('div', { class: 'passes-head' }, fmt.coord(latlng.lng, latlng.lat)),
    // Worked out on the spot rather than asked for: the sun's position is
    // arithmetic, so this is on screen before either request has answered,
    // and it still works with nothing on the other end of the network.
    sunBlock(latlng.lat, latlng.lng),
    sky,
    el('div', { class: 'passes-wait', id: 'passWait' }, 'Asking the catalogue…'));
  const popup = L.popup({ className: 'pass-popup', maxWidth: 340, ...POPUP })
    .setLatLng(latlng).setContent(box).openOn(map);

  // Both at once. The pass prediction is the slower of the two and the
  // weather has nothing to wait for.
  const [weather, passes] = await Promise.allSettled([
    api.weather(lon, lat), api.passes(lon, lat),
  ]);

  sky.replaceChildren(weather.status === 'fulfilled'
    ? weatherBlock(weather.value)
    : el('div', { class: 'wx-fail' }, `Weather unavailable: ${weather.reason.message}`));

  box.querySelector('#passWait')?.remove();
  if (passes.status === 'fulfilled') {
    for (const sat of passes.value.satellites) box.append(passRow(sat));
    box.append(el('div', { class: 'passes-foot' },
      'Predicted from the passes already flown over this point, '
      + 'stepping each ground track on by the interval it actually repeats on.'));
  } else {
    box.append(el('div', { class: 'wx-fail' }, `Passes unavailable: ${passes.reason.message}`));
  }
  popup.update();
}

/**
 * The weather half of the right-click.
 *
 * The cloud figure is given more room than the temperature, because it is the
 * one that decides whether the satellite pass underneath is worth waiting for.
 */
function weatherBlock(w) {
  const now = w.now ?? {};
  const round = (v, unit = '') => (v == null ? '—' : `${Math.round(v)}${unit}`);

  return el('div', {},
    el('div', { class: 'wx-now' },
      el('span', { class: 'wx-glyph' }, now.glyph ?? '•'),
      el('span', { class: 'wx-temp' }, round(now.temperature, '°')),
      el('span', { class: 'wx-label' }, now.label ?? ''),
      w.demo ? el('span', { class: 'wx-demo' }, 'synthetic') : null),
    el('dl', { class: 'wx-facts' },
      ...factRows([
        ['Cloud', now.cloud == null ? '—' : `${Math.round(now.cloud)}%`],
        ['Wind', now.wind == null ? '—' : `${Math.round(now.wind)} km/h ${compass(now.wind_from)}`],
        ['Feels', round(now.feels_like, '°')],
        ['Humidity', round(now.humidity, '%')],
        ['Visibility', now.visibility_km == null ? '—' : `${Math.round(now.visibility_km)} km`],
      ])),
    el('div', { class: 'wx-outlook' }, w.optical_outlook ?? ''),
    el('div', { class: 'wx-days' },
      ...(w.days ?? []).map((d) => el('div', { class: 'wx-day' },
        el('b', {}, dayName(d.date)),
        el('span', { class: 'wx-day-glyph' }, d.glyph),
        el('span', {}, `${round(d.high, '°')} / ${round(d.low, '°')}`),
        el('span', { class: 'dim' }, `${round(d.cloud, '%')} cloud`)))),
    el('div', { class: 'wx-credit' }, w.attribution ?? 'Open-Meteo'));
}

function factRows(pairs) {
  return pairs.flatMap(([label, value]) => [el('dt', {}, label), el('dd', {}, value)]);
}

const POINTS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'];
const compass = (deg) => (deg == null ? '' : POINTS[Math.round(deg / 45) % 8]);

function dayName(iso) {
  const d = new Date(`${iso}T12:00:00`);
  return Number.isNaN(d.getTime()) ? iso
    : d.toLocaleDateString(undefined, { weekday: 'short' });
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
