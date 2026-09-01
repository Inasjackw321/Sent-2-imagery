// Lightning, as it happens.
//
// Blitzortung's volunteer receivers hear the radio crack a stroke makes and a
// server triangulates it from the arrival times, usually within a second or
// two. That makes this the fastest-moving thing on the map by a wide margin:
// the radar is ten minutes old at best, the cloud mosaic a few hours, and the
// imagery days. A stroke is on screen while the thunder is still travelling.
//
// Each stroke is a moment, not a state, so they are drawn fading: bright white
// when they have just arrived, dimming to a faint blue over the window, and
// gone at the end of it. A dot that never faded would say a storm is where it
// was half an hour ago.
//
// The coverage caveat matters more here than for any other layer. The network
// is dense over Europe, good over North America and Australia, and thin to
// absent over the oceans, Africa and much of Asia -- so a blank map is as
// likely to mean nobody is listening as it is to mean nothing is happening,
// and the panel says which rather than leaving a quiet map to be read as calm.

import { api } from './api.js';
import { $, el, debounce, askableBounds } from './ui.js';

// How far back the map looks. Long enough to see the shape of a storm, short
// enough that everything on screen happened while you were watching.
const WINDOWS = [5, 15, 30];

// How often to ask again. The feed is continuous, so this is really "how often
// is the picture worth redrawing" -- often enough to feel live, rarely enough
// to be a decent way to treat a volunteer network's server.
const REFRESH_MS = 20000;

// The freshest strokes are drawn biggest and brightest; a stroke at the end of
// the window is a small dim dot.
const NEW_RADIUS = 7;
const OLD_RADIUS = 2.5;

let map = null;
let pane = null;
let canvas = null;
let layer = null;
let enabled = false;
let minutes = 15;
let strokes = [];
let timer = null;
let fader = null;
let note = '';

export function initLightning(leafletMap) {
  map = leafletMap;
  // Above the radar: rain is the context and the strike is the event, and the
  // point of having both is to see one inside the other.
  pane = map.createPane('lightning');
  pane.style.zIndex = 465;
  canvas = L.canvas({ pane: 'lightning', padding: 0.3 });
  buildDock();
  map.on('moveend', debounce(() => { if (enabled) refresh(); }, 600));
}

// ── The map ────────────────────────────────────────────────────

/** How old a stroke is, from 0 (just now) to 1 (about to drop off). */
const age = (stroke, now) => Math.min(1, Math.max(0,
  (now - stroke.time * 1000) / (minutes * 60000)));

function paint() {
  layer?.remove();
  if (!enabled || !strokes.length) { layer = null; return; }

  const now = Date.now();
  layer = L.layerGroup(strokes.map((stroke) => {
    const old = age(stroke, now);
    const fade = 1 - old;
    return L.circleMarker([stroke.lat, stroke.lon], {
      renderer: canvas,
      pane: 'lightning',
      // Not clickable. A stroke is a point in time with nothing to say about
      // itself beyond where and when, both of which the drawing already
      // carries -- and thousands of hit targets over the radar would take
      // every click on the map with them.
      interactive: false,
      radius: OLD_RADIUS + (NEW_RADIUS - OLD_RADIUS) * fade * fade,
      // White-hot when new, cooling to the blue of the older ones.
      color: '#ffffff',
      weight: fade > 0.75 ? 1.5 : 0,
      opacity: fade * 0.9,
      fillColor: fade > 0.5 ? '#eaf4ff' : '#7cc4ff',
      fillOpacity: 0.15 + 0.75 * fade,
    });
  })).addTo(map);
}

async function refresh() {
  if (!enabled) return;
  const box = askableBounds(map);
  try {
    const got = await api.lightning({ ...box, minutes });
    strokes = got.strokes ?? [];
    note = got.state ?? '';
    paint();
    paintDock(got);
  } catch (err) {
    note = err.message;
    paintDock();
  }
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#lightningDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'bolt-toggle', id: 'boltToggle', onclick: toggle },
      el('span', { class: 'bolt-mark' }, '⚡'), 'Lightning'),
    el('div', { class: 'bolt-body', id: 'boltBody', hidden: true },
      el('div', { class: 'bolt-windows' },
        ...WINDOWS.map((m) => el('button', {
          class: `bolt-window${m === minutes ? ' is-on' : ''}`,
          dataset: { minutes: m },
          onclick: () => setWindow(m),
        }, `${m} min`))),
      el('div', { class: 'bolt-count', id: 'boltCount' }, 'Nothing loaded yet'),
      el('div', { class: 'bolt-key' },
        el('span', { class: 'bolt-key-row' },
          el('i', { style: 'background:#ffffff' }), 'seconds ago'),
        el('span', { class: 'bolt-key-row' },
          el('i', { style: 'background:#7cc4ff;opacity:.5' }), 'minutes ago')),
      el('div', { class: 'bolt-note', id: 'boltNote' }, '')));
}

function setWindow(next) {
  minutes = next;
  for (const button of document.querySelectorAll('.bolt-window')) {
    button.classList.toggle('is-on', Number(button.dataset.minutes) === minutes);
  }
  refresh();
}

function toggle() {
  enabled = !enabled;
  $('#boltToggle').classList.toggle('is-on', enabled);
  $('#boltBody').hidden = !enabled;
  if (enabled) {
    refresh();
    timer = setInterval(refresh, REFRESH_MS);
    // Redrawn between fetches as well, so the fade is continuous rather than
    // stepping every twenty seconds.
    fader = setInterval(() => { if (strokes.length) paint(); }, 2000);
  } else {
    clearInterval(timer);
    clearInterval(fader);
    timer = fader = null;
    strokes = [];
    layer?.remove();
    layer = null;
  }
}

function paintDock(got) {
  const count = $('#boltCount');
  const text = $('#boltNote');
  if (!count || !text) return;

  count.textContent = strokes.length
    ? `${strokes.length.toLocaleString()} strokes in the last ${minutes} min`
    : 'No strokes here in this window';

  const lines = [];
  if (got?.attribution && got.attribution !== 'synthetic') {
    lines.push(`${got.attribution} — volunteer receivers, free to use.`);
  } else if (got?.attribution === 'synthetic') {
    lines.push('Demo mode: these strokes are invented.');
  }
  // The honest reading of an empty map, which is not "no lightning".
  if (!strokes.length) {
    lines.push('Coverage follows where the receivers are: dense over Europe, '
      + 'good over North America and Australia, thin elsewhere. An empty map '
      + 'here may mean nobody is listening rather than nothing is happening.');
  }
  if (note && !/listening|demo/.test(note)) lines.push(note);
  text.textContent = lines.join(' ');
}

/** Used by the tests and by anything that wants the current count. */
export const held = () => strokes.length;
