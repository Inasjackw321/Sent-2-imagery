// Earthquakes, and the instruments that recorded them.
//
// Two layers that answer each other. The quakes are the USGS's located events:
// where the ground moved, how much, and when. The seismographs are the open
// stations of the global networks -- click one and it fetches the last stretch
// of ground motion from that instrument and draws it, which is the evidence
// the dots on the map are derived from.
//
// Both are keyless. Neither needs an account.

import { api } from './api.js';
import { store } from './store.js';
import { $, el, toast, fmt, debounce } from './ui.js';
import { POPUP } from './fires.js';

let map = null;
let quakeLayer = null;
let stationLayer = null;

let showQuakes = false;
let showStations = false;
let hours = 168;
let minMagnitude = 2.5;
let traceMinutes = 60;
let open = false;          // is the panel expanded
let showing = null;        // the station whose trace is plotted
// Bumped on every plot request, so a slow answer for a station you have since
// clicked away from cannot overwrite the one you are actually looking at.
let traceToken = 0;
let inFlight = false;
// The ground already fetched for. Asked for over rather more than the screen,
// so an ordinary pan is answered from what is already drawn.
let covered = null;
const MARGIN = 0.35;

// Magnitude is logarithmic, so the radius follows it directly rather than
// through a square root: a 7 really is a different order of thing from a 4 and
// the map should say so.
const RADIUS_MIN = 2.5;
const RADIUS_PER_MAG = 1.8;

// Depth, not magnitude, decides the colour. A shallow quake does far more at
// the surface than a deep one of the same size, and it is the fact a magnitude
// alone hides.
const DEPTHS = [
  { under: 30, colour: '#ff4d6d', edge: '#ff8fa3', label: 'shallow, under 30 km' },
  { under: 100, colour: '#ffa62b', edge: '#ffd166', label: '30 – 100 km' },
  { under: 300, colour: '#ffe66d', edge: '#fff3b0', label: '100 – 300 km' },
  { under: Infinity, colour: '#8ecae6', edge: '#bde0fe', label: 'deep, over 300 km' },
];

export function initSeismic(leafletMap) {
  map = leafletMap;
  quakeLayer = L.layerGroup();
  stationLayer = L.layerGroup();
  buildDock();
  buildTracePanel();
  map.on('moveend', debounce(() => refresh(), 700));
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#seismicDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'seis-toggle', id: 'seisToggle', onclick: togglePanel },
      el('span', { class: 'seis-mark' }, '⋀'), 'Seismic'),
    el('div', { class: 'seis-body', id: 'seisBody', hidden: true },
      // Two independent layers, so two checkboxes rather than a segmented
      // control: wanting stations without events is an ordinary thing to want.
      el('label', { class: 'seis-check' },
        el('input', {
          type: 'checkbox', id: 'seisQuakes',
          onchange: (e) => setLayer('quakes', e.target.checked),
        }),
        'Earthquakes'),
      el('div', { class: 'seis-sub', id: 'seisQuakeOpts', hidden: true },
        el('div', { class: 'seis-row' },
          ...[24, 168, 720].map((h) => el('button', {
            class: `seis-chip${h === hours ? ' is-active' : ''}`,
            dataset: { hours: h },
            onclick: () => setWindow(h),
          }, h === 24 ? '24 h' : h === 168 ? '7 d' : '30 d'))),
        el('label', { class: 'seis-mag' },
          el('span', {}, 'Magnitude '), el('b', { id: 'seisMagValue' }, minMagnitude.toFixed(1)),
          el('span', {}, ' and up'),
          el('input', {
            type: 'range', id: 'seisMag', min: '0', max: '7', step: '0.5',
            value: String(minMagnitude),
            oninput: (e) => previewMagnitude(e.target.value),
            onchange: (e) => setMagnitude(e.target.value),
          })),
        el('div', { class: 'seis-key' },
          ...DEPTHS.map((d) => el('div', { class: 'seis-key-row' },
            el('span', { class: 'seis-dot', style: `background:${d.colour}` }), d.label)))),

      el('label', { class: 'seis-check' },
        el('input', {
          type: 'checkbox', id: 'seisStations',
          onchange: (e) => setLayer('stations', e.target.checked),
        }),
        'Seismographs'),
      el('div', { class: 'seis-sub', id: 'seisStationOpts', hidden: true },
        el('div', { class: 'seis-row' },
          ...[10, 60, 360].map((m) => el('button', {
            class: `seis-chip${m === traceMinutes ? ' is-active' : ''}`,
            dataset: { minutes: m },
            onclick: () => setTraceWindow(m),
          }, m === 10 ? '10 min' : m === 60 ? '1 h' : '6 h'))),
        el('div', { class: 'seis-hint' },
          'Click a station to plot that much of its ground motion. Only '
          + 'stations whose recordings this data centre holds are shown.')),

      el('div', { class: 'seis-count', id: 'seisCount' }, 'Nothing loaded yet'),
      el('div', { class: 'seis-note', id: 'seisNote' },
        'USGS events · EarthScope/FDSN stations · no account needed')),
  );
}

function togglePanel() {
  open = !open;
  $('#seisToggle').classList.toggle('is-on', open);
  $('#seisBody').hidden = !open;
}

function setLayer(which, on) {
  if (which === 'quakes') {
    showQuakes = on;
    $('#seisQuakeOpts').hidden = !on;
    if (on) quakeLayer.addTo(map);
    else { quakeLayer.remove(); quakeLayer.clearLayers(); }
  } else {
    showStations = on;
    $('#seisStationOpts').hidden = !on;
    if (on) stationLayer.addTo(map);
    else { stationLayer.remove(); stationLayer.clearLayers(); showTrace(null); }
  }
  covered = null;
  if (showQuakes || showStations) refresh({ force: true });
  else $('#seisCount').textContent = 'Nothing loaded yet';
}

function setWindow(next) {
  hours = next;
  for (const b of document.querySelectorAll('.seis-chip[data-hours]')) {
    b.classList.toggle('is-active', Number(b.dataset.hours) === hours);
  }
  refresh({ force: true });
}

/** Update the readout while the slider is still moving. */
function previewMagnitude(value) {
  $('#seisMagValue').textContent = Number(value).toFixed(1);
}

function setMagnitude(value) {
  minMagnitude = Number(value);
  previewMagnitude(minMagnitude);
  refresh({ force: true });
}

function setTraceWindow(minutes) {
  traceMinutes = minutes;
  for (const b of document.querySelectorAll('.seis-chip[data-minutes]')) {
    b.classList.toggle('is-active', Number(b.dataset.minutes) === traceMinutes);
  }
  // A trace already on screen is now showing the wrong window, so redraw it
  // rather than leaving the buttons disagreeing with the picture.
  if (showing) showTrace(showing);
}

// ── Fetching ───────────────────────────────────────────────────

async function refresh({ force = false } = {}) {
  if (!(showQuakes || showStations) || inFlight) return;
  const view = map.getBounds();
  if (!force && covered?.contains(view)) return;

  const box = view.pad(MARGIN);
  const bounds = {
    west: box.getWest(), south: box.getSouth(),
    east: box.getEast(), north: box.getNorth(),
  };
  inFlight = true;
  $('#seisCount').textContent = 'Asking…';
  const said = [];
  try {
    // Both in flight together: they are separate services and one being slow
    // should not hold the other's dots off the map.
    const [quakes, stations] = await Promise.allSettled([
      showQuakes ? api.quakes({ ...bounds, hours, minMagnitude }) : null,
      showStations ? api.seismographs(bounds) : null,
    ]);
    covered = box;

    if (showQuakes) {
      if (quakes.status === 'fulfilled') said.push(drawQuakes(quakes.value));
      else { said.push('earthquakes unavailable'); covered = null; }
    }
    if (showStations) {
      if (stations.status === 'fulfilled') said.push(drawStations(stations.value));
      else { said.push('stations unavailable'); covered = null; }
    }
    // One message for both, rather than a toast per failed service.
    const broke = [quakes, stations].find((r) => r?.status === 'rejected');
    if (broke) toast(`Seismic data: ${broke.reason.message}`, 'err');
    $('#seisCount').innerHTML = said.join('<br>');
  } finally {
    inFlight = false;
  }
}

// ── Earthquakes on the map ─────────────────────────────────────

const depthBand = (km) => DEPTHS.find((d) => (km ?? 0) < d.under) ?? DEPTHS.at(-1);

function drawQuakes(data) {
  quakeLayer.clearLayers();
  for (const q of data.quakes) {
    const { colour, edge } = depthBand(q.depth_km);
    L.circleMarker([q.lat, q.lon], {
      radius: RADIUS_MIN + RADIUS_PER_MAG * Math.max(q.magnitude ?? 0, 0.5),
      color: edge, weight: 1, fillColor: colour, fillOpacity: 0.45, opacity: 0.9,
    }).bindPopup(() => quakePopup(q), POPUP).addTo(quakeLayer);
  }
  const window = hours === 24 ? '24 h' : hours === 168 ? '7 days' : '30 days';
  return data.count
    ? `<b>${data.count.toLocaleString()}</b> quake${data.count === 1 ? '' : 's'}`
      + ` · M${minMagnitude.toFixed(1)}+ · last ${window}`
    : `No quakes over M${minMagnitude.toFixed(1)} here in the last ${window}`;
}

function quakePopup(q) {
  const ago = q.time ? (Date.now() - new Date(q.time).getTime()) / 3.6e6 : null;
  return el('div', { class: 'seis-popup' },
    el('div', { class: 'seis-popup-head' },
      `M ${q.magnitude?.toFixed(1) ?? '?'}`,
      q.magnitude_type ? el('span', { class: 'dim' }, ` ${q.magnitude_type}`) : null),
    q.place ? el('div', {}, q.place) : null,
    el('div', { class: 'dim' },
      `${q.depth_km != null ? `${q.depth_km} km deep` : 'depth unknown'}`
      + `${ago != null ? ` · ${fmt.duration(ago)} ago` : ''}`),
    q.felt ? el('div', { class: 'dim' }, `${q.felt.toLocaleString()} felt reports`) : null,
    q.tsunami ? el('div', { class: 'seis-warn' }, 'Tsunami evaluation issued') : null,
    el('div', { class: 'dim' }, fmt.coord(q.lon, q.lat)),
    q.url ? el('div', {},
      el('a', { href: q.url, target: '_blank', rel: 'noopener noreferrer' }, 'USGS event page ↗')) : null,
  ).outerHTML;
}

// ── Stations on the map ────────────────────────────────────────

function drawStations(data) {
  stationLayer.clearLayers();
  for (const s of data.stations) {
    L.marker([s.lat, s.lon], {
      riseOnHover: true,
      title: `${s.network}.${s.station}`,
      icon: L.divIcon({
        className: 'seis-pin',
        html: '<svg viewBox="0 0 24 24" aria-hidden="true">'
          + '<circle cx="12" cy="12" r="9" fill="#0d1015" stroke="#7ed6ff" stroke-width="2"/>'
          + '<path d="M4 12h3l2-4 3 8 2.5-6 1.5 2h4" fill="none" stroke="#7ed6ff"'
          + ' stroke-width="1.6" stroke-linejoin="round"/></svg>',
        iconSize: [24, 24], iconAnchor: [12, 12],
      }),
    }).on('click', () => showTrace(s)).addTo(stationLayer);
  }
  if (!data.count) {
    // Not the same as "no instruments here". The list is narrowed to stations
    // whose recordings this data centre actually holds, and saying so is the
    // difference between an explained empty map and a broken-looking one.
    return data.checked === false
      ? 'No open seismographs in view'
      : 'No seismographs here with recordings held at this data centre';
  }
  return `<b>${data.count.toLocaleString()}</b> seismograph${data.count === 1 ? '' : 's'}`
    + (data.capped ? ' <span class="dim">(nearest shown)</span>' : '')
    + (data.checked === false
      ? '<br><span class="dim">availability unchecked — some may not plot</span>'
      : '');
}

// ── The trace ──────────────────────────────────────────────────

function buildTracePanel() {
  if ($('#tracePanel')) return;
  document.body.append(
    el('div', { class: 'trace-panel', id: 'tracePanel', hidden: true },
      el('div', { class: 'trace-bar' },
        // Close on the left, unlike every other panel here. The camera player
        // is anchored to the other corner and overlaps this one on a narrow
        // window; a control at the far left stays reachable underneath it.
        el('button', { class: 'trace-close', title: 'Close', onclick: () => showTrace(null) }, '×'),
        el('b', { id: 'traceName' }, ''),
        el('span', { class: 'trace-where', id: 'traceWhere' }, '')),
      el('div', { class: 'trace-plot', id: 'tracePlot' }),
      el('div', { class: 'trace-foot', id: 'traceFoot' })),
  );
}

/**
 * Plot one station's recent ground motion, or close the panel.
 *
 * The data centre draws the plot and hands back a PNG. Fetching samples and
 * rendering them here would mean a waveform library in the browser for a
 * picture that already exists, and the plot it returns is the conventional one.
 */
function showTrace(station) {
  showing = station;
  const panel = $('#tracePanel');
  const plot = $('#tracePlot');
  if (!panel || !plot) return;

  plot.replaceChildren();
  if (!station) { panel.hidden = true; return; }

  const label = `${station.network}.${station.station}`;
  const span = store.config.seismic?.trace_minutes?.[traceMinutes] ?? `${traceMinutes} min`;

  const waiting = el('div', { class: 'trace-wait' },
    el('span', {}, `Plotting ${label}…`),
    el('small', {}, 'The data centre draws it on request, which takes a moment.'));
  plot.append(waiting);
  fetchTrace(station, label, span, plot, waiting);

  $('#traceName').textContent = `${label} · ${station.channel}`;
  $('#traceWhere').textContent = station.instrument || fmt.coord(station.lon, station.lat);
  $('#traceFoot').textContent =
    `Last ${span} of vertical ground motion at ${fmt.coord(station.lon, station.lat)}, `
    + `${station.elevation_m} m elevation. `
    + `${store.config.seismic?.stations ?? 'EarthScope / FDSN'}.`;
  panel.hidden = false;
}

/**
 * Fetch the plot, and say what went wrong when it does not arrive.
 *
 * Deliberately not an <img src>. The backend explains a failure in words --
 * the station is behind, or its waveforms live at another data centre -- and
 * an <img> throws all of that away and fires a bare error event, which is how
 * every failure ended up reading as the same unhelpful "no data". Fetching it
 * means the reason reaches the reader.
 */
async function fetchTrace(station, label, span, plot, waiting) {
  const token = ++traceToken;
  const url = api.traceUrl({
    network: station.network, station: station.station,
    channel: station.channel, loc: station.loc, minutes: traceMinutes,
  });

  let blobUrl = null;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body.detail) detail = body.detail;
      } catch { /* the error body was not JSON */ }
      throw new Error(detail);
    }
    blobUrl = URL.createObjectURL(await res.blob());
  } catch (err) {
    // Another station was clicked while this was in the air: its plot is the
    // one on screen now, and this answer is about a panel that has moved on.
    if (token !== traceToken) return;
    waiting.replaceChildren(
      el('span', {}, `${label} could not be plotted`),
      el('small', {}, err.message));
    return;
  }

  if (token !== traceToken) { URL.revokeObjectURL(blobUrl); return; }

  const image = el('img', { src: blobUrl, alt: `Ground motion at ${label}, last ${span}` });
  // The bytes are held by the object URL, not the element, so it has to be
  // handed back once the browser has decoded them or the blob leaks for the
  // life of the page.
  image.addEventListener('load', () => URL.revokeObjectURL(blobUrl), { once: true });
  waiting.remove();
  plot.append(image);
}
