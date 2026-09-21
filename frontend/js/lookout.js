// The lookout: a model asked the same questions about every square of an area.
//
// Everything else on this map answers "show me this place". This answers
// "somewhere in this area there is a thing that looks like THIS -- where is
// it", which is a question nobody can answer by scrolling: the Kyiv area is
// eleven thousand square kilometres and a screen of Sentinel-2 is about forty.
//
// The area is cut into squares, each square is rendered from one satellite
// pass, and each rendered square is put to OpenJev with a fixed list of typed
// questions. A square whose answers match the pattern is drawn on the map.
//
// What this panel is careful about is the difference between a hit, a miss
// and a square nobody could answer for. All three are drawn, in three
// different ways, because a sweep that shows only its hits is a sweep you
// cannot tell from one that failed silently -- and "we looked at forty-nine
// squares and nine of them were under cloud" is the useful sentence.

import { api } from './api.js';
import { $, el } from './ui.js';

// How often to ask how the sweep is going while one is running. A square is
// a render and a request, so seconds rather than milliseconds.
const POLL_MS = 2000;

let map = null;
let layer = null;
let ink = null;
let enabled = false;
let state = null;
let poller = null;
let problem = '';

export function initLookout(leafletMap) {
  map = leafletMap;
  // Over the imagery and under the air marks: a hit is a statement about the
  // ground, and the things in the air are drawn on top of it.
  map.createPane('lookout').style.zIndex = 432;
  map.getPane('lookout').style.pointerEvents = 'auto';
  // An explicit SVG renderer, for the reason the tracker's layers have one:
  // the map is built with preferCanvas, and a canvas-rendered rectangle has
  // no element, so className is dropped and a hit cannot be made to pulse.
  ink = L.svg({ pane: 'lookout' });
  layer = L.layerGroup([], { pane: 'lookout' });
  buildDock();
}

// ── What is drawn ──────────────────────────────────────────────

/** Whether a square is worth drawing as a hit. */
export function isHit(square) {
  return Boolean(square?.hit);
}

/** Whether a square could not be answered for at all. */
export function isBlank(square) {
  return Boolean(square?.trouble);
}

/** The one line a square's answers make, for the popup and the list. */
export function saidOf(square, asked) {
  if (square?.trouble) return square.trouble;
  const answers = square?.answers ?? {};
  return (asked ?? [])
    .map((q) => {
      const got = answers[q.id];
      const said = got?.said ?? '—';
      const odds = typeof got?.p === 'number' ? ` ${Math.round(got.p * 100)}%` : '';
      return `${q.ask}: ${said}${odds}`;
    })
    .join('\n');
}

/** Which question stopped a square being a hit, said plainly. */
export function whyNot(square, asked) {
  if (square?.trouble) return square.trouble;
  if (square?.hit) return 'every answer matched';
  const named = (id) => (asked ?? []).find((q) => q.id === id)?.ask ?? id;
  const missed = (square?.missed ?? []).map(named);
  const unsure = (square?.unsure ?? []).map(named);
  return [
    missed.length ? `answered the other way: ${missed.join(', ')}` : null,
    unsure.length ? `not sure enough: ${unsure.join(', ')}` : null,
  ].filter(Boolean).join(' · ') || 'nothing to go on';
}

function draw() {
  if (!layer) return;
  layer.clearLayers();
  if (!enabled || !state) return;

  const area = (state.areas ?? []).find((a) => a.key === state.area)
    ?? (state.areas ?? [])[0];
  if (area) {
    const [south, north, west, east] = area.bbox;
    L.rectangle([[south, west], [north, east]], {
      pane: 'lookout', renderer: ink, interactive: false,
      color: '#7fc4ff', weight: 1, opacity: 0.5, dashArray: '5 5', fill: false,
    }).addTo(layer);
  }

  for (const square of state.looked ?? []) {
    const [south, north, west, east] = square.bbox;
    const hit = isHit(square);
    const blank = isBlank(square);
    const shape = L.rectangle([[south, west], [north, east]], {
      pane: 'lookout',
      renderer: ink,
      className: hit ? 'lk-hit' : '',
      // A hit is loud. A square that was looked at and was not a hit is a
      // faint outline -- it is worth seeing that the ground WAS looked at,
      // and worth it being obviously not the answer. A square nobody could
      // answer for is neither: it is hatched grey, because "cloud" and "no"
      // are different results and drawing them alike is the one mistake that
      // makes a sweep untrustworthy.
      color: hit ? '#ffd23f' : blank ? '#6b7689' : '#39506e',
      weight: hit ? 2.5 : 1,
      opacity: hit ? 1 : 0.55,
      fill: true,
      fillColor: hit ? '#ffd23f' : blank ? '#6b7689' : '#1b2536',
      fillOpacity: hit ? 0.28 : blank ? 0.12 : 0.04,
    });
    shape.bindPopup(() => popupFor(square));
    shape.addTo(layer);
  }
}

function popupFor(square) {
  const asked = state?.questions ?? [];
  return el('div', { class: 'lk-pop' },
    el('div', { class: 'lk-pop-head' },
      isHit(square) ? 'Hit' : isBlank(square) ? 'Could not answer' : 'Looked at',
      el('small', {}, `${square.lat.toFixed(3)}, ${square.lon.toFixed(3)}`)),
    el('pre', { class: 'lk-pop-said' }, saidOf(square, asked)),
    el('div', { class: 'lk-pop-why' }, whyNot(square, asked))).outerHTML;
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#lookoutDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'lk-toggle', id: 'lookoutToggle', onclick: toggle },
      el('span', { class: 'lk-mark' }, '◎'), 'Lookout'),
    el('div', { class: 'lk-body', id: 'lookoutBody', hidden: !enabled },
      el('div', { class: 'lk-row' },
        el('select', { class: 'lk-area', id: 'lookoutArea' },
          ...(state?.areas ?? [{ key: 'kyiv', name: 'Kyiv' }]).map(
            (a) => el('option', { value: a.key }, a.name))),
        el('button', {
          class: 'lk-go', id: 'lookoutGo', type: 'button', onclick: go,
        }, 'Sweep')),
      // The key, and the fact that it is never written down. Said on the
      // panel rather than in a document nobody opens.
      el('div', { class: 'lk-row' },
        el('input', {
          type: 'password', class: 'lk-key', id: 'lookoutKey',
          placeholder: state?.have_key ? 'key held for this run' : 'Codiv API key',
          autocomplete: 'off',
        }),
        el('button', { class: 'lk-save', type: 'button', onclick: saveKey },
          'Use')),
      el('div', { class: 'lk-count', id: 'lookoutCount' }, ''),
      // Why they could not be answered, not only how many. Its own row and
      // its own colour, because on the sweep where it says anything it is the
      // only line on this panel worth reading.
      el('div', { class: 'lk-why', id: 'lookoutWhy', hidden: true }, ''),
      el('div', { class: 'lk-list', id: 'lookoutList' }),
      el('details', { class: 'lk-asked' },
        el('summary', {}, 'What it asks'),
        el('div', { class: 'lk-asked-body', id: 'lookoutAsked' })),
      el('div', { class: 'lk-note', id: 'lookoutNote' }, '')));
  paint();
}

async function saveKey() {
  const box = $('#lookoutKey');
  const key = box?.value ?? '';
  try {
    await api.lookoutKey(key);
    if (box) box.value = '';
    problem = '';
    await refresh();
  } catch (err) {
    problem = err.message;
    paint();
  }
}

async function go() {
  if (state?.running) {
    try { await api.lookoutStop(); } catch (err) { problem = err.message; }
    return;
  }
  const area = $('#lookoutArea')?.value ?? 'kyiv';
  problem = '';
  paint();
  try {
    state = await api.lookoutSweep(area);
    draw();
    paint();
    watch();
  } catch (err) {
    problem = err.message;
    paint();
  }
}

function watch() {
  clearInterval(poller);
  poller = setInterval(refresh, POLL_MS);
}

async function refresh() {
  try {
    state = await api.lookout();
    problem = '';
  } catch (err) {
    problem = err.message;
  }
  if (!state?.running) { clearInterval(poller); poller = null; }
  draw();
  paint();
}

function toggle() {
  enabled = !enabled;
  $('#lookoutToggle').classList.toggle('is-on', enabled);
  $('#lookoutBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    refresh();
  } else {
    clearInterval(poller);
    poller = null;
    layer.remove();
    layer.clearLayers();
  }
  paint();
}

/** "12 of 49 · 2 hits · 3 could not be answered", or what is stopping it. */
export function progressLine(said) {
  if (!said) return 'Loading…';
  const blank = (said.looked ?? []).filter(isBlank).length;
  if (!said.of) return said.have_key ? 'Ready' : 'Needs a Codiv API key';
  return [
    `${said.done} of ${said.of}`,
    `${(said.hits ?? []).length} hit${(said.hits ?? []).length === 1 ? '' : 's'}`,
    blank ? `${blank} could not be answered` : null,
  ].filter(Boolean).join(' · ');
}

/** WHY the squares that could not be answered could not be answered.
 *
 * The line this panel was missing. A sweep where every square failed the same
 * way is one cause with a count in front of it, and the count on its own is a
 * shrug -- "49 could not be answered" sent somebody back to me with "???",
 * which is the correct response to it.
 */
export function blankLine(said) {
  const blank = said?.blanks;
  if (!blank?.count || !blank.why) return '';
  const all = blank.worst === blank.count && blank.count > 1;
  const many = blank.kinds > 1 ? `, and ${blank.kinds - 1} other reason`
    + `${blank.kinds > 2 ? 's' : ''}` : '';
  return `${all ? 'All ' : ''}${blank.worst} of them: ${clamp(blank.why)}${many}`;
}

// How much of a reason fits on the line before it stops being read.
//
// A urllib connection error runs to two hundred and fifty characters of
// retry counts and nested exception names, and a panel that shows all of it
// is a panel where the first eight words -- the part that says what went
// wrong -- are buried. The whole text is still on the square itself, which
// is where somebody goes once they know what they are chasing.
const MOST_WHY = 120;

export function clamp(said, most = MOST_WHY) {
  const text = String(said ?? '');
  if (text.length <= most) return text;
  // Cut at a word rather than mid-word, where there is one to cut at.
  const cut = text.slice(0, most);
  const space = cut.lastIndexOf(' ');
  return `${(space > most * 0.6 ? cut.slice(0, space) : cut).trimEnd()}…`;
}

function paint() {
  if (!enabled) return;
  const count = $('#lookoutCount');
  const list = $('#lookoutList');
  const note = $('#lookoutNote');
  const go = $('#lookoutGo');
  const asked = $('#lookoutAsked');
  if (count) count.textContent = progressLine(state);
  if (go) {
    go.textContent = state?.running ? 'Stop' : 'Sweep';
    go.classList.toggle('is-on', Boolean(state?.running));
    go.disabled = !state?.have_key && !state?.running;
  }

  list?.replaceChildren(...(state?.hits ?? []).map((square) => el('button', {
    class: 'lk-row-hit', type: 'button',
    title: saidOf(square, state?.questions),
    onclick: () => {
      map?.setView([square.lat, square.lon], 11);
      // Open the square's own popup, so the click answers the question the
      // row raises rather than only moving the map.
      layer?.eachLayer((shape) => {
        const at = shape.getBounds?.();
        if (at?.contains([square.lat, square.lon]) && shape.getPopup) {
          shape.openPopup();
        }
      });
    },
  }, `${square.lat.toFixed(3)}, ${square.lon.toFixed(3)}`)));

  asked?.replaceChildren(...(state?.questions ?? []).map((q) => el(
    'div', { class: 'lk-asked-row' },
    el('span', { class: 'lk-asked-q' }, q.ask),
    el('span', { class: 'lk-asked-hit' },
      q.hit == null ? 'for interest' : `hit when ${q.hit}`))));

  const why = $('#lookoutWhy');
  if (why) {
    why.textContent = blankLine(state);
    why.hidden = !why.textContent;
  }

  if (note) {
    note.textContent = [
      problem,
      state?.trouble,
      state?.scene?.date ? `One pass: ${state.scene.date}.` : null,
      state?.square_km ? `Squares about ${state.square_km} km across.` : null,
      'Answers come from OpenJev at Codiv. The key is held in memory for this '
      + 'run and written nowhere.',
    ].filter(Boolean).join(' ');
  }
}
