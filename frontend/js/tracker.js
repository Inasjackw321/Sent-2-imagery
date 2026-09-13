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
  areas = L.layerGroup([], { pane: 'trackerArea' });
  buildDock();
}

// ── The arithmetic ─────────────────────────────────────────────

/** Where you get to going `km` along a bearing, on a sphere.
 *
 * Nothing travels any more, so the only thing left that needs this is nudge():
 * trailing the several objects of one report behind the place it named. Still
 * done on a great circle rather than by adding degrees, because adding degrees
 * puts the line at the wrong angle and gets worse the further north you are,
 * and these reports are all from fifty degrees up.
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

/** Where a report goes: where it was reported, and nowhere else. */
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

// The number the report gave, on the mark.
//
// "Група БпЛА на Сумщині" and "12 шахедів над Одещиною" are one mark each,
// and drawing them identically to a single drone loses the only number in the
// sentence. Same plate as a mass count, so the two read as the same idea --
// the difference being that a mass count is how many marks were grouped and
// this is how many the report said were there.
//
// Counter-rotated, because the arrow it sits on is rotated to its course and
// a rotated numeral is unreadable at this size.
function countPlate(n, colour, turn) {
  if (!(n > 1)) return '';
  const wide = n > 99;
  const back = turn == null ? '' : ` transform="rotate(${(-turn).toFixed(1)} 9 9)"`;
  return `<g class="ao-count-plate"${back}>
      <rect x="${wide ? 8.6 : 9.6}" y="0" rx="3.4" ry="3.4"
            width="${wide ? 9.4 : 7.8}" height="7.4"
            fill="rgba(13,16,21,.92)" stroke="${colour}" stroke-width="0.9"/>
      <text x="${(wide ? 8.6 + 4.7 : 9.6 + 3.9).toFixed(1)}" y="4.0"
            text-anchor="middle" dominant-baseline="central" fill="${colour}"
            style="font: 700 5.4px system-ui, sans-serif"
            >${n > 999 ? '999' : n}</text></g>`;
}

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
    // In the air, and nothing anywhere said which way -- not the report, and
    // not the group around it. An arrow here would point north and mean it,
    // which is the invention this whole layer exists to avoid. So it is a
    // ring: something is here, and its course is not known.
    return svg('dot',
      `<circle cx="9" cy="9" r="6.4" fill="none" stroke="${colour}"
               stroke-width="1.6"/>`
      + `<circle cx="9" cy="9" r="2" fill="${colour}"/>`
      + countPlate(event.count, colour, null));
  }
  // Solid when the course came from the report, hollow when it was borrowed
  // from the group. Both are arrows and both point somewhere real; the weight
  // is the difference between an observation and an inference, and it is on
  // the map rather than only in the popup because that is where it is read.
  const borrowed = event.course_from === 'group';
  const slim = SLIM_KINDS.has(event.kind);
  const shape = `${slim ? 'missile' : 'arrow'}${borrowed ? '-borrowed' : ''}`;
  const draw = borrowed ? BORROWED : (slim ? SLIM : ARROW);
  return svg(shape, draw(colour) + countPlate(event.count, colour, facing),
             facing);
}

/** One marker: its glyph, and its label underneath. */
function icon(event, facing) {
  const colour = colourOf(event);
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
    const shown = Math.min(MOST_SHOWN, Number(event.count) || 1);
    rows.push(`${Number(event.count)} reported together, drawn as `
      + `${shown} mark${shown === 1 ? '' : 's'} spread a couple of kilometres `
      + 'apart to be countable — the report gave one position, not '
      + `${shown} of them.`);
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
    rows.push('Course shown, not followed — the mark stays where the report '
      + 'put it.');
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

  if (event.derived) {
    rows.push('<b>Not reported as a warning.</b> Derived from the '
      + `${event.from_marks ?? 0} report(s) placed inside this region.`);
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
    fillOpacity: event.kind === 'alert' ? 0
      : event.region_scope === 'located' ? 0.04
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

  // No outline yet, and a warning does not get a circle instead.
  //
  // The real boundary arrives from the gazetteer a moment later; until it
  // does, the honest stand-in is the region's EXTENT as a rectangle, drawn
  // dotted so it reads as provisional. A disc centred on an oblast is not
  // shaped like any province and was the thing that read as wrong: a smooth
  // circle in a country made of jagged borders looks like a blast radius,
  // which is a claim about ground nobody made.
  if (event.kind === 'alert') {
    const [south, north, west, east] = event.bbox
      ?? [event.origin_lat, event.origin_lat, event.origin_lon, event.origin_lon];
    return L.rectangle([[south, west], [north, east]], {
      ...style,
      className: `${style.className} is-provisional`,
      fillOpacity: 0,
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
        icon: icon(event, at.facing), pane: 'tracker', keyboard: false,
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
const APART_KM = 1.2;

/**
 * Where the nth object of a group is drawn.
 *
 * In a line along the reported course, not a ring around the point.
 *
 * A ring was the first version and it was wrong twice over. It put marks
 * upwind of the position as often as downwind, so half of a group of six sat
 * on the far side of the place they were reported over; and a rosette is a
 * shape nothing in the air makes. Six drones reported together over one town
 * are a stream, and drawn as a short line along their own course they read as
 * one -- which is both the truer picture and the easier one to count.
 *
 * The line runs backwards from the reported point rather than forwards. The
 * report is the front of what was seen, so extrapolating ahead of it would be
 * putting marks where nothing has been reported at all; trailing them behind
 * says "these came through here", which is what was actually said.
 *
 * Spacing halved as well, to 1.2 km, which is comfortably inside the accuracy
 * of a position given as a town name. It makes them countable without
 * claiming anybody knows the interval.
 */
function nudge(at, event, index) {
  if (index === 0 || drawnCount(event) < 2) return [at.lat, at.lon];
  const course = event.heading;
  if (course == null) {
    // No course to trail along, so a tight ring is all that is left -- and
    // with no direction claimed, a ring makes no claim either.
    const step = (360 * ((index - 1) % 8)) / 8;
    const ring = Math.floor((index - 1) / 8) + 1;
    return advance(at.lat, at.lon, step, APART_KM * ring);
  }
  // Behind, in a line, with a slight stagger so a long stream does not become
  // one arrow drawn twelve times in the same pixels at low zoom.
  const back = (course + 180) % 360;
  const along = advance(at.lat, at.lon, back, APART_KM * index);
  const side = (index % 2 ? 1 : -1) * Math.ceil(index / 6) * APART_KM * 0.45;
  return side ? advance(along[0], along[1], (course + 90) % 360, side) : along;
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
    feed = await api.tracker();
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
      el('div', { class: 'ao-note', id: 'trackerNote' }, '')));
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

  } else {
    clearInterval(poller);
    poller = null;
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

function paintDock() {
  const count = $('#trackerCount');
  const note = $('#trackerNote');
  const list = $('#trackerList');
  if (!count || !note || !enabled) return;

  // Objects, not reports: each mark is one drone or one missile, so this is
  // the number in the air, which is what the line is read for.
  const n = drawn.size;
  const strikes = [...drawn.values()].filter((h) => h.event.kind === 'explosion').length;
  const grouped = concentrated ? (masses?.length ?? 0) : 0;
  const missed = (feed?.reports?.unplaced ?? 0);
  count.textContent = n || grouped
    ? [
      grouped ? `${grouped} mass${grouped === 1 ? '' : 'es'}` : null,
      `${n} on the map`,
      strikes ? `${strikes} struck` : null,
      // Said here rather than three paragraphs down in the note. "Four on the
      // map" beside a list of eight reports reads as the map being broken;
      // "four on the map, 3 unplaced" says what actually happened.
      missed ? `${missed} unplaced` : null,
    ].filter(Boolean).join(' · ')
    : missed ? `Nothing on the map · ${missed} unplaced`
      : 'Nothing on the map';
  const find = $('#trackerFind');
  if (find) find.disabled = !(n || grouped);

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
      title: item.by === 'derived'
        ? 'Not reported — derived from the marks in this region'
        : item.by === 'rules' ? 'Read by pattern' : '',
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
    // No claim of tracking. Each mark sits where a report put it and does not
    // move; the arrow points along the reported course, which is a direction
    // somebody said, not a path anybody watched.
    lines.push('One arrow per drone or missile, at the place the report named. '
      + 'Nothing moves — the arrow points along the reported course, which is '
      + 'a direction that was stated, not a track.');
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
    lines.push(`Strikes are held for `
      + `${Math.round((feed?.keep?.explosion ?? 1500) / 60)} hours and warnings `
      + `for ${Math.round((feed?.keep?.alert ?? 60))} minutes, both fading as `
      + `they age; things in flight go after ${feed?.keep_minutes ?? 20} `
      + 'minutes, when the report has stopped describing anything current.');
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
