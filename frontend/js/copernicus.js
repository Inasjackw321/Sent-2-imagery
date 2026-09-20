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
// Which of the days on offer is showing. null is the newest one, and stays
// the newest one: a refresh that brings a fresher day moves the picture on
// rather than leaving it a day behind, which is what "live" has to mean for a
// panel that reloads itself every quarter of an hour.
let dayAt = null;
// Whether the whole catalogue is showing, rather than the handful of products
// per satellite that the backend marks as the everyday ones.
let showAll = false;
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
    // A whole day -- not an instant.
    //
    // One instant from a polar orbiter is one orbit strip: a few hundred
    // kilometres of the planet and nothing else, which on a world map reads
    // as a broken layer rather than as a satellite that has not been over the
    // rest of the world yet. A WMS given a TIME range draws everything inside
    // it, so a day is every pass that day -- and at three hundred metres that
    // is the globe.
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
    // An everyday one by preference, so the panel does not open on a product
    // that is only visible after pressing "all products".
    chosen = all.find((c) => c.layer.id === chosen?.layer?.id)
      ?? all.find((c) => c.layer.everyday !== false) ?? all[0] ?? null;
    if (!all.length) problem = nothingLive(catalogue);
  } catch (err) {
    problem = err.message;
    catalogue = null;
    chosen = null;
  }
  buildDock();
  show();
}

/** Which day of a layer's week is showing: the newest unless scrubbed back. */
export function dayIndex(days, at) {
  if (!days?.length) return 0;
  if (at == null) return days.length - 1;
  return Math.min(Math.max(at, 0), days.length - 1);
}

/** Where a step of the scrubber lands, with the newest day meaning "live".
 *
 * Returning null rather than the last index is the whole of what keeps the
 * panel live: an index would pin the picture to whatever today happened to be
 * when you scrubbed, and a quarter of an hour later that is yesterday.
 */
export function stepTo(days, at, step) {
  const last = (days?.length ?? 0) - 1;
  if (last < 0) return null;
  const next = dayIndex(days, at) + step;
  return next >= last ? null : Math.max(0, next);
}

/** The TIME parameter for whichever day is selected. */
export function wmsTime(layer, at) {
  const days = layer?.days ?? [];
  if (!days.length) return layer?.time_default ?? null;
  const day = days[dayIndex(days, at)];
  // One whole day, expressed as the range that covers it. A bare date works
  // on some servers and is read as midnight exactly on others, which would
  // put us back to one instant and one strip.
  return `${day}T00:00:00Z/${day}T23:59:59Z`;
}

function timeParam() {
  return wmsTime(chosen?.layer, dayAt);
}

/** Which of a family's products the panel lists.
 *
 * The everyday handful, unless everything was asked for -- and whatever is
 * currently drawn, always, because a list that can hide the thing it is
 * showing leaves no way back to it.
 */
export function productsShown(layers, all, chosenId) {
  return (layers ?? []).filter(
    (layer) => all || layer.everyday !== false || layer.id === chosenId);
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
  const held = families.reduce((n, f) => n + f.layers.length, 0);
  const listed = families.reduce(
    (n, f) => n + productsShown(f.layers, showAll, chosen?.layer?.id).length, 0);
  const days = chosen?.layer?.days ?? [];
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
          // Said out loud, because the list below is a shortlist now.
          el('small', { class: 'cop-many' }, (() => {
            const shown = productsShown(
              family.layers, showAll, chosen?.layer?.id).length;
            return shown < family.layers.length
              ? `${shown} of ${family.layers.length}`
              : `${shown} product${shown === 1 ? '' : 's'}`;
          })())),
        // The everyday handful rather than the whole catalogue. EUMETSAT
        // serves a dozen or more products per satellite, and a panel that
        // opens with forty buttons on it opens with nothing in particular on
        // it. The rest are behind one button at the bottom.
        el('div', { class: 'cop-layers' },
          ...productsShown(family.layers, showAll, chosen?.layer?.id)
            .map((layer) => el('button', {
              class: `cop-layer${layer.id === chosen?.layer?.id ? ' is-on' : ''}`,
              title: `${layer.title} — ${layer.id}`,
              onclick: () => { chosen = { family, layer }; buildDock(); show(); },
            }, shortName(layer)))))),
      // Live by default, and scrub back through the week from there. There is
      // no composite any more: a whole day of a polar orbiter's passes is
      // already the whole globe, so the week added nothing but a mode in which
      // the picture was never of any particular moment.
      days.length > 1
        ? el('div', { class: 'cop-span', onwheel: onWheel },
          el('button', {
            class: `cop-live${dayAt == null ? ' is-on' : ''}`, id: 'copLive',
            title: 'The newest day, and it stays the newest day',
            onclick: () => { dayAt = null; retime(); refreshSpan(); paint(); },
          }, 'Live'),
          el('input', {
            type: 'range', class: 'cop-scrub', id: 'copScrub',
            min: '0', max: String(days.length - 1), step: '1',
            value: String(dayIndex(days, dayAt)),
            title: 'Scroll or drag back through the week',
            // Deliberately not a rebuild: rebuilding the panel mid-drag takes
            // the slider out from under the pointer and the drag stops dead.
            oninput: (e) => {
              const last = days.length - 1;
              const to = Number(e.target.value);
              dayAt = to >= last ? null : to;
              retime();
              refreshSpan();
              paint();
            },
          }),
          el('span', { class: 'cop-when', id: 'copWhen' },
            days[dayIndex(days, dayAt)]))
        : null,
      listed < held || showAll
        ? el('button', {
          class: `cop-more${showAll ? ' is-on' : ''}`,
          title: 'Every product EUMETSAT is serving from these satellites',
          onclick: () => { showAll = !showAll; buildDock(); },
        }, showAll ? `Just the everyday ${listedEveryday(families)}`
          : `All ${held} products`)
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

/** How many products the short list holds, for the button that goes back. */
function listedEveryday(families) {
  return families.reduce(
    (n, f) => n + productsShown(f.layers, false, chosen?.layer?.id).length, 0);
}

/** Retime the drawn layer in place.
 *
 * setParams rather than a rebuild: scrubbing across a week is seven steps, and
 * tearing the layer down and building it again at each one flashes the map
 * empty seven times.
 */
function retime() {
  if (!drawn) { show(); return; }
  const when = timeParam();
  drawn.setParams(when ? { time: when } : {});
}

/** Put the scrubber, its date and the Live chip back in step. */
function refreshSpan() {
  const days = chosen?.layer?.days ?? [];
  const scrub = $('#copScrub');
  const when = $('#copWhen');
  if (scrub) scrub.value = String(dayIndex(days, dayAt));
  if (when) when.textContent = days[dayIndex(days, dayAt)] ?? '';
  $('#copLive')?.classList.toggle('is-on', dayAt == null);
}

/** Scrolling over the span row walks the week: down is back in time. */
function onWheel(e) {
  const days = chosen?.layer?.days ?? [];
  if (days.length < 2) return;
  // Otherwise the panel scrolls underneath instead, which is the one thing a
  // reader cannot mean by scrolling on a row of dates.
  e.preventDefault();
  dayAt = stepTo(days, dayAt, e.deltaY > 0 ? -1 : 1);
  retime();
  refreshSpan();
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
    // the whole disc, so it says what it is rather than which day it is.
    !spans
      ? 'The latest full disc, as it was taken.'
      : dayAt == null
        ? 'Today: every pass so far, which at this scale is the whole Earth. '
          + 'One instant would be a single orbit strip. Scroll the dates to go '
          + 'back.'
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
