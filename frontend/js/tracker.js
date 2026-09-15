// Air-threat reports from public Telegram channels, drawn as arrows.
//
// Four monitoring channels post a running commentary of what is in the air --
// drones crossing an oblast, cruise missiles on a heading, strikes where they
// land. The backend reads their public web pages, has a local model turn the
// prose into a kind and a place name, a gazetteer turn the name into
// coordinates, and hands them here.
//
// One mark per object, at the place its report named, and it does not move.
// Markers used to slide along their reported heading at a typical speed for
// their kind -- dead reckoning, labelled as an estimate. It is gone. A map
// where everything drifts is hard to read, the marks wander off the places the
// reports actually named, and a mark sliding across a province looks tracked
// whatever the panel says.
//
// What is drawn instead is an arrow pointing along the reported course, which
// is a direction somebody stated rather than a path anybody watched. Three
// things follow from that and they are most of the care in this file:
//
//   solid arrow    the course came from the report -- a compass bearing in the
//                  text, or the bearing between two places it named.
//
//   hollow arrow   the course was borrowed from the group around it. An
//                  inference, drawn differently so it can be seen as one.
//
//   ring           nothing said which way, not even its neighbours. A ring
//                  makes no claim about direction, which is the honest
//                  drawing when there is nothing to claim.
//
// Concentrate mode replaces a group of arrows with one bigger arrow carrying
// the count and the group's average trajectory.

import { api } from './api.js';
import { $, el } from './ui.js';

// How often to ask for new reports. The backend keeps its own floor under
// this, so several open tabs cost one read of the channels between them.
const POLL_MS = 60000;

// How soon to ask again while the backend says a read is in flight.
//
// The backend answers instantly now and reads the channels on its own thread,
// so a first open gets an empty answer in a few milliseconds and the reports
// land a second or two later. Without this the page would sit empty for a
// whole minute waiting for its next tick -- which would make an immediate
// backend feel slower than the blocking one it replaced.
const CATCHUP_MS = 1200;

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
// Concentrate mode: one shape over a mass of marks instead of every mark.
let concentrated = false;
let masses = null;
let feed = null;
let problem = '';
let poller = null;
// A short follow-up while the backend is still reading.
let catchup = null;
// The moment this browser received the feed it is drawing, by this browser's
// own clock. Everything about drift is measured from here; see driftMinutes().
let feedAt = 0;
// The tick that carries the moving marks along between polls.
let drifter = null;

// id -> { event, marker }. Kept across polls so a marker that is still being
// reported is moved rather than destroyed and rebuilt, which would flicker and
// would drop an open popup.
const drawn = new Map();
// The mass arrows of concentrate mode.
const massShapes = [];

export function initTracker(leafletMap) {
  map = leafletMap;
  // Above the imagery panes and above the ship and quake markers: these are
  // the things you opened the layer for.
  map.createPane('tracker').style.zIndex = 632;
  // The shaded areas go in their own pane, under the markers. A wash over a
  // whole oblast drawn in the marker pane would sit on top of the arrows
  // crossing it, and swallow their clicks with them.
  map.createPane('trackerArea').style.zIndex = 428;
  map.getPane('trackerArea').style.pointerEvents = 'none';
  // An explicit SVG renderer, because the map is built with preferCanvas and
  // a canvas-rendered circle is pixels: it has no element, so className is
  // ignored and the CSS that makes an alert pulse never applies to anything.
  // There are a handful of these at a time, so the reason preferCanvas exists
  // -- hundreds of vectors -- does not arise.
  areaInk = L.svg({ pane: 'trackerArea' });
  layer = L.layerGroup([], { pane: 'tracker' });
  // Popup content is built on demand, so the pictures only exist once one is
  // open -- which is also the only moment their loading can be watched.
  map.on('popupopen', (e) => tidyPictures(e.popup.getElement()));
  // One delegated listener for every dismiss button there will ever be --
  // in a popup Leaflet rebuilds on each open, or in the panel list which is
  // repainted on every poll.
  map.getContainer().addEventListener('click', onDismissClick);
  // What overlaps what is a question about pixels, so it is a different
  // question at every zoom: two marks 30 km apart are one blob at country
  // zoom and a finger apart three levels in.
  map.on('zoomend', declump);
  areas = L.layerGroup([], { pane: 'trackerArea' });
  buildDock();
}

// ── The arithmetic ─────────────────────────────────────────────

/** Where you get to going `km` along a bearing, on a sphere.
 *
 * One caller: positionOf(), which carries a track along from the last
 * position its source confirmed. Done on a great circle rather than by adding
 * degrees, because adding degrees puts the line at the wrong angle and gets
 * worse the further north you are, and these reports are all from fifty
 * degrees up.
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

// A warning's colour says what the warning is ABOUT, not that it is a warning.
//
// "Lipetsk Oblast Drone Alert" and "Voronezh Oblast Missile Alert" are both
// warnings, and the difference between them is the difference between fifteen
// minutes and ninety seconds. Drawing both amber said neither.
//
// Yellow for drones and RED for missiles, which is what was asked for and is
// worth being deliberate about: the missile ARROW is purple, so a missile
// warning is not the same hue as a missile in flight. The alternative -- reuse
// the in-flight colours so the key is learnt once -- is tidier and loses the
// thing that matters more, which is that red is what a person already reads as
// "take cover now". A warning is an instruction to a viewer; an arrow is an
// observation about the sky. They are allowed to use different alphabets, and
// the shapes already differ (a triangle in a shaded province against an arrow).
//
// A warning that does not say what it is about keeps the neutral amber. That
// is a real third state, and inventing a cause for it would be worse than the
// colour it started with.
const WARNING_COLOURS = { drone: '#ffd400', missile: '#ff3b30' };

function colourOf(event) {
  const base = look(event).colour ?? '#ff8a3b';
  if (event.kind !== 'alert' || !event.cause) return base;
  return WARNING_COLOURS[event.cause] ?? base;
}
const motionOf = (event) => event.motion ?? look(event).motion ?? 'track';

/** How long this kind of marker stays, in minutes. */
const keepOf = (event) => feed?.keep?.[event.kind] ?? look(event).keep
  ?? feed?.keep_minutes ?? 20;

// The furthest a mark is carried past its last confirmed position, in
// minutes. The backend's number; the fallback matches it and exists only for
// a feed old enough not to carry the field.
const driftCap = () => feed?.drift_cap_minutes ?? 10;

/**
 * How long this track has been running unconfirmed, right now.
 *
 * Two clocks, added. The backend says how long it had been since the source
 * confirmed the position at the moment it built the feed; this adds however
 * long the feed has been sitting in this browser. Neither end has to agree
 * with the other about what o'clock it is -- only about how long a second is
 * -- which matters because a viewer's clock being ten minutes out is common
 * and would otherwise drag every mark a Shahed's half-hour across the map.
 *
 * null for everything that does not move: a warning, a strike, an areaOnly
 * track, anything whose source gave no speed or no course. The backend
 * decides that -- it is the end holding the source's rules -- and sends no
 * drift_minutes at all for those, so there is nothing to get wrong here.
 */
function driftMinutes(event) {
  const reported = event.drift_minutes;
  if (typeof reported !== 'number') return null;
  const here = Math.max(0, (Date.now() - feedAt) / 60000);
  return Math.min(driftCap(), reported + here);
}

/** How far along its course a mark has been carried, in kilometres. */
function driftKm(event) {
  const minutes = driftMinutes(event);
  if (minutes == null || event.heading == null) return 0;
  const speed = event.speed_kmh;
  if (!(typeof speed === 'number' && speed > 0)) return 0;
  return speed * (minutes / 60);
}

/**
 * Where a mark goes: the last position its source confirmed, carried along
 * that source's own course at that source's own speed for the time since.
 *
 * This is dead reckoning, and it is the arithmetic NEPTUN's own SDK does in
 * predict() -- position at confirmedAt, plus velocity.bearingDeg and
 * velocity.speedKmh multiplied by the elapsed time. It is here because a
 * track confirmed every few seconds and redrawn every thirty was a mark that
 * sat still and then jumped fifteen kilometres, which reads as a glitch
 * rather than as a drone.
 *
 * It is emphatically NOT the thing this layer removed once. That version
 * flew a mark along at a speed looked up from a table of what that kind of
 * aircraft typically does -- an assumption in the costume of telemetry. Every
 * number here came from the source. Where one of them did not, the mark does
 * not move at all, and `carried` is zero: nothing is ever moved on a guess.
 */
function positionOf(event) {
  const at = {
    lat: event.origin_lat,
    lon: event.origin_lon,
    facing: event.heading,
    carried: 0,
  };
  const km = driftKm(event);
  if (!(km > 0)) return at;
  const [lat, lon] = advance(at.lat, at.lon, event.heading, km);
  return { lat, lon, facing: event.heading, carried: km };
}

/**
 * How new a report is, as a fraction of its own lifetime. 1 is now, 0 is due
 * to go.
 *
 * Measured against the marker's OWN lifetime, because they differ by a factor
 * of seventy-five: a drone is gone in twenty minutes, a warning lasts an hour
 * and a strike stays twenty-five. Against a fixed window every strike would
 * sit at full strength for its whole life and then vanish.
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
 * Different shapes for different things being said, because drawing them
 * alike was the bug in the picture that prompted this: a jet drone flying
 * past a town on a course north was drawn as something that had come down,
 * because it had a compass course and no named destination and only a
 * destination counted as movement.
 *
 *   arrow    it is flying, and this is the way. Rotated to the course.
 *   ring     it is on station over here, going round. The gap in the ring
 *            turns with it, so it reads as circling rather than as a dot.
 *   dot      it is in the air and nothing said which way.
 *   warning  a warning about a place, which is not an object at all.
 */
// The drawings, one per kind, on an 18-unit grid with north up.
//
// Colour alone was not enough. Every flying kind shared one arrow, so a
// Shahed, a jet drone, a cruise missile and a ballistic missile were the same
// triangle in slightly different reds -- which on a map at a glance is no
// information at all. These are silhouettes: the thing itself, pointed the
// way it is going.
// One arrow, for everything in the air.
//
// There were seven silhouettes here: a delta wing for a Shahed, a swept wing
// with an exhaust for a jet drone, a thin finned body for a cruise missile, a
// dart for a ballistic one, wings and a tailplane for an aircraft. They were
// careful drawings and they were the wrong idea.
//
// At twenty-odd pixels a delta wing and a swept delta wing are the same grey
// triangle, so the detail cost legibility and bought nothing -- and worse, it
// implied a precision the data does not have. These reports say "a Shahed" or
// "a cruise missile"; drawing a recognisable airframe suggests somebody
// identified a type, which nobody did.
//
// So: one arrow, pointed along the reported course. What kind of thing it is
// comes from the colour, which is per kind and defined once on the server, and
// from the label under it. The arrow is the one thing the drawing can honestly
// claim -- something was reported here, going that way.
// A plain filled triangle, point forward. The shape the published Ukrainian
// air-situation maps use for a Shahed, and deliberately the same: somebody who
// has looked at one of those already knows what it means, and there is nothing
// to gain by inventing a different glyph for the same thing.
//
// No notch at the back. The notched version read as a stylised aircraft at
// small sizes, which is the airframe-identification claim the silhouettes were
// removed for.
const ARROW = (c) => `<path d="M9 1.4 L15.6 15.6 L2.4 15.6 Z" fill="${c}"/>`;

// The same arrow, narrower and longer, for the things that are not drones.
//
// Not a reinstated silhouette: it says nothing about an airframe, only that
// this is a missile rather than a drone -- which is the one distinction in
// these reports that always matters and that a viewer must never have to
// guess at. Colour alone was carrying it and was not carrying it well enough:
// red and orange-red are 58 apart out of 765, which is fine beside each other
// and not fine across a map.
const SLIM = (c) => `<path d="M9 0.6 L13 16.8 L5 16.8 Z" fill="${c}"/>`;

// Which kinds get the slim arrow. Missiles, and nothing else.
//
// One entry now rather than two: cruise and ballistic were folded into one
// "missile", because the difference matters enormously in life and not at all
// on this map -- both are inbound, both are drawn at the same place, and one
// kind that is always right beats two that are sometimes swapped.
const SLIM_KINDS = new Set(['missile']);

// The same arrow as an outline, for a course borrowed from the group around it
// rather than stated for that mark. Hollow because the difference is worth
// seeing on the map and not only in a popup: a solid arrow is what a report
// said, an outlined one is an inference from its neighbours.
const BORROWED = (c) => `<path d="M9 2.2 L14.8 15 L3.2 15 Z"
  fill="none" stroke="${c}" stroke-width="1.7" stroke-linejoin="round"/>`;

// No number on a mark. It was a plate reading "3" on the arrow, and before
// that it was three arrows; both are gone for the same reason, said twice
// from the other side of the screen: the count is the least dependable thing
// in a report and it was being drawn as though it were the most definite.
//
// NEPTUN draw no number either. A triangle says a thing was reported there,
// which is what is actually known. How many the report claimed is still in
// the popup, where it can be read with the sentence it came from instead of
// floating on the map as a fact.

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
    // A filled warning triangle with a glow behind it, sitting in the middle
    // of the area it applies to, with the words under it. The reference maps
    // put the label on rather than behind a hover, and they are right to: on
    // a screen nobody is standing at, a mark that has to be pointed at to be
    // understood is a mark that is not understood.
    return svg('warning',
      `<circle cx="9" cy="9.4" r="8.6" fill="${colour}" opacity="0.18"/>
       <circle cx="9" cy="9.4" r="5.6" fill="${colour}" opacity="0.22"/>
       <path d="M9 2.6 L16.4 15.6 H1.6 Z" fill="${colour}"
             stroke="rgba(13,16,21,.85)" stroke-width="1"
             stroke-linejoin="round"/>
       <path d="M9 6.6 v4.2" stroke="rgba(13,16,21,.9)" stroke-width="1.7"
             stroke-linecap="round"/>
       <circle cx="9" cy="13.2" r="1.05" fill="rgba(13,16,21,.9)"/>`);
  }
  if (motion === 'orbit') {
    return svg('ring',
      `<path d="M9 2.2 A6.8 6.8 0 1 1 4.2 4.2" fill="none" stroke="${colour}"
             stroke-width="2" stroke-linecap="round"/>
       <path d="M9 2.2 L6.2 0.4 L6.2 4 Z" fill="${colour}"/>
       <circle cx="9" cy="9" r="1.6" fill="${colour}"/>`, facing ?? 0);
  }

  if (facing == null) {
    // In the air, and nothing anywhere said which way -- not the report, and
    // not the group around it. An arrow here would point north and mean it,
    // which is the invention this whole layer exists to avoid. So it is a
    // ring: something is here, and its course is not known.
    return svg('dot',
      `<circle cx="9" cy="9" r="6.4" fill="none" stroke="${colour}"
               stroke-width="1.6"/>`
      + `<circle cx="9" cy="9" r="2" fill="${colour}"/>`
      );
  }
  // Solid when the course came from the report, hollow when it was borrowed
  // from the group. Both are arrows and both point somewhere real; the weight
  // is the difference between an observation and an inference, and it is on
  // the map rather than only in the popup because that is where it is read.
  const borrowed = event.course_from === 'group';
  const slim = SLIM_KINDS.has(event.kind);
  const shape = `${slim ? 'missile' : 'arrow'}${borrowed ? '-borrowed' : ''}`;
  const draw = borrowed ? BORROWED : (slim ? SLIM : ARROW);
  return svg(shape, draw(colour), facing);
}

/** One marker: its glyph, and its label underneath. */
function icon(event, facing) {
  const colour = colourOf(event);
  // "Surveillance, not a signal to hide." NEPTUN's words about advisory
  // tracks, and their argument is the right one: a MiG-31K taking off is
  // worth recording and is not a reason to take cover, and drawing it like
  // one teaches people to ignore the signal that matters. So it keeps its
  // colour and loses the pulsing halo and the label plate.
  const loud = event.kind === 'alert' && !event.advisory;
  return L.divIcon({
    // A warning is the thing somebody scanning this map is looking for, and
    // at the zoom it gets used at a 24-pixel glyph in a colour was
    // disappearing into the basemap. It gets a pulsing halo behind it and a
    // label with a background, which is the difference between something you
    // can find and something you have to hunt for.
    className: `ao-pin${loud ? ` is-loud is-${event.kind}` : ''}`
      + (event.advisory ? ' is-advisory' : '')
      + (event.area_only ? ' is-area-only' : ''),
    // The label says what is being reported. An opaque identifier told the
    // reader nothing they could not see, and made them open a popup to find
    // out whether a triangle was a drone or a missile.
    //
    // No speed: the speed is a table lookup for the type, not a measurement
    // of the object, and printing it would dress an assumption up as telemetry.
    // A label only on the things that are a place rather than a direction.
    //
    // Every mark used to carry one, and on a busy map they collided: two
    // drones reported over the same town drew their labels on top of each
    // other and read as "D(o)ne". The reference maps put no text on their
    // triangles either, and they are right -- a triangle's colour and shape
    // already say what it is, and where several are close together the words
    // are the first thing to become unreadable.
    //
    // Warnings and strikes keep theirs, because those ARE a statement about a
    // place and the words are the statement.
    html: (loud ? `<span class="ao-halo" style="background:${colour}"></span>` : '')
      + `${glyph(event, colour, facing)}`
      + (loud
        ? `<span class="ao-tag" style="color:${colour}">${label(event)}</span>`
        : ''),
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
    rows.push(`<b>${Number(event.count)} reported together</b>, drawn as one `
      + 'mark with the number on it — the report gave one position, not '
      + `${Number(event.count)} of them, and how many were counted is the `
      + 'least reliable thing in it.');
  }
  rows.push(`${since(event.age_minutes ?? 0)} since the report`);
  // Where the arrow's direction came from. Three different claims, and the
  // map draws the first two as arrows, so the popup is where they are told
  // apart properly.
  if (event.heading != null) {
    const deg = `${Math.round(event.heading)}° ${compass(event.heading)}`;
    if (event.course_from === 'group') {
      rows.push(`Heading <b>${deg}</b> — <b>borrowed from the group</b>. This `
        + 'report gave no course; the arrow is the average of the '
        + `${event.course_from_count ?? 'other'} nearby marks that did, and is `
        + 'drawn hollow because it is an inference rather than something '
        + 'anybody said about this one.');
    } else if (event.course_from === 'destination') {
      rows.push(`Heading <b>${deg}</b> — the bearing to the place the report `
        + 'named as its destination.');
    } else {
      rows.push(`Heading <b>${deg}</b> — as the report stated it.`);
    }
    const carried = driftKm(event);
    if (carried > 0) {
      rows.push(`<b>Carried ${carried < 10 ? carried.toFixed(1)
        : Math.round(carried)} km</b> along that course since the position was `
        + `last confirmed ${since(driftMinutes(event))} ago, at `
        + `${Math.round(event.speed_kmh)} km/h — the course and the speed are `
        + 'the source’s own, and the dashed tail behind the mark is the '
        + 'part of the track that is this arithmetic rather than a report. '
        + `Stops after ${Math.round(driftCap())} minutes without a new `
        + 'confirmation.');
    } else {
      rows.push('Course shown, not followed — the mark stays where the report '
        + 'put it, because nothing here reported a speed to carry it at.');
    }
  } else {
    rows.push('<b>No course reported</b>, and none of the marks near it had '
      + 'one either — so it is drawn as a ring. An arrow would have to point '
      + 'somewhere, and nothing here knows where.');
  }
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

  if (event.advisory) {
    rows.push('<b>Advisory</b> — reported as something observed, not as a '
      + 'reason to take cover. NEPTUN marks these so they are not read as an '
      + 'alarm; a MiG-31K taking off is the usual case.');
  }
  if (event.area_only) {
    // NEPTUN's sharpest caveat, said in full because the mark looks like any
    // other region mark and the difference is invisible.
    rows.push('<b>Region only — there is no point.</b> The sources named this '
      + 'region and nothing finer, so the position is the middle of the '
      + 'region rather than anywhere anybody reported. No course, no '
      + 'distance, and nothing here is extrapolated from it.');
  }
  if (event.speed_kmh) {
    // Theirs, and said to be theirs. This map refuses to print a speed it
    // worked out from "this is a Shahed and Shaheds do about 180" -- that
    // dresses an assumption up as telemetry. A reported one is a different
    // claim and is attributed.
    rows.push(`Reported at <b>${Math.round(event.speed_kmh)} km/h</b>`
      + (event.by === 'neptun' ? ' by NEPTUN' : '') + '.');
  }
  if (event.by === 'neptun') {
    rows.push('From <b>NEPTUN</b>'
      + (event.confidence ? ` — confidence ${escapeHtml(event.confidence)}` : '')
      + (event.uncertainty_km ? `, ±${Math.round(event.uncertainty_km)} km` : '')
      + '.');
  }

  return `<div class="ao-pop">
    <h4>${label(event)}</h4>
    ${rows.map((r) => `<p>${r}</p>`).join('')}
    ${event.summary ? `<p class="ao-sum">${escapeHtml(event.summary)}</p>` : ''}
    ${event.text ? `<blockquote>${escapeHtml(event.text)}</blockquote>` : ''}
    ${pictures(event)}
    <p class="ao-src">${escapeHtml(event.channel ?? '')}${event.link
      ? ` · <a href="${escapeHtml(event.link)}" target="_blank" rel="noopener noreferrer">the post</a>` : ''}</p>
    <p class="ao-act"><button type="button" class="ao-dismiss"
      data-id="${escapeHtml(event.id)}">${event.kind === 'alert'
        ? 'Cancel this warning' : 'Remove from map'}</button></p>
  </div>`;
}

/**
 * Take a mark off the map by hand.
 *
 * Somebody watching this knows things the feed does not: a drone was shot
 * down and the channel has not said so yet, a warning is stale, a report was
 * plainly a duplicate. Until now the only answer was to wait out the keep
 * time -- twenty-five hours, for a strike.
 *
 * It hides a mark; it does not edit the record. The report stays in the panel
 * marked "removed", so what a channel actually said is not something a browser
 * can change, and so somebody who dismissed the wrong thing can see that they
 * did and put it back.
 *
 * Delegated from the map container rather than bound per popup: script-src is
 * 'self', inline handlers are dead, and Leaflet rebuilds popup content on
 * every open -- so a listener attached at build time would be attached to an
 * element that is thrown away.
 */
async function onDismissClick(ev) {
  const button = ev.target.closest('.ao-dismiss, .ao-restore');
  if (!button) return;
  const restore = button.classList.contains('ao-restore');
  const id = button.dataset.id;
  if (!id) return;
  button.disabled = true;
  try {
    await api.trackerDismiss(id, restore);
    map.closePopup();
    await load();
  } catch (err) {
    problem = err.message;
    button.disabled = false;
    paintDock();
  }
}

/**
 * The pictures a report came with, for a popup.
 *
 * Loaded lazily, and only once a popup is actually opened -- Leaflet builds
 * popup content on demand, so nothing here is fetched for a marker nobody has
 * clicked. On a busy night that is the difference between three pictures and
 * three hundred.
 *
 * Every src is a path through this app, never a Telegram URL. The browser
 * talks to Telegram nowhere else in this layer and should not start for a
 * thumbnail: an <img> pointed at their CDN hands them the viewer's address
 * every time a popup opens.
 */
function pictures(event) {
  const shots = Array.isArray(event.photos) ? event.photos.slice(0, 4) : [];
  if (!shots.length) return '';
  // No inline onerror handler, however convenient: script-src is 'self', so
  // the browser refuses inline handlers and the attribute would be silently
  // dead. Broken pictures are hidden by tidyPictures below instead.
  const frames = shots.map((src) => `<a class="ao-shot"`
    + ` href="${escapeHtml(src)}" target="_blank" rel="noopener noreferrer">`
    + `<img src="${escapeHtml(src)}" alt="" loading="lazy" decoding="async">`
    + `</a>`).join('');
  return `<div class="ao-shots">${frames}</div>`
    + '<p class="ao-shots-note">From the post. Fetched through this app, not '
    + 'from your browser.</p>';
}

/**
 * Hide the pictures that did not arrive.
 *
 * Telegram expires preview files, and a strike held for a day will outlive
 * some of its own. A broken-image icon in a popup reads as this app being
 * broken, where the honest reading is "that picture is no longer there" -- so
 * the frame goes and the rest of the popup stands.
 *
 * Called on popupopen because that is when the pictures first exist: Leaflet
 * builds popup content on demand, which is also why a busy night costs three
 * thumbnails rather than three hundred.
 */
function tidyPictures(popupNode) {
  if (!popupNode) return;
  for (const img of popupNode.querySelectorAll('.ao-shot img')) {
    if (img.complete && img.naturalWidth === 0) {
      img.closest('.ao-shot')?.classList.add('is-gone');
      continue;
    }
    img.addEventListener('error',
      () => img.closest('.ao-shot')?.classList.add('is-gone'), { once: true });
  }
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
  // The shaded province takes the warning's cause colour too, so the outline
  // and the triangle inside it agree about what is being warned against.
  const colour = colourOf(event);
  const style = {
    pane: 'trackerArea',
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
      : event.region_wide ? 0.95 : 0.6,
    // The dash pattern is NOT set here. It lives in the stylesheet, on
    // .ao-area-alert, and a CSS stroke-dasharray overrides the presentation
    // attribute Leaflet would write -- so setting it in both places would
    // leave a dead value here that looks like it is doing something.
    fillColor: colour,
    // Warnings are an OUTLINE, not a wash.
    //
    // They were filled at 0.3, and at the size of an oblast that is a solid
    // slab of colour over a tenth of the country -- it hid the basemap under
    // it, it hid the marks inside it, and where two overlapped the map turned
    // to mud. The dashed border already says "this province is under a
    // warning" and says it without covering up the thing a reader is looking
    // at, which is what is actually flying over that province.
    //
    // Strikes keep their fill: those are small and the fill is what makes them
    // findable.
    // A warning fills its region.
    //
    // It was an outline only, because a filled BOUNDING BOX was covering a
    // third of the country in a shape no province has. With the rectangles
    // gone the fill is the right drawing again -- a province under a warning
    // reading as a state of that province from across a room is most of what
    // this layer is for, and an outline alone does not do it at country zoom.
    //
    // A track located only to a region is still barely tinted: that outline
    // says how precisely something was located, not that the province is
    // under anything, and filling it would say the second.
    fillOpacity: event.region_scope === 'located' ? 0.05
      : event.kind === 'alert' ? 0.42
        : event.region_wide ? 0.2 : 0.18,
  };

  // A warning covering a whole region gets that region's actual outline. A
  // circle over the middle of an oblast both misses ground the warning covers
  // and covers ground it does not, and at the size of a province that is not
  // a rounding error -- it is most of a country's worth of wrong.
  if (event.shape) {
    return L.geoJSON({ type: 'Feature', geometry: event.shape, properties: {} }, {
      pane: 'trackerArea',
      renderer: areaInk,
      interactive: false,
      style,
    });
  }

  // A warning with no boundary gets NO AREA AT ALL.
  //
  // It used to get the region's extent as a dotted rectangle, and a screen
  // full of those is what "the map looks wrong" meant: ten dashed boxes in a
  // country made of jagged borders, none of them the shape of anything, each
  // one covering ground the warning does not cover and missing ground it
  // does. A rectangle is not a cautious version of a province — it is a
  // different and wrong claim about where a warning applies.
  //
  // So the triangle stands alone until the real outline arrives, which for
  // NEPTUN's own alerts is immediate (their alert keys index their boundary
  // files) and for anything else is the next poll or two. A mark with no
  // shading says "a warning here, area not drawn"; a rectangle says "this
  // rectangle", and only one of those is true.
  if (event.kind === 'alert') return null;

  return L.circle([event.origin_lat, event.origin_lon], {
    ...style,
    radius: Math.max(1500, (event.area_km ?? 8) * 1000),
  });
}

/**
 * Where a track has been REPORTED, drawn as a line that fades into the past.
 *
 * Every point in it is a position the source gave at a time it gave it.
 * Nothing is interpolated between them and nothing is extended past the last
 * one -- which is the difference between a trail and a predicted path, and
 * this map only has grounds to draw the first.
 *
 * Drawn as separate legs rather than one polyline because the whole point is
 * that the old end is fainter than the new end, and a polyline takes one
 * opacity. Twenty legs is cheap; the alternative is an SVG gradient per track,
 * which is a lot of machinery for a line.
 *
 * Returns null where there is nothing to draw: one point is a position, not a
 * path, and an areaOnly track has no positions at all -- only the middles of
 * provinces, which a line between would be a flight nobody reported.
 */
function trailFor(event) {
  const path = Array.isArray(event.trail) ? event.trail : [];
  if (path.length < 2 || event.area_only) return null;
  const colour = colourOf(event);
  const legs = [];
  for (let i = 1; i < path.length; i += 1) {
    // Oldest leg faintest. The newest is still well under the mark's own
    // weight, so the trail reads as history rather than as another object.
    const through = i / (path.length - 1);
    legs.push(L.polyline([[path[i - 1][0], path[i - 1][1]],
                          [path[i][0], path[i][1]]], {
      pane: 'trackerArea',
      renderer: areaInk,
      interactive: false,
      className: 'ao-trail',
      color: colour,
      weight: 1 + through * 1.6,
      opacity: 0.08 + through * 0.5,
    }));
  }
  return legs;
}

/**
 * The part of the track nobody has confirmed: from the last reported position
 * to where the thing has been reckoned to since.
 *
 * Dashed, and drawn apart from the trail proper, because the difference
 * between the two is the whole of what makes this honest. The solid legs are
 * places a source said something was. This one is arithmetic. A viewer who
 * learns nothing else about this layer can still see where the reporting
 * stopped and the reckoning started, without opening anything.
 */
function liveLegFor(event) {
  if (event.area_only || !(driftKm(event) > 0)) return null;
  const at = positionOf(event);
  return L.polyline([[event.origin_lat, event.origin_lon], [at.lat, at.lon]], {
    pane: 'trackerArea',
    renderer: areaInk,
    interactive: false,
    className: 'ao-trail is-reckoned',
    color: colourOf(event),
    weight: 1.8,
    opacity: 0.5,
    dashArray: '3 5',
  });
}

/** Whether this report is about an area rather than something passing over. */
const hasArea = (event) => event.placed !== false
  && Number.isFinite(event.origin_lat)
  // A warning is drawn as an area only when there is a real region to draw.
  // Anything else — a rectangle round its extent, a circle on its centre —
  // is a claim about ground nobody made.
  && (event.kind === 'alert'
    ? Boolean(event.shape)
    // Either it covers ground, or the report only located it to a region --
    // both are worth drawing, and they are drawn differently.
    : motionOf(event) === 'still' || Boolean(event.shape));

/**
 * Move the map so that everything drawn is on screen.
 *
 * Includes the areas as well as the marks: a warning covering an oblast is one
 * of the things you are looking for, and fitting only the points would put its
 * outline half off the edge.
 *
 * Does nothing rather than guessing when there is nothing drawn -- the panel
 * already says so, and flying to a default place would imply there was
 * something there.
 */
// The two countries this layer reports on, as the airspace each one covers.
//
// Asked for as a way to see one country's sky at a time. The map fits to
// everything drawn by default, which on a night with warnings in Tatarstan and
// drones over Volyn means a view four thousand kilometres wide where neither
// is legible. These put one country on the screen at the size it is.
//
// Bounds rather than a centre and a zoom, so the framing is right whatever
// shape the window is. Russia is its European part: the radar channel reports
// as far as the Urals and no further, and fitting to Kamchatka would put the
// half of the country that is ever mentioned into a corner.
const AIRSPACE = {
  ua: { name: 'Ukraine', bounds: [[44.0, 22.0], [52.5, 40.4]] },
  ru: { name: 'Russia', bounds: [[43.5, 27.0], [61.0, 60.0]] },
};

// What can be shown, and hidden, one group at a time.
//
// Asked for as "just show drones and missiles", and built as a group per
// button rather than a single drones-and-missiles mode, because the useful
// question differs by the minute: on a night of Shaheds the warnings are
// noise, and on a quiet night with one alert the warnings are the whole of
// what you came for. Buttons that each turn one group off answer both, and
// answer "only drones" as well, which one combined mode would not.
//
// Grouped by what a reader is actually asking to see, and the groups are
// kinds from the backend's own table rather than a second list of names: a
// kind added there and not here would silently stop being drawable.
const GROUPS = [
  { key: 'drones', label: 'Drones', kinds: ['drone', 'jet_drone'] },
  { key: 'missiles', label: 'Missiles', kinds: ['missile', 'bomb'] },
  { key: 'aircraft', label: 'Aircraft', kinds: ['aircraft', 'unknown'] },
  { key: 'warnings', label: 'Warnings', kinds: ['alert'] },
];

// Which groups are on. Everything, until somebody says otherwise.
const showing = new Set(GROUPS.map((g) => g.key));

const groupOf = (kind) =>
  GROUPS.find((g) => g.kinds.includes(kind))?.key ?? 'aircraft';

/** Whether this report's kind is one of the groups currently switched on. */
const isShown = (event) => showing.has(groupOf(event.kind));

/** Turn one group on or off, and redraw without refetching. */
function toggleGroup(key) {
  if (showing.has(key)) showing.delete(key);
  else showing.add(key);
  // No fetch: the events are already here, so the switch is instant. Marks
  // that go are removed by reconcile's own sweep -- anything not in `alive`
  // this pass comes off the map with its area and its trails.
  reconcile(feed?.events ?? []);
  drawMasses();
  paintDock();
}

/** Put one country's airspace on the screen, marks and all. */
function showAirspace(which) {
  const want = AIRSPACE[which];
  if (!want || !map) return;
  const bounds = L.latLngBounds(want.bounds);
  // Extended to take in anything drawn inside it that reaches past the box --
  // an oblast outline on the border, a mark just outside. The box is a frame,
  // not a filter: nothing is hidden, the view is just put where the country is.
  for (const held of drawn.values()) {
    const at = held.marker.getLatLng();
    if (bounds.contains(at) && held.area?.getBounds) {
      bounds.extend(held.area.getBounds());
    }
  }
  map.flyToBounds(bounds, { duration: 0.7 });
}

function fitToMarks() {
  const points = [];
  for (const held of drawn.values()) {
    const at = held.marker.getLatLng();
    points.push([at.lat, at.lng]);
  }
  for (const mass of (concentrated ? masses ?? [] : [])) {
    const [west, south, east, north] = mass.bbox;
    points.push([south, west], [north, east]);
  }
  if (!points.length) return;
  const bounds = L.latLngBounds(points);
  for (const held of drawn.values()) {
    if (held.area?.getBounds) bounds.extend(held.area.getBounds());
  }
  // A single mark has zero-sized bounds, which fitBounds answers by zooming to
  // the maximum. Padded so one drone lands at a zoom where the surrounding
  // country is still legible.
  map.flyToBounds(bounds.pad(0.25), { maxZoom: 9, duration: 0.6 });
}

/** Bring the drawn markers into line with the events just fetched. */
function reconcile(events) {
  const alive = new Set();
  // In concentrate mode the marks that belong to a mass are represented by
  // the mass instead of individually -- otherwise the shape sits on top of
  // the forty glyphs it exists to replace and the map is busier, not clearer.
  //
  // Everything NOT in a mass still draws normally. A lone drone over Odesa is
  // not hidden because five others are grouped over Kyiv; it is the thing the
  // mode would otherwise lose, and it is often the more interesting mark.
  const inAMass = concentrated
    ? new Set((masses ?? []).flatMap((m) => m.ids ?? []))
    : new Set();

  for (const event of events) {
    if (inAMass.has(event.id)) continue;
    // Hidden groups are simply not drawn. Not filtered out of the feed --
    // the panel list and the counts still say what arrived, because "no
    // missiles on the map" and "missiles switched off" are different things
    // and confusing them is how a filter becomes a lie.
    if (!isShown(event)) continue;
    const at = positionOf(event);

    // One marker per report, at the position the report gave. The several
    // objects of one report are one mark; how many were claimed is in the
    // popup and not on the map.
    const id = event.id;
    alive.add(id);
    const held = drawn.get(id);
    const where = [at.lat, at.lon];
    if (held) {
      // A fresh report for something already on the map: the course or the
      // kind may have changed, so the icon is rebuilt only when it differs.
      if (held.event.heading !== event.heading
          || held.event.kind !== event.kind
          || held.event.count !== event.count) {
        held.marker.setIcon(icon(event, at.facing));
      }
      held.event = event;
      held.marker.setLatLng(where);
      // The reckoned tail is rebuilt rather than moved: a fresh report can
      // turn drift on or off -- a track that stopped reporting a speed, or
      // started -- and moving a line that should no longer exist would
      // leave it on the map until the mark itself expired.
      refreshLive(held);
      age(held);
      continue;
    }
    const marker = L.marker(where, {
      icon: icon(event, at.facing), pane: 'tracker', keyboard: false,
    });
    marker.bindPopup(() => popup(event));
    marker.addTo(layer);
    const area = hasArea(event) ? areaFor(event) : null;
    area?.addTo(areas);
    const trail = trailFor(event);
    trail?.forEach((leg) => leg.addTo(areas));
    const live = liveLegFor(event);
    live?.addTo(areas);
    const made = { event, marker, area, trail, live };
    drawn.set(id, made);
    age(made);
  }
  for (const [id, held] of drawn) {
    if (alive.has(id)) continue;
    layer.removeLayer(held.marker);
    if (held.area) areas.removeLayer(held.area);
    held.trail?.forEach((leg) => areas.removeLayer(leg));
    if (held.live) areas.removeLayer(held.live);
    drawn.delete(id);
  }
  declump();
}

// ── Keeping marks off each other ───────────────────────────────

/**
 * Whether this mark is AT somewhere, or merely about somewhere.
 *
 * The distinction the whole of declump() rests on, and it is a real one
 * rather than a convenience.
 *
 * A drone reported over Myrhorod is at Myrhorod. Its position is the report;
 * move it and the map says something nobody said.
 *
 * A warning for Poltava oblast is not at any point. It is drawn at the
 * region's centroid because a label has to go somewhere, and the centroid is
 * a point nobody reported -- the warning covers the shaded province, which is
 * what actually carries the meaning. The same is true of a track located only
 * to a region: NEPTUN's own word for it is that "there is no dot".
 *
 * So the second kind may be moved to stay legible and the first may not. That
 * is what fixes the picture this came from -- a drone arrow sitting on top of
 * a warning triangle -- and it fixes it by moving the thing that was never
 * claiming to be there.
 */
const floats = (event) => event.kind === 'alert'
  || Boolean(event.area_only)
  || event.region_scope === 'located';

// How much air to leave between two marks, in pixels.
//
// In pixels rather than kilometres because that is what overlapping IS: two
// marks 30 km apart are one blob at country zoom and a finger apart three
// levels in, and a distance in kilometres would fix one and not the other.
const AIR_PX = 3;

// Where a crowded mark tries to go, in order: straight up first, then the
// diagonals, then sideways and down. Up first because a warning carries its
// label underneath it, so lifting it takes the label off the mark as well.
const OUT = 30;
const WAYS_OUT = [
  [0, -1], [1, -1], [-1, -1], [1, 0], [-1, 0], [1, 1], [-1, 1], [0, 1],
];

/** Do these two rectangles touch, with a little air allowed for? */
const clashes = (a, b) => !(a.right + AIR_PX <= b.left
  || b.right + AIR_PX <= a.left
  || a.bottom + AIR_PX <= b.top
  || b.bottom + AIR_PX <= a.top);

const slid = (box, dx, dy) => ({
  left: box.left + dx, right: box.right + dx,
  top: box.top + dy, bottom: box.bottom + dy,
});

/**
 * What one mark actually covers on screen, label and all.
 *
 * Measured rather than assumed, and that is the whole of why this works. The
 * first version reserved a 24-pixel square around each glyph, which is what
 * an arrow looks like -- and it left the complaint exactly where it was,
 * because a WARNING is a triangle with a plate reading "Air alert" under it,
 * three times as wide and half as far again down the screen. The drone was
 * never on the triangle. It was on the label.
 *
 * Reading the real rectangles also covers what nobody would think to assume:
 * an arrow rotated to its course sweeps a box half again as wide as itself,
 * and the plate's width depends on the words in it.
 */
function footprint(el) {
  const glyph = el.querySelector('svg.ao-glyph');
  const tag = el.querySelector('.ao-tag');
  const first = (glyph ?? el).getBoundingClientRect();
  const box = {
    left: first.left, right: first.right,
    top: first.top, bottom: first.bottom,
  };
  if (tag) {
    const label = tag.getBoundingClientRect();
    box.left = Math.min(box.left, label.left);
    box.right = Math.max(box.right, label.right);
    box.top = Math.min(box.top, label.top);
    box.bottom = Math.max(box.bottom, label.bottom);
  }
  return box;
}

/**
 * Move the marks that are in the way of other marks.
 *
 * Screen-space, and applied as a margin on the icon rather than by changing
 * the marker's position, so the mark still belongs to its own coordinates --
 * its popup, its area and its trail all stay where they were. Only the
 * drawing shifts.
 *
 * Marks that are AT their position are placed first and never move. What is
 * left goes in the first free spot it can find near where it belongs.
 *
 * Two passes over the DOM rather than one interleaved read-and-write: every
 * margin is cleared, then every rectangle is read, then every margin is set.
 * Interleaving them would make the browser lay the page out again between
 * each pair, and would measure some marks with last poll's offset still on.
 */
function declump() {
  if (!map || !drawn.size) return;
  const held = [...drawn.values()];

  // Clear, so what is measured is where each mark actually belongs.
  const parts = [];
  for (const one of held) {
    const el = one.marker.getElement?.();
    if (!el) continue;
    el.style.marginLeft = '';
    el.style.marginTop = '';
    parts.push([one, el]);
  }
  // Read.
  const boxes = new Map();
  for (const [one, el] of parts) boxes.set(one, footprint(el));

  const taken = [];
  const free = (box, dx, dy) => {
    const want = slid(box, dx, dy);
    return !taken.some((other) => clashes(want, other));
  };
  // How much room a spot has: the smallest gap to anything already placed,
  // for choosing between spots when none of them is actually free.
  const room = (box, dx, dy) => {
    const want = slid(box, dx, dy);
    let worst = Infinity;
    for (const other of taken) {
      const gap = Math.max(other.left - want.right, want.left - other.right,
                           other.top - want.bottom, want.top - other.bottom);
      worst = Math.min(worst, gap);
    }
    return worst;
  };

  // Pinned first, in whatever order: they are immovable, so the order among
  // them changes nothing.
  const put = new Map();
  for (const [one] of parts) {
    if (floats(one.event)) continue;
    put.set(one, [0, 0]);
    taken.push(boxes.get(one));
  }
  // Then the floating ones, in a fixed order so the picture does not
  // reshuffle itself every poll. By id, which is stable across polls.
  const loose = parts.filter(([one]) => floats(one.event));
  loose.sort(([a], [b]) => String(a.event.id).localeCompare(String(b.event.id)));
  for (const [one] of loose) {
    const box = boxes.get(one);
    let to = null;
    if (free(box, 0, 0)) {
      to = [0, 0];
    } else {
      for (const ring of [1, 2, 3]) {
        for (const [dx, dy] of WAYS_OUT) {
          const spot = [dx * OUT * ring, dy * OUT * ring];
          if (free(box, spot[0], spot[1])) {
            to = spot;
            break;
          }
        }
        if (to) break;
      }
    }
    if (!to) {
      // Nowhere free. Take the emptiest spot rather than giving up: on a busy
      // cluster everything is crowded by something, and the one with the most
      // air around it is still better than sitting on top of another mark.
      // Its own point is in the running, so it only moves if moving helps.
      let best = -Infinity;
      for (const [dx, dy] of [[0, 0], ...WAYS_OUT.map(([x, y]) => [x * OUT, y * OUT])]) {
        const air = room(box, dx, dy);
        if (air > best) {
          best = air;
          to = [dx, dy];
        }
      }
    }
    put.set(one, to);
    taken.push(slid(box, to[0], to[1]));
  }
  // Write.
  for (const [one, el] of parts) shift(one, el, ...(put.get(one) ?? [0, 0]));
}

/** Nudge one mark's drawing by a pixel offset, leaving its position alone. */
function shift(held, el, dx, dy) {
  held.dx = dx;
  held.dy = dy;
  el.style.marginLeft = dx ? `${dx}px` : '';
  el.style.marginTop = dy ? `${dy}px` : '';
  // Said on the element so it can be seen in a browser, and so the moved
  // ones are findable without reading this function.
  el.classList.toggle('is-moved', Boolean(dx || dy));
}

/** Put the reckoned tail back in step with the report just received. */
function refreshLive(held) {
  if (held.live) {
    areas.removeLayer(held.live);
    held.live = null;
  }
  const leg = liveLegFor(held.event);
  if (!leg) return;
  leg.addTo(areas);
  held.live = leg;
}

// How often the moving marks are carried along, in milliseconds.
//
// A second. Fast enough that a Shahed at 180 km/h moves about fifty metres a
// tick, which at any zoom this map is read at is a glide rather than a series
// of hops; slow enough that a hundred marks cost nothing. The alternative --
// an animation frame -- would redraw sixty times a second to show the same
// fifty metres, for a picture nobody can tell apart.
const DRIFT_MS = 1000;

/**
 * Carry the moving marks along, between polls.
 *
 * This is what makes the marks move. reconcile() puts them where the feed
 * said; this keeps them going from there on the source's own course and speed
 * until the next feed arrives and resets them to a confirmed position.
 *
 * Marks that do not move are skipped rather than rewritten -- positionOf
 * returns carried: 0 for them -- so a map of forty warnings and three drones
 * does three pieces of work a second.
 */
function slide() {
  if (!enabled || !feed) return;
  let moved = false;
  for (const held of drawn.values()) {
    const at = positionOf(held.event);
    if (!(at.carried > 0)) continue;
    held.marker.setLatLng([at.lat, at.lon]);
    held.live?.setLatLngs([[held.event.origin_lat, held.event.origin_lon],
      [at.lat, at.lon]]);
    moved = true;
  }
  // Only when something actually moved. On a map of warnings and nothing in
  // flight this is the whole cost of the tick, once a second, forever.
  if (moved) declump();
}

/**
 * Draw the masses, or take them away.
 *
 * A circle and a box for each, because which one reads better depends on the
 * shape of the group and the backend cannot decide that: a tight cluster is a
 * circle, and a corridor of drones following a river is a long thin box that a
 * circle would draw as a vast disc of mostly empty ground. Both are drawn,
 * faintly, and the eye takes whichever fits -- the box is the honest extent
 * and the circle is the centre of mass.
 *
 * The count goes on it as a label, because that is the entire point: one shape
 * saying forty is the reading the individual glyphs cannot give you.
 */
function drawMasses() {
  for (const held of massShapes) {
    areas.removeLayer(held);
    layer.removeLayer(held);
  }
  massShapes.length = 0;
  masses = feed?.masses ?? [];
  if (!concentrated || !masses.length) return;

  for (const mass of masses) {
    // A mass of nothing but hidden kinds goes with them. Otherwise switching
    // warnings off in concentrate mode left the shape that stands for forty
    // of them sitting on an empty map, which is the filter failing in the
    // one mode where it is hardest to notice.
    if (!Object.keys(mass.kinds ?? {}).some((kind) => showing.has(groupOf(kind)))) {
      continue;
    }
    const colour = feed?.kinds?.[Object.keys(mass.kinds)[0]]?.colour ?? '#ff3b30';
    // One arrow, big, pointed along the group's own trajectory, with the
    // count on it. That is the whole of concentrate mode now.
    //
    // It used to draw three things per mass -- a filled circle, a dashed
    // bounding box and a separate label -- which was three ways of saying
    // "roughly here" and none of saying which way the group was going. The
    // arrow says both: where the middle of it is, and where it is heading.
    // The extent went with them, because a radius drawn over a corridor is a
    // disc of mostly empty ground and the number is the part anybody reads.
    const arrow = L.marker([mass.lat, mass.lon], {
      pane: 'tracker', keyboard: false, interactive: true,
      icon: L.divIcon({
        className: 'ao-mass-mark',
        html: massGlyph(mass, colour),
        iconSize: [MASS_GLYPH, MASS_GLYPH],
        // Centred on the mass, so the arrow's own middle is the point rather
        // than its top-left corner being it.
        iconAnchor: [MASS_GLYPH / 2, MASS_GLYPH / 2],
      }),
    });
    arrow.bindPopup(() => massPopup(mass));
    arrow.addTo(layer);
    massShapes.push(arrow);
  }
}

// How big a mass arrow is. Bigger than a single mark, because it stands for
// several and should read as the more important thing on the map.
const MASS_GLYPH = 46;

/**
 * A mass, drawn as one arrow with its count.
 *
 * The count is inside the arrow rather than beside it, so the two cannot be
 * read apart or drift over each other at low zoom -- and so that one glance
 * gives both numbers that matter: how many, and which way.
 *
 * With no agreed trajectory the arrow becomes a ring, for exactly the reason a
 * single mark with no course does: the backend returns no course for a group
 * flying in opposite directions, and an arrow would be inventing an agreement
 * that is not there.
 */
function massGlyph(mass, colour) {
  const n = mass.count;
  // Two overlapping triangles, which is how the published maps distinguish a
  // group from a single Shahed -- and it reads as "several" instantly, in a
  // way a number alone does not.
  const pair = `<path d="M23 4 L33 26 L13 26 Z" fill="${colour}"
      opacity="0.55"/>
    <path d="M23 12 L34 35 L12 35 Z" fill="${colour}"
      stroke="rgba(13,16,21,.8)" stroke-width="1.2"/>`;
  // The count beside the pair rather than inside it: a triangle has no middle
  // wide enough for two digits, and the reference maps put nothing inside
  // theirs either.
  const tag = `<g class="ao-mass-count">
      <rect x="28" y="1" rx="6" ry="6" width="${n > 99 ? 17 : 14}" height="13"
            fill="rgba(13,16,21,.9)" stroke="${colour}" stroke-width="1.1"/>
      <text x="${28 + (n > 99 ? 8.5 : 7)}" y="7.8" text-anchor="middle"
            dominant-baseline="central" fill="${colour}"
            style="font: 700 9.5px system-ui, sans-serif"
            >${n > 999 ? '999' : n}</text></g>`;

  if (mass.course == null) {
    // No agreed trajectory, so nothing may point anywhere -- same refusal as
    // a single mark with no course. A ring with the pair inside it says
    // "several, here" and claims no direction.
    return `<svg class="ao-glyph" data-shape="mass-ring" width="${MASS_GLYPH}"
        height="${MASS_GLYPH}" viewBox="0 0 46 46">
      <circle cx="23" cy="23" r="17" fill="none" stroke="${colour}"
              stroke-width="1.6" opacity="0.65"/>
      <g transform="translate(23 23) scale(0.62) translate(-23 -23)"
        >${pair}</g>${tag}</svg>`;
  }
  // The triangles rotate; the count must not, or half of them would be upside
  // down. So the rotation is on a group of its own and the badge sits outside.
  return `<svg class="ao-glyph" data-shape="mass-arrow" width="${MASS_GLYPH}"
      height="${MASS_GLYPH}" viewBox="0 0 46 46">
    <g style="transform: rotate(${mass.course.toFixed(1)}deg);
              transform-origin: 23px 23px">${pair}</g>${tag}</svg>`;
}

/** What a mass says when clicked. */
function massPopup(mass) {
  const kinds = Object.entries(mass.kinds)
    .map(([kind, n]) => `${n} × ${escapeHtml(feed?.kinds?.[kind]?.label ?? kind)}`)
    .join(', ');
  const across = Math.round(mass.radius_km);
  const known = mass.course_from_count ?? 0;
  // How the trajectory was arrived at, said plainly: it is the average of the
  // courses that exist, not a course reported for the group as a whole.
  const where = mass.course == null
    ? `<p><b>No general trajectory.</b> ${known
      ? 'The courses in this group do not agree well enough to average — '
        + 'they point different ways, so one arrow would be inventing an '
        + 'agreement that is not there.'
      : 'None of these reports gave a course.'} Drawn as a ring instead.</p>`
    : `<p>Heading <b>${Math.round(mass.course)}° ${compass(mass.course)}</b> — `
      + `averaged from the ${known} of ${mass.count} that were reported with a `
      + `course${known < mass.count ? ', and lent to the rest' : ''}.</p>`;
  return `<div class="ao-pop"><h4>${mass.count} tracks together</h4>`
    + `<p>${kinds}</p>${where}`
    + `<p>Spread over about ${across} km, centred on `
    + `${mass.lat.toFixed(2)}, ${mass.lon.toFixed(2)}.</p>`
    + `<p class="ao-pop-note">Grouped because each is within `
    + `${feed?.mass_within_km ?? 60} km of another in the group — real `
    + `distance, not screen distance, so this means the same at every zoom. `
    + `Turn off Concentrate to see them one by one.</p></div>`;
}

/** A bearing as a compass point, for people who do not read degrees. */
function compass(deg) {
  const points = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'];
  return points[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
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
  try {
    const got = await api.tracker();
    // Stamped the moment it lands, by this browser's clock, and only on a
    // feed that actually arrived. Everything the marks are carried by is
    // measured from here, so a failed fetch must not move the line -- the
    // marks would jump back to where they were when the last good one came.
    feedAt = Date.now();
    feed = got;
    // The backend reads the channels in the background, so an answer that
    // says a read is in flight is an answer that is about to be superseded.
    // Ask again shortly rather than waiting out the minute.
    clearTimeout(catchup);
    if (feed.polling && enabled) catchup = setTimeout(load, CATCHUP_MS);
    problem = '';
    reconcile(feed.events ?? []);
    drawMasses();
  } catch (err) {
    problem = err.message;
  }
  paintDock();
}


function buildDock() {
  const dock = $('#trackerDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'ao-toggle', id: 'trackerToggle', onclick: toggle },
      el('span', { class: 'ao-mark' }, '▲'), 'Air tracker'),
    el('div', { class: 'ao-body', id: 'trackerBody', hidden: !enabled },
      // Concentrate mode. A checkbox rather than a zoom threshold, because
      // which reading you want is a question about what you are looking for,
      // not about how far out you are.
      el('label', { class: 'ao-check' },
        el('input', {
          type: 'checkbox', checked: concentrated,
          onchange: (e) => {
            concentrated = e.target.checked;
            // No fetch: the masses came with the feed, so the switch is
            // instant either way.
            drawMasses();
            reconcile(feed?.events ?? []);
            paintDock();
          },
        }),
        'Concentrate'),
      el('div', { class: 'ao-head' },
        el('div', { class: 'ao-count', id: 'trackerCount' }, 'Loading…'),
        // Because "not placed" and "placed somewhere I am not looking" are
        // indistinguishable on a country-sized map. Four marks on Ukraine at
        // the zoom the app opens at is four marks you will not find, and the
        // honest answer to that is a button rather than a paragraph.
        el('button', {
          class: 'ao-find', id: 'trackerFind', type: 'button',
          title: 'Move the map to fit everything currently drawn',
          onclick: fitToMarks,
        }, 'Find')),
      // One country's sky at a time. With warnings in Tatarstan and drones
      // over Volyn, "fit everything" is a view four thousand kilometres wide
      // in which neither is readable.
      // What to draw. Each button turns one group off and on again.
      el('div', { class: 'ao-airspace ao-shows' },
        el('span', { class: 'ao-airspace-what' }, 'Show'),
        ...GROUPS.map((group) => el('button', {
          class: 'ao-country ao-show', type: 'button',
          'data-group': group.key,
          title: `Show or hide ${group.label.toLowerCase()}`,
          onclick: () => toggleGroup(group.key),
        }, group.label))),
      el('div', { class: 'ao-airspace' },
        el('span', { class: 'ao-airspace-what' }, 'Airspace'),
        ...Object.entries(AIRSPACE).map(([code, what]) => el('button', {
          class: 'ao-country', type: 'button', 'data-country': code,
          title: `Show ${what.name}'s airspace`,
          onclick: () => showAirspace(code),
        }, what.name))),
      // The reports, whether or not they could be put on the map. This list is
      // the fix for the complaint that the layer "does not work": a night when
      // the gazetteer cannot place the names still shows six reports here,
      // which is a feed doing its job, and looks nothing like a broken one.
      el('div', { class: 'ao-list', id: 'trackerList' }),
      // What each channel actually gave. "I cannot see reports from the
      // other accounts" is unanswerable without this: a channel can be
      // unreachable, reachable but quiet, posting things this cannot read, or
      // naming places the gazetteer does not know. Four different problems
      // that all look like an empty map.
      el('details', { class: 'ao-sources' },
        el('summary', {}, 'Channels'),
        el('div', { id: 'trackerSources' })),
      el('div', { class: 'ao-note', id: 'trackerNote' }, ''),
      // NEPTUN's credit. Their only condition of use is a visible link beside
      // the data, so it is a fixed part of the panel rather than a sentence
      // in the note -- a note gets rewritten, and this must not quietly go
      // with it. The caveat beside it is theirs too, and it belongs on
      // anything somebody might use to decide whether to take cover.
      el('div', { class: 'ao-credit', id: 'trackerCredit' })));
  paintDock();
}

function toggle() {
  enabled = !enabled;
  $('#trackerToggle').classList.toggle('is-on', enabled);
  $('#trackerBody').hidden = !enabled;
  if (enabled) {
    areas.addTo(map);
    layer.addTo(map);
    load();
    poller = setInterval(load, POLL_MS);
    drifter = setInterval(slide, DRIFT_MS);

  } else {
    clearInterval(poller);
    poller = null;
    clearInterval(drifter);
    drifter = null;
    clearTimeout(catchup);
    catchup = null;
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

/** NEPTUN's link and caveat, which their terms require beside the data. */
function paintCredit() {
  const host = $('#trackerCredit');
  const said = feed?.attribution;
  if (!host || !said?.url) return;
  if (host.dataset.url === said.url) return;
  host.dataset.url = said.url;
  host.replaceChildren(
    el('a', {
      href: said.url, target: '_blank', rel: 'noopener noreferrer',
      class: 'ao-credit-link',
    }, said.english ?? said.text ?? 'NEPTUN'),
    el('span', { class: 'ao-credit-caveat' }, said.caveat ?? ''));
}

function paintDock() {
  const count = $('#trackerCount');
  const note = $('#trackerNote');
  const list = $('#trackerList');
  if (!count || !note || !enabled) return;

  // Objects, not reports: each mark is one drone or one missile, so this is
  // the number in the air, which is what the line is read for.
  const n = drawn.size;
  const grouped = concentrated ? (masses?.length ?? 0) : 0;
  const missed = (feed?.reports?.unplaced ?? 0);
  count.textContent = n || grouped
    ? [
      grouped ? `${grouped} mass${grouped === 1 ? '' : 'es'}` : null,
      `${n} on the map`,
      // Said here rather than three paragraphs down in the note. "Four on the
      // map" beside a list of eight reports reads as the map being broken;
      // "four on the map, 3 unplaced" says what actually happened.
      missed ? `${missed} unplaced` : null,
    ].filter(Boolean).join(' · ')
    : missed ? `Nothing on the map · ${missed} unplaced`
      : 'Nothing on the map';
  const find = $('#trackerFind');
  if (find) find.disabled = !(n || grouped);

  // Which groups are switched on, and how many of each arrived -- counted
  // over everything the feed sent rather than over what is drawn, so a
  // switched-off group still says how much is being held back. A button
  // reading "Missiles" that turns out to have been hiding nine of them is
  // the failure this number prevents.
  const perGroup = new Map();
  for (const event of feed?.events ?? []) {
    const key = groupOf(event.kind);
    perGroup.set(key, (perGroup.get(key) ?? 0) + 1);
  }
  for (const button of document.querySelectorAll('.ao-show')) {
    const key = button.dataset.group;
    const had = perGroup.get(key) ?? 0;
    button.classList.toggle('is-off', !showing.has(key));
    button.classList.toggle('is-empty', had === 0);
    const group = GROUPS.find((g) => g.key === key);
    button.textContent = had ? `${group.label} ${had}` : group.label;
  }

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
    // A warning takes its cause's colour here too, so the row and the mark
    // it points at agree about what is being warned against.
    const dot = (item.kind === 'alert' && item.cause
      && feed?.kinds?.[item.cause]) ? WARNING_COLOURS[item.cause] : kind.colour;
    const row = el('div', {
      // A warning that was lifted, and the all-clear that lifted it, are
      // both worth keeping in the stream and neither is on the map. Marked
      // rather than removed: "the warning over Kyiv oblast ended" is a thing
      // that happened and reads as news. Same for one a person dismissed.
      class: `ao-row${item.lifts ? ' is-lifts' : ''}`
        + `${item.lifted ? ' is-over' : ''}`
        + `${item.dismissed ? ' is-dropped' : ''}`
        + `${item.placed ? '' : ' is-unplaced'}`,
    },
    el('button', {
      class: 'ao-row-go', type: 'button', title: item.text ?? '',
      onclick: () => goTo(item),
    },
    el('i', { style: dot ? `background:${dot}` : '' }),
    el('span', {
      class: `ao-row-what${item.by === 'rules' ? ' is-plain' : ''}`,
      title: item.by === 'rules' ? 'Read by pattern'
        : item.by === 'neptun' ? 'From NEPTUN' : '',
    }, item.summary || kind.label || item.kind),
    el('span', { class: 'ao-row-when' }, mins < 1 ? 'now' : ago(mins))),
    // Removing a mark from the panel rather than only from its popup,
    // because on a busy map finding the one drone you want to take off is
    // the hard part and the list is already sorted the way you are reading.
    el('button', {
      class: item.dismissed ? 'ao-restore' : 'ao-dismiss',
      type: 'button',
      'data-id': item.id,
      title: item.dismissed ? 'Put this back on the map'
        : item.kind === 'alert' ? 'Cancel this warning'
          : 'Remove this from the map',
    }, item.dismissed ? '↺' : '×'));
    return row;
  }), ...(hidden > 0
    ? [el('div', { class: 'ao-row-more' }, `+${hidden} older`)]
    : []));

  paintCredit();
  paintSources();

  // One delegated listener for the whole list, rebound each paint because
  // replaceChildren has just thrown the old rows away.
  if (list && !list.dataset.wired) {
    list.addEventListener('click', onDismissClick);
    list.dataset.wired = '1';
  }

  const lines = [];
  const demo = feed?.state?.startsWith('demo');
  const got = tally();
  if (demo) {
    lines.push('Demo mode: these reports are invented.');
  } else {
    lines.push(`${(feed?.channels ?? []).length} public Telegram channels, read `
      + 'by pattern — no model. One post can be many marks: a movement '
      + 'summary naming a dozen towns per oblast gets an arrow for each.');
  }
  // Said plainly, because it is the number that explains an empty map.
  if (got.total) {
    lines.push(`${got.placed} of ${got.total} reports placed`
      + (got.unplaced ? `; ${got.unplaced} named nowhere a map knows.` : '.'));
  }
  if (n) {
    // Still no claim of tracking, and now there is more to say about it: a
    // mark with a source's own course and speed is carried along from the
    // last position that source confirmed, and a mark without them sits
    // exactly where the report put it. Both are on the map at once, so the
    // note has to say which is which rather than one sentence for both.
    const moving = [...drawn.values()]
      .filter((h) => driftKm(h.event) > 0).length;
    lines.push('One arrow per drone or missile, at the place the report named.'
      + (moving
        ? ` ${moving} carried along from the last confirmed position on the `
          + 'source’s own course and speed — the dashed tail is that '
          + 'reckoning rather than a report.'
        : ' Nothing is moving: the arrows point along a course that was '
          + 'stated, which is a direction rather than a track.'));
    // How many of them actually have a direction, and where from. The honest
    // number, because the arrows themselves cannot carry it: a solid and a
    // hollow arrow are distinguishable side by side and not across a map.
    // Counted over the things that GO somewhere. A loitering recon drone has
    // no course because it is not travelling, which is a different thing from
    // a course nobody reported -- and calling it unknown would make the
    // gazetteer and the channels look worse than they are.
    const flying = [...drawn.values()].map((h) => h.event)
      .filter((e) => motionOf(e) === 'track');
    const loitering = [...drawn.values()]
      .filter((h) => motionOf(h.event) === 'orbit').length;
    const said = flying.filter((e) => e.course_from === 'stated'
      || e.course_from === 'destination').length;
    const lent = flying.filter((e) => e.course_from === 'group').length;
    const none = flying.length - said - lent;
    if (flying.length) {
      lines.push(`Direction: ${said} of ${flying.length} reported`
        + (lent ? `, ${lent} borrowed from the group (hollow arrows)` : '')
        + (none ? `, ${none} not known (rings)` : '')
        + '.');
    }
    if (loitering) {
      lines.push(`${loitering} loitering — drawn as a circling mark, which is `
        + 'not the same as an unknown direction: something on station is not '
        + 'going anywhere.');
    }
    lines.push('Warnings are held for '
      + `${Math.round((feed?.keep?.alert ?? 90))} minutes, fading as they age; `
      + `things in flight go after ${feed?.keep_minutes ?? 20} minutes, when `
      + 'the report has stopped describing anything current. Strikes are not '
      + 'drawn at all — a report of explosions names a city, not a place, and '
      + 'a star on a city centroid claimed a precision nobody gave.');
  }
  if (concentrated) {
    lines.push(`Concentrate: groups within ${feed?.mass_within_km ?? 60} km of `
      + `one another, ${feed?.mass_least ?? 3} or more, drawn as one arrow `
      + 'carrying the count and the group\u2019s average trajectory. Grouped '
      + 'by real distance, so a mass means the same at every zoom. Anything '
      + 'not in a group still shows on its own.');
  }
  // Never a silent state. "The map is missing things" and "I hid those" look
  // identical from across a room, and only one of them is a bug.
  const hiddenByHand = feed?.dismissed ?? 0;
  if (hiddenByHand) {
    lines.push(`${hiddenByHand} taken off by hand — the reports are still `
      + 'listed, marked, with ↺ to put them back.');
  }
  if (problem) lines.push(problem);
  else if (!demo && feed?.state && feed.state !== 'nothing new') lines.push(feed.state);
  note.textContent = lines.filter(Boolean).join(' ');
}

/** One row per channel: what it gave, and where it stopped. */
function paintSources() {
  const host = $('#trackerSources');
  if (!host) return;
  const rows = feed?.sources ?? [];
  if (!rows.length) {
    host.replaceChildren(el('div', { class: 'ao-src-row' }, 'Nothing read yet.'));
    return;
  }
  host.replaceChildren(...rows.map((row) => {
    // Said in the order the reading happens, so where it stops is where the
    // problem is: reached, in the window, read, placed.
    //
    // "fresh" is the step that was missing, and leaving it out made the panel
    // lie in the most confusing way available. A channel with twenty posts,
    // none of them inside the twenty-minute window, has fresh 0 and therefore
    // read 0 -- and this said "20 posts, none readable", which claims the app
    // cannot read that channel. All four said it at once on a quiet start.
    // What was true was "nothing new", which is not a fault at all.
    const why = row.problem ? 'unreachable'
      : row.posts === 0 ? 'no posts'
        : row.fresh === 0 ? `${row.posts} posts, nothing new`
          : row.read === 0 ? `${row.fresh} new, none readable`
            : row.placed === 0 ? `${row.read} read, none placeable`
              : `${row.placed} placed of ${row.read} read`;
    return el('div', {
      // Dimmed for a fault, not for quiet. A channel with nothing new is
      // working perfectly and should not be greyed out as though it were not.
      class: `ao-src-row${row.problem
        || (row.fresh > 0 && !row.placed) ? ' is-quiet' : ''}`,
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
