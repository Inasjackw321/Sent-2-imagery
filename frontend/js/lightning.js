// Lightning, from NASA's GOES Lightning Mappers.
//
// Every GOES satellite carries a camera watching the whole disc of the earth
// five hundred times a second for the flash of a stroke through the cloud
// tops. NASA republishes it through GIBS as map tiles -- the same service the
// daily cloud mosaic already comes from -- so this is a tile layer and nothing
// more: no account, no key, no stream to hold open.
//
// The layer names are asked for rather than written down. GIBS publishes its
// own catalogue and the backend reads the GLM entries out of it, so a rename
// changes what appears in the picker instead of quietly serving nothing.
//
// The thing to be clear about is where it can see. GOES sits over the
// Americas, and a geostationary camera sees less than a hemisphere: Europe,
// Africa and Asia are over its horizon and stay blank whatever the weather is
// doing there. An empty map that means "quiet" and one that means "not
// watched from here" look identical, so the panel always says which.

import { api } from './api.js';
import { $, el } from './ui.js';

// Roughly where each satellite can see anything at all: a geostationary
// camera at this height is useful to about 65 degrees from the point beneath
// it, and hopeless past that. Used only to tell someone why their map is
// empty, never to hide the layer.
const SEEN_FROM = {
  east: { lon: -75.2, label: 'GOES-East' },
  west: { lon: -137.2, label: 'GOES-West' },
};
const REACH_DEGREES = 65;

// How often to ask GIBS for a newer frame. The product is published every few
// minutes; this is the cheapest cadence that keeps up with it.
const REFRESH_MS = 120000;

let map = null;
let layer = null;
let enabled = false;
let catalogue = null;
let chosen = null;
let opacity = 0.85;
let timer = null;
let problem = '';
// Until somebody picks a satellite themselves, the map picks the one looking
// at whatever is on screen. After that it stays put: an automatic choice that
// overrides a deliberate one is worse than no automatic choice at all.
let manual = false;

export function initLightning(leafletMap) {
  map = leafletMap;
  // Above the radar: rain is the context, the strike is the event, and the
  // point of having both is to see one inside the other.
  map.createPane('lightning').style.zIndex = 465;
  map.getPane('lightning').style.pointerEvents = 'none';
  buildDock();
  map.on('moveend', () => {
    if (!enabled) return;
    // Panning from Florida to California is a change of satellite, not just of
    // view -- so the choice is remade, and only redrawn if it actually moved.
    if (!manual) {
      const next = pick();
      if (next && next !== chosen) { chosen = next; buildDock(); show(); return; }
    }
    paintDock();
  });
}

// ── The layer ──────────────────────────────────────────────────

/** The tile URL for one catalogue entry, with the placeholders filled in. */
function urlFor(entry) {
  return catalogue.template
    .replace('{layer}', entry.id)
    // GIBS wants a time or the word default; a layer with no time dimension
    // has nothing to substitute and takes the latter.
    .replace('{time}', entry.time_default ?? 'default')
    .replace('{matrix}', entry.matrix ?? 'GoogleMapsCompatible_Level7')
    // Leaflet fills in {z}, {y} and {x} itself and throws on anything else it
    // is handed, so the extension has to be substituted here rather than left
    // in the template for it to trip over.
    .replace('{fmt}', entry.format ?? 'png');
}

function show() {
  layer?.remove();
  layer = null;
  if (!enabled || !chosen) return;

  let missing = 0;
  layer = L.tileLayer(urlFor(chosen), {
    pane: 'lightning',
    opacity,
    // GIBS stops at the zoom the product is gridded to; past that the tiles
    // are stretched rather than withheld, so the layer stays on screen when
    // you zoom into a storm instead of vanishing.
    maxNativeZoom: 7,
    maxZoom: 19,
    className: 'bolt-tiles',
    attribution: catalogue.attribution,
  });
  // A missing tile over the ocean is ordinary; every tile missing means the
  // layer name is wrong or the product has moved, and that is worth saying
  // rather than showing an empty map and letting it read as calm weather.
  layer.on('tileerror', () => {
    missing += 1;
    if (missing === 8) { problem = `${chosen.id} is not answering.`; paintDock(); }
  });
  layer.on('tileload', () => {
    if (!problem) return;
    problem = '';
    paintDock();
  });
  layer.addTo(map);
}

async function load() {
  try {
    catalogue = await api.lightning();
    // A miss that says only "none" leaves nowhere to go. If GIBS answered with
    // a catalogue but nothing in it looked like lightning, say how big the
    // catalogue was and name what came closest -- that is the difference
    // between a dead end and a next step.
    problem = catalogue.layers.length ? '' : noneFound(catalogue);
    chosen = pick();
    buildDock();
    show();
  } catch (err) {
    problem = err.message;
    paintDock();
  }
}

/** What to say when GIBS answered but had no lightning layer in it. */
function noneFound(got) {
  const size = got.catalogue_size ? ` of ${got.catalogue_size}` : '';
  if (got.nearby?.length) {
    return `No lightning layer${size} at NASA GIBS. Closest names there: `
      + `${got.nearby.slice(0, 5).join(', ')}.`;
  }
  return `NASA GIBS is serving no lightning layer${size} in this projection.`;
}

/** Whichever satellite is looking at the middle of the map. */
function pick() {
  if (!catalogue?.layers?.length) return null;
  const centre = map.getCenter().lng;
  const distance = (entry) => {
    const seen = SEEN_FROM[entry.satellite];
    if (!seen) return 999;
    return Math.abs(((centre - seen.lon + 540) % 360) - 180);
  };
  return [...catalogue.layers].sort((a, b) => distance(a) - distance(b))[0];
}

/** How far the map's centre is from the nearest satellite's view. */
function reach() {
  const seen = SEEN_FROM[chosen?.satellite];
  if (!seen) return null;
  const centre = map.getCenter().lng;
  const away = Math.abs(((centre - seen.lon + 540) % 360) - 180);
  return { away, label: seen.label, inside: away <= REACH_DEGREES };
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#lightningDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'bolt-toggle', id: 'boltToggle', onclick: toggle },
      el('span', { class: 'bolt-mark' }, '⚡'), 'Lightning'),
    el('div', { class: 'bolt-body', id: 'boltBody', hidden: !enabled },
      catalogue?.layers?.length > 1
        ? el('div', { class: 'bolt-windows' },
          ...catalogue.layers.map((entry) => el('button', {
            class: `bolt-window${entry === chosen ? ' is-on' : ''}`,
            dataset: { layer: entry.id },
            title: entry.title,
            onclick: () => { chosen = entry; manual = true; buildDock(); show(); },
          }, SEEN_FROM[entry.satellite]?.label ?? entry.id.slice(0, 12))))
        : null,
      el('div', { class: 'bolt-count', id: 'boltCount' }, 'Loading…'),
      el('div', { class: 'bolt-key' },
        el('span', { class: 'bolt-key-row' },
          el('i', { style: 'background:#2b1d5e' }), 'few'),
        el('span', { class: 'bolt-key-row' },
          el('i', { style: 'background:#c86bff' }), 'many flashes')),
      el('div', { class: 'bolt-note', id: 'boltNote' }, '')));
  paintDock();
}

function toggle() {
  enabled = !enabled;
  $('#boltToggle').classList.toggle('is-on', enabled);
  $('#boltBody').hidden = !enabled;
  if (enabled) {
    if (catalogue) { chosen ??= pick(); show(); } else load();
    // Re-made rather than refreshed: the time is baked into the URL, so a
    // newer frame is a different layer.
    timer = setInterval(() => {
      if (!enabled) return;
      if (!manual) chosen = pick();
      load();
    }, REFRESH_MS);
  } else {
    clearInterval(timer);
    timer = null;
    layer?.remove();
    layer = null;
  }
  paintDock();
}

function paintDock() {
  const count = $('#boltCount');
  const text = $('#boltNote');
  if (!count || !text || !enabled) return;

  const view = reach();
  count.textContent = chosen
    ? (view && !view.inside
      ? `Nothing to see here — ${view.label} cannot look this far`
      : `${chosen.title.replace(/ \(demo\)$/, '')}`)
    : 'No lightning layer available';

  const lines = [];
  if (view && !view.inside) {
    lines.push(catalogue?.coverage ?? '');
  } else if (catalogue?.attribution === 'synthetic') {
    lines.push('Demo mode: the tiles are stand-ins.');
  } else if (chosen) {
    lines.push(`${catalogue.attribution}. Flash extent density — how many `
      + 'flashes touched each cell, not single strokes.');
  }
  if (problem) lines.push(problem);
  text.textContent = lines.filter(Boolean).join(' ');
}
