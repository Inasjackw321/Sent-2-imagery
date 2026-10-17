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
import { $, el, toast, fmt, debounce, askableBounds } from './ui.js';
import { POPUP } from './fires.js';

let map = null;
let layer = null;
let canvas = null;
let enabled = false;
let hours = 6;
let inFlight = false;
let covered = null;
let timer = null;
const MARGIN = 0.35;

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

export function initConnectivity(leafletMap) {
  map = leafletMap;
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
      el('div', { class: 'net-count', id: 'netCount' }, 'Nothing loaded yet'),
      el('div', { class: 'net-countries', id: 'netCountries' }),
      el('div', { class: 'net-key' },
        ...['down', 'back', 'gone', 'up'].map((key) => el('div', { class: 'net-key-row' },
          el('span', { class: 'net-dot', style: `background:${STATES[key].colour}` }),
          STATES[key].label))),
      el('div', { class: 'net-note' },
        'RIPE Atlas · each dot is one volunteer’s probe, wired into their '
        + 'network. One going dark is one site; many in one area at once is an '
        + 'outage. Positions are blurred by RIPE by up to a few kilometres.')),
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
      }).bindPopup(() => popup(probe), POPUP).addTo(layer);
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

function popup(probe) {
  const look = STATES[probe.state];
  const ago = probe.since ? (Date.now() - new Date(probe.since).getTime()) / 3.6e6 : null;
  const headline = {
    down: 'Went offline', back: 'Back online', gone: 'Offline', up: 'Online',
  }[probe.state];
  return el('div', { class: 'net-popup' },
    el('div', { class: 'net-popup-head', style: `color:${look.colour}` },
      `${headline}${ago != null ? ` ${fmt.duration(ago)} ago` : ''}`),
    probe.since ? el('div', { class: 'dim' }, fmt.when(probe.since)) : null,
    // The owner's own words about it, as text. Free text from a stranger.
    probe.description ? el('div', {}, probe.description) : null,
    el('div', { class: 'dim' },
      [probe.country, probe.asn ? `AS${probe.asn}` : null,
       probe.anchor ? 'anchor' : null].filter(Boolean).join(' · ')),
    el('div', { class: 'dim' }, `${fmt.coord(probe.lon, probe.lat)} · blurred by RIPE`),
    el('div', {},
      el('a', { href: `https://atlas.ripe.net/probes/${encodeURIComponent(probe.id)}/`,
                target: '_blank', rel: 'noopener noreferrer' },
        `Probe ${probe.id} on RIPE Atlas ↗`)),
  ).outerHTML;
}
