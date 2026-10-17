// Internet connectivity, from RIPE Atlas probes.
//
// Each dot is one small measurement device that a volunteer has plugged into
// their network, holding a permanent connection open to the RIPE NCC. When
// its site loses its connection -- or its power -- the probe drops, and the
// minute it happened is recorded.
//
// One probe going dark is one household or office. Several in one area going
// dark in the same hour is an outage: a power cut, an ISP failing, a shutdown,
// a strike on infrastructure. So the panel leads with how many went down and
// where, by country, rather than with the dots -- a single red dot is the
// thing most likely to be over-read.

import { api } from './api.js';
import { store } from './store.js';
import { $, el, toast, fmt, debounce, askableBounds, savePicture } from './ui.js';
import { openWindow, closeWindow, closeAll, isOpen } from './windows.js';

let map = null;
let layer = null;
let canvas = null;
let enabled = false;
let hours = 6;
let inFlight = false;
let covered = null;
let timer = null;
const MARGIN = 0.35;
// How far back the graphs look, in hours: one probe's, and an area's.
let historyHours = 48;
// The box tool, handed over by the map at start-up.
let pickArea = null;

// A probe's status is reported every few minutes, and the backend holds an
// answer for two. Asking every three keeps the panel within a few minutes of
// RIPE without asking for the same answer twice.
const REFRESH_MS = 3 * 60 * 1000;

// What each state looks like. The two that are news are big and bright; the
// two that are background are small and quiet, so a screenful of steady
// probes does not drown the three that just went down.
export const STATES = {
  down: { colour: '#ff4d4f', edge: '#ffd0d0', radius: 6, fill: 0.95,
          label: 'went offline in the window' },
  back: { colour: '#ffd166', edge: '#fff1c2', radius: 5, fill: 0.9,
          label: 'came back in the window' },
  gone: { colour: '#7a8594', edge: '#a9b3c0', radius: 3.5, fill: 0.6,
          label: 'offline for longer' },
  up: { colour: '#3ddc84', edge: '#3ddc84', radius: 2.6, fill: 0.5,
        label: 'online' },
};

// Drawn in this order, so the changes are on top of the steady ones -- and,
// because their click targets are drawn last too, a click between a steady
// probe and one that just went down opens the one that went down.
const LAYERING = ['up', 'gone', 'back', 'down'];

// How close the pointer has to get for a click to count, in pixels.
const HIT_RADIUS = 10;

export function initConnectivity(leafletMap, tools = {}) {
  map = leafletMap;
  pickArea = tools.pickArea ?? null;
  // A canvas: a continental view is a few thousand dots, which as SVG
  // elements is a few thousand nodes for the browser to move on every pan.
  canvas = L.canvas({ padding: 0.4 });
  layer = L.layerGroup();
  buildDock();
  map.on('moveend', debounce(() => { if (enabled) refresh(); }, 600));
}

function buildDock() {
  const dock = $('#connectivityDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'net-toggle', id: 'netToggle', onclick: toggle },
      el('span', { class: 'net-mark' }, '📶'), 'Internet connectivity'),
    el('div', { class: 'net-body', id: 'netBody', hidden: true },
      el('div', { class: 'net-windows' },
        ...Object.entries(store.config?.connectivity?.windows ?? { 1: '1 h', 6: '6 h', 24: '24 h' })
          .map(([h, label]) => el('button', {
            class: `net-window${Number(h) === hours ? ' is-active' : ''}`,
            dataset: { hours: h },
            onclick: () => setWindow(Number(h)),
          }, label))),
      // How far back a probe's graph looks. A day shows this evening's
      // outage against this morning; a week shows whether it happens every
      // evening.
      el('div', { class: 'net-graphs' },
        el('span', { class: 'net-graphs-label' }, 'Graphs'),
        ...Object.entries(store.config?.connectivity?.history
                          ?? { 24: '24 h', 48: '48 h', 168: '7 days' })
          .map(([h, label]) => el('button', {
            class: `net-span${Number(h) === historyHours ? ' is-active' : ''}`,
            dataset: { hours: h },
            onclick: () => setHistory(Number(h)),
          }, label))),
      // Drag a box, get one graph of every probe inside it. data-draw puts
      // the button in the map's own tool family: it lights while armed, and
      // Escape or a click elsewhere puts it away like any other tool.
      el('button', {
        class: 'net-area', id: 'netArea', type: 'button', dataset: { draw: 'pick' },
        title: 'Drag a box on the map: every probe inside it becomes one graph',
        onclick: armArea,
      }, '▭ Graph an area'),
      el('div', { class: 'net-count', id: 'netCount' }, 'Nothing loaded yet'),
      el('div', { class: 'net-countries', id: 'netCountries' }),
      el('div', { class: 'net-key' },
        ...['down', 'back', 'gone', 'up'].map((key) => el('div', { class: 'net-key-row' },
          el('span', { class: 'net-dot', style: `background:${STATES[key].colour}` }),
          STATES[key].label))),
      el('div', { class: 'net-note' },
        'RIPE Atlas · each dot is one volunteer’s probe, wired into their '
        + 'network. One going dark is one site; many in one area at once is an '
        + 'outage. Positions are blurred by RIPE by up to a few kilometres. '
        + 'Click one for its graph.')),
  );
}

function toggle() {
  enabled = !enabled;
  $('#netToggle').classList.toggle('is-on', enabled);
  $('#netBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    refresh({ force: true });
    // Only while it is showing: a timer asking RIPE about a layer nobody can
    // see is somebody else's bandwidth for nothing.
    timer = setInterval(() => {
      if (!document.hidden) refresh({ force: true });
    }, REFRESH_MS);
  } else {
    clearInterval(timer);
    timer = null;
    layer.remove();
    layer.clearLayers();
    covered = null;
    // Their timers with them: a graph refreshing itself for a layer that
    // has been switched off is a request a minute for nobody.
    closeAll(isGraphWindow);
  }
}

function setHistory(next) {
  historyHours = next;
  for (const b of document.querySelectorAll('.net-span')) {
    b.classList.toggle('is-active', Number(b.dataset.hours) === historyHours);
  }
  // Graphs already open are now showing the wrong span, so they are all
  // redrawn rather than left disagreeing with the buttons.
  for (const [id, what] of [...graphs.entries()]) {
    if (what.area) openArea(what.area, { redraw: true, id });
    else openGraph(what, { redraw: true });
  }
}

function setWindow(next) {
  hours = next;
  for (const b of document.querySelectorAll('.net-window')) {
    b.classList.toggle('is-active', Number(b.dataset.hours) === hours);
  }
  refresh({ force: true });
}

async function refresh({ force = false } = {}) {
  if (!enabled || inFlight) return;
  const view = map.getBounds();
  if (!force && covered?.contains(view)) return;
  const box = view.pad(MARGIN);
  inFlight = true;
  if (!covered) $('#netCount').textContent = 'Asking RIPE Atlas…';
  try {
    const data = await api.connectivity({ ...askableBounds(map, MARGIN), hours });
    covered = box;
    draw(data);
  } catch (err) {
    covered = null;
    $('#netCount').textContent = 'Could not load the probes';
    toast(`Internet connectivity unavailable: ${err.message}`, 'err');
  } finally {
    inFlight = false;
  }
}

function draw(data) {
  layer.clearLayers();
  const byState = Object.groupBy
    ? Object.groupBy(data.probes, (p) => p.state)
    : data.probes.reduce((o, p) => ((o[p.state] ??= []).push(p), o), {});
  for (const state of LAYERING) {
    for (const probe of byState[state] ?? []) {
      const look = STATES[state];
      L.circleMarker([probe.lat, probe.lon], {
        renderer: canvas,
        radius: look.radius,
        color: look.edge,
        weight: state === 'down' ? 2 : 1,
        fillColor: look.colour,
        fillOpacity: look.fill,
        opacity: state === 'up' ? 0.5 : 0.95,
        interactive: false,
      }).addTo(layer);
      // The click goes to an invisible circle a fingertip wide, not to the
      // dot. A canvas dot's own hit area is its radius -- three pixels for a
      // steady probe -- and Leaflet cancels a click if the map moved at all
      // while the button was down, so aiming at one and missing by a pixel
      // pans the map instead. The seismic panel learned this first.
      L.circleMarker([probe.lat, probe.lon], {
        renderer: canvas, radius: HIT_RADIUS,
        stroke: false, fill: true, fillOpacity: 0, interactive: true,
      }).on('click', () => openGraph(probe)).addTo(layer);
    }
  }
  $('#netCount').innerHTML = countLine(data);
  paintCountries(data.countries ?? []);
}

/** The headline: how many went down, out of how many were up. */
export function countLine(data) {
  const s = data.summary ?? {};
  const window = (store.config?.connectivity?.windows ?? {})[data.hours] ?? `${data.hours} h`;
  if (!data.count) return 'No RIPE Atlas probes in view';
  const wasUp = (s.up ?? 0) + (s.down ?? 0) + (s.back ?? 0);
  const parts = [
    `<b>${(s.up ?? 0) + (s.back ?? 0)}</b> of ${wasUp} online`,
    s.down
      ? `<b class="net-bad">${s.down}</b> went offline in the last ${window}`
        + ` (${(100 * (s.share_down ?? 0)).toFixed(1)}%)`
      : `none went offline in the last ${window}`,
  ];
  if (s.back) parts.push(`${s.back} came back`);
  return parts.join(' · ')
    + (data.capped ? '<br><span class="dim">nearest shown — zoom in for the rest</span>' : '');
}

/**
 * Countries where probes went down, worst first.
 *
 * The number is out of the probes that were up at the start of the window --
 * one that was already offline yesterday cannot go offline today, and counting
 * it would make a quiet country look half dark. Only countries with something
 * to report are listed: a list of every country at 0 % is a list of nothing.
 */
function paintCountries(rows) {
  const box = $('#netCountries');
  box.innerHTML = '';
  const shown = rows.filter((r) => r.down > 0);
  if (!shown.length) return;
  for (const row of shown) {
    const wasUp = row.up + row.down + row.back;
    box.append(el('div', { class: 'net-country' },
      el('span', { class: 'net-cc' }, row.country),
      el('span', { class: 'net-bar' },
        el('i', { style: `width:${Math.max(4, Math.round(100 * row.share_down))}%` })),
      el('span', { class: 'net-frac' },
        `${row.down} of ${wasUp} · ${(100 * row.share_down).toFixed(0)}%`)));
  }
}

// ── One probe's graph ──────────────────────────────────────────
//
// Like a seismograph's trace: click a probe and a window opens with its last
// day or week drawn -- online and offline along the top, the round trip to a
// root DNS server underneath -- and it redraws itself every minute while it
// is open. Several can be open at once, because two probes in the same town
// going dark at the same minute is the comparison worth making.

const WIN = 'net:';
const GRAPH_REFRESH_MS = 60000;
const graphs = new Map();     // window id -> the probe it is drawing
const ticking = new Map();    // window id -> its refresh timer
const tokens = new Map();     // window id -> the newest request, so a slow one cannot win

/** Whether a window is one of these graphs. */
export function isGraphWindow(id) {
  return String(id).startsWith(WIN);
}

const HEADLINES = {
  down: 'Went offline', back: 'Back online', gone: 'Offline', up: 'Online',
};

function openGraph(probe, { redraw = false } = {}) {
  const id = `${WIN}${probe.id}`;
  if (isOpen(id) && !redraw) {
    closeWindow(id);
    return;
  }
  const span = (store.config?.connectivity?.history ?? {})[historyHours]
    ?? `${historyHours} h`;
  const plot = el('div', { class: 'trace-plot' });
  const waiting = el('div', { class: 'trace-wait' },
    el('span', {}, `Drawing probe ${probe.id}…`),
    el('small', {}, 'RIPE Atlas is asked for its connection log and its pings.'));
  const stamp = el('div', { class: 'trace-live', 'data-live': '' }, 'fetching…');
  plot.append(waiting, stamp);

  if (redraw && isOpen(id)) {
    const existing = document.querySelector(`[data-win="${CSS.escape(id)}"]`);
    existing?.querySelector('.win-body')?.replaceChildren(plot);
    existing?.querySelector('.win-foot')?.replaceWith(
      el('div', { class: 'win-foot' }, graphNote(probe, span)));
  } else {
    graphs.set(id, probe);
    openWindow({
      id,
      title: `Probe ${probe.id}`,
      where: whereSaid(probe),
      link: `https://atlas.ripe.net/probes/${encodeURIComponent(probe.id)}/`,
      body: plot,
      sizes: [420, 'min(820px, calc(100vw - 40px))'],
      foot: graphNote(probe, span),
      onClose: () => {
        graphs.delete(id);
        stopTicking(id);
      },
    });
  }

  fetchGraph(id, probe, plot, waiting);
  stopTicking(id);
  ticking.set(id, setInterval(() => {
    if (!isOpen(id)) { stopTicking(id); return; }
    fetchGraph(id, probe, plot, waiting, { quiet: true });
  }, GRAPH_REFRESH_MS));
}

function stopTicking(id) {
  clearInterval(ticking.get(id));
  ticking.delete(id);
}

/** The line in the window's title bar: what it is doing, and since when. */
export function whereSaid(probe) {
  const ago = probe.since ? (Date.now() - new Date(probe.since).getTime()) / 3.6e6 : null;
  return [`${HEADLINES[probe.state] ?? ''}${ago != null ? ` ${fmt.duration(ago)} ago` : ''}`,
          probe.country, probe.asn ? `AS${probe.asn}` : null]
    .filter(Boolean).join(' · ');
}

/** Under the graph: whose probe, what the lines are, and what not to read in. */
function graphNote(probe, span) {
  return el('div', {},
    // The owner's own words about it, as text. Free text from a stranger.
    probe.description ? el('div', {}, probe.description) : null,
    el('div', {},
      `Last ${span}. Top: online (green) and offline (red), from RIPE's `
      + 'connection log. Below: round trip to k.root-servers.net in '
      + 'milliseconds, every few minutes; ticks along the floor are lost '
      + 'packets. A climbing line or a run of ticks before a red stretch is '
      + 'a connection failing rather than a plug being pulled.'),
    el('div', { class: 'dim' }, `${fmt.coord(probe.lon, probe.lat)} · blurred by RIPE`));
}

function clockNow() {
  return new Date().toLocaleTimeString(undefined,
    { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/**
 * Fetch the drawn graph, and say what went wrong when it does not arrive.
 *
 * Fetched rather than set as an <img src>, for the seismograph's reason: the
 * backend explains a failure in words, and an <img> throws them away.
 */
async function fetchGraph(id, probe, plot, waiting, { quiet = false } = {}) {
  const token = (tokens.get(id) ?? 0) + 1;
  tokens.set(id, token);
  const stamp = plot.querySelector('[data-live]');
  const url = api.probeGraphUrl({ id: probe.id, hours: historyHours,
                                  state: probe.state, since: probe.since });
  let blobUrl = null;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body.detail) detail = body.detail;
      } catch { /* not JSON */ }
      throw new Error(detail);
    }
    blobUrl = URL.createObjectURL(await res.blob());
  } catch (err) {
    if (tokens.get(id) !== token) return;
    // A refresh that fails keeps the graph that is there, and says so.
    if (quiet && plot.querySelector('img')) {
      if (stamp) {
        stamp.textContent = `not updating — ${err.message}`;
        stamp.classList.add('is-stale');
      }
      return;
    }
    waiting.replaceChildren(
      el('span', {}, `Probe ${probe.id} could not be drawn`),
      el('small', {}, err.message));
    stamp?.remove();
    return;
  }
  if (tokens.get(id) !== token) { URL.revokeObjectURL(blobUrl); return; }
  let image = plot.querySelector('img');
  const previous = image?.src;
  if (!image) {
    image = el('img', { alt: `Connectivity of RIPE Atlas probe ${probe.id}` });
    waiting.remove();
    plot.prepend(image);
  }
  // Each refresh's picture is handed back once its replacement has decoded,
  // or every minute leaks one for the life of the page.
  image.addEventListener('load', () => {
    if (previous && previous !== blobUrl) URL.revokeObjectURL(previous);
  }, { once: true });
  image.src = blobUrl;
  if (stamp) {
    stamp.textContent = `updated ${clockNow()}`;
    stamp.classList.remove('is-stale');
  }
}

// ── A whole area's graph ───────────────────────────────────────
//
// Drag a box and every probe inside it becomes one line: how much of the
// area's internet was up, through the last day, two days or week. Drawn by
// the backend, in a look of its own, with the mark on it -- it is the picture
// somebody will want to save and post, so it has a Save button.

// An area graph is asked for less often than a probe's: it is a few hundred
// probes' worth of history, and a regional line moves on the scale of tens
// of minutes, not seconds.
const AREA_REFRESH_MS = 5 * 60 * 1000;
const AREA_STYLE = { color: '#ff9f43', weight: 2, dashArray: '6,4', fill: true,
  fillColor: '#ff9f43', fillOpacity: 0.06, interactive: false };
let areas = 0;
const outlines = new Map();   // window id -> the box drawn on the map
// The bytes of the picture each area window is showing, kept for Save. Not
// fetched back from the image's blob: address -- the page's security policy
// does not let fetch read blob: URLs, and it should not be widened for this.
const pictures = new WeakMap();  // plot element -> Blob

function armArea() {
  if (!pickArea) return;
  if (!enabled) toggle();
  pickArea((bounds) => openArea(bounds));
}

/** The four edges, rounded to what the backend is asked for. */
export function edges(bounds) {
  return {
    west: Math.max(-180, bounds.getWest()), south: Math.max(-90, bounds.getSouth()),
    east: Math.min(180, bounds.getEast()), north: Math.min(90, bounds.getNorth()),
  };
}

function openArea(bounds, { redraw = false, id = null } = {}) {
  const winId = id ?? `${WIN}area:${(areas += 1)}`;
  const span = (store.config?.connectivity?.history ?? {})[historyHours] ?? `${historyHours} h`;
  const plot = el('div', { class: 'trace-plot net-area-plot' });
  const waiting = el('div', { class: 'trace-wait' },
    el('span', {}, 'Gathering every probe in the box…'),
    el('small', {}, 'Each one’s connection log is asked for, then added into one line.'));
  const stamp = el('div', { class: 'trace-live', 'data-live': '' }, 'fetching…');
  plot.append(waiting, stamp);
  const save = el('button', {
    class: 'net-save', type: 'button', disabled: true,
    onclick: () => saveArea(plot, winId),
  }, 'Save PNG');

  if (redraw && isOpen(winId)) {
    const existing = document.querySelector(`[data-win="${CSS.escape(winId)}"]`);
    existing?.querySelector('.win-body')?.replaceChildren(plot);
    existing?.querySelector('.win-foot')?.replaceWith(
      el('div', { class: 'win-foot' }, areaNote(span, save)));
  } else {
    graphs.set(winId, { area: bounds });
    outlines.set(winId, L.rectangle(bounds, AREA_STYLE).addTo(map));
    openWindow({
      id: winId,
      title: 'Area',
      where: 'every probe inside the box',
      body: plot,
      sizes: [560, 'min(1100px, calc(100vw - 40px))'],
      foot: areaNote(span, save),
      onClose: () => {
        graphs.delete(winId);
        stopTicking(winId);
        outlines.get(winId)?.remove();
        outlines.delete(winId);
      },
    });
  }

  fetchArea(winId, bounds, plot, waiting, save);
  stopTicking(winId);
  ticking.set(winId, setInterval(() => {
    if (!isOpen(winId)) { stopTicking(winId); return; }
    fetchArea(winId, bounds, plot, waiting, save, { quiet: true });
  }, AREA_REFRESH_MS));
}

function areaNote(span, save) {
  return el('div', {},
    el('div', {},
      `Last ${span}. The share of the probes in the box that were online, `
      + 'against the most that were up at once. Teal is nearly all of them; '
      + 'amber is the ordinary wobble of home connections; red is enough gone '
      + 'at once to be a regional outage rather than a few houses.'),
    el('div', { class: 'dim' },
      'Probes already offline before the window began are left out, so a '
      + 'long-dead probe does not drag the line down.'),
    el('div', { class: 'net-save-row' }, save));
}

/** Title-bar line for an area: its name, and the numbers that matter. */
export function areaSaid(headers) {
  const name = decodeURIComponent(headers.get('X-Name') ?? '');
  const now = headers.get('X-Now');
  const low = headers.get('X-Low');
  const probes = headers.get('X-Probes');
  return [name || null,
          now != null && now !== '' ? `now ${now}%` : null,
          low != null && low !== '' ? `lowest ${low}%` : null,
          probes ? `${probes} probes` : null].filter(Boolean).join(' · ');
}

async function fetchArea(id, bounds, plot, waiting, save, { quiet = false } = {}) {
  const token = (tokens.get(id) ?? 0) + 1;
  tokens.set(id, token);
  const stamp = plot.querySelector('[data-live]');
  const url = api.areaGraphUrl({ ...edges(bounds), hours: historyHours });
  let blobUrl = null;
  let said = '';
  try {
    const res = await fetch(url);
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body.detail) detail = body.detail;
      } catch { /* not JSON */ }
      throw new Error(detail);
    }
    said = areaSaid(res.headers);
    const blob = await res.blob();
    if (tokens.get(id) === token) pictures.set(plot, blob);
    blobUrl = URL.createObjectURL(blob);
  } catch (err) {
    if (tokens.get(id) !== token) return;
    if (quiet && plot.querySelector('img')) {
      if (stamp) {
        stamp.textContent = `not updating — ${err.message}`;
        stamp.classList.add('is-stale');
      }
      return;
    }
    waiting.replaceChildren(
      el('span', {}, 'That area could not be graphed'),
      el('small', {}, err.message));
    stamp?.remove();
    return;
  }
  if (tokens.get(id) !== token) { URL.revokeObjectURL(blobUrl); return; }
  let image = plot.querySelector('img');
  const previous = image?.src;
  if (!image) {
    image = el('img', { alt: 'Internet connectivity of the area drawn on the map' });
    waiting.remove();
    plot.prepend(image);
  }
  image.addEventListener('load', () => {
    if (previous && previous !== blobUrl) URL.revokeObjectURL(previous);
  }, { once: true });
  image.src = blobUrl;
  save.disabled = false;
  const where = document.querySelector(`[data-win="${CSS.escape(id)}"] .win-where`);
  if (where && said) where.textContent = said;
  if (stamp) {
    stamp.textContent = `updated ${clockNow()}`;
    stamp.classList.remove('is-stale');
  }
}

/** Save the picture exactly as drawn: the same pixels, mark and all. */
async function saveArea(plot, id) {
  const blob = pictures.get(plot);
  if (!blob) return;
  const name = (document.querySelector(`[data-win="${CSS.escape(id)}"] .win-where`)
    ?.textContent ?? 'area').split(' · ')[0];
  const slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'area';
  await savePicture(blob, `connectivity_${slug}_${historyHours}h_kaldockhi.png`,
                    { title: `Internet connectivity · ${name}`, what: 'The area graph' });
}
