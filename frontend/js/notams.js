// Which airspace is shut, and when.
//
// The other half of the air tracker's question. That layer shows what
// somebody said is flying; this one shows what the authorities have closed --
// a corridor reserved for firing, a whole FIR shut to civil traffic, a drone
// operation at a named point. One is a report, the other is a rule.
//
// The notices come from a paste box rather than from a service, and that is
// the whole design rather than a shortcut. Three attempts at fetching them
// failed for three different reasons -- a key, a 403, a page that can change
// shape -- and there is no keyless worldwide NOTAM service to move to. There
// is, however, a NOTAM source that works perfectly well for everybody: their
// own browser. So they open one, copy, and paste it here.
//
// What that buys is a layer that cannot 403, cannot rate-limit, cannot expire
// a key, needs no registration, and reads the notices from ANY source,
// because it reads the ICAO format rather than one service's JSON.
//
// Drawn as outlines rather than washes, because a NOTAM is a boundary: inside
// it the rule applies and outside it does not, and a filled disc over a
// province would say the same thing the air tracker's warnings say and mean
// something quite different.

import { api } from './api.js';
import { $, el, toast } from './ui.js';

let map = null;
let layer = null;
let ink = null;
let enabled = false;
let got = null;
let state = '';

// The colour is deliberately not one the air tracker uses. Amber is a
// warning, purple is a missile, yellow is a drone; a closure is none of those
// and must not read as one. A cool blue says "rule" rather than "threat".
const INK = '#5ac8fa';

// Where to get the text. Named in the panel, because "paste NOTAMs here" is
// only easy if somebody knows where to copy them FROM -- and that was the
// real gap: the data was always a browser tab away.
const SOURCES = [
  ['FAA DINS', 'https://www.notams.faa.gov/dinsQueryWeb/',
   'pick Raw, enter an ICAO code'],
  ['FAA NOTAM Search', 'https://notams.aim.faa.gov/notamSearch/',
   'search by location, then Save as Text'],
];

export function initNotams(leafletMap) {
  map = leafletMap;
  // Its own pane, under the air tracker's marks: a closure is context for
  // them rather than a thing to be drawn over them.
  map.createPane('notams').style.zIndex = 425;
  map.getPane('notams').style.pointerEvents = 'auto';
  // An explicit SVG renderer, because the map is built with preferCanvas and
  // a canvas-rendered circle is pixels: it has no element, so className is
  // ignored and the dashed outline never applies to anything. There are a
  // handful of these at a time, so the reason preferCanvas exists does not
  // arise. The same lesson the tracker's areas learnt.
  ink = L.svg({ pane: 'notams' });
  layer = L.layerGroup([], { pane: 'notams' });
  buildDock();
}

function buildDock() {
  const dock = $('#notamDock');
  if (!dock) return;
  dock.innerHTML = '';

  const box = el('textarea', {
    class: 'notam-paste', id: 'notamPaste', rows: 4, spellcheck: false,
    placeholder: 'Paste NOTAM text here',
    // Pasting IS the action. A paste box with a submit button beside it asks
    // somebody to do the same thing twice.
    onpaste: (e) => setTimeout(() => plot(e.target.value), 0),
    oninput: (e) => { if (!e.target.value.trim()) clear(); },
  });

  dock.append(
    el('button', { class: 'notam-toggle', id: 'notamToggle', onclick: toggle },
      el('span', { class: 'notam-mark' }, '⛔'), 'Closed airspace'),
    el('div', { class: 'notam-body', id: 'notamBody', hidden: true },
      box,
      el('div', { class: 'notam-actions' },
        el('label', { class: 'notam-file' }, 'File…',
          el('input', {
            type: 'file', accept: '.txt,.json,.htm,.html,text/plain',
            onchange: (e) => openFile(e.target.files?.[0]),
          })),
        el('button', { class: 'notam-clear', id: 'notamClear', onclick: clear },
          'Clear'),
        el('button', { class: 'notam-demo', onclick: showDemo }, 'Example')),
      el('div', { class: 'notam-count', id: 'notamCount' },
        'Nothing pasted yet'),
      el('div', { class: 'notam-list', id: 'notamList' }),
      el('div', { class: 'notam-where', id: 'notamWhere' },
        'Copy the notices from: ',
        ...SOURCES.flatMap(([name, url, how], i) => [
          i ? ' · ' : '',
          el('a', { href: url, target: '_blank', rel: 'noreferrer noopener',
                    title: how }, name),
        ])),
      el('div', { class: 'notam-note', id: 'notamNote' }, '')),
  );
}

function toggle() {
  enabled = !enabled;
  $('#notamToggle').classList.toggle('is-on', enabled);
  $('#notamBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    paint();
    $('#notamPaste')?.focus();
  } else {
    layer.remove();
    layer.clearLayers();
  }
}

/** Read a blob of pasted text and draw what is in it. */
async function plot(text) {
  if (!String(text || '').trim()) return clear();
  state = '';
  try {
    got = await api.readNotams(text);
  } catch (err) {
    got = null;
    state = err.message;
    paint();
    return;
  }
  paint();
  const drawn = got.notams.length;
  if (drawn) {
    // Go to them. Somebody who has just pasted a briefing for another FIR is
    // looking at the wrong part of the world, and a layer that drew three
    // circles two thousand kilometres off screen would look like it had done
    // nothing at all.
    const bounds = L.latLngBounds(got.notams.map((n) => [n.lat, n.lon]));
    map.fitBounds(bounds.pad(0.35), { maxZoom: 9 });
  }
  toast(summary(), drawn ? 'ok' : 'warn');
}

async function openFile(file) {
  if (!file) return;
  try {
    const text = await file.text();
    const box = $('#notamPaste');
    if (box) box.value = text.slice(0, 4000);
    await plot(text);
  } catch (err) {
    state = `That file could not be read: ${err.message}`;
    paint();
  }
}

async function showDemo() {
  try {
    got = await api.notamsDemo();
    state = '';
  } catch (err) {
    state = err.message;
  }
  paint();
  if (got?.notams?.length) {
    map.fitBounds(L.latLngBounds(got.notams.map((n) => [n.lat, n.lon]))
      .pad(0.35), { maxZoom: 8 });
  }
}

function clear() {
  got = null;
  state = '';
  const box = $('#notamPaste');
  if (box) box.value = '';
  paint();
}

function summary() {
  if (!got) return 'Nothing read';
  const bits = [`${got.notams.length} drawn`];
  if (got.wide.length) bits.push(`${got.wide.length} area-wide`);
  if (got.unplaced.length) bits.push(`${got.unplaced.length} unplaced`);
  if (got.later) bits.push(`${got.later} not yet in force`);
  return bits.join(' · ');
}

function paint() {
  layer.clearLayers();
  for (const notice of got?.notams ?? []) {
    L.circle([notice.lat, notice.lon], {
      pane: 'notams',
      renderer: ink,
      radius: notice.radius_km * 1000,
      color: INK,
      weight: 1.6,
      opacity: 0.85,
      // An outline with barely any fill. A closure is a boundary, and a solid
      // disc would hide the ground the rest of this map is about.
      fillColor: INK,
      fillOpacity: 0.06,
      // Dashed, because a NOTAM's circle is a radius around a point rather
      // than a surveyed edge, and a solid line claims a precision the Q-line
      // does not have.
      dashArray: '5 4',
      className: 'notam-ring',
    }).bindPopup(() => popup(notice)).addTo(layer);
  }
  paintDock();
}

/** A notice, read as a person would need it: what, where, and until when. */
function popup(notice) {
  const rows = [];
  if (notice.location) rows.push(`Location <b>${escapeHtml(notice.location)}</b>`);
  rows.push(`Radius ${Math.round(notice.radius_km)} km around the point given`);
  rows.push(when(notice));
  return `<div class="notam-pop"><h4>NOTAM ${escapeHtml(notice.id)}</h4>`
    + `<blockquote>${escapeHtml(notice.text)}</blockquote>`
    + rows.map((r) => `<p>${r}</p>`).join('')
    + '</div>';
}

function when(notice) {
  const from = notice.from ? new Date(notice.from * 1000) : null;
  const to = notice.to ? new Date(notice.to * 1000) : null;
  const said = (d) => d.toISOString().slice(0, 16).replace('T', ' ') + 'Z';
  if (from && to) return `In force ${said(from)} to ${said(to)}`;
  if (from) return `In force from ${said(from)} — no stated end (permanent)`;
  if (to) return `In force until ${said(to)}`;
  return 'No stated period';
}

function paintDock() {
  const count = $('#notamCount');
  const list = $('#notamList');
  const note = $('#notamNote');
  if (!count) return;

  count.textContent = state ? 'Could not read that'
    : got ? summary() : 'Nothing pasted yet';

  // The ones that are real and are not circles, plus the ones with no
  // position. Both are notices in force and neither is on the map, so the
  // list is the only place they exist -- and a layer that silently dropped
  // them would be saying the airspace is open.
  const missing = [...(got?.wide ?? []), ...(got?.unplaced ?? [])];
  list?.replaceChildren(...missing.slice(0, 8).map((n) => el('div',
    { class: 'notam-row', title: n.text },
    el('span', { class: 'notam-id' }, n.id),
    el('span', { class: 'notam-why' },
      n.wide ? `${Math.round(n.radius_km)} km — too wide to draw as a circle`
        : 'no position'))));

  if (state) {
    note.textContent = state;
    return;
  }
  note.textContent =
    'A NOTAM is a notice to aircraft, not a report of anything in the air. '
    + 'Circles are a radius around a point, not a surveyed boundary.'
    + (got ? ` Read as ${got.how}; ${got.read} notice`
      + `${got.read === 1 ? '' : 's'} in that text.` : '')
    + (got?.later ? ` ${got.later} start later and are not drawn yet.` : '')
    + (got?.capped ? ' Only the first few hundred were read.' : '');
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
