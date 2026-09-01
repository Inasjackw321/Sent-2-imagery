// Where it is day and where it is night, drawn on the map.
//
// This belongs in an imagery app more than it might look. Every optical
// satellite here sees by reflected sunlight, so the night side of the earth is
// exactly the part none of them can photograph -- and the low sun near the
// terminator is where shadows are longest and a scene looks least like the
// scene taken a month earlier. Put next to the overpass prediction in the
// right-click panel, it answers the question that follows it: the satellite
// comes over at this time, will there be any light.
//
// It is drawn under everything else and takes no clicks. The shading is a
// property of the basemap, not of the data on top of it, and a rendered scene
// is from whatever date it was taken -- shading that by tonight's darkness
// would be saying something untrue about the picture.

import { $, el } from './ui.js';
import { subsolar, nightRing, sunTimes, elevation, TWILIGHT } from './sun.js';

// The bands, darkest last. Each is drawn over the one before, so the alpha
// accumulates and the night deepens towards the antisolar point without any
// of them having to know about the others.
const BANDS = [
  { altitude: 0, fill: 0.13, label: 'sunset' },
  { altitude: TWILIGHT.civil, fill: 0.13, label: 'civil twilight' },
  { altitude: TWILIGHT.nautical, fill: 0.13, label: 'nautical twilight' },
  { altitude: TWILIGHT.astronomical, fill: 0.13, label: 'night' },
];

// One point per degree of longitude. Finer is invisible at any zoom the whole
// terminator is on screen at, and this is redrawn every minute.
const STEP = 1;

// How often the layer catches up with the sky. The terminator moves a quarter
// of a degree a minute, which is under a pixel at the zoom you would watch it
// at, so this is as often as there is anything to see.
const TICK_MS = 60000;

// How far the time control can be pushed either way.
const SCRUB_HOURS = 24;

let map = null;
let pane = null;
let shapes = [];
let sunPin = null;
let enabled = false;
let timer = null;
// Minutes away from now. Zero means live, and the clock is followed.
let offset = 0;

const when = () => new Date(Date.now() + offset * 60000);

export function initDayNight(leafletMap) {
  map = leafletMap;
  // Above the basemap, below everything that carries information. Night is
  // context for the map underneath, not another dataset competing with the
  // imagery, the radar or the pins.
  pane = map.createPane('daynight');
  pane.style.zIndex = 250;
  pane.style.pointerEvents = 'none';
  buildDock();
}

// ── Drawing ────────────────────────────────────────────────────

function draw() {
  clear();
  if (!enabled) return;
  const at = when();

  for (const band of BANDS) {
    shapes.push(L.polygon(nightRing(at, band.altitude, STEP), {
      pane: 'daynight',
      interactive: false,
      stroke: band.altitude === 0,
      color: '#4a5a7a',
      weight: 1,
      opacity: 0.5,
      fillColor: '#0a0f1c',
      fillOpacity: band.fill,
    }).addTo(map));
  }

  // The subsolar point: the one place on earth with the sun straight up.
  const sun = subsolar(at);
  sunPin = L.marker([sun.lat, sun.lon], {
    pane: 'daynight',
    interactive: false,
    keyboard: false,
    icon: L.divIcon({
      className: 'sun-pin',
      html: '<svg viewBox="0 0 24 24" aria-hidden="true">'
        + '<circle cx="12" cy="12" r="5" fill="#ffd66b"/>'
        + '<g stroke="#ffd66b" stroke-width="1.6" stroke-linecap="round">'
        + '<path d="M12 1v3M12 20v3M1 12h3M20 12h3"/>'
        + '<path d="M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M19.8 4.2l-2.1 2.1M6.3 17.7l-2.1 2.1"/>'
        + '</g></svg>',
      iconSize: [28, 28], iconAnchor: [14, 14],
    }),
  }).addTo(map);
  shapes.push(sunPin);

  paintDock();
}

function clear() {
  for (const shape of shapes) shape.remove();
  shapes = [];
  sunPin = null;
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#dayNightDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'sun-toggle', id: 'sunToggle', onclick: toggle },
      el('span', { class: 'sun-mark' }, '☀'), 'Day & night'),
    el('div', { class: 'sun-body', id: 'sunBody', hidden: true },
      el('div', { class: 'sun-time', id: 'sunTime' }, '—'),
      el('input', {
        type: 'range', class: 'sun-scrub', id: 'sunScrub',
        min: String(-SCRUB_HOURS * 60), max: String(SCRUB_HOURS * 60), value: '0',
        step: '10',
        oninput: (e) => { offset = e.target.valueAsNumber; draw(); },
      }),
      el('div', { class: 'sun-row' },
        el('button', {
          class: 'sun-now', id: 'sunNow',
          onclick: () => { offset = 0; $('#sunScrub').value = '0'; draw(); },
        }, 'now'),
        el('span', { class: 'sun-sub', id: 'sunSub' }, '')),
      el('div', { class: 'sun-note' },
        'The night side is the half no optical satellite can photograph. '
        + 'Drag to see where the line falls at another time.')));
}

function toggle() {
  enabled = !enabled;
  if (enabled) {
    draw();
    // Only while it is showing: a timer redrawing an invisible layer every
    // minute is work nobody asked for.
    timer = setInterval(() => { if (!offset) draw(); }, TICK_MS);
  } else {
    clearInterval(timer);
    timer = null;
    clear();
  }
  paintDock();
}

function paintDock() {
  const button = $('#sunToggle');
  const body = $('#sunBody');
  if (!button || !body) return;
  button.classList.toggle('is-on', enabled);
  body.hidden = !enabled;
  if (!enabled) return;

  const at = when();
  const label = $('#sunTime');
  label.textContent = offset === 0
    ? `now — ${at.toLocaleTimeString()}`
    : `${at.toLocaleString()} (${offset > 0 ? '+' : ''}${(offset / 60).toFixed(1)}h)`;
  label.classList.toggle('is-live', offset === 0);

  const sun = subsolar(at);
  $('#sunSub').textContent = `sun overhead at ${sun.lat.toFixed(1)}°, ${sun.lon.toFixed(1)}°`;
}

/**
 * The sun half of the right-click panel.
 *
 * Built here rather than in map.js so the one place that knows how to phrase
 * "the sun is up for 16 hours here" is the same place that knows what the
 * layer is drawing.
 */
export function sunBlock(lat, lon, at = new Date()) {
  const times = sunTimes(lat, lon, at);
  const height = elevation(lat, lon, at);
  const clock = (d) => (d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—');

  const state = height > 0
    ? `sun up, ${height.toFixed(0)}° above the horizon`
    : height > TWILIGHT.civil
      ? 'twilight — too dark for optical imagery'
      : `dark, sun ${Math.abs(height).toFixed(0)}° below the horizon`;

  const rows = [
    el('div', { class: 'sun-state' }, state),
  ];
  if (times.polar === 'day') {
    rows.push(el('div', { class: 'sun-line' }, 'The sun does not set here today.'));
  } else if (times.polar === 'night') {
    rows.push(el('div', { class: 'sun-line' }, 'The sun does not rise here today.'));
  } else {
    rows.push(el('div', { class: 'sun-line' },
      el('span', {}, `↑ ${clock(times.sunrise)}`),
      el('span', {}, `↓ ${clock(times.sunset)}`),
      el('span', {}, `${times.hours.toFixed(1)} h of daylight`)));
  }
  // Local time, because the times above are shown in the viewer's clock and a
  // reader in another timezone would otherwise have no way to tell.
  rows.push(el('div', { class: 'sun-foot' },
    `Times in your own timezone, for ${at.toLocaleDateString()}.`));
  return el('div', { class: 'sun-block' }, ...rows);
}
