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
 * Read from the feed rather than kept here, so the kinds are defined once, on
 * the server, and the two ends cannot drift apart about what a jet drone is.
 */
const look = (event) => feed?.kinds?.[event.kind] ?? {};
const motionOf = (event) => event.motion ?? look(event).motion ?? 'track';

/** How long this kind of marker stays, in minutes. */
const keepOf = (event) => feed?.keep?.[event.kind] ?? look(event).keep
  ?? feed?.keep_minutes ?? 20;

/**
 * Where a report goes: where it was reported, and nowhere else.
 *
 * Markers used to be carried along their reported course between polls, at a
 * typical speed for their kind, and loitering drones flown in circles. Both
 * were labelled as estimates and both are gone. A map where everything drifts
 * is hard to read, the marks wander off the places the reports actually
 * named, and a mark sliding across a province looks tracked whatever the
 * panel says. The course is still known and still drawn -- the icon points
 * along it -- but nothing is carried anywhere on the strength of it.
 */
const positionOf = (event) => ({
  lat: event.origin_lat,
  lon: event.origin_lon,
  facing: event.heading,
});

/**
 * How new a report is, as a fraction of its own lifetime. 1 is now, 0 is due
 * to go.
 *
 * Measured against the marker's OWN lifetime, because they differ by a factor
 * of eighteen: a drone is gone in twenty minutes and a strike stays six
 * hours. Against a fixed window every strike would sit at full strength for
 * its whole life and then vanish.
 */
function freshness(event) {
  const minutes = event.age_minutes ?? 0;
  return Math.max(0, Math.min(1, 1 - minutes / Math.max(1, keepOf(event))));
}

// How faint a marker gets by the end of its life. Not to nothing: it is still
// a thing that happened, and it should be findable right up until it goes.
const FADE_TO = 0.45;

const paleness = (event) => FADE_TO + (1 - FADE_TO) * freshness(event);

// ── The markers ────────────────────────────────────────────────

/** What to write under a marker: the thing itself, and how many of it.
 *
 * "×3" rather than a plural, because the labels come from a table that has
 * "Ballistic" and "Cruise missile" in it and English plurals are not a
 * suffix. A count of one is left off entirely -- it is the common case and
 * saying it adds nothing.
 */
function label(event) {
  // No count on the mark. Each one IS one object now, so "Drone ×3" written
  // under each of three drones would read as nine.
  return escapeHtml(feed?.kinds?.[event.kind]?.label ?? 'Unidentified');
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
// The drawings, one per kind, on an 18-unit grid with north up.
//
// Colour alone was not enough. Every flying kind shared one arrow, so a
// Shahed, a jet drone, a cruise missile and a ballistic missile were the same
// triangle in slightly different reds -- which on a map at a glance is no
// information at all. These are silhouettes: the thing itself, pointed the
// way it is going.
const SILHOUETTE = {
  // A delta wing. What a Shahed looks like from above, and unmistakably a
  // one-way attack drone rather than a missile.
  drone: (c) => `<path d="M9 1.2 L14.8 15.2 L9 12.2 L3.2 15.2 Z" fill="${c}"/>`,

  // The same wing, swept back harder, with an exhaust behind it.
  jet_drone: (c) => `<path d="M9 0.8 L15.4 14 L9 11 L2.6 14 Z" fill="${c}"/>
                     <path d="M7.6 14.4 h2.8 v2.6 h-2.8 Z" fill="${c}"/>`,

  // A body with stub wings and a tail: long and thin, which is the thing that
  // reads as "missile" and not "aircraft" at this size.
  cruise: (c) => `<path d="M9 0.6 L10.5 4.4 v8.6 h-3 V4.4 Z" fill="${c}"/>
                  <path d="M7.5 7.4 L3.4 11 v1.6 l4.1 -1.8 Z" fill="${c}"/>
                  <path d="M10.5 7.4 L14.6 11 v1.6 l-4.1 -1.8 Z" fill="${c}"/>
                  <path d="M7.2 13.4 h3.6 l-1.8 3.6 Z" fill="${c}"/>`,

  // A dart: narrower still, with fins at the very back. Ballistic things are
  // the fastest thing on this map and the shape says so.
  ballistic: (c) => `<path d="M9 0.4 L10.4 5 v7.4 h-2.8 V5 Z" fill="${c}"/>
                     <path d="M7.6 12 L5.4 16.6 h2.2 Z" fill="${c}"/>
                     <path d="M10.4 12 L12.6 16.6 h-2.2 Z" fill="${c}"/>
                     <path d="M8.2 12.4 h1.6 v4.4 h-1.6 Z" fill="${c}"/>`,

  // Wings and a tailplane. A crewed aircraft, not a munition.
  aircraft: (c) => `<path d="M9 0.8 c1 0 1.5 1.4 1.5 3.4 v3.2 l5.6 3.2 v2
                             l-5.6 -1.8 v3.2 l2 1.6 v1.2 l-3.5 -1 l-3.5 1
                             v-1.2 l2 -1.6 v-3.2 L1.9 12.6 v-2 l5.6 -3.2
                             V4.2 c0 -2 0.5 -3.4 1.5 -3.4 Z" fill="${c}"/>`,

  helicopter: (c) => `<path d="M1.5 3.2 h15 v1.4 h-15 Z" fill="${c}"/>
                      <path d="M8.4 4.6 h1.2 v2.2 h-1.2 Z" fill="${c}"/>
                      <ellipse cx="9" cy="10.2" rx="3.4" ry="3.4" fill="${c}"/>
                      <path d="M11.8 12 L16 15.6 v1.2 l-4.8 -3.2 Z" fill="${c}"/>`,
};

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

  const drawn = SILHOUETTE[event.kind];
  if (facing == null) {
    // In the air, but the report said nothing about which way. A silhouette
    // here would point north and mean it -- the same invention this whole
    // layer exists to avoid, just with a nicer shape. So the thing is drawn
    // inside a ring instead: what it is, with no claim about its course.
    return svg('dot',
      `<circle cx="9" cy="9" r="7.6" fill="none" stroke="${colour}"
               stroke-width="1.2" opacity="0.5"/>`
      + `<g transform="translate(9 9) scale(0.62) translate(-9 -9)">`
      + `${(drawn ?? SILHOUETTE.drone)(colour)}</g>`);
  }
  if (drawn) return svg(event.kind, drawn(colour), facing);
  return svg('arrow',
    `<path d="M9 1 L15.5 16 L9 12.4 L2.5 16 Z" fill="${colour}"/>`, facing);
}

/** One marker: its glyph, and its label underneath. */
function icon(event, facing) {
  const colour = look(event).colour ?? '#ff8a3b';
  const loud = event.kind === 'alert' || event.kind === 'explosion';
  return L.divIcon({
    // Warnings and strikes are the two things somebody scanning this map is
    // looking for, and at the zoom it gets used at a 24-pixel glyph in a
    // colour was disappearing into the basemap. They get a pulsing halo
    // behind them and a label with a background, which is the difference
    // between something you can find and something you have to hunt for.
    className: `ao-pin${loud ? ` is-loud is-${event.kind}` : ''}`,
    // The label says what is being reported. An opaque identifier told the
    // reader nothing they could not see, and made them open a popup to find
    // out whether a triangle was a drone or a missile.
    //
    // No speed: the speed is a table lookup for the type, not a measurement
    // of the object, and printing it would dress an assumption up as telemetry.
    html: (loud ? `<span class="ao-halo" style="background:${colour}"></span>` : '')
      + `${glyph(event, colour, facing)}`
      + `<span class="ao-tag" style="color:${colour}">${label(event)}</span>`,
    // The anchor is the middle of the glyph, which is the reported position.
    // Derived from the size rather than written out, so the two cannot drift
    // apart and quietly offset every marker on the map.
    iconSize: [GLYPH, GLYPH],
    iconAnchor: [GLYPH / 2, GLYPH / 2],
  });
}

function popup(event) {
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
  if (event.count > 1) {
    const shown = Math.min(MOST_SHOWN, Number(event.count) || 1);
    rows.push(`${Number(event.count)} reported together, drawn as `
      + `${shown} mark${shown === 1 ? '' : 's'} spread a couple of kilometres `
      + 'apart to be countable — the report gave one position, not '
      + `${shown} of them.`);
  }
  rows.push(`${since(event.age_minutes ?? 0)} since the report`);
  if (event.toward) rows.push('Course shown, not followed — the mark stays '
    + 'where the report put it.');
  if (event.region_wide) {
    rows.push('<b>Region-wide</b> — the report names the whole area, and the '
      + 'outline is that area\u2019s own boundary.');
  } else if (event.region_scope === 'located') {
    // The distinction that matters: the outline is not what it is over, it is
    // how well anyone knows where it is.
    rows.push('<b>Located to this region only</b> — the report named the '
      + 'region and no place within it, so the outline is the area it could '
      + 'be anywhere in, not an area under attack.');
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
    className: `ao-area ao-area-${event.kind}`
      + (event.region_scope ? ` is-region is-${event.region_scope}` : ''),
    color: colour,
    // A warning covering a region is the loudest thing this layer draws, so
    // it gets the heaviest line. At the zoom a whole country fits in, a
    // one-pixel stroke was simply not visible.
    weight: event.region_wide ? 3 : 1.5,
    opacity: event.region_scope === 'located' ? 0.4
      : event.region_wide ? 0.85 : 0.6,
    fillColor: colour,
    // A region that merely says how precisely something was located is barely
    // filled. Filling it like a warning would say the whole province is under
    // attack, when all the report said was which province it was over.
    fillOpacity: event.region_scope === 'located' ? 0.04
      : event.region_wide ? 0.16 : 0.18,
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
  && Number.isFinite(event.origin_lat)
  // Either it covers ground, or the report only located it to a region --
  // both are worth drawing, and they are drawn differently.
  && (motionOf(event) === 'still' || Boolean(event.shape));

/** Bring the drawn markers into line with the events just fetched. */
function reconcile(events) {
  const alive = new Set();
  for (const event of events) {
    const at = positionOf(event);

    // One marker per object. A report of three drones is three things in the
    // air, and drawing it as a single marker with a count beside it meant the
    // map never showed how much was up there -- which is the first thing
    // anyone looks at it for.
    for (let n = 0; n < drawnCount(event); n += 1) {
      const id = `${event.id}#${n}`;
      alive.add(id);
      const held = drawn.get(id);
      const where = nudge(at, event, n);
      if (held) {
        // A fresh report for something already on the map: the course or the
        // kind may have changed, so the icon is rebuilt only when it differs.
        if (held.event.heading !== event.heading || held.event.kind !== event.kind) {
          held.marker.setIcon(icon(event, at.facing));
        }
        held.event = event;
        held.marker.setLatLng(where);
        age(held);
        continue;
      }
      const marker = L.marker(where, {
        icon: icon(event, at.facing), pane: 'osint', keyboard: false,
      });
      marker.bindPopup(() => popup(event));
      marker.addTo(layer);
      // One area per report, not per object: the ground a strike covers does
      // not multiply with how many things caused it.
      const area = (n === 0 && hasArea(event)) ? areaFor(event) : null;
      area?.addTo(areas);
      const made = { event, marker, area, index: n };
      drawn.set(id, made);
      age(made);
    }
  }
  for (const [id, held] of drawn) {
    if (alive.has(id)) continue;
    layer.removeLayer(held.marker);
    if (held.area) areas.removeLayer(held.area);
    drawn.delete(id);
  }
}

// The most objects one report will be drawn as. A channel occasionally
// reports a wave in the dozens, and past this many the marks stop being
// countable and start being a smear -- at which point one per object has
// stopped serving the purpose it exists for.
const MOST_SHOWN = 24;

const drawnCount = (event) =>
  Math.max(1, Math.min(MOST_SHOWN, Number(event.count) || 1));

// How far apart to draw objects reported together, in kilometres.
//
// Small, and deliberately smaller than the accuracy of the position they came
// from: a report locates a group to a town or an oblast, so nudging them a
// couple of kilometres apart adds nothing to the error that was already
// there. It is a way of making them countable, not a claim that anybody knows
// they are three kilometres apart.
const APART_KM = 2.2;

/** Where the nth object of a group is drawn. */
function nudge(at, event, index) {
  if (index === 0 || drawnCount(event) < 2) return [at.lat, at.lon];
  // A ring, expanding by a row every eight, so a dozen do not end up on one
  // circle at the spacing of a wedding cake.
  const ring = Math.floor((index - 1) / 8) + 1;
  const step = (2 * Math.PI * ((index - 1) % 8)) / 8;
  // Rotated by the object's own course, where it has one, so a group reads as
  // travelling together rather than as a fixed rosette pinned to north.
  const turn = ((event.heading ?? 0) * Math.PI) / 180;
  return advance(at.lat, at.lon,
    ((step + turn) * 180) / Math.PI, APART_KM * ring);
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
      // What each channel actually gave. "I cannot see reports from the
      // other accounts" is unanswerable without this: a channel can be
      // unreachable, reachable but quiet, posting things this cannot read, or
      // naming places the gazetteer does not know. Four different problems
      // that all look like an empty map.
      el('details', { class: 'ao-sources' },
        el('summary', {}, 'Channels'),
        el('div', { id: 'osintSources' })),
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

  } else {
    clearInterval(poller);
    poller = null;
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

  // Objects, not reports: each mark is one drone or one missile now, so this
  // is the number in the air, which is what the line is read for.
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

  paintSources();

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

/** One row per channel: what it gave, and where it stopped. */
function paintSources() {
  const host = $('#osintSources');
  if (!host) return;
  const rows = feed?.sources ?? [];
  if (!rows.length) {
    host.replaceChildren(el('div', { class: 'ao-src-row' }, 'Nothing read yet.'));
    return;
  }
  host.replaceChildren(...rows.map((row) => {
    // Said in the order the reading happens, so where it stops is where the
    // problem is: reached, read, placed.
    const why = row.problem ? 'unreachable'
      : row.posts === 0 ? 'no posts'
        : row.read === 0 ? `${row.posts} posts, none readable`
          : row.placed === 0 ? `${row.read} read, none placeable`
            : `${row.placed} placed of ${row.read} read`;
    return el('div', {
      class: `ao-src-row${row.problem || !row.placed ? ' is-quiet' : ''}`,
      title: row.problem ?? '',
    },
    el('b', {}, row.channel),
    el('span', {}, why));
  }));
}

/** Take the map to a report, if it is somewhere. */
function goTo(item) {
  // Marks are keyed by report AND by which object of it they are, so a report
  // is found by its first mark rather than by its id alone.
  const held = drawn.get(`${item.id}#0`);
  if (!held) return;
  // A region-wide warning is framed rather than flown to. Keeping the current
  // zoom puts you inside an oblast looking at a wash of colour with no edge
  // in view, which tells you nothing that the panel had not already said.
  const bounds = held.event.region_wide ? held.area?.getBounds?.() : null;
  if (bounds?.isValid?.()) map.flyToBounds(bounds, { padding: [40, 40], duration: 0.7 });
  else map.flyTo(held.marker.getLatLng(), Math.max(map.getZoom(), 8), { duration: 0.7 });
  held.marker.openPopup();
}
