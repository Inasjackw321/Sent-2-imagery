// The satellites that cannot be imagery, as live layers.
//
// The imagery flow is: draw an area, pick a date, get a rendered scene.
// Sentinel-1, Sentinel-2 and Landsat live there. These cannot join them, and
// the reason is worth saying rather than leaving as a gap in the picker.
//
// Sentinel-3 and Sentinel-5P are published as NetCDF granules -- a whole swath
// in one file, not tiled and not cloud-optimised. Pulling a hundred-kilometre
// box out of one means downloading the entire granule, and this app's pipeline
// is built on windowed reads of COGs. Different data, different pipeline. The
// weather satellites are further from it still: a geostationary full disc is
// not a scene over your area at all.
//
// EUMETSAT serves all of them as ordinary WMS with no account and no key,
// which is how they arrive here: the current picture, drawn under the imagery.
// Coarse on purpose, and the coarseness is the point. Sentinel-2 sees a field
// once every five days; Sentinel-3 sees the whole planet every day; Meteosat
// sees Europe every ten minutes, including at night.
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
// Which day of the week on offer is showing. -1 is the whole week composited
// into one picture, which is the fullest globe available.
let dayAt = -1;
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
    // A whole day, or a whole week -- not an instant.
    //
    // One instant from a polar orbiter is one orbit strip: a few hundred
    // kilometres of the planet and nothing else, which on a world map reads
    // as a broken layer rather than as a satellite that has not been over the
    // rest of the world yet. A WMS given a TIME range draws everything inside
    // it, so a day is every pass that day and a week is seven of them -- and
    // at three hundred metres that is the globe.
    ...(timeParam() ? { time: timeParam() } : {}),
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

/** The TIME parameter for whatever span is selected. */
function timeParam() {
  const days = chosen?.layer?.days ?? [];
  if (!days.length) return chosen?.layer?.time_default ?? null;
  if (dayAt < 0) return chosen.layer.whole_week;
  const day = days[Math.min(dayAt, days.length - 1)];
  // One whole day, expressed as the range that covers it. A bare date works
  // on some servers and is read as midnight exactly on others, which would
  // put us back to one instant and one strip.
  return `${day}T00:00:00Z/${day}T23:59:59Z`;
}

/** What to say when EUMETSAT answered but nothing it holds is current. */
function nothingLive(got) {
  const stale = (got.families ?? []).reduce((n, f) => n + (f.stale ?? 0), 0);
  const hours = Math.round(got.live_within_hours ?? 36);
  if (stale) {
    return `EUMETSAT has ${stale} matching layers but none from the last `
      + `${hours} h. Shown only when current.`;
  }
  return 'No Sentinel, Meteosat or Metop layers at EUMETSAT'
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
      el('span', { class: 'cop-mark' }, '🛰'), 'Live satellites'),
    el('div', { class: 'cop-body', id: 'copBody', hidden: !enabled },
      ...families.map((family) => el('div', { class: 'cop-family' },
        el('div', { class: 'cop-name' },
          el('i', { style: `background:${family.colour}` }),
          family.short,
          el('small', {}, family.resolution),
          // Said out loud now the list is long enough to scroll.
          el('small', { class: 'cop-many' },
            `${family.layers.length} product${family.layers.length === 1 ? '' : 's'}`)),
        // All of them, not the first six. EUMETSAT serves a dozen or more
        // products per satellite and the cap was hiding most of them behind
        // nothing -- no count, no "more", just a list that stopped. The panel
        // scrolls instead.
        el('div', { class: 'cop-layers' },
          ...family.layers.map((layer) => el('button', {
            class: `cop-layer${layer.id === chosen?.layer?.id ? ' is-on' : ''}`,
            title: `${layer.title} — ${layer.id}`,
            onclick: () => { chosen = { family, layer }; buildDock(); show(); },
          }, shortName(layer)))))),
      // The span: a week composited, or one day of it at a time.
      chosen?.layer?.days?.length
        ? el('div', { class: 'cop-span' },
          el('button', {
            class: `cop-day${dayAt < 0 ? ' is-on' : ''}`,
            title: 'Every pass in the last week, drawn together — the fullest '
              + 'picture of the whole Earth available',
            onclick: () => { dayAt = -1; buildDock(); show(); },
          }, 'Whole week'),
          el('button', {
            class: 'cop-step', title: 'A day earlier',
            onclick: () => {
              dayAt = dayAt < 0 ? chosen.layer.days.length - 2
                : Math.max(0, dayAt - 1);
              buildDock();
              show();
            },
          }, '‹'),
          el('span', { class: 'cop-when' },
            dayAt < 0 ? `${chosen.layer.days.length} days`
              : chosen.layer.days[Math.min(dayAt, chosen.layer.days.length - 1)]),
          el('button', {
            class: 'cop-step', title: 'A day later',
            onclick: () => {
              if (dayAt >= 0 && dayAt < chosen.layer.days.length - 1) dayAt += 1;
              buildDock();
              show();
            },
          }, '›'))
        : null,
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
    // The satellite's name is already the heading above the list, so it is
    // only taking room away from the part that says which product this is.
    .replace(/^(sentinel[-\s]?[35]p?|metop[-\s]?[abc]?|meteosat|msg|mtg|fci|seviri|avhrr)\s*/i, '')
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

  const spans = chosen.layer.days?.length > 0;
  const lines = [
    // A geostationary satellite has no week to offer: every frame is already
    // the whole disc, so it says what it is instead of what it is composited
    // from.
    !spans
      ? 'The latest full disc, as it was taken.'
      : dayAt < 0
        ? `Every pass in the last ${chosen.layer.days.length} days drawn `
          + 'together, which is the whole Earth. One instant would be a single '
          + 'orbit strip.'
        : 'One whole day: every pass that day.',
    chosen.family.about,
  ];
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
