// Which airspace is shut, and when.
//
// The other half of the air tracker's question. That layer shows what
// somebody said is flying; this one shows what the authorities have closed --
// a corridor reserved for firing, a whole FIR shut to civil traffic, a drone
// operation at a named point. One is a report, the other is a rule.
//
// Drawn as outlines rather than washes, because a NOTAM is a boundary: inside
// it the rule applies and outside it does not, and a filled disc over a
// province would say the same thing the air tracker's warnings say and mean
// something quite different.

import { api } from './api.js';
import { $, el } from './ui.js';

let map = null;
let layer = null;
let ink = null;
let enabled = false;
let shown = [];
let state = '';
let covered = null;

// The colour is deliberately not one the air tracker uses. Amber is a
// warning, purple is a missile, yellow is a drone; a closure is none of those
// and must not read as one. A cool blue says "rule" rather than "threat".
const INK = '#5ac8fa';

// How far outside the view to ask for, as a fraction. A closure whose centre
// is just off screen still covers ground on it.
const MARGIN = 0.25;

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
  // Panning somewhere new is a request for that somewhere's notices.
  map.on('moveend', () => { if (enabled) refresh(); });
}

function buildDock() {
  const dock = $('#notamDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'notam-toggle', id: 'notamToggle', onclick: toggle },
      el('span', { class: 'notam-mark' }, '⛔'), 'Closed airspace'),
    el('div', { class: 'notam-body', id: 'notamBody', hidden: true },
      el('div', { class: 'notam-count', id: 'notamCount' }, 'Nothing loaded yet'),
      el('div', { class: 'notam-list', id: 'notamList' }),
      el('div', { class: 'notam-note', id: 'notamNote' }, '')),
  );
}

function toggle() {
  enabled = !enabled;
  $('#notamToggle').classList.toggle('is-on', enabled);
  $('#notamBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    refresh({ force: true });
  } else {
    layer.remove();
    layer.clearLayers();
    shown = [];
    covered = null;
  }
}

/** Whether what is on screen is already inside what was last asked for. */
function alreadyCovered(view) {
  return Boolean(covered && covered.contains(view));
}

async function refresh({ force = false } = {}) {
  if (!map || !enabled) return;
  const view = map.getBounds();
  if (!force && alreadyCovered(view)) return;
  const asked = view.pad(MARGIN);
  try {
    const got = await api.notams({
      west: asked.getWest(), south: asked.getSouth(),
      east: asked.getEast(), north: asked.getNorth(),
    });
    shown = got.notams ?? [];
    covered = asked;
    state = got.configured === false
      ? (got.problem ?? 'no key is set, so no notices can be fetched')
      : '';
    paint(got);
  } catch (err) {
    state = err.message;
    paint({ notams: [], unplaced: [], count: 0 });
  }
}

function paint(got) {
  layer.clearLayers();
  for (const notice of shown) {
    if (notice.wide) continue;
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
  paintDock(got);
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

function paintDock(got) {
  const count = $('#notamCount');
  const list = $('#notamList');
  const note = $('#notamNote');
  if (!count || !enabled) return;

  const wide = shown.filter((n) => n.wide).length;
  const unplaced = (got?.unplaced ?? []).length;
  if (state) {
    count.textContent = 'Not available';
  } else {
    count.textContent = [
      `${shown.length - wide} drawn`,
      wide ? `${wide} area-wide` : null,
      unplaced ? `${unplaced} unplaced` : null,
    ].filter(Boolean).join(' · ') || 'Nothing closed in view';
  }

  // The ones that are real and are not circles, plus the ones with no
  // position. Both are notices in force and neither is on the map, so the
  // list is the only place they exist -- and a layer that silently drops
  // them would be saying the airspace is open.
  const missing = [...shown.filter((n) => n.wide), ...(got?.unplaced ?? [])];
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
  // What was actually asked, and of whom. "It does not work" is otherwise
  // unanswerable from the outside: an empty map means "nothing is closed" and
  // "nothing was asked" and they look identical. This is the one line that
  // tells them apart -- and it was not there when the layer was silently
  // asking for a radius four times wider than the service accepts.
  const asked = (got?.asked ?? []).join(', ');
  note.textContent = `${got?.source ?? 'FAA NOTAM API'} — a NOTAM is a notice `
    + 'to aircraft, not a report of anything in the air. Circles are a radius '
    + 'around a point, not a surveyed boundary.'
    + (asked ? ` Asked: ${asked}.` : '')
    + (got?.partial ? ' This view is wider than one query covers — zoom in for'
      + ' the rest.' : '')
    + (got?.capped ? ` ${got.total} notices exist here; the first few hundred`
      + ' were read.' : '');
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
