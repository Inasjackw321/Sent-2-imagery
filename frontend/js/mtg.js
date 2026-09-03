// Lightning from Meteosat Third Generation, over Europe and Africa.
//
// MTG-I1 sits over the Gulf of Guinea with four cameras watching the whole
// disc a thousand times a second for the flash of a stroke. That makes it the
// instrument for this half of the world -- the half the American satellites
// cannot see, and the half this app is mostly pointed at.
//
// EUMETSAT serves it as ordinary WMS with no account and no key, so this is a
// map layer and nothing more. The layer names come from their catalogue rather
// than from here, and the backend only offers one whose newest frame is recent
// enough to be about now: an old product is refused by its own timestamps
// rather than by anything as unreliable as its name.
//
// The flashes go over the infrared cloud tops from the same satellite, because
// on their own they are dots in a void; over the cloud they are inside the
// storm that made them.

import { api } from './api.js';
import { $, el } from './ui.js';

// The instrument's own reach. It is used to explain an empty map, never to
// switch the layer off -- a blank that means "quiet" and one that means "over
// the horizon" look identical, and only one of them is worth waiting out.
const SUB_SATELLITE_LON = 0;
const REACH_DEGREES = 70;

// How often to ask for a newer frame. The accumulated products publish every
// few minutes; this keeps up without leaning on somebody else's server.
const REFRESH_MS = 120000;

let map = null;
let flashes = null;
let backdrop = null;
let showCloud = true;
let enabled = false;
let catalogue = null;
let chosen = null;
let timer = null;
let problem = '';

export function initMTG(leafletMap) {
  map = leafletMap;
  // Above the radar: rain is the context, the strike is the event, and the
  // point of having both is to see one inside the other.
  map.createPane('mtg').style.zIndex = 465;
  map.getPane('mtg').style.pointerEvents = 'none';
  buildDock();
  map.on('moveend', () => { if (enabled) paintDock(); });
}

// ── The layers ─────────────────────────────────────────────────

function wmsLayer(entry, { over, opacity, className }) {
  return L.tileLayer.wms(catalogue.wms, {
    layers: entry.id,
    format: 'image/png',
    transparent: true,
    // 1.3.0 is what the capabilities are read at, and mixing versions is how
    // you get axis order wrong and the picture mirrored.
    version: '1.3.0',
    // The frame the catalogue says is newest. Without it EUMETSAT serves
    // whatever its own default is, which is not necessarily the latest.
    ...(entry.time_default ? { time: entry.time_default } : {}),
    pane: 'mtg',
    opacity,
    zIndex: over,
    className,
    attribution: catalogue.attribution,
  });
}

/** The infrared from the same satellite, for the flashes to sit on. */
const cloudFor = () => catalogue?.imagery?.[0] ?? null;

function show() {
  flashes?.remove();
  backdrop?.remove();
  flashes = backdrop = null;
  if (!enabled) return;

  const cloud = showCloud ? cloudFor() : null;
  if (cloud) {
    backdrop = wmsLayer(cloud, { over: 1, opacity: 0.5, className: 'mtg-cloud' });
    backdrop.addTo(map);
  }
  if (!chosen) return;

  let missing = 0;
  flashes = wmsLayer(chosen, { over: 2, opacity: 0.9, className: 'mtg-flash' });
  // Missing tiles off the edge of the disc are ordinary -- most of the world
  // is off the edge of the disc. A run of them everywhere means the layer has
  // moved, and that is worth saying rather than showing a blank.
  flashes.on('tileerror', () => {
    missing += 1;
    if (missing === 12) { problem = `${chosen.id} is not answering.`; paintDock(); }
  });
  flashes.on('tileload', () => {
    if (!problem) return;
    problem = '';
    paintDock();
  });
  flashes.addTo(map);
}

async function load() {
  try {
    catalogue = await api.mtg();
    problem = catalogue.live.length ? '' : noneLive(catalogue);
    chosen = catalogue.live.find((e) => e === chosen) ?? catalogue.live[0] ?? null;
    buildDock();
    show();
  } catch (err) {
    problem = err.message;
    paintDock();
  }
}

/** What to say when EUMETSAT answered but nothing it holds is current. */
function noneLive(got) {
  const hours = got.live_within_hours ?? 6;
  if (got.stale?.length) {
    const oldest = got.stale
      .map((e) => (e.age_minutes == null ? 'undated' : `${Math.round(e.age_minutes / 60)} h old`))
      .slice(0, 3).join(', ');
    return `EUMETSAT has MTG lightning but nothing from the last ${hours} h `
      + `(${oldest}). Shown only when it is current.`;
  }
  return `No MTG lightning layer at EUMETSAT${
    got.catalogue_size ? ` among ${got.catalogue_size} layers` : ''}.`;
}

/** How far the map's centre is from the middle of the satellite's disc. */
function reach() {
  const away = Math.abs(((map.getCenter().lng - SUB_SATELLITE_LON + 540) % 360) - 180);
  return { away, inside: away <= REACH_DEGREES };
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#mtgDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'bolt-toggle', id: 'boltToggle', onclick: toggle },
      el('span', { class: 'bolt-mark' }, '⚡'), 'Lightning'),
    el('div', { class: 'bolt-body', id: 'boltBody', hidden: !enabled },
      catalogue?.live?.length > 1
        ? el('div', { class: 'bolt-windows' },
          ...catalogue.live.slice(0, 3).map((entry) => el('button', {
            class: `bolt-window${entry === chosen ? ' is-on' : ''}`,
            title: entry.title,
            onclick: () => { chosen = entry; buildDock(); show(); },
          }, shortName(entry))))
        : null,
      catalogue?.imagery?.length
        ? el('label', { class: 'bolt-check' },
          el('input', {
            type: 'checkbox', checked: showCloud,
            onchange: (e) => { showCloud = e.target.checked; show(); },
          }),
          'Cloud tops underneath')
        : null,
      el('div', { class: 'bolt-count', id: 'boltCount' }, 'Loading…'),
      el('div', { class: 'bolt-note', id: 'boltNote' }, '')));
  paintDock();
}

/** A label that fits, from a title that does not. */
function shortName(entry) {
  return entry.title
    .replace(/\s*\(.*\)\s*$/, '')
    .replace(/accumulated\s*/i, '')
    .trim()
    .slice(0, 16) || entry.id.slice(0, 16);
}

function toggle() {
  enabled = !enabled;
  $('#boltToggle').classList.toggle('is-on', enabled);
  $('#boltBody').hidden = !enabled;
  if (enabled) {
    load();
    // The newest frame is part of the URL, so keeping up means asking the
    // catalogue again rather than refreshing a layer in place.
    timer = setInterval(load, REFRESH_MS);
  } else {
    clearInterval(timer);
    timer = null;
    flashes?.remove();
    backdrop?.remove();
    flashes = backdrop = null;
  }
  paintDock();
}

function paintDock() {
  const count = $('#boltCount');
  const note = $('#boltNote');
  if (!count || !note || !enabled) return;

  const view = reach();
  if (!chosen) {
    count.textContent = catalogue?.imagery?.length && showCloud
      ? 'MTG cloud tops — no live lightning'
      : 'No live lightning layer';
    note.textContent = problem;
    return;
  }

  const age = chosen.age_minutes;
  count.textContent = view.inside
    ? `${shortName(chosen)} — ${age == null ? 'live' : age < 1 ? 'just now' : `${age} min ago`}`
    : 'Nothing to see here — MTG cannot look this far';

  const lines = [];
  if (!view.inside) {
    lines.push(catalogue.coverage);
  } else if (catalogue.attribution === 'synthetic') {
    lines.push('Demo mode: the tiles are stand-ins.');
  } else {
    lines.push(`${catalogue.attribution}. Flashes accumulated over the last few `
      + 'minutes, not single strokes.');
  }
  if (problem) lines.push(problem);
  note.textContent = lines.filter(Boolean).join(' ');
}
