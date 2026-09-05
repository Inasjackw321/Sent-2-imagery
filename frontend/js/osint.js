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

// How many reports the panel lists before saying how many more there are.
// Generous because strikes are held for hours and the list scrolls.
const LIST_ROWS = 20;

// How often to redraw the drift. A second is under the eye's threshold for
// "moving" at these speeds and is a hundredth of the work of a frame loop.
const STEP_MS = 1000;

// How big a marker is drawn, in pixels. The glyphs are authored on an 18-unit
// grid and scaled to this, so one number changes all of them together.
const GLYPH = 24;

const EARTH_KM = 6371.0088;

let map = null;
let layer = null;
let areas = null;
let areaInk = null;
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
  // The shaded areas go in their own pane, under the markers. A wash over a
  // whole oblast drawn in the marker pane would sit on top of the arrows
  // crossing it, and swallow their clicks with them.
  map.createPane('osintArea').style.zIndex = 428;
  map.getPane('osintArea').style.pointerEvents = 'none';
  // An explicit SVG renderer, because the map is built with preferCanvas and
  // a canvas-rendered circle is pixels: it has no element, so className is
  // ignored and the CSS that makes an alert pulse never applies to anything.
  // There are a handful of these at a time, so the reason preferCanvas exists
  // -- hundreds of vectors -- does not arise.
  areaInk = L.svg({ pane: 'osintArea' });
  layer = L.layerGroup([], { pane: 'osint' });
  areas = L.layerGroup([], { pane: 'osintArea' });
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

/** What kind of thing this is, from the table the backend sent.
 *
 * Read from the feed rather than kept here, so the speeds and the way each
 * kind moves are defined once, on the server, and the two ends cannot drift
 * into disagreeing about how fast a jet drone flies.
 */
const look = (event) => feed?.kinds?.[event.kind] ?? {};
const speedOf = (event) => look(event).speed ?? feed?.speeds?.[event.kind] ?? 0;
const motionOf = (event) => event.motion ?? look(event).motion ?? 'track';

/** How long this kind of marker stays, in minutes. */
const keepOf = (event) => feed?.keep?.[event.kind] ?? look(event).keep
  ?? feed?.keep_minutes ?? 20;

/**
 * How new a report is, as a fraction of its own lifetime. 1 is now, 0 is due
 * to go.
 *
 * It is measured against the marker's OWN lifetime rather than a fixed
 * window, because they differ by a factor of eighteen: a drone is gone in
 * twenty minutes and a strike stays six hours. Against a fixed window every
 * strike would sit at full strength for its whole life and then vanish, so a
 * map at the end of a long night would show a wall of bursts all looking as
 * though they had just happened.
 */
function freshness(event) {
  const minutes = event.age_minutes ?? 0;
  const life = Math.max(1, keepOf(event));
  return Math.max(0, Math.min(1, 1 - minutes / life));
}

// How faint a marker gets by the end of its life. Not to nothing: it is still
// a thing that happened, and it should be findable right up until it goes.
const FADE_TO = 0.4;

const paleness = (event) => FADE_TO + (1 - FADE_TO) * freshness(event);

// Matches the backend's orbit. Both ends compute it, so both ends need the
// same numbers; they arrive with the feed for exactly that reason.
const orbitKm = () => feed?.orbit?.km ?? 9;
const orbitMinutes = () => feed?.orbit?.minutes ?? 7;

/** Where a reported object is now, if it carried on as reported. */
function positionOf(event, atSeconds) {
  const still = { lat: event.origin_lat, lon: event.origin_lon, km: 0, arrived: false };
  const speed = speedOf(event);
  const motion = motionOf(event);
  const minutes = Math.max(0, (atSeconds - event.seen) / 60);

  if (motion === 'orbit' && speed > 0) {
    // On station: round and round the place it was reported over, pointing
    // along the circle. Carrying a loitering drone off in a straight line for
    // twenty minutes would put it in the next country and claim something the
    // report never said.
    const angle = (360 * minutes / orbitMinutes()) % 360;
    const [lat, lon] = advance(event.origin_lat, event.origin_lon, angle, orbitKm());
    return { lat, lon, km: 0, arrived: false, facing: (angle + 90) % 360 };
  }

  if (event.heading == null || !(speed > 0) || motion !== 'track') return still;

  let km = speed * (minutes / 60);
  // A report that named where it was going also said where this stops. The
  // marker goes no further than the place it was flying to, and then goes.
  const arrived = event.dest_km > 0 && km >= event.dest_km;
  if (arrived) km = event.dest_km;
  const [lat, lon] = advance(event.origin_lat, event.origin_lon, event.heading, km);
  return { lat, lon, km, arrived, facing: event.heading };
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

/** The drawing for one kind of thing, pointing where it is going.
 *
 * Three shapes, because there are three things being said and drawing them
 * alike was the bug in the picture that prompted this: a jet drone flying past
 * a town on a course north was drawn as a starburst -- the mark for something
 * that has come down -- because it had a compass course and no named
 * destination, and only a destination counted as movement.
 *
 *   arrow    it is flying, and this is the way. Rotated to the course.
 *   ring     it is on station over here, going round. The gap in the ring
 *            turns with it, so it reads as circling rather than as a dot.
 *   burst    it is not flying: a strike, or something brought down.
 *   chevron  a warning about a place, which is not an object at all.
 */
function glyph(event, colour, facing) {
  const motion = motionOf(event);
  // Drawn at GLYPH pixels from an 18-unit viewBox, so making them bigger is
  // one number here: the artwork scales rather than being redrawn, and the
  // anchor below moves with it.
  const svg = (shape, body, turn) =>
    `<svg class="ao-glyph" data-shape="${shape}" width="${GLYPH}" height="${GLYPH}"
          viewBox="0 0 18 18"${turn == null ? '' : ` style="transform: rotate(${turn.toFixed(1)}deg)"`}>`
    + `${body}</svg>`;

  if (event.kind === 'alert') {
    return svg('chevron',
      `<path d="M9 1.5 L17 15.5 H1 Z" fill="none" stroke="${colour}"
             stroke-width="2" stroke-linejoin="round"/>
       <path d="M9 6.5 v3.6" stroke="${colour}" stroke-width="2" stroke-linecap="round"/>
       <circle cx="9" cy="12.8" r="1.1" fill="${colour}"/>`);
  }
  if (motion === 'still') {
    return svg('burst',
      `<path d="M9 0.5 L11 6.4 L17.5 5 L13 9.4 L17.5 13.8 L11 12.4
                L9 17.5 L7 12.4 L0.5 13.8 L5 9.4 L0.5 5 L7 6.4 Z" fill="${colour}"/>`);
  }
  if (motion === 'orbit') {
    return svg('ring',
      `<path d="M9 2.2 A6.8 6.8 0 1 1 4.2 4.2" fill="none" stroke="${colour}"
             stroke-width="2" stroke-linecap="round"/>
       <path d="M9 2.2 L6.2 0.4 L6.2 4 Z" fill="${colour}"/>
       <circle cx="9" cy="9" r="1.6" fill="${colour}"/>`, facing ?? 0);
  }
  if (facing == null) {
    // In the air, but the report said nothing about which way. An arrow here
    // would point north and mean it -- the same invention this whole layer
    // exists to avoid, just with a nicer shape. A ringed dot says "reported
    // over here" and claims nothing further.
    return svg('dot',
      `<circle cx="9" cy="9" r="6.4" fill="none" stroke="${colour}"
               stroke-width="1.6" opacity="0.65"/>
       <circle cx="9" cy="9" r="3.1" fill="${colour}"/>`);
  }
  return svg('arrow',
    `<path d="M9 1 L15.5 16 L9 12.4 L2.5 16 Z" fill="${colour}"/>`, facing);
}

/** One marker: its glyph, and its label underneath. */
function icon(event, facing) {
  const colour = look(event).colour ?? '#ff8a3b';
  return L.divIcon({
    className: 'ao-pin',
    // The label says what is being reported. An opaque identifier told the
    // reader nothing they could not see, and made them open a popup to find
    // out whether a triangle was a drone or a missile.
    //
    // No speed: the speed is a table lookup for the type, not a measurement
    // of the object, and printing it would dress an assumption up as telemetry.
    html: `${glyph(event, colour, facing)}`
      + `<span class="ao-tag" style="color:${colour}">${label(event)}</span>`,
    // The anchor is the middle of the glyph, which is the reported position.
    // Derived from the size rather than written out, so the two cannot drift
    // apart and quietly offset every marker on the map.
    iconSize: [GLYPH, GLYPH],
    iconAnchor: [GLYPH / 2, GLYPH / 2],
  });
}

function popup(event, km) {
  const rows = [];
  if (event.place) {
    // What the report said, and what the gazetteer matched it to. They are
    // different claims and the second is where the marker actually came from,
    // so a bad match can be seen rather than having to be deduced.
    rows.push(`Reported over ${escapeHtml(event.place)}`
      + (event.place_match && !event.place_match.toLowerCase().startsWith(event.place.toLowerCase())
        ? ` <span class="ao-match">→ ${escapeHtml(event.place_match.split(',').slice(0, 2).join(','))}</span>`
        : ''));
  }
  if (event.toward) {
    rows.push(`Reported travelling to ${escapeHtml(event.toward)}`
      + (event.dest_km ? `, ${Math.round(event.dest_km)} km away` : ''));
  }
  if (event.count > 1) rows.push(`${Number(event.count) || 1} reported together`);
  rows.push(`${since(event.age_minutes ?? 0)} since the report`);
  if (event.region_wide) {
    rows.push('<b>Region-wide</b> — the report names the whole area, and the '
      + 'outline is that area\u2019s own boundary.');
  } else if (hasArea(event)) {
    rows.push(`Shaded about ${Math.round(event.area_km ?? 8)} km around — the `
      + 'size of the place named, not a measured extent.');
  }
  if (motionOf(event) === 'orbit') {
    // Said plainly, because a marker going round in circles is the one thing
    // here most likely to be read as a measurement of a flight path.
    rows.push('<b>Shown circling</b> — reported on station here. The circle is '
      + 'a way of saying "over this place and still flying", not a track.');
  } else if (km > 0.5) {
    rows.push('<b>Position estimated</b> — carried '
      + `${Math.round(km)} km along the reported course at a typical speed `
      + `(${Math.round(speedOf(event))} km/h) for the type. Not a track.`);
  }
  return `<div class="ao-pop">
    <h4>${label(event)}</h4>
    ${rows.map((r) => `<p>${r}</p>`).join('')}
    ${event.summary ? `<p class="ao-sum">${escapeHtml(event.summary)}</p>` : ''}
    ${event.text ? `<blockquote>${escapeHtml(event.text)}</blockquote>` : ''}
    <p class="ao-src">${escapeHtml(event.channel ?? '')}</p>
  </div>`;
}

/** A compact age for the list: minutes, then hours. */
const ago = (minutes) => (minutes < 60
  ? `${Math.round(minutes)}m`
  : `${Math.floor(minutes / 60)}h`);

/** How long ago, in a unit that suits how long ago it was. */
function since(minutes) {
  const mins = Math.round(minutes);
  if (mins < 1) return 'less than a minute';
  if (mins < 90) return `${mins} min`;
  const hours = Math.floor(mins / 60);
  const rest = mins % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

const escapeHtml = (s) => String(s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/**
 * The shaded ground an alert or a strike covers.
 *
 * Only for the things that ARE somewhere rather than passing over it: an
 * air-raid warning and a strike. A drone crossing an oblast is at a point on
 * its way through, and shading the whole region for it would say the warning
 * covers ground nobody mentioned.
 *
 * The radius comes from the gazetteer's own extent for the place, not from a
 * fixed number, because the two cases are enormously different in size -- a
 * strike in a village is a couple of kilometres and a warning over an oblast
 * is a hundred. One size would either lose the strike in a blob or shrink the
 * oblast to a dot.
 */
function areaFor(event) {
  const colour = look(event).colour ?? '#ff8a3b';
  const style = {
    pane: 'osintArea',
    renderer: areaInk,
    interactive: false,
    className: `ao-area ao-area-${event.kind}${event.region_wide ? ' is-region' : ''}`,
    color: colour,
    weight: event.region_wide ? 2 : 1.5,
    opacity: 0.55,
    fillColor: colour,
    fillOpacity: event.region_wide ? 0.1 : 0.12,
  };

  // A warning covering a whole region gets that region's actual outline. A
  // circle over the middle of an oblast both misses ground the warning covers
  // and covers ground it does not, and at the size of a province that is not
  // a rounding error -- it is most of a country's worth of wrong.
  if (event.shape) {
    return L.geoJSON({ type: 'Feature', geometry: event.shape, properties: {} }, {
      pane: 'osintArea',
      renderer: areaInk,
      interactive: false,
      style,
    });
  }

  return L.circle([event.origin_lat, event.origin_lon], {
    ...style,
    radius: Math.max(1500, (event.area_km ?? 8) * 1000),
  });
}

/** Whether this report is about an area rather than something passing over. */
const hasArea = (event) => event.placed !== false
  && motionOf(event) === 'still'
  && Number.isFinite(event.origin_lat);

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
        held.marker.setIcon(icon(event, at.facing));
      }
      held.event = event;
      held.marker.setLatLng([at.lat, at.lon]);
      age(held);
    } else {
      const marker = L.marker([at.lat, at.lon], {
        icon: icon(event, at.facing), pane: 'osint', keyboard: false,
      });
      marker.bindPopup(() => popup(event, positionOf(event, Date.now() / 1000).km));
      marker.addTo(layer);
      const area = hasArea(event) ? areaFor(event) : null;
      area?.addTo(areas);
      const held = { event, marker, area };
      drawn.set(event.id, held);
      age(held);
    }
  }
  for (const [id, held] of drawn) {
    if (alive.has(id)) continue;
    layer.removeLayer(held.marker);
    if (held.area) areas.removeLayer(held.area);
    drawn.delete(id);
  }
}

/**
 * Fade a marker towards the end of its life.
 *
 * Applied as element opacity rather than as the circle's own fill, because
 * the fill is what the pulse animation drives -- setting it here would be
 * overwritten on the next keyframe. Element opacity multiplies with it
 * instead, so an old alert still breathes, faintly.
 */
function age(held) {
  const pale = paleness(held.event);
  held.marker.setOpacity(pale);
  // A circle is one element; a boundary drawn through GeoJSON is a group of
  // them, one per ring. Both have to be faded, so this walks whatever the
  // layer turned out to be.
  held.area?.eachLayer?.((part) => {
    const ink = part.getElement?.();
    if (ink) ink.style.opacity = String(pale);
  });
  const own = held.area?.getElement?.();
  if (own) own.style.opacity = String(pale);
}

/** Move every marker along. Called once a second. */
function step() {
  // Nothing to see and nothing to spend: a hidden tab gets no arithmetic.
  if (!enabled || document.hidden || !drawn.size) return;
  const now = Date.now() / 1000;
  let gone = false;
  for (const [id, held] of drawn) {
    const motion = motionOf(held.event);
    if (motion === 'still') continue;
    if (motion === 'track' && held.event.heading == null) continue;

    const at = positionOf(held.event, now);
    if (at.arrived) {
      // It has reached the place the report said it was going to. Carrying it
      // past there would be inventing a second journey nobody described.
      layer.removeLayer(held.marker);
      if (held.area) areas.removeLayer(held.area);
      drawn.delete(id);
      gone = true;
      continue;
    }
    held.marker.setLatLng([at.lat, at.lon]);

    // Something circling changes the way it is facing every second, so its
    // glyph has to turn with it. The SVG is spun in place rather than the
    // icon rebuilt: setIcon replaces the element, which would drop an open
    // popup and re-run the marker's layout sixty times a minute.
    if (motion === 'orbit' && at.facing != null) {
      const mark = held.marker.getElement()?.querySelector('.ao-glyph');
      if (mark) mark.style.transform = `rotate(${at.facing.toFixed(1)}deg)`;
    }
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
      // The reports, whether or not they could be put on the map. This list is
      // the fix for the complaint that the layer "does not work": a night when
      // the gazetteer cannot place the names still shows six reports here,
      // which is a feed doing its job, and looks nothing like a broken one.
      el('div', { class: 'ao-list', id: 'osintList' }),
      el('div', { class: 'ao-note', id: 'osintNote' }, '')));
  paintDock();
}

function toggle() {
  enabled = !enabled;
  $('#osintToggle').classList.toggle('is-on', enabled);
  $('#osintBody').hidden = !enabled;
  if (enabled) {
    areas.addTo(map);
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
    areas.remove();
    areas.clearLayers();
    drawn.clear();
  }
  paintDock();
}

/** How many reports arrived, and how many of them could be placed. */
function tally() {
  const got = feed?.reports ?? { placed: 0, unplaced: 0 };
  return { ...got, total: (got.placed ?? 0) + (got.unplaced ?? 0) };
}

function paintDock() {
  const count = $('#osintCount');
  const note = $('#osintNote');
  const list = $('#osintList');
  if (!count || !note || !enabled) return;

  if (!keySaved) {
    count.textContent = 'Needs an OpenRouter key';
    list?.replaceChildren();
    note.textContent = 'The reports are prose, so a model reads them on the server. '
      + 'Where they are comes from a gazetteer, not from the model. '
      + 'The key is used on the server and stored nowhere.';
    return;
  }

  const n = drawn.size;
  const strikes = [...drawn.values()].filter((h) => h.event.kind === 'explosion').length;
  count.textContent = n
    ? `${n} on the map${strikes ? ` · ${strikes} struck` : ''}`
    : 'Nothing on the map';

  // The recent reports, newest first, the mapped ones clickable.
  //
  // The cap used to be eight with nothing said about the rest, which quietly
  // reintroduced the thing the backend is careful about: strikes are held for
  // hours, so an older one sat on the map with no row in the panel to explain
  // it. The list scrolls, so it can be longer -- and when it is still cut, it
  // says so rather than just ending.
  const everything = feed?.alerts ?? [];
  const alerts = everything.slice(0, LIST_ROWS);
  const hidden = everything.length - alerts.length;
  list?.replaceChildren(...alerts.map((item) => {
    const kind = feed?.kinds?.[item.kind] ?? {};
    const mins = Math.max(0, Math.round(Date.now() / 1000 - item.seen) / 60);
    const row = el('button', {
      class: `ao-row${item.placed ? '' : ' is-unplaced'}`,
      type: 'button',
      title: item.text ?? '',
      onclick: () => goTo(item),
    },
    el('i', { style: kind.colour ? `background:${kind.colour}` : '' }),
    el('span', {
      class: `ao-row-what${item.by === 'rules' ? ' is-plain' : ''}`,
      title: item.by === 'rules' ? 'Read without the model' : '',
    }, item.summary || kind.label || item.kind),
    el('span', { class: 'ao-row-when' }, mins < 1 ? 'now' : ago(mins)));
    return row;
  }), ...(hidden > 0
    ? [el('div', { class: 'ao-row-more' }, `+${hidden} older`)]
    : []));

  const lines = [];
  const demo = feed?.state?.startsWith('demo');
  const got = tally();
  const by = feed?.read_by ?? {};
  if (demo) {
    lines.push('Demo mode: these reports are invented.');
  } else if (by.rules && !by.model) {
    // The case that used to show an empty map and a line of red text. It is
    // worth saying plainly that the layer is working, just not as well.
    lines.push(`${(feed?.channels ?? []).length} public Telegram channels, read `
      + 'without the model — the patterns these reports are written in are '
      + 'regular enough to follow. A model reads them better.');
  } else if (by.rules) {
    lines.push(`${(feed?.channels ?? []).length} public Telegram channels. `
      + `${by.model} read by the model, ${by.rules} by pattern.`);
  } else {
    lines.push(`${(feed?.channels ?? []).length} public Telegram channels. `
      + 'A model reads the words; a gazetteer decides where they are.');
  }
  // Said plainly, because it is the number that explains an empty map.
  if (got.total) {
    lines.push(`${got.placed} of ${got.total} reports placed`
      + (got.unplaced ? `; ${got.unplaced} named nowhere a map knows.` : '.'));
  }
  if (n) {
    lines.push('Arrows are carried along the reported course at a typical speed '
      + 'for the type — estimated, not tracked. Strikes are held for '
      + `${Math.round((feed?.keep?.explosion ?? 360) / 60)} hours and fade as `
      + 'they age; things in flight go after '
      + `${feed?.keep_minutes ?? 20} minutes, when the estimate stops meaning much.`);
  }
  if (problem) lines.push(problem);
  else if (!demo && feed?.state && feed.state !== 'nothing new') lines.push(feed.state);
  note.textContent = lines.filter(Boolean).join(' ');
}

/** Take the map to a report, if it is somewhere. */
function goTo(item) {
  const held = drawn.get(item.id);
  if (!held) return;
  // A region-wide warning is framed rather than flown to. Keeping the current
  // zoom puts you inside an oblast looking at a wash of colour with no edge
  // in view, which tells you nothing that the panel had not already said.
  const bounds = held.event.region_wide ? held.area?.getBounds?.() : null;
  if (bounds?.isValid?.()) map.flyToBounds(bounds, { padding: [40, 40], duration: 0.7 });
  else map.flyTo(held.marker.getLatLng(), Math.max(map.getZoom(), 8), { duration: 0.7 });
  held.marker.openPopup();
}
