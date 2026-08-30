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
import { $, el, toast, fmt, debounce, askableBounds } from './ui.js';
import { POPUP } from './fires.js';
import { openWindow, closeWindow, closeAll, isOpen } from './windows.js';

let map = null;
let quakeLayer = null;
let stationLayer = null;
// One canvas for the stations and one for the quakes, rather than the default
// shared renderer: redrawing one layer then leaves the other alone.
let stationCanvas = null;
let quakeCanvas = null;

let showQuakes = false;
let showStations = false;
let hours = 168;
let minMagnitude = 2.5;
let traceMinutes = 60;
let open = false;          // is the panel expanded
// Stations whose traces are on screen, so switching the window length can
// redraw all of them rather than only the last one clicked.
const showing = new Map();
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

// How close the pointer has to get before a dot counts as clicked.
//
// Leaflet gives a canvas circle a hit area of its own radius plus half its
// stroke -- about six pixels for a station -- and then cancels the click
// outright if the map moved even slightly while the button was down. Between
// the two, aiming at a small dot and missing by a pixel does not select
// anything; it pans the map instead, which feels exactly like the pointer
// being stuck in drag mode. Every dot therefore carries an invisible target
// this big, which is roughly a fingertip on a trackpad.
const HIT_RADIUS = 12;

/** An invisible, comfortably large circle that carries the click. */
function hitTarget(latlng, renderer, radius = HIT_RADIUS) {
  return L.circleMarker(latlng, {
    renderer, radius: Math.max(radius, HIT_RADIUS),
    // Drawn, but with nothing to see: a canvas path has to be painted to be
    // hit-tested, so it cannot simply be skipped.
    stroke: false, fill: true, fillOpacity: 0, interactive: true,
  });
}

// Depth, not magnitude, decides the colour. A shallow quake does far more at
// the surface than a deep one of the same size, and it is the fact a magnitude
// alone hides.
//
// The ramp used to run red-orange-yellow, which is the fire palette almost
// exactly -- and fires are drawn as graded circles too, so on a map with both
// layers on there was no telling an earthquake from a burning field. It is a
// cool ramp now, and the marker below is a ring with a dot in the middle
// rather than a disc: colour separates them at a glance, shape separates them
// for anyone who cannot rely on colour.
const DEPTHS = [
  { under: 30, colour: '#7ef0ff', edge: '#d6faff', label: 'shallow, under 30 km' },
  { under: 100, colour: '#4cc2ff', edge: '#9adcff', label: '30 – 100 km' },
  { under: 300, colour: '#7d8cff', edge: '#b6bfff', label: '100 – 300 km' },
  { under: Infinity, colour: '#c77dff', edge: '#e3c2ff', label: 'deep, over 300 km' },
];

export function initSeismic(leafletMap) {
  map = leafletMap;
  // padding: the canvas is drawn larger than the viewport so a small pan does
  // not expose an unpainted edge before the next redraw.
  quakeCanvas = L.canvas({ padding: 0.4 });
  stationCanvas = L.canvas({ padding: 0.4 });
  quakeLayer = L.layerGroup();
  stationLayer = L.layerGroup();
  buildDock();
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
            el('span', { class: 'seis-dot', style: `color:${d.colour}` }), d.label)))),

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
          'Click a station to plot that much of its ground motion. The '
          + 'recording is fetched from whichever data centre holds it.')),

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
    else { stationLayer.remove(); stationLayer.clearLayers(); closeAll((id) => id.startsWith(WIN)); }
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
  // Traces already on screen are now showing the wrong window, so they are all
  // redrawn rather than left disagreeing with the buttons.
  for (const station of [...showing.values()]) plotStation(station, { redraw: true });
}

// ── Fetching ───────────────────────────────────────────────────

async function refresh({ force = false } = {}) {
  if (!(showQuakes || showStations) || inFlight) return;
  const view = map.getBounds();
  if (!force && covered?.contains(view)) return;

  const box = view.pad(MARGIN);
  const bounds = askableBounds(map, MARGIN);
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
    const radius = RADIUS_MIN + RADIUS_PER_MAG * Math.max(q.magnitude ?? 0, 0.5);

    // A ring, not a disc: an epicentre is a point with an extent around it,
    // and a hollow ring reads that way while a filled circle reads as an area
    // that is on fire. The ring also stays legible where several overlap,
    // which a stack of translucent discs does not.
    L.circleMarker([q.lat, q.lon], {
      renderer: quakeCanvas, radius,
      color: colour, weight: 2, opacity: 0.95, fill: false, interactive: false,
    }).addTo(quakeLayer);

    // The centre dot marks the epicentre itself, and keeps a small distant
    // event visible when its ring is only a few pixels across.
    L.circleMarker([q.lat, q.lon], {
      renderer: quakeCanvas, radius: 1.6,
      color: edge, weight: 0, fillColor: edge, fillOpacity: 0.95,
      interactive: false,
    }).addTo(quakeLayer);

    hitTarget([q.lat, q.lon], quakeCanvas, radius)
      .bindPopup(() => quakePopup(q), POPUP)
      .addTo(quakeLayer);
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

/**
 * Draw the stations.
 *
 * On a canvas, not as elements. Each station used to be a marker carrying an
 * inline SVG, which is four DOM nodes each: three thousand of them put twelve
 * thousand nodes in the document, and every mouse move during a drag cost the
 * browser about 600 ms of hit-testing and repositioning. Dragging the map was
 * seven seconds of frozen main thread. The earthquakes, which were already
 * drawn this way, cost nothing measurable at the same count.
 *
 * What is lost is the little waveform glyph. What is kept is everything the
 * glyph was for: a distinct colour and outline that says "instrument, not
 * event", a click that plots it, and a name on hover.
 */
function drawStations(data) {
  stationLayer.clearLayers();
  for (const s of data.stations) {
    L.circleMarker([s.lat, s.lon], {
      renderer: stationCanvas,
      radius: 5, weight: 1.6,
      color: '#7ed6ff', fillColor: '#0d1015', fillOpacity: 0.85, opacity: 0.95,
      interactive: false,
    }).addTo(stationLayer);

    hitTarget([s.lat, s.lon], stationCanvas)
      .bindTooltip(`${s.network}.${s.station}`, { direction: 'top', offset: [0, -12] })
      .on('click', () => plotStation(s))
      .addTo(stationLayer);
  }
  if (!data.count) return 'No open seismographs in view';
  // Which indexes answered, because a thin answer over a well-instrumented
  // country is usually a node that did not reply rather than empty ground.
  const asked = data.services?.length ?? 0;
  const quiet = data.missing?.length ?? 0;
  return `<b>${data.count.toLocaleString()}</b> seismograph${data.count === 1 ? '' : 's'}`
    + (data.capped ? ' <span class="dim">(nearest shown)</span>' : '')
    + (asked ? `<br><span class="dim">${asked} index${asked === 1 ? '' : 'es'} answered`
      + `${quiet ? `, ${quiet} did not` : ''}</span>` : '');
}

// ── The trace ──────────────────────────────────────────────────

// Window ids are prefixed so the cameras' windows and these cannot collide.
const WIN = 'trace:';

/**
 * Plot one station's recent ground motion, or close it if already open.
 *
 * Several can be open at once. Comparing the same minute at two instruments is
 * how you tell a local event from a distant one, and that is impossible if
 * opening the second closes the first.
 *
 * The data centre draws the plot and hands back a PNG. Fetching samples and
 * rendering them here would mean a waveform library in the browser for a
 * picture that already exists.
 */
function plotStation(station, { redraw = false } = {}) {
  const id = WIN + `${station.network}.${station.station}`;
  if (isOpen(id) && !redraw) {
    closeWindow(id);
    return;
  }

  const label = `${station.network}.${station.station}`;
  const span = store.config.seismic?.trace_minutes?.[traceMinutes] ?? `${traceMinutes} min`;
  const plot = el('div', { class: 'trace-plot' });

  const waiting = el('div', { class: 'trace-wait' },
    el('span', {}, `Plotting ${label}…`),
    el('small', {}, 'The data centre draws it on request, which takes a moment.'));
  plot.append(waiting);

  // Redrawing replaces the body of a window that is already open, rather than
  // closing and reopening it -- which would throw away wherever it was dragged
  // to and shuffle every other window along the cascade.
  if (redraw && isOpen(id)) {
    const existing = document.querySelector(`[data-win="${CSS.escape(id)}"]`);
    existing?.querySelector('.win-body')?.replaceChildren(plot);
    existing?.querySelector('.win-foot')?.replaceWith(
      el('div', { class: 'win-foot' }, footnote(station, span)));
  } else {
    showing.set(id, station);
    openWindow({
      id,
      title: `${label} · ${station.channel}`,
      where: station.instrument || fmt.coord(station.lon, station.lat),
      body: plot,
      // A trace is wide and short, so bigger means wider rather than square.
      sizes: [420, 'min(820px, calc(100vw - 40px))'],
      foot: footnote(station, span),
      onClose: () => showing.delete(id),
    });
  }

  fetchTrace(id, station, label, span, plot, waiting);
}

function footnote(station, span) {
  // What else this instrument records. A station offering only an
  // accelerometer channel is deaf to small distant events by design, and that
  // is worth knowing before reading a flat trace as a quiet afternoon.
  const also = (station.channels ?? []).filter((c) => c !== station.channel);
  return `Last ${span} of vertical ground motion at `
    + `${fmt.coord(station.lon, station.lat)}, ${station.elevation_m} m elevation. `
    + (also.length ? `Also records ${also.join(', ')}. ` : '')
    + `${store.config.seismic?.stations ?? 'EarthScope / FDSN'}.`;
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
async function fetchTrace(id, station, label, span, plot, waiting) {
  const token = (traceTokens.get(id) ?? 0) + 1;
  traceTokens.set(id, token);

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
    // The window length was changed, or the window closed, while this was in
    // the air: this answer is about a picture nobody is waiting for now.
    if (traceTokens.get(id) !== token) return;
    waiting.replaceChildren(
      el('span', {}, `${label} could not be plotted`),
      el('small', {}, err.message));
    return;
  }

  if (traceTokens.get(id) !== token) { URL.revokeObjectURL(blobUrl); return; }

  const image = el('img', { src: blobUrl, alt: `Ground motion at ${label}, last ${span}` });
  // The bytes are held by the object URL, not the element, so it has to be
  // handed back once the browser has decoded them or the blob leaks for the
  // life of the page.
  image.addEventListener('load', () => URL.revokeObjectURL(blobUrl), { once: true });
  waiting.remove();
  plot.append(image);
}

// One per window, so a slow answer for a trace that has since been redrawn or
// closed cannot overwrite the picture that replaced it.
const traceTokens = new Map();
