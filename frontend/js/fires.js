// NASA FIRMS active fire detections, on the map alongside the imagery.
//
// A detection is one satellite pixel that came back hot -- a few hundred
// metres across, at a known minute. So the marker is deliberately not drawn as
// a fire: it is a dot at a point, sized by how much energy the fire is
// radiating, and it says when it was seen. Reading it as a burnt area would be
// reading far more into it than the data supports.

import { api } from './api.js';
import { store } from './store.js';
import { $, el, toast, fmt, debounce, askableBounds } from './ui.js';

let map = null;
let layer = null;
let enabled = false;
let hours = 24;
let inFlight = false;
// The ground already fetched for. Detections are asked for over rather more
// than the screen, so an ordinary pan is answered from what is already drawn:
// redrawing on every move would throw away whichever popup was open and ask
// NASA for the same fires again.
let covered = null;
// Whether the last answer was trimmed, and how much ground it covered. A
// trimmed answer over a large area gets better when you look at a small one.
let lastCapped = false;
let coveredArea = 0;
const MARGIN = 0.4;

// How much smaller the view has to get before it is worth re-asking. A
// quarter is one zoom level, which is roughly when the cap starts letting
// through detections it had dropped before.
const SHRUNK = 0.3;

// Above this zoom a detection is drawn at the size of the pixel that saw it
// -- 375 m for VIIRS, a kilometre for MODIS -- rather than as a fixed dot.
// Below it those circles would be sub-pixel and the map would look empty.
const FOOTPRINT_ZOOM = 11;

const area = (bounds) => Math.abs(
  (bounds.getEast() - bounds.getWest()) * (bounds.getNorth() - bounds.getSouth()));

/** Is there more to be had by asking again for this smaller view? */
function worthAnotherLook(view) {
  if (!lastCapped || !coveredArea) return false;
  return area(view.pad(MARGIN)) < coveredArea * SHRUNK;
}

// Fire radiative power, in megawatts. The scale is heavily skewed -- most
// detections are a few MW and a big fire front is hundreds -- so the radius
// follows the square root, which keeps the small ones visible without letting
// the large ones swallow the map.
const RADIUS_MIN = 3.5;
const RADIUS_MAX = 15;

// The docks sit over the map's corners, and a popup opening underneath one is
// a popup you cannot read. Padding the auto-pan by their footprint makes the
// map shift itself far enough for the popup to clear them.
export const POPUP = {
  autoPan: true,
  autoPanPaddingTopLeft: [24, 24],
  autoPanPaddingBottomRight: [210, 150],
};

const BANDS = [
  { over: 100, colour: '#fff3b0', edge: '#ffffff', label: 'over 100 MW' },
  { over: 30, colour: '#ffb703', edge: '#ffd166', label: '30 – 100 MW' },
  { over: 10, colour: '#fb8500', edge: '#ffa94d', label: '10 – 30 MW' },
  { over: 0, colour: '#e5383b', edge: '#ff6b6b', label: 'under 10 MW' },
];

export function initFires(leafletMap) {
  map = leafletMap;
  layer = L.layerGroup();
  buildDock();
  // Panning to somewhere new is a request for that somewhere's fires.
  map.on('moveend', debounce(() => { if (enabled) refresh(); }, 600));
  // Redrawing on zoom is separate from re-fetching: the shapes change at the
  // footprint threshold even when the detections behind them have not.
  map.on('zoomend', () => { if (enabled && shown.length) paint(); });
}

function buildDock() {
  const dock = $('#fireDock');
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'fire-toggle', id: 'fireToggle', onclick: toggle },
      el('span', { class: 'fire-flame' }, '🔥'), 'Active fires'),
    el('div', { class: 'fire-body', id: 'fireBody', hidden: true },
      el('div', { class: 'fire-windows', id: 'fireWindows' },
        ...[24, 48, 168].map((h) => el('button', {
          class: `fire-window${h === hours ? ' is-active' : ''}`,
          dataset: { hours: h },
          onclick: () => setWindow(h),
        }, h === 168 ? '7 days' : `${h} h`))),
      el('div', { class: 'fire-count', id: 'fireCount' }, 'Nothing loaded yet'),
      el('div', { class: 'fire-key', id: 'fireKey' },
        ...BANDS.map((b) => el('div', { class: 'fire-key-row' },
          el('span', { class: 'fire-dot', style: `background:${b.colour}` }), b.label))),
      el('div', { class: 'fire-note' },
        'NASA FIRMS · each dot is one hot satellite pixel, not a burnt area')),
  );
}

function toggle() {
  enabled = !enabled;
  $('#fireToggle').classList.toggle('is-on', enabled);
  $('#fireBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    refresh({ force: true });
  } else {
    layer.remove();
    layer.clearLayers();
    covered = null;
  }
}

function setWindow(next) {
  hours = next;
  for (const b of document.querySelectorAll('.fire-window')) {
    b.classList.toggle('is-active', Number(b.dataset.hours) === hours);
  }
  refresh({ force: true });
}

/**
 * Ask for the fires under whatever part of the world is on screen.
 *
 * Zooming in re-asks, which it used to skip. A continent's worth of
 * detections is more than the map can hold, so the service returns the
 * fiercest few thousand and drops the rest -- which means the answer for a
 * whole country is a different, coarser answer than the one for a single
 * valley inside it. Treating the first as good enough for the second is why
 * zooming in never revealed anything new: the small fires had already been
 * discarded before the tiles were drawn.
 *
 * Only when the last answer was actually trimmed, though. If nothing was
 * dropped there is no more detail to be had and re-asking is just traffic.
 */
async function refresh({ force = false } = {}) {
  if (!enabled || inFlight) return;
  const view = map.getBounds();
  if (!force && covered?.contains(view) && !worthAnotherLook(view)) return;

  const box = view.pad(MARGIN);
  inFlight = true;
  $('#fireCount').textContent = 'Asking NASA…';
  try {
    const data = await api.fires({ ...askableBounds(map, MARGIN), hours });
    covered = box;
    lastCapped = Boolean(data.capped);
    coveredArea = area(box);
    draw(data);
  } catch (err) {
    covered = null;
    lastCapped = false;
    $('#fireCount').textContent = 'Could not load fires';
    toast(`Fire data unavailable: ${err.message}`, 'err');
  } finally {
    inFlight = false;
  }
}

let shown = [];

function draw(data) {
  shown = data.fires;
  paint();
  const window = hours === 168 ? '7 days' : `${hours} h`;
  $('#fireCount').innerHTML = data.count
    ? `<b>${data.count.toLocaleString()}</b> detection${data.count === 1 ? '' : 's'} · last ${window}`
      + (data.capped ? `<br><span class="dim">fiercest of ${data.total.toLocaleString()} —`
        + ' zoom in for the rest</span>' : '')
    : `Nothing burning here in the last ${window}`;
}

/**
 * Draw what is held, at whatever detail the current zoom deserves.
 *
 * Zoomed out, a detection is a dot sized by how much energy it is radiating.
 * Zoomed in, it becomes the actual footprint of the satellite pixel that saw
 * it -- 375 m across for VIIRS, a kilometre for MODIS -- which is the honest
 * shape of the evidence. A fixed dot at high zoom implies a precision the
 * data does not have, in both directions: too small over a MODIS pixel, far
 * too large over a city block.
 */
function paint() {
  layer.clearLayers();
  const footprints = map.getZoom() >= FOOTPRINT_ZOOM;
  for (const fire of shown) {
    const base = style(fire);
    const marker = footprints
      ? L.circle([fire.lat, fire.lon], {
        ...base,
        // Radius, so half the pixel's width.
        radius: (fire.resolution_m ?? 375) / 2,
        // Zoomed right in, a real 375 m footprint covers the screen. That is
        // the honest size of the evidence, but filled solid it hides the
        // ground it is evidence about -- so the outline carries it and the
        // fill only tints.
        fillOpacity: base.fillOpacity * 0.35,
        weight: 1.5,
      })
      : L.circleMarker([fire.lat, fire.lon], base);
    marker.bindPopup(() => popup(fire), POPUP).addTo(layer);
  }
}

const band = (frp) => BANDS.find((b) => (frp ?? 0) > b.over) ?? BANDS.at(-1);

function style(fire) {
  const power = Math.max(fire.frp ?? 0, 0);
  const scale = Math.min(Math.sqrt(power / 120), 1);
  const { colour, edge } = band(fire.frp);
  return {
    radius: RADIUS_MIN + (RADIUS_MAX - RADIUS_MIN) * scale,
    color: edge,
    weight: 1,
    fillColor: colour,
    // A low-confidence detection is drawn fainter rather than hidden: it is
    // still evidence, just weaker evidence.
    fillOpacity: 0.25 + 0.5 * (fire.confidence ?? 0.6),
    opacity: 0.9,
  };
}

function popup(fire) {
  const ago = (Date.now() - new Date(fire.acquired).getTime()) / 3.6e6;
  const sensor = store.config.fires?.sensors?.[fire.sensor] ?? fire.sensor;
  return el('div', { class: 'fire-popup' },
    el('div', { class: 'fire-popup-head' },
      fire.frp != null ? `${fire.frp.toLocaleString()} MW` : 'Thermal detection'),
    el('div', {}, `${fmt.duration(ago)} ago · ${fmt.when(fire.acquired)}`),
    el('div', { class: 'dim' },
      `${sensor} · ${fire.resolution_m} m pixel · ${fire.confidence_label} confidence`
      + `${fire.day ? ' · daytime' : ' · night'}`),
    el('div', { class: 'dim' }, fmt.coord(fire.lon, fire.lat)),
  ).outerHTML;
}
