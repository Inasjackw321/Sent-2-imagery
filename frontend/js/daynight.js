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

// The bands, lightest first. Each is drawn over the one before, so the alpha
// accumulates and the night deepens towards the antisolar point without any
// of them having to know about the others.
//
// Four steps -- one per named twilight -- drew four visible edges, and the sky
// has no edges in it. Stepping through the same eighteen degrees in twice as
// many stops, at less than half the opacity each, is the same total darkness
// arriving as a gradient rather than as stairs.
// The count follows the zoom as well. Zoomed right out the whole eighteen
// degrees of twilight is only a few dozen pixels wide, so ten steps buys no
// visible smoothness and costs ten sets of antialiased edges stacked on top of
// each other -- which is what the remaining stripes were. Zoomed in the ramp
// is most of the screen and every step earns its place.
function bandsFor(zoom) {
  const count = Math.max(4, Math.min(10, Math.round(zoom * 1.6)));
  // The same total darkness however many steps it arrives in: each one is
  // thinned so that stacking them all comes to the same place.
  const fill = 1 - (1 - TOTAL_DARK) ** (1 / count);
  const out = [];
  for (let i = 0; i < count; i += 1) {
    out.push({ altitude: (TWILIGHT.astronomical * i) / (count - 1), fill });
  }
  return out;
}

// How dark the deepest night gets, all bands together.
const TOTAL_DARK = 0.42;

/**
 * How far apart to sample the terminator, in degrees of longitude.
 *
 * Chosen from the zoom so the points land roughly ten pixels apart whatever
 * the scale, and not from taste. Too coarse and the curve shows its corners
 * when you zoom in on it. Too fine is worse and less obvious: with vertices
 * closer together than a pixel, each band's antialiased edge leaves a
 * sub-pixel seam, and ten of them stacked turn those seams into vertical
 * stripes ruled down the whole night side. Measured at zoom 2: sampling every
 * half degree gave 202 reversals along one scanline, every five degrees gave
 * ten.
 */
function stepFor(zoom) {
  const pixelsPerDegree = (256 * 2 ** zoom) / 360;
  return Math.min(6, Math.max(0.25, 10 / pixelsPerDegree));
}

// How often the layer catches up with the sky. The terminator moves a quarter
// of a degree a minute, which is under a pixel at the zoom you would watch it
// at, so this is as often as there is anything to see.
const TICK_MS = 60000;

let map = null;
let pane = null;
let shapes = [];
let sunPin = null;
let enabled = false;
let timer = null;

// Always now. This used to be draggable a day either way, which sounds useful
// and is not: the whole worth of the layer is that it is a fact about the
// present, and a shading that might be showing any time at all cannot be read
// at a glance. Anything that can be set wrong will be, and then quietly
// misread as the truth.
const when = () => new Date();

export function initDayNight(leafletMap) {
  map = leafletMap;
  // Above the basemap, below everything that carries information. Night is
  // context for the map underneath, not another dataset competing with the
  // imagery, the radar or the pins.
  pane = map.createPane('daynight');
  pane.style.zIndex = 250;
  pane.style.pointerEvents = 'none';
  buildDock();
  // The sampling depends on the scale, so a zoom needs the curve rebuilt --
  // otherwise zooming in shows the corners it was drawn with, and zooming out
  // brings back the stripes it was drawn to avoid.
  map.on('zoomend', () => { if (enabled) draw(); });
}

// ── Drawing ────────────────────────────────────────────────────

function draw() {
  clear();
  if (!enabled) return;
  const at = when();

  const zoom = map.getZoom();
  const step = stepFor(zoom);
  // How many copies of the world are on screen at this zoom, so the shading
  // covers all of them rather than stopping at the date line on each.
  const worlds = Math.ceil(map.getSize().x / (256 * 2 ** zoom));
  const wraps = Math.min(5, Math.max(1, worlds + 1));
  for (const band of bandsFor(zoom)) {
    shapes.push(L.polygon(nightRing(at, band.altitude, step, wraps), {
      pane: 'daynight',
      interactive: false,
      // Only the outermost band is outlined, and faintly: the terminator is a
      // real line worth seeing, and the eight behind it are a gradient that
      // stops being one the moment any of them is given an edge.
      // The terminator itself is worth a line; the bands behind it are a
      // gradient and stop being one the moment any of them is given an edge.
      // Kept faint -- at a low zoom the curve runs nearly north-south for
      // thousands of miles, and a firm line there reads as a border.
      stroke: band.altitude === 0,
      color: '#9fb6e0',
      weight: 1,
      opacity: 0.16,
      fillColor: '#050a16',
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
      el('div', { class: 'sun-sub', id: 'sunSub' }, ''),
      // A reading of the bands rather than a control: the shading means
      // something specific, and four words of key save guessing at it.
      el('div', { class: 'sun-key' },
        el('span', { class: 'sun-key-row' },
          el('i', { style: 'background:rgba(5,10,22,.10)' }), 'dusk'),
        el('span', { class: 'sun-key-row' },
          el('i', { style: 'background:rgba(5,10,22,.28)' }), 'twilight'),
        el('span', { class: 'sun-key-row' },
          el('i', { style: 'background:rgba(5,10,22,.46)' }), 'night')),
      el('div', { class: 'sun-note' },
        'The dark half is the part no optical satellite can photograph. '
        + 'Right-click anywhere for its sunrise, sunset and day length.')));
}

function toggle() {
  enabled = !enabled;
  if (enabled) {
    draw();
    // Only while it is showing: a timer redrawing an invisible layer every
    // minute is work nobody asked for.
    timer = setInterval(draw, TICK_MS);
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
  $('#sunTime').textContent = at.toLocaleTimeString();

  const sun = subsolar(at);
  const ns = sun.lat >= 0 ? 'N' : 'S';
  const ew = sun.lon >= 0 ? 'E' : 'W';
  $('#sunSub').textContent = `sun overhead at ${Math.abs(sun.lat).toFixed(1)}°${ns} `
    + `${Math.abs(sun.lon).toFixed(1)}°${ew}`;
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
