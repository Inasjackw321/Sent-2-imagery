// Air-threat reports from public Telegram channels, drawn as moving markers.
//
// Four monitoring channels post a running commentary of what is in the air --
// drones crossing an oblast, cruise missiles on a heading, strikes where they
// land. The backend reads their public web pages, has a language model turn the
// prose into positions and headings, and hands them here.
//
// Two different things are drawn, and the difference is the whole reason this
// file is careful:
//
//   the report    a place a channel actually named. That is an observation,
//                 and it is where the marker starts.
//
//   the drift     the marker slides along the reported heading at a typical
//                 speed for its kind. That is dead reckoning -- arithmetic, not
//                 tracking -- and it is wrong by more every second that passes.
//
// So the sliding is done here rather than fetched: the browser has the origin,
// the heading and the timestamp, which is everything the arithmetic needs, and
// re-deriving it locally means the markers move smoothly at one frame a second
// instead of jumping once a minute when the network answers. The backend is
// asked for new reports every minute and nothing more.
//
// Because the drift is a guess, tracks expire twenty minutes after the report
// that made them, the popup says how far a marker has been carried and from
// where, and a marker with no heading in its report simply does not move.

import { api } from './api.js';
import { $, el } from './ui.js';

// How often to ask for new reports. The backend keeps its own floor under
// this, so several open tabs cost one read of the channels between them.
const POLL_MS = 60000;

// How often to redraw the drift. A second is under the eye's threshold for
// "moving" at these speeds and is a hundredth of the work of a frame loop.
const STEP_MS = 1000;

const EARTH_KM = 6371.0088;

let map = null;
let layer = null;
let enabled = false;
let keySaved = false;
let feed = null;
let problem = '';
let poller = null;
let stepper = null;

// id -> { event, marker }. Kept across polls so a marker that is still being
// reported is moved rather than destroyed and rebuilt, which would flicker and
// would drop an open popup.
const drawn = new Map();

export function initOsint(leafletMap) {
  map = leafletMap;
  // Above the imagery panes and above the ship and quake markers: these are
  // the things you opened the layer for.
  map.createPane('osint').style.zIndex = 632;
  layer = L.layerGroup([], { pane: 'osint' });
  buildDock();
}

// ── The arithmetic ─────────────────────────────────────────────

/** Where you get to going `km` along a bearing, on a sphere.
 *
 * The same great circle the backend walks, for the same reason: adding degrees
 * of latitude and longitude is out by kilometres over the distance a cruise
 * missile covers between two reports, and wrong in a way that grows with
 * latitude -- which is where all of these reports come from.
 */
function advance(lat, lon, heading, km) {
  if (!(km > 0)) return [lat, lon];
  const d = km / EARTH_KM;
  const a = lat * Math.PI / 180;
  const brg = heading * Math.PI / 180;
  const sinLat = Math.min(1, Math.max(-1,
    Math.sin(a) * Math.cos(d) + Math.cos(a) * Math.sin(d) * Math.cos(brg)));
  const lat2 = Math.asin(sinLat);
  const lon2 = lon * Math.PI / 180 + Math.atan2(
    Math.sin(brg) * Math.sin(d) * Math.cos(a),
    Math.cos(d) - Math.sin(a) * sinLat);
  return [lat2 * 180 / Math.PI, ((lon2 * 180 / Math.PI) + 540) % 360 - 180];
}

/** Where a reported object is now, if it kept going as reported. */
function positionOf(event, atSeconds) {
  const speed = feed?.speeds?.[event.kind] ?? 0;
  if (event.heading == null || !(speed > 0)) {
    return { lat: event.origin_lat, lon: event.origin_lon, km: 0, arrived: false };
  }
  const minutes = Math.max(0, (atSeconds - event.seen) / 60);
  let km = speed * (minutes / 60);
  // A report that named where it was going also said where this stops. The
  // marker goes no further than the place it was flying to, and then goes.
  const arrived = event.dest_km > 0 && km >= event.dest_km;
  if (arrived) km = event.dest_km;
  const [lat, lon] = advance(event.origin_lat, event.origin_lon, event.heading, km);
  return { lat, lon, km, arrived };
}

// ── The markers ────────────────────────────────────────────────

/** What to write under a marker: the thing itself, and how many of it.
 *
 * "×3" rather than a plural, because the labels come from a table that has
 * "Ballistic" and "Cruise missile" in it and English plurals are not a
 * suffix. A count of one is left off entirely -- it is the common case and
 * saying it adds nothing.
 */
function label(event) {
  const name = escapeHtml(feed?.kinds?.[event.kind]?.label ?? 'Unidentified');
  return event.count > 1 ? `${name} ×${Number(event.count) || 1}` : name;
}

/** One marker: a triangle pointing where it is going, and its label.
 *
 * The heading is baked into the icon at build time rather than applied every
 * step, because a heading comes from a report and only changes when a new
 * report arrives. Moving the marker is then a `setLatLng` and nothing else.
 */
function icon(event) {
  const colour = feed?.kinds?.[event.kind]?.colour ?? '#ff8a3b';
  const moving = event.heading != null && (feed?.speeds?.[event.kind] ?? 0) > 0;
  const shape = moving
    ? `<svg class="ao-glyph" width="18" height="18" viewBox="0 0 18 18"
            style="transform: rotate(${event.heading.toFixed(1)}deg)">
         <path d="M9 1 L15.5 16 L9 12.4 L2.5 16 Z" fill="${colour}"/>
       </svg>`
    // Nothing with a heading, so nothing that points: a burst for a strike,
    // which is a place rather than a direction.
    : `<svg class="ao-glyph" width="18" height="18" viewBox="0 0 18 18">
         <path d="M9 0.5 L11 6.4 L17.5 5 L13 9.4 L17.5 13.8 L11 12.4
                  L9 17.5 L7 12.4 L0.5 13.8 L5 9.4 L0.5 5 L7 6.4 Z"
               fill="${colour}"/>
       </svg>`;
  return L.divIcon({
    className: 'ao-pin',
    // The label says what is being reported. An opaque identifier told the
    // reader nothing they could not see, and made them open a popup to find
    // out whether a triangle was a drone or a missile.
    //
    // No speed: the speed is a table lookup for the type, not a measurement
    // of the object, and printing it would dress an assumption up as telemetry.
    html: `${shape}<span class="ao-tag" style="color:${colour}">${label(event)}</span>`,
    iconSize: [18, 18],
    iconAnchor: [9, 9],
  });
}

function popup(event, km) {
  const rows = [];
  if (event.place) rows.push(`Reported over ${escapeHtml(event.place)}`);
  if (event.toward) {
    rows.push(`Reported travelling to ${escapeHtml(event.toward)}`
      + (event.dest_km ? `, ${Math.round(event.dest_km)} km away` : ''));
  }
  if (event.count > 1) rows.push(`${Number(event.count) || 1} reported together`);
  rows.push(`${Math.round(event.age_minutes ?? 0)} min since the report`);
  if (km > 0.5) {
    rows.push('<b>Position estimated</b> — carried '
      + `${Math.round(km)} km along the reported course at a typical speed for `
      + 'the type. Not a track.');
  }
  return `<div class="ao-pop">
    <h4>${label(event)}</h4>
    ${rows.map((r) => `<p>${r}</p>`).join('')}
    ${event.text ? `<blockquote>${escapeHtml(event.text)}</blockquote>` : ''}
    <p class="ao-src">${escapeHtml(event.channel ?? '')}</p>
  </div>`;
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** Bring the drawn markers into line with the events just fetched. */
function reconcile(events) {
  const now = Date.now() / 1000;
  const alive = new Set();
  for (const event of events) {
    const at = positionOf(event, now);
    // Already where it was going by the time it got here. The backend expires
    // these too, but the two clocks are a minute apart and a marker sitting
    // on its destination for that minute is the thing being fixed.
    if (at.arrived) continue;
    alive.add(event.id);
    const held = drawn.get(event.id);
    if (held) {
      // A fresh report for something already on the map: the course or the
      // count may have changed, so the icon is rebuilt only when it differs.
      if (held.event.heading !== event.heading
          || held.event.kind !== event.kind
          || held.event.count !== event.count) {
        held.marker.setIcon(icon(event));
      }
      held.event = event;
      held.marker.setLatLng([at.lat, at.lon]);
    } else {
      const marker = L.marker([at.lat, at.lon], {
        icon: icon(event), pane: 'osint', keyboard: false,
      });
      marker.bindPopup(() => popup(event, positionOf(event, Date.now() / 1000).km));
      marker.addTo(layer);
      drawn.set(event.id, { event, marker });
    }
  }
  for (const [id, held] of drawn) {
    if (alive.has(id)) continue;
    layer.removeLayer(held.marker);
    drawn.delete(id);
  }
}

/** Slide every marker along its heading. Called once a second. */
function step() {
  // Nothing to see and nothing to spend: a hidden tab gets no arithmetic.
  if (!enabled || document.hidden || !drawn.size) return;
  const now = Date.now() / 1000;
  let gone = false;
  for (const [id, held] of drawn) {
    if (held.event.heading == null) continue;
    const at = positionOf(held.event, now);
    if (at.arrived) {
      // It has reached the place the report said it was going to. Carrying it
      // past there would be inventing a second journey nobody described.
      layer.removeLayer(held.marker);
      drawn.delete(id);
      gone = true;
      continue;
    }
    held.marker.setLatLng([at.lat, at.lon]);
  }
  if (gone) paintDock();
}

// ── The feed ───────────────────────────────────────────────────

async function load() {
  const had = keySaved;
  try {
    feed = await api.osint();
    keySaved = Boolean(feed.keyed);
    problem = '';
    reconcile(feed.events ?? []);
  } catch (err) {
    problem = err.message;
  }
  // The key field is built, not shown and hidden, so learning that the server
  // already has a key -- in demo mode, or after a reload with the process
  // still up -- means rebuilding rather than repainting. Without this the
  // panel asks for a key it is not using.
  if (keySaved !== had) buildDock();
  else paintDock();
}

async function saveKey(value) {
  try {
    const out = await api.osintKey(value);
    keySaved = out.set;
    buildDock();
    if (out.set && enabled) await load();
  } catch (err) {
    problem = err.message;
    paintDock();
  }
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#osintDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'ao-toggle', id: 'osintToggle', onclick: toggle },
      el('span', { class: 'ao-mark' }, '▲'), 'Air threats'),
    el('div', { class: 'ao-body', id: 'osintBody', hidden: !enabled },
      ...(keySaved ? [] : [
        el('div', { class: 'keyfield' },
          el('input', {
            type: 'password', id: 'osintKey', autocomplete: 'off', spellcheck: false,
            placeholder: 'Paste your OpenRouter key',
            onkeydown: (e) => { if (e.key === 'Enter') saveKey(e.target.value); },
          }),
          el('button', {
            class: 'keyfield-go', type: 'button',
            onclick: () => saveKey($('#osintKey').value),
          }, 'Save')),
        el('div', { class: 'ao-hint' },
          el('a', {
            href: 'https://openrouter.ai/keys',
            target: '_blank', rel: 'noopener noreferrer',
          }, 'Get a free key'),
          ' · kept in memory only'),
      ]),
      el('div', { class: 'ao-count', id: 'osintCount' }, 'Loading…'),
      el('div', { class: 'ao-note', id: 'osintNote' }, '')));
  paintDock();
}

function toggle() {
  enabled = !enabled;
  $('#osintToggle').classList.toggle('is-on', enabled);
  $('#osintBody').hidden = !enabled;
  if (enabled) {
    layer.addTo(map);
    load();
    poller = setInterval(load, POLL_MS);
    stepper = setInterval(step, STEP_MS);
  } else {
    clearInterval(poller);
    clearInterval(stepper);
    poller = stepper = null;
    layer.remove();
    layer.clearLayers();
    drawn.clear();
  }
  paintDock();
}

function paintDock() {
  const count = $('#osintCount');
  const note = $('#osintNote');
  if (!count || !note || !enabled) return;

  if (!keySaved) {
    count.textContent = 'Needs an OpenRouter key';
    note.textContent = 'The reports are prose; a model turns them into positions. '
      + 'The key is used on the server and stored nowhere.';
    return;
  }
  const n = drawn.size;
  count.textContent = n
    ? `${n} in the air or down, last ${feed?.keep_minutes ?? 20} min`
    : 'Nothing being reported';

  const lines = [];
  const demo = feed?.state?.startsWith('demo');
  if (demo) {
    lines.push('Demo mode: these events are invented.');
  } else {
    lines.push('Public Telegram channels: '
      + `${(feed?.channels ?? []).join(', ')}. Markers with an arrow are `
      + 'carried along the reported heading at a typical speed — estimated, not tracked.');
  }
  // The state is the backend explaining itself -- a channel that would not
  // answer, a rate limit. It is worth saying, but not when it is only
  // repeating what the line above already said.
  if (problem) lines.push(problem);
  else if (!demo && feed?.state && feed.state !== 'nothing new') lines.push(feed.state);
  note.textContent = lines.filter(Boolean).join(' ');
}
