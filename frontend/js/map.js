// Leaflet map, AOI drawing tools (lasso / circle / box / polygon) and the render overlay.

import { api } from './api.js';
import { store, emit, on } from './store.js';
import { $, $$, toast, fmt, el } from './ui.js';

let map;
let aoiLayer = null;
let sketchLayer = null;
let overlay = null;
let overlayVisible = true;
let mode = 'lasso';
let drawing = null;

const AOI_STYLE = { color: '#4cc2ff', weight: 2, fillColor: '#4cc2ff', fillOpacity: 0.10, dashArray: null };
const SKETCH_STYLE = { color: '#ffd166', weight: 2, dashArray: '5,5', fill: false };

const HINTS = {
  lasso: 'Drag on the map to circle your region',
  circle: 'Drag out from the centre of your area',
  rect: 'Drag a box around your area',
  polygon: 'Click each corner — double-click to finish',
};

export function initMap() {
  map = L.map('map', { zoomControl: true, worldCopyJump: true, preferCanvas: true })
    .setView([48.86, 2.35], 11);

  const osm = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '© OpenStreetMap contributors',
  });
  const esri = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { maxZoom: 19, attribution: 'Esri, Maxar, Earthstar Geographics' },
  );
  const labels = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    maxZoom: 20, attribution: '© OpenStreetMap contributors © CARTO',
  });
  labels.addTo(map);
  L.control.layers(
    { 'Dark basemap': labels, 'Street map': osm, 'Satellite context': esri },
    {}, { position: 'topright' },
  ).addTo(map);

  L.control.scale({ imperial: false, position: 'bottomleft' }).addTo(map);

  bindDrawTools();
  bindOverlayDock();

  on('activeCapture', showOverlay);
  return map;
}

export const getMap = () => map;

// ── Drawing ────────────────────────────────────────────────────

function bindDrawTools() {
  $$('#drawTools .tool').forEach((btn) => btn.addEventListener('click', () => {
    $$('#drawTools .tool').forEach((b) => b.classList.toggle('is-active', b === btn));
    setMode(btn.dataset.draw);
  }));

  map.on('mousedown', onDown);
  map.on('mousemove', onMove);
  map.on('mouseup', onUp);
  map.on('click', onClick);
  map.on('dblclick', onDblClick);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') cancelSketch();
  });

  $('#clearAoi').addEventListener('click', clearAoi);
  $('#zoomAoi').addEventListener('click', () => {
    if (aoiLayer) map.fitBounds(aoiLayer.getBounds(), { padding: [40, 40] });
  });
}

function setMode(next) {
  cancelSketch();
  mode = next;
  $('#mapHint').textContent = HINTS[mode];
  map.doubleClickZoom[mode === 'polygon' ? 'disable' : 'enable']();
}

function onDown(e) {
  if (mode === 'polygon' || drawing) return;
  map.dragging.disable();
  drawing = { mode, start: e.latlng, points: [e.latlng], lastPoint: e.containerPoint };
  sketchLayer?.remove();
  sketchLayer = (mode === 'circle'
    ? L.circle(e.latlng, { radius: 1, ...SKETCH_STYLE })
    : L.polyline([e.latlng], SKETCH_STYLE)).addTo(map);
}

function onMove(e) {
  if (!drawing) return;
  if (drawing.mode === 'circle') {
    sketchLayer.setRadius(Math.max(map.distance(drawing.start, e.latlng), 1));
  } else if (drawing.mode === 'rect') {
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
  cancelSketch();

  if (sketch.mode === 'circle') {
    const radius = map.distance(sketch.start, e.latlng);
    if (radius < 20) return;
    setAoi({ type: 'circle', lon: sketch.start.lng, lat: sketch.start.lat, radius });
  } else if (sketch.mode === 'rect') {
    const ring = boxRing(sketch.start, e.latlng);
    if (map.distance(sketch.start, e.latlng) < 20) return;
    setAoi(ringToPolygon(ring));
  } else if (sketch.mode === 'lasso') {
    if (sketch.points.length < 4) return;
    setAoi(ringToPolygon(sketch.points));
  }
}

function onClick(e) {
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
  cancelSketch();
  if (points.length >= 3) setAoi(ringToPolygon(points));
}

function cancelSketch() {
  drawing = null;
  sketchLayer?.remove();
  sketchLayer = null;
  map.dragging.enable();
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
  $('#aoiSummary').innerHTML = '<em>Draw on the map to define your area.</em>';
  emit('aoi', null);
}

function renderAoiSummary(info) {
  const [w, s, e, n] = info.bounds;
  const widthM = map.distance(L.latLng(info.center[1], w), L.latLng(info.center[1], e));
  const heightM = map.distance(L.latLng(s, info.center[0]), L.latLng(n, info.center[0]));
  $('#aoiSummary').innerHTML = `
    <div>Area <b>${fmt.area(info.area_km2)}</b></div>
    <div>Extent <b>${fmt.distance(widthM)} × ${fmt.distance(heightM)}</b></div>
    <div>Centre <b>${fmt.coord(info.center[0], info.center[1])}</b></div>`;
}

// ── Overlay of the rendered image ──────────────────────────────

function showOverlay(capture) {
  overlay?.remove();
  overlay = null;
  $('#mapLegend').hidden = true;
  $('#opacityDock').hidden = true;
  if (!capture?.meta?.grid) return;

  const [w, s, e, n] = capture.meta.grid.bounds;
  overlay = L.imageOverlay(capture.src, [[s, w], [n, e]], {
    opacity: $('#overlayOpacity').valueAsNumber / 100,
    interactive: false,
    className: 'render-overlay',
  }).addTo(map);
  overlayVisible = true;
  $('#toggleOverlay').textContent = 'Hide';
  $('#opacityDock').hidden = false;
  drawMapLegend(capture.meta);
}

function drawMapLegend(meta) {
  const box = $('#mapLegend');
  const legend = meta.legend;
  if (!legend) {
    box.hidden = true;
    return;
  }
  box.innerHTML = '';
  if (legend.type === 'categorical') {
    box.append(el('div', {}, legend.label));
    for (const cls of legend.classes.slice(0, 8)) {
      box.append(el('div', { class: 'ends' },
        el('span', {},
          el('i', { class: 'swatch', style: `background:${cls.color}` }),
          ` ${cls.label}`),
        el('span', {}, `${cls.percent}%`)));
    }
  } else {
    const stops = legend.stops.map((s) => `${s.color} ${s.pos * 100}%`).join(', ');
    box.append(
      el('div', {}, `${legend.label}${legend.unit ? ` (${legend.unit})` : ''}`),
      el('div', { class: 'bar', style: `background: linear-gradient(90deg, ${stops})` }),
      el('div', { class: 'ends' },
        el('span', {}, legend.vmin.toFixed(2)),
        el('span', {}, legend.vmax.toFixed(2))),
    );
  }
  box.hidden = false;
}

function bindOverlayDock() {
  $('#overlayOpacity').addEventListener('input', (e) => {
    overlay?.setOpacity(e.target.valueAsNumber / 100);
  });
  $('#toggleOverlay').addEventListener('click', (e) => {
    if (!overlay) return;
    overlayVisible = !overlayVisible;
    overlay.setOpacity(overlayVisible ? $('#overlayOpacity').valueAsNumber / 100 : 0);
    e.target.textContent = overlayVisible ? 'Hide' : 'Show';
  });
}

export function invalidateMap() {
  setTimeout(() => map?.invalidateSize(), 60);
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
