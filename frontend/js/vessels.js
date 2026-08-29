// Ships, drawn from their own broadcasts.
//
// AIS is a VHF transmission every vessel over a certain size makes
// continuously: who it is, where it is, how fast and which way. Receivers
// ashore hear it and some publish what they hear.
//
// Which makes it a natural third thing to have beside the imagery. A Sentinel-1
// pass shows a bright dot on the water and cannot tell you what it is; AIS
// names the ships that chose to be named. Where a dot has no ship against it,
// that is worth noticing on its own.

import { store } from './store.js';
import { $, el, toast, debounce } from './ui.js';
import { api } from './api.js';

// Colour by what the ship is for. Tankers and cargo are the two anyone is
// really looking for, so they get the colours that carry furthest.
const CATEGORIES = {
  tanker: { colour: '#ff8f5e', label: 'Tanker' },
  cargo: { colour: '#4cc2ff', label: 'Cargo' },
  passenger: { colour: '#3ee39f', label: 'Passenger' },
  fast: { colour: '#c78bff', label: 'High-speed' },
  special: { colour: '#ffd166', label: 'Fishing, tug, pilot' },
  wig: { colour: '#9aa7bd', label: 'Wing-in-ground' },
  other: { colour: '#8b98ad', label: 'Other' },
};

// Below this a ship is drawn as a dot rather than an arrow: a vessel that is
// not moving has no direction worth pointing, and a moored fleet of arrows all
// aimed at whatever heading they last reported reads as motion that is not
// happening.
const MOVING_KNOTS = 0.6;

// Panning inside the area already fetched should not fetch again. The pad is
// how much slack there is before it does.
const PAD = 0.25;

// Where the ships come from.
//
// Digitraffic is open and asks for nothing, and covers the Baltic. aisstream
// covers the world and wants a free API key. Neither is better; they answer
// different questions, and the panel says which is which rather than leaving
// an empty map to be interpreted.
const SOURCES = {
  digitraffic: {
    label: 'Baltic', needsKey: false,
    reach: 'Baltic & Gulf of Finland',
    note: 'Digitraffic — open, no key needed.',
  },
  aisstream: {
    label: 'Global', needsKey: true,
    reach: 'Anywhere, with a key',
    note: 'aisstream.io — asks the service at most once every five minutes.',
  },
};

let map = null;
let layer = null;
let enabled = false;
let showNames = false;
let covered = null;      // last box fetched, padded
let ships = [];
let refresher = null;
let source = 'digitraffic';
// How long before the global feed may be asked again. The backend enforces
// this; the panel only reports it, so a reload cannot talk it into going
// sooner.
let nextIn = 0;
// Whether the backend is holding a key. Tracked here as well as in the config
// because the config is read once at startup and would still say "no key" for
// the rest of the session after one was pasted in.
let keySaved = false;

export function initVessels(leafletMap) {
  map = leafletMap;
  map.createPane('vessels').style.zIndex = 470;
  buildDock();
  map.on('moveend', debounce(() => { if (enabled) maybeRefetch(); }, 350));
}

// ── Fetching ───────────────────────────────────────────────────

const boxOf = (bounds) => [
  bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth(),
];

function padded([w, s, e, n]) {
  const dx = (e - w) * PAD;
  const dy = (n - s) * PAD;
  return [w - dx, s - dy, e + dx, n + dy];
}

const inside = (box, outer) =>
  box[0] >= outer[0] && box[1] >= outer[1] && box[2] <= outer[2] && box[3] <= outer[3];

async function maybeRefetch() {
  const box = boxOf(map.getBounds());
  if (covered && inside(box, covered)) return;
  await load(box);
}

async function load(box) {
  const ask = padded(box);
  // The global feed listens to the stream for a few seconds before answering,
  // which is a long time to sit in front of a panel that says nothing.
  note(source === 'aisstream' ? 'Listening to the stream…' : 'Loading…');
  try {
    const data = await api.vessels(ask, source);
    covered = ask;
    nextIn = data.next_in ?? 0;
    ships = data.vessels ?? [];
    draw(data);
  } catch (err) {
    // The panel carries the detail; the toast only says something went wrong,
    // since the message can be a paragraph and a toast is not the place.
    toast('AIS: no ships loaded', 'err');
    note(err.message, 'bad');
  }
}

// ── Drawing ────────────────────────────────────────────────────

/**
 * A ship as an arrow pointing where it is going, or a dot if it is not going.
 *
 * Heading is where the bow points and course is where the ship is actually
 * travelling; they differ in a current or a crosswind. Heading is used when
 * the ship reports one, because that is what a shape on a map is showing.
 */
function marker(ship) {
  const spec = CATEGORIES[ship.category] ?? CATEGORIES.other;
  const moving = (ship.speed ?? 0) >= MOVING_KNOTS;
  const angle = ship.heading ?? ship.course ?? 0;

  const icon = moving
    ? L.divIcon({
      className: 'ship',
      html: `<svg viewBox="0 0 24 24" style="transform:rotate(${angle}deg)">
               <path d="M12 2 L18 21 L12 17 L6 21 Z" fill="${spec.colour}"
                     stroke="rgba(0,0,0,.55)" stroke-width="1.2"/></svg>`,
      iconSize: [18, 18], iconAnchor: [9, 9],
    })
    : L.divIcon({
      className: 'ship',
      html: `<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="6" fill="${spec.colour}"
               stroke="rgba(0,0,0,.55)" stroke-width="1.5"/></svg>`,
      iconSize: [13, 13], iconAnchor: [6.5, 6.5],
    });

  const pin = L.marker([ship.lat, ship.lon], {
    icon, pane: 'vessels', riseOnHover: true,
    title: ship.name || `MMSI ${ship.mmsi}`,
  });
  pin.bindPopup(() => describe(ship), { className: 'ship-popup', maxWidth: 260 });

  if (showNames && ship.name) {
    pin.bindTooltip(ship.name, {
      permanent: true, direction: 'right', offset: [10, 0], className: 'ship-name',
    });
  }
  return pin;
}

function describe(ship) {
  const spec = CATEGORIES[ship.category] ?? CATEGORIES.other;
  const rows = [
    ['Type', ship.type],
    ['Speed', ship.speed == null ? null : `${ship.speed} kn`],
    ['Course', ship.course == null ? null : `${Math.round(ship.course)}°`],
    ['Status', ship.status],
    ['Bound for', ship.destination],
    ['Draught', ship.draught ? `${ship.draught} m` : null],
    ['Length', ship.length ? `${ship.length} m` : null],
    ['MMSI', ship.mmsi],
    ['Call sign', ship.callsign],
    // How stale the position is. A ship that stopped broadcasting an hour ago
    // is not where this says it is, and the popup should not imply otherwise.
    ['Reported', ship.age_min == null ? null
      : ship.age_min < 1 ? 'just now' : `${Math.round(ship.age_min)} min ago`],
  ].filter(([, value]) => value != null && value !== '');

  return el('div', { class: 'ship-card' },
    el('div', { class: 'ship-head' },
      el('span', { class: 'ship-dot', style: `background:${spec.colour}` }),
      el('b', {}, ship.name || `MMSI ${ship.mmsi}`)),
    el('dl', {}, ...rows.flatMap(([label, value]) =>
      [el('dt', {}, label), el('dd', {}, String(value))])),
  ).outerHTML;
}

function draw(data) {
  layer?.remove();
  layer = L.layerGroup(ships.map(marker), { pane: 'vessels' });
  layer.addTo(map);

  if (data && !data.covered) {
    note(data.note ?? 'No AIS coverage here.', 'warn');
    return;
  }
  const shown = ships.length;
  const total = data?.count ?? shown;
  if (shown) {
    note(`${shown} vessel${shown === 1 ? '' : 's'}${total > shown ? ` of ${total}` : ''}`
      + `${data?.demo ? ' — synthetic' : ''}${data?.cached ? ' · from the last look' : ''}`);
  } else if (data?.messages === 0) {
    // Nothing arrived at all, which is a different problem from an empty sea
    // and wants a different answer from whoever is looking at it.
    note('Connected, but nothing came through. Try a wider area.', 'warn');
  } else {
    note('No vessels broadcasting here right now.', 'warn');
  }
  legend();
}

function note(text, kind = '') {
  const box = $('#vesselNote');
  if (!box) return;
  box.className = `vessel-status${kind ? ` is-${kind}` : ''}`;
  box.textContent = text;
}

function legend() {
  const host = $('#vesselKey');
  if (!host) return;
  const seen = new Set(ships.map((s) => s.category));
  host.replaceChildren(...Object.entries(CATEGORIES)
    .filter(([key]) => seen.has(key))
    .map(([, spec]) => el('span', { class: 'ship-swatch' },
      el('i', { style: `background:${spec.colour}` }), spec.label)));
}

// ── Turning it on and off ──────────────────────────────────────

async function toggle() {
  enabled = !enabled;
  paintDock();
  if (!enabled) {
    clearInterval(refresher);
    layer?.remove();
    layer = null;
    covered = null;
    ships = [];
    return;
  }
  await load(boxOf(map.getBounds()));
  // Half a minute suits the keyless Baltic feed, which is a plain HTTP
  // request against a cache. The global one has a five-minute floor of its
  // own in the backend, so asking more often would only be answered from
  // what it already has -- there is no point pretending otherwise.
  const every = source === 'aisstream' ? 5 * 60 * 1000 : 30000;
  refresher = setInterval(() => { covered = null; maybeRefetch(); }, every);
}

// ── The panel ──────────────────────────────────────────────────

async function setSource(next) {
  if (next === source) return;
  source = next;
  covered = null;
  clearInterval(refresher);
  buildDock();
  if (!enabled) return;
  await load(boxOf(map.getBounds()));
  const every = source === 'aisstream' ? 5 * 60 * 1000 : 30000;
  refresher = setInterval(() => { covered = null; maybeRefetch(); }, every);
}

async function saveKey(value) {
  try {
    const out = await api.aisKey(value);
    keySaved = out.set;
    buildDock();
    note(out.set ? 'Key saved. Listening…' : 'Key cleared.');
    if (out.set && enabled) { covered = null; await load(boxOf(map.getBounds())); }
  } catch (err) {
    note(err.message, 'bad');
  }
}

function buildDock() {
  const dock = $('#vesselDock');
  if (!dock) return;
  const spec = SOURCES[source];
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'vessel-toggle', id: 'vesselToggle', onclick: toggle },
      el('span', { class: 'vessel-mark' }, '⛴'), 'Ships'),
    el('div', { class: 'vessel-body', id: 'vesselBody', hidden: !enabled },

      // A segmented control rather than two buttons that happen to sit
      // together: one track, one lit segment, so which of the two is in force
      // reads at a glance instead of having to be compared.
      el('div', { class: 'seg', role: 'tablist' },
        ...Object.entries(SOURCES).map(([key, opt]) =>
          el('button', {
            class: `seg-item${key === source ? ' is-on' : ''}`,
            dataset: { source: key }, role: 'tab',
            'aria-selected': String(key === source),
            title: opt.reach,
            onclick: () => setSource(key),
          }, opt.label))),

      el('div', { class: 'vessel-reach', id: 'vesselReach' }, spec.reach),

      // The key, and only when the chosen feed wants one.
      ...(spec.needsKey ? [
        el('div', { class: 'keyfield', id: 'keyField' },
          el('input', {
            type: 'password', id: 'aisKey', autocomplete: 'off', spellcheck: false,
            placeholder: keySaved ? 'Key saved' : 'Paste your API key',
            onkeydown: (e) => { if (e.key === 'Enter') saveKey(e.target.value); },
            oninput: () => paintKeyButton(),
          }),
          el('button', {
            class: 'keyfield-go', id: 'aisKeySave', type: 'button',
            onclick: () => saveKey($('#aisKey').value),
          }, keySaved ? 'Change' : 'Save')),
        el('div', { class: 'vessel-hint' },
          keySaved ? el('span', { class: 'ok-dot' }, '● key saved') : '',
          el('a', {
            href: 'https://aisstream.io/', target: '_blank', rel: 'noopener noreferrer',
          }, 'Get a free key'),
          ' · kept in memory only'),
        // When the map is empty, guessing stops being useful. This connects
        // once over the Dover Strait -- busy at every hour -- and says what
        // actually happened.
        el('button', {
          class: 'vessel-test', id: 'aisTest', type: 'button',
          onclick: runTest,
        }, 'Test the connection'),
      ] : []),

      el('label', { class: 'vessel-check' },
        el('input', {
          type: 'checkbox', checked: showNames,
          onchange: (e) => { showNames = e.target.checked; draw(null); },
        }), 'Show names'),

      el('div', { class: 'vessel-key', id: 'vesselKey' }),
      el('div', { class: 'vessel-status', id: 'vesselNote' }, enabled ? 'Loading…' : ''),
      el('div', { class: 'vessel-where', id: 'vesselWhere' })),
  );
  // The button starts out with nothing to save, and should say so.
  paintKeyButton();
  paintDock();
}

async function runTest() {
  const button = $('#aisTest');
  if (button) { button.disabled = true; button.textContent = 'Testing…'; }
  note('Connecting over the Dover Strait…');
  try {
    const out = await api.aisTest();
    note(out.detail, out.ok ? '' : 'bad');
  } catch (err) {
    note(err.message, 'bad');
  } finally {
    if (button) { button.disabled = false; button.textContent = 'Test the connection'; }
  }
}

/** The save button only offers itself when there is something to save. */
function paintKeyButton() {
  const field = $('#aisKey');
  const button = $('#aisKeySave');
  if (!field || !button) return;
  button.disabled = !field.value.trim();
}

function paintDock() {
  const button = $('#vesselToggle');
  const body = $('#vesselBody');
  if (!button || !body) return;
  button.classList.toggle('is-on', enabled);
  body.hidden = !enabled;
  keySaved = keySaved || Boolean(store.config?.vessels?.global_key_set);
  const where = $('#vesselWhere');
  if (where && enabled) {
    // Say where the feed reaches, up front. A blank map over the Atlantic is
    // otherwise indistinguishable from a broken layer.
    const wait = nextIn > 0 && source === 'aisstream'
      ? ` Next look in ${Math.ceil(nextIn / 60)} min.` : '';
    where.textContent = SOURCES[source].note + wait;
  }
}
