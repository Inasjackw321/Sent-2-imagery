// Sentinel-3 and Sentinel-5P, as live layers rather than as imagery.
//
// The other two Sentinels are in the imagery flow: draw an area, pick a date,
// get a rendered scene. These two cannot join them, and the reason is worth
// saying rather than leaving as a gap in the picker.
//
// They are published as NetCDF granules -- a whole swath in one file, not
// tiled and not cloud-optimised. Pulling a hundred-kilometre box out of one
// means downloading the entire granule, and this app's pipeline is built on
// windowed reads of COGs. Different data, different pipeline.
//
// EUMETSAT serves them as ordinary WMS with no account and no key, which is
// how they arrive here instead: the current picture, drawn under the imagery.
// Coarse on purpose -- three hundred metres for Sentinel-3 and seven
// kilometres for Sentinel-5P -- and that coarseness is the point of them.
// Sentinel-2 sees a field once every five days; these see the whole planet
// every day.

import { api } from './api.js';
import { $, el } from './ui.js';

// How often to ask for a newer frame. These are polar orbiters publishing a
// few times a day, so this is about catching up after the tab has been open a
// while rather than about keeping pace with anything.
const REFRESH_MS = 15 * 60 * 1000;

let map = null;
let drawn = null;
let enabled = false;
let catalogue = null;
let chosen = null;      // { family, layer }
let opacity = 0.8;
let timer = null;
let problem = '';

export function initCopernicus(leafletMap) {
  map = leafletMap;
  // Under the cloud mask and the radar, over the basemap. This is a picture
  // of the ground and the air above it, so it belongs at the bottom of the
  // things drawn on top of the map.
  map.createPane('copernicus').style.zIndex = 420;
  map.getPane('copernicus').style.pointerEvents = 'none';
  buildDock();
}

// ── The layer ──────────────────────────────────────────────────

function show() {
  drawn?.remove();
  drawn = null;
  if (!enabled || !chosen || !catalogue) return;

  let missing = 0;
  drawn = L.tileLayer.wms(catalogue.wms, {
    layers: chosen.layer.id,
    format: 'image/png',
    transparent: true,
    // 1.3.0 is what the capabilities are read at, and mixing versions is how
    // you get axis order wrong and the picture mirrored.
    version: '1.3.0',
    // The frame the catalogue says is newest. Without it EUMETSAT serves
    // whatever its own default is, which is not necessarily the latest.
    ...(chosen.layer.time_default ? { time: chosen.layer.time_default } : {}),
    pane: 'copernicus',
    opacity,
    attribution: catalogue.attribution,
  });
  // A swath does not cover the globe, so missing tiles are ordinary: a polar
  // orbiter has simply not been over that ground today. A run of them
  // everywhere means the layer has moved, which is worth saying rather than
  // showing a blank.
  drawn.on('tileerror', () => {
    missing += 1;
    if (missing === 16) { problem = `${chosen.layer.id} is not answering.`; paint(); }
  });
  drawn.on('tileload', () => {
    if (!problem) return;
    problem = '';
    paint();
  });
  drawn.addTo(map);
}

async function load() {
  try {
    catalogue = await api.copernicus();
    problem = '';
    // Keep what was chosen if it is still on offer; otherwise take the first
    // live layer of the first family that has one.
    const all = (catalogue.families ?? []).flatMap(
      (family) => family.layers.map((layer) => ({ family, layer })));
    chosen = all.find((c) => c.layer.id === chosen?.layer?.id) ?? all[0] ?? null;
    if (!all.length) problem = nothingLive(catalogue);
  } catch (err) {
    problem = err.message;
    catalogue = null;
    chosen = null;
  }
  buildDock();
  show();
}

/** What to say when EUMETSAT answered but nothing it holds is current. */
function nothingLive(got) {
  const stale = (got.families ?? []).reduce((n, f) => n + (f.stale ?? 0), 0);
  const hours = Math.round(got.live_within_hours ?? 36);
  if (stale) {
    return `EUMETSAT has ${stale} Sentinel-3 and Sentinel-5P layers but none `
      + `from the last ${hours} h. Shown only when current.`;
  }
  return 'No Sentinel-3 or Sentinel-5P layers at EUMETSAT'
    + `${got.catalogue_size ? ` among ${got.catalogue_size} layers` : ''}.`;
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#copernicusDock');
  if (!dock) return;
  const families = (catalogue?.families ?? []).filter((f) => f.layers.length);
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'cop-toggle', id: 'copToggle', onclick: toggle },
      el('span', { class: 'cop-mark' }, '🛰'), 'Sentinel-3 / 5P'),
    el('div', { class: 'cop-body', id: 'copBody', hidden: !enabled },
      ...families.map((family) => el('div', { class: 'cop-family' },
        el('div', { class: 'cop-name' },
          el('i', { style: `background:${family.colour}` }),
          family.short,
          el('small', {}, family.resolution)),
        el('div', { class: 'cop-layers' },
          ...family.layers.slice(0, 6).map((layer) => el('button', {
            class: `cop-layer${layer.id === chosen?.layer?.id ? ' is-on' : ''}`,
            title: `${layer.title} — ${layer.id}`,
            onclick: () => { chosen = { family, layer }; buildDock(); show(); },
          }, shortName(layer)))))),
      families.length
        ? el('label', { class: 'cop-fade' }, 'Fade',
          el('input', {
            type: 'range', min: '20', max: '100', value: String(opacity * 100),
            oninput: (e) => {
              opacity = Number(e.target.value) / 100;
              drawn?.setOpacity(opacity);
            },
          }))
        : null,
      el('div', { class: 'cop-count', id: 'copCount' }, 'Loading…'),
      el('div', { class: 'cop-note', id: 'copNote' }, '')));
  paint();
}

/** A label that fits, from a title that does not. */
function shortName(layer) {
  return layer.title
    .replace(/sentinel[-\s]?[35]p?\s*/i, '')
    .replace(/\s*\(.*\)\s*$/, '')
    .trim()
    .slice(0, 18) || layer.id.split(':').pop().slice(0, 18);
}

function toggle() {
  enabled = !enabled;
  $('#copToggle').classList.toggle('is-on', enabled);
  $('#copBody').hidden = !enabled;
  if (enabled) {
    load();
    timer = setInterval(load, REFRESH_MS);
  } else {
    clearInterval(timer);
    timer = null;
    drawn?.remove();
    drawn = null;
  }
  paint();
}

function paint() {
  const count = $('#copCount');
  const note = $('#copNote');
  if (!count || !note || !enabled) return;

  if (!chosen) {
    count.textContent = catalogue ? 'Nothing current' : 'Loading…';
    note.textContent = problem;
    return;
  }

  const age = chosen.layer.age_minutes;
  count.textContent = `${chosen.family.short} · ${
    age == null ? 'live' : age < 60 ? `${age} min ago`
      : `${Math.round(age / 60)} h ago`}`;

  const lines = [chosen.family.about];
  if (catalogue.attribution === 'synthetic') {
    lines.push('Demo mode: the tiles are stand-ins.');
  } else {
    // Said once, because it is the question the picker raises: why are these
    // two not in the list of satellites you can search by date?
    lines.push('Published as NetCDF rather than tiled GeoTIFF, so these are '
      + 'live layers rather than scenes you can pick a date for.');
    lines.push(catalogue.attribution);
  }
  if (problem) lines.push(problem);
  note.textContent = lines.filter(Boolean).join(' ');
}
