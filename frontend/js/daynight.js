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
import { subsolar, nightRings, terminator, tile, withinMap, sunTimes, elevation,
  TWILIGHT } from './sun.js';

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
 * How far apart to sample the curve, in degrees of bearing around it.
 *
 * Chosen from the zoom so the points land roughly ten pixels apart whatever
 * the scale, and not from taste. Too coarse and the curve shows its corners
 * when you zoom in on it. Too fine is worse and less obvious: with vertices
 * closer together than a pixel, each band's antialiased edge leaves a
 * sub-pixel seam, and several of them stacked turn those seams into vertical
 * stripes ruled down the night side.
 */
function stepFor(zoom) {
  const pixelsPerDegree = (256 * 2 ** zoom) / 360;
  return Math.min(6, Math.max(0.4, 10 / pixelsPerDegree));
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
  // What the map can actually see, rather than a count of copies of the
  // world. Leaflet lets you pan sideways for ever, so the shading is tiled
  // over the longitudes on screen -- a shape tiled around the meridian runs
  // out partway across a view of the Pacific.
  const bounds = map.getBounds();
  const span = { west: bounds.getWest() - 20, east: bounds.getEast() + 20 };

  for (const band of bandsFor(zoom)) {
    // A list of rings rather than one: near an equinox the deeper bands do
    // not reach a pole, so night at that depth is a closed ring lying across
    // the globe with daylight on both sides of it -- and at low zoom there is
    // one copy of the whole thing per copy of the world on screen. Handed to
    // Leaflet as a multipolygon, which is what several separate outlines are.
    const rings = nightRings(at, band.altitude, step, span);
    if (!rings.length) continue;
    shapes.push(L.polygon(rings.map((ring) => [ring]), {
      pane: 'daynight',
      interactive: false,
      // No outline on any of them. The bands are a gradient and stop being
      // one the moment any of them is given an edge -- and where a band is
      // closed along the top or bottom of the projection, that closing edge
      // is not a real line in the sky at all: it would draw a hairline at 84
      // degrees south straight across the daylight. The terminator itself is
      // worth seeing and is drawn below, as the curve alone.
      stroke: false,
      fillColor: '#050a16',
      fillOpacity: band.fill,
    }).addTo(map));
  }

  // The terminator, as a line.
  //
  // Drawn from the curve rather than taken from the shaded polygon's outline,
  // because the polygon has edges in it that the sky does not: the side it is
  // closed along, and the seams where one copy of the world meets the next.
  // Kept faint -- at a low zoom it runs nearly north-south for thousands of
  // miles, and a firm line there reads as a border.
  for (const curve of curves(terminator(at, 0, step), span)) {
    shapes.push(L.polyline(curve, {
      pane: 'daynight',
      interactive: false,
      color: '#9fb6e0',
      weight: 1,
      opacity: 0.16,
      fill: false,
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

/** One copy of a curve per turn of longitude on screen. */
function curves(curve, span) {
  // Only the parts of it the map can place. The rest is pinned to the edge
  // by the projection and draws as a line along the bottom of the world,
  // which is nowhere near where the terminator is.
  return tile(curve, span.west, span.east).flatMap((one) => withinMap(one));
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
