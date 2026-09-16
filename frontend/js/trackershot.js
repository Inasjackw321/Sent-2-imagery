// A picture of what is in the air, to send somebody.
//
// Not a screenshot. The map's own tiles come from another origin and Leaflet
// loads them without asking for permission to read the pixels back, so a
// canvas that has drawn one cannot be exported at all -- the browser refuses,
// and it refuses at the last step, after everything else has worked. So this
// draws its own map: the province borders the app already holds, the marks in
// their own artwork, and nothing else.
//
// Which turns out to be the right picture anyway. What was asked for was the
// icons without the warnings, and a basemap under them would be the noisiest
// thing in the frame.

import { WATERMARK } from './capture.js';

// The picture's width in pixels. Height follows from the shape of the area
// asked for, so a country comes out landscape and an oblast can come out
// tall. Big enough to stand being looked at, small enough to send.
const WIDE = 1600;
const TALLEST = 2000;

// The glyphs are drawn from an 18-unit box. On a 1600-pixel picture the
// 24-pixel screen size is a speck, so they are drawn larger -- still small
// enough that forty of them over a country do not merge.
//
// Twenty-eight rather than forty-four. Forty-four was chosen against a
// picture of a whole country; once the frame tightens to what is actually in
// the air (see framing below) the same glyph covers far more ground, and a
// mark wide enough to cover the town it is over is a mark that has stopped
// saying where it is.
// Rastered once at a size nothing needs to exceed, then drawn smaller. The
// glyph is an SVG, so this costs nothing and means the draw size can change
// without throwing the cache away.
const GLYPH_PX = 64;

/**
 * How big a mark is drawn, in pixels.
 *
 * A share of the picture's height rather than a fixed number. Twenty-eight
 * was right against an 864-pixel-tall crop and the same twenty-eight on a
 * 1280-tall one is two thirds the size -- so a single track, whose picture
 * is the tallest one this draws, came out as the smallest speck in the set.
 * Bounded at both ends: never so big it covers the town it is over, never so
 * small it cannot be seen.
 */
function markSize(frame) {
  return Math.round(Math.min(34, Math.max(24, frame.height * 0.032)));
}

// How much room to leave around the marks, as a fraction of their spread.
const AIR = 0.18;

// And the least ground a picture covers, in degrees of latitude.
//
// Without a floor, two drones a mile apart would fill the frame at a
// thousandth of a degree across -- a picture of nothing, at enormous
// magnification, with no landmark in it to say where it was taken. About
// eighty kilometres, which holds a couple of districts.
const LEAST_SPAN = 0.75;

// What the picture says about whose data it is.
//
// NEPTUN ask one thing in return for their feed: a visible credit beside the
// data. A picture is the data travelling somewhere this app's panel does not
// follow it, so the credit travels with it -- and it is spelt out here rather
// than only passed in, because a credit that depends on the feed having
// answered is a credit that vanishes on exactly the picture taken while the
// feed was down.
const CREDIT = 'Data supplied by NEPTUN — neptun.in.ua';

// The ground, and how much of it you can see.
//
// The first version was one flat near-black with hairline borders at 16%
// white and a land fill at 2%. On a phone, or anywhere with the brightness
// down, that is a black rectangle with some dots on it -- the borders were
// there in the file and not there to the eye, which is the same as not
// drawing them. They are the thing that makes a mark mean anything: an arrow
// on an empty field is a dot, and an arrow inside Poltava oblast is a report.
const SKY_TOP = '#141926';
const SKY_BOTTOM = '#080a0f';
const BORDER = 'rgba(150, 180, 220, 0.38)';
const LAND = 'rgba(120, 160, 210, 0.055)';
const LABEL = 'rgba(200, 218, 240, 0.55)';

// The bands top and bottom. Text over a map needs something behind it or it
// sits on whichever border happens to be under it.
const SCRIM = 'rgba(8, 10, 15, 0.82)';

const TITLE = 'Air tracker';

/** Web Mercator, normalised to 0..1. The projection the map is drawn in. */
function project(lat, lon) {
  const x = (lon + 180) / 360;
  const clipped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const sin = Math.sin((clipped * Math.PI) / 180);
  const y = 0.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI);
  return [x, y];
}

/**
 * How to turn a latitude and longitude into a pixel in the picture.
 *
 * Built once from the area asked for, so every mark and every border goes
 * through the same arithmetic. Getting this wrong in two places is how a
 * picture ends up with the borders in one projection and the marks in
 * another, which looks like the marks being in the wrong place.
 */
/**
 * Pull the frame in to what is actually in the air.
 *
 * The picture used to be framed by the map view, which is how it was asked
 * for -- "an image of an area or the whole country". But the view is chosen
 * for looking at a map with panels down the side, and the marks in it are
 * usually clustered in one part of it, so most of the picture was empty
 * ground and the thing being sent was a few specks in a lot of dark.
 *
 * So the frame closes on the marks with room around them, never growing
 * past the view it was asked for and never shrinking past LEAST_SPAN. Both
 * limits matter: the first keeps "the whole country" meaning the country
 * when that is what is on screen, and the second stops two nearby marks
 * producing a picture of nothing at huge magnification.
 */
export function closeIn(bounds, marks) {
  if (!marks?.length) return bounds;
  let north = -90, south = 90, west = 180, east = -180;
  for (const mark of marks) {
    north = Math.max(north, mark.lat);
    south = Math.min(south, mark.lat);
    west = Math.min(west, mark.lon);
    east = Math.max(east, mark.lon);
  }
  // Room around them, per axis. Measuring it off the bigger of the two
  // spreads instead gave a group spread across a country a margin of a
  // whole degree in BOTH directions, which is most of the crop given back
  // -- and the floor below already covers the case that was meant to
  // protect against.
  const tall = (north - south) * AIR;
  const wide = (east - west) * AIR;
  north += tall; south -= tall; west -= wide; east += wide;

  // Never smaller than the floor, grown about the middle.
  const short = LEAST_SPAN - (north - south);
  if (short > 0) { north += short / 2; south -= short / 2; }
  const narrow = LEAST_SPAN - (east - west);
  if (narrow > 0) { east += narrow / 2; west -= narrow / 2; }

  // And never wider than what was asked for.
  return shapely({
    north: Math.min(bounds.north, north),
    south: Math.max(bounds.south, south),
    west: Math.max(bounds.west, west),
    east: Math.min(bounds.east, east),
  }, bounds);
}

// What shape a picture should be, as width over height.
//
// The floor above is in DEGREES, and a degree of longitude at fifty north is
// two thirds of a degree of latitude -- then Mercator stretches the vertical
// again. So one mark on its own, floored to three quarters of a degree each
// way, came out as a 1600 by 2000 tower: a portrait of empty ground with a
// speck in the middle of it, which is not a picture anybody sends.
const WIDEST = 2.0;
const NARROWEST = 1.25;

/** Undo the Mercator y, so a box can be grown by a measured amount of it. */
function latAt(y) {
  const t = (0.5 - y) * 4 * Math.PI;
  return (Math.asin(Math.tanh(t / 2)) * 180) / Math.PI;
}

/**
 * Pull a crop towards a sensible shape, without moving what it contains.
 *
 * Only ever grows, and only ever within the view it was asked for, so
 * everything that was in the frame stays in it. Where the view itself is
 * that shape -- somebody looking at a tall strip -- there is nothing to fix
 * and it is left alone.
 */
function shapely(box, bounds) {
  let { north, south, west, east } = box;
  for (let pass = 0; pass < 2; pass += 1) {
    const [x0, y0] = project(north, west);
    const [x1, y1] = project(south, east);
    const spanX = x1 - x0;
    const spanY = y1 - y0;
    if (!(spanX > 0) || !(spanY > 0)) break;
    const aspect = spanX / spanY;
    if (aspect < NARROWEST) {
      // Too tall: widen it.
      const want = spanY * NARROWEST;
      const mid = (west + east) / 2;
      const half = ((want / spanX) * (east - west)) / 2;
      west = Math.max(bounds.west, mid - half);
      east = Math.min(bounds.east, mid + half);
    } else if (aspect > WIDEST) {
      // Too wide: make it taller, in Mercator rather than in degrees.
      const want = spanX / WIDEST;
      const midY = (y0 + y1) / 2;
      north = Math.min(bounds.north, latAt(midY - want / 2));
      south = Math.max(bounds.south, latAt(midY + want / 2));
    } else {
      break;
    }
  }
  return { north, south, west, east };
}

function framing(bounds) {
  const [x0, y0] = project(bounds.north, bounds.west);
  const [x1, y1] = project(bounds.south, bounds.east);
  const spanX = Math.max(1e-9, x1 - x0);
  const spanY = Math.max(1e-9, y1 - y0);
  const height = Math.min(TALLEST, Math.max(300, Math.round(WIDE * (spanY / spanX))));
  return {
    width: WIDE,
    height,
    at(lat, lon) {
      const [x, y] = project(lat, lon);
      return [((x - x0) / spanX) * WIDE, ((y - y0) / spanY) * height];
    },
  };
}

/** One glyph as an image, ready to be stamped. Cached: the same few repeat. */
const painted = new Map();
function glyphImage(part) {
  const key = `${part.shape}|${part.body}`;
  const had = painted.get(key);
  if (had) return had;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${GLYPH_PX}"`
    + ` height="${GLYPH_PX}" viewBox="0 0 18 18">${part.body}</svg>`;
  const image = new Image(GLYPH_PX, GLYPH_PX);
  // A data URL is same-origin, so drawing this does not taint the canvas --
  // which is the whole reason the artwork goes through an SVG string rather
  // than through the <img> elements already on the map.
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  const ready = image.decode().then(() => image).catch(() => null);
  painted.set(key, ready);
  return ready;
}

function walkRings(shape, each) {
  if (!shape) return;
  if (shape.type === 'Polygon') {
    for (const ring of shape.coordinates ?? []) each(ring);
  } else if (shape.type === 'MultiPolygon') {
    for (const part of shape.coordinates ?? []) {
      for (const ring of part ?? []) each(ring);
    }
  }
}

/**
 * Draw the picture.
 *
 * `marks` are already the ones to show: the caller decides what a warning is
 * and leaves them out, because the caller is the thing that knows.
 */
export async function drawShot({ bounds, marks, outlines, credit, at }) {
  const frame = framing(closeIn(bounds, marks));
  const canvas = document.createElement('canvas');
  canvas.width = frame.width;
  canvas.height = frame.height;
  const ctx = canvas.getContext('2d');

  // The ground: a gradient rather than a flat fill, so the picture has a
  // horizon rather than being a rectangle of one colour.
  const sky = ctx.createLinearGradient(0, 0, 0, frame.height);
  sky.addColorStop(0, SKY_TOP);
  sky.addColorStop(1, SKY_BOTTOM);
  ctx.fillStyle = sky;
  ctx.fillRect(0, 0, frame.width, frame.height);

  const bands = bandSizes(frame, marks);
  drawLand(ctx, frame, outlines);
  nameRegions(ctx, frame, outlines, bands, marks);
  await drawMarks(ctx, frame, marks);
  drawFurniture(ctx, frame, marks, credit, at, bands);
  return canvas;
}

/**
 * How tall the heading and the footer are.
 *
 * Worked out once and handed to both the labels and the furniture, because
 * the two have to agree: a province name drawn where the heading will be
 * ends up printed underneath it, which is how "сумська" came out sitting in
 * the timestamp.
 */
function bandSizes(frame, marks) {
  const unit = Math.max(13, Math.round(frame.width * 0.0135));
  return {
    unit,
    pad: Math.round(unit * 1.5),
    head: unit * 3.6,
    foot: unit * (legendKeys(marks).length ? 4.6 : 3.4),
  };
}

function drawLand(ctx, frame, outlines) {
  ctx.lineWidth = 1.1;
  ctx.strokeStyle = BORDER;
  ctx.fillStyle = LAND;
  ctx.lineJoin = 'round';
  for (const outline of outlines ?? []) {
    walkRings(outline.shape, (ring) => {
      ctx.beginPath();
      ring.forEach(([lon, lat], i) => {
        const [x, y] = frame.at(lat, lon);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    });
  }
}

/**
 * Write the provinces' names on them.
 *
 * The one thing the picture was missing. Without names it is an abstract
 * pattern of outlines that happens to have dots on it -- somebody sent it
 * cannot tell which country they are looking at, let alone which oblast a
 * drone is over, and the border under the mark is doing no work at all.
 *
 * Only where there is room. A province drawn thirty pixels across cannot
 * carry its own name, and a label that does not fit inside its own outline
 * ends up sitting on its neighbour and saying something false.
 */
function nameRegions(ctx, frame, outlines, bands, marks) {
  const size = Math.max(11, Math.round(frame.width * 0.0105));
  ctx.font = `600 ${size}px system-ui, -apple-system, Segoe UI, sans-serif`;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  // Letter-spacing is what makes a map label read as a label rather than as a
  // word dropped on the picture. Not in every engine, so it is set through a
  // property that is simply ignored where it is unknown.
  ctx.letterSpacing = `${Math.max(1, Math.round(size * 0.12))}px`;

  // The marks' own room, claimed before any name is placed. The marks are
  // drawn after the labels and so win every overlap, which is the right
  // priority and the wrong picture: an arrow sitting across "ПОЛТАВСЬКА"
  // leaves a province whose name cannot be read and a mark that looks like
  // it has a word growing out of it. So the names move instead.
  const placed = (marks ?? []).map((mark) => {
    const [x, y] = frame.at(mark.lat, mark.lon);
    const half = markSize(frame) * 0.72;
    return { x0: x - half, x1: x + half, y0: y - half, y1: y + half };
  });
  for (const outline of outlines ?? []) {
    const name = shortName(outline.name);
    if (!name) continue;
    const width = ctx.measureText(name).width;
    const spots = labelSpot(outline.shape, frame, width, size * 2.4, bands);
    if (!spots) continue;
    // The first spot that is not already taken. Nearest the middle of the
    // visible part comes first, so a name only shifts as far as it must.
    let spot = null;
    let box = null;
    for (const candidate of spots) {
      const tryBox = { x0: candidate.x - width / 2, x1: candidate.x + width / 2,
                       y0: candidate.y - size, y1: candidate.y + size };
      if (placed.some((had) => overlaps(had, tryBox))) continue;
      spot = candidate;
      box = tryBox;
      break;
    }
    if (!spot) continue;
    placed.push(box);
    ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
    ctx.fillText(name, spot.x + 1, spot.y + 1);
    ctx.fillStyle = LABEL;
    ctx.fillText(name, spot.x, spot.y);
  }
  ctx.letterSpacing = '0px';
}

const overlaps = (a, b) => a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;

/**
 * Where to write a region's name, or null if there is nowhere honest.
 *
 * The first version put it at the region's centroid and gave up when that
 * fell outside the picture -- which is exactly backwards. The tighter the
 * crop, the more of it one province fills, and the more certainly that
 * province's centroid is off the edge. So on the pictures that most needed
 * naming, nothing was named at all: at the crop this now uses, not one of
 * seventy-four provinces had its centre on screen.
 *
 * What it does instead is label the part you can SEE: the middle of where
 * the province and the picture overlap. Then it checks that point is really
 * inside the outline, because the middle of an overlap can easily be in the
 * next province along -- and a name written on somebody else's land is worse
 * than no name, it is a picture that lies about where a drone is.
 */
export function labelSpot(shape, frame, wide, tall, bands) {
  const margin = Math.max(wide / 2, 8) + 6;
  // The heading and the footer are opaque, so a name placed under one is a
  // name nobody sees. Kept out of both.
  const top = (bands?.head ?? 0) + tall / 2;
  const floor = frame.height - (bands?.foot ?? 0) - tall / 2;
  let best = null;
  walkRings(shape, (ring) => {
    if (ring.length < 3) return;
    const pixels = ring.map(([lon, lat]) => frame.at(lat, lon));
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const [x, y] of pixels) {
      x0 = Math.min(x0, x); x1 = Math.max(x1, x);
      y0 = Math.min(y0, y); y1 = Math.max(y1, y);
    }
    // The part of it on the picture. Whether the name FITS is asked of this,
    // at its full size.
    const seen = {
      x0: Math.max(x0, 0), x1: Math.min(x1, frame.width),
      y0: Math.max(y0, top), y1: Math.min(y1, frame.height - 0, floor),
    };
    const room = (seen.x1 - seen.x0) * (seen.y1 - seen.y0);
    if (seen.x1 - seen.x0 < wide || seen.y1 - seen.y0 < tall) return;
    if (best && room <= best.room) return;

    // Where the name's CENTRE may sit is a smaller box: inset by half the
    // name, so no part of it is cut off by the picture's edge.
    //
    // Two boxes rather than one, because insetting first and then asking
    // whether the name fits counts the name twice. A province spanning the
    // whole picture -- which is most of them, on a tight crop -- had its
    // centre-box shrink below the width of its own name, so the widest
    // provinces in the picture were exactly the ones that went unnamed.
    const reach = {
      x0: Math.min(seen.x0 + margin, (seen.x0 + seen.x1) / 2),
      x1: Math.max(seen.x1 - margin, (seen.x0 + seen.x1) / 2),
      y0: Math.min(seen.y0 + tall / 2, (seen.y0 + seen.y1) / 2),
      y1: Math.max(seen.y1 - tall / 2, (seen.y0 + seen.y1) / 2),
    };
    // Candidates, nearest the middle of what is visible first. One point can
    // easily fall in a bay or across a border; a handful of tries is the
    // difference between labelling a province and skipping it.
    const midX = (reach.x0 + reach.x1) / 2;
    const midY = (reach.y0 + reach.y1) / 2;
    const stepX = (reach.x1 - reach.x0) / 4;
    const stepY = (reach.y1 - reach.y0) / 4;
    // EVERY place the name would sit honestly, not just the first.
    //
    // Returning one was the reason a province with a drone over it lost its
    // name: the caller keeps labels off the marks, and with a single
    // candidate "keep off" can only mean "give up". A province with
    // something in the air over it is the one you most want named, so the
    // name moves along the list instead.
    const spots = [];
    for (const [dx, dy] of [[0, 0], [0, -1], [0, 1], [-1, 0], [1, 0],
                            [-1, -1], [1, -1], [-1, 1], [1, 1],
                            [0, -0.5], [0, 0.5], [-0.5, 0], [0.5, 0]]) {
      const x = midX + dx * stepX;
      const y = midY + dy * stepY;
      if (!inside(pixels, x, y)) continue;
      // Both ends of the name have to be on the land too, or it runs off
      // the province and onto the next one.
      if (!inside(pixels, x - wide / 2, y) || !inside(pixels, x + wide / 2, y)) {
        continue;
      }
      spots.push({ x, y });
    }
    if (spots.length) best = { room, spots };
  });
  return best?.spots ?? null;
}

/** Ray casting: is this pixel inside this ring? */
function inside(ring, x, y) {
  let within = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i, i += 1) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y)
        && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      within = !within;
    }
  }
  return within;
}

/** "Полтавська область" reads as "Полтавська" on a map. */
export function shortName(raw) {
  const name = String(raw ?? '').trim();
  if (!name) return '';
  const trimmed = name.replace(
    /\s+(?:область|обл\.?|oblast|region|province|krai|край|okrug|округ)$/i,
    '');
  // Upper case, because the data arrives lower case -- "київська область" --
  // and a lower-case word dropped on a map reads as a note somebody left
  // rather than as the name of the ground under it.
  return (trimmed || name).slice(0, 28).toLocaleUpperCase('uk');
}

async function drawMarks(ctx, frame, marks) {
  const size = markSize(frame);
  for (const mark of marks ?? []) {
    const image = await glyphImage(mark);
    if (!image) continue;
    const [x, y] = frame.at(mark.lat, mark.lon);
    // A dark disc under each one. The glyphs are bright on a dark ground
    // until they land on a border or a label, and then they are bright on
    // bright -- this is what keeps a mark reading as a mark wherever it falls.
    ctx.beginPath();
    ctx.arc(x, y, size * 0.62, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(6, 8, 12, 0.55)';
    ctx.fill();

    ctx.save();
    ctx.translate(x, y);
    if (mark.turn != null) ctx.rotate((mark.turn * Math.PI) / 180);
    ctx.drawImage(image, -size / 2, -size / 2, size, size);
    ctx.restore();
  }
}

/**
 * What is written on the picture: a heading, a key, and whose data it is.
 *
 * The picture used to carry two lines of text in opposite corners and
 * nothing else, which left somebody sent it unable to answer any of the
 * three questions a picture like this is sent to answer: what am I looking
 * at, when was it, and what do the colours mean.
 *
 * The credit is not decoration and not optional, which is why it cannot be
 * left out: an empty one falls back to CREDIT rather than to nothing. It used
 * to return early on a missing credit, so a picture taken before the first
 * feed arrived went out with NEPTUN's data on it and their name nowhere.
 */
function drawFurniture(ctx, frame, marks, credit, at, bands) {
  const said = credit || CREDIT;
  const { unit, pad, head, foot } = bands;
  const keys = legendKeys(marks);

  // The bands. Faded rather than edged, so they sit over the map without
  // looking like two more rectangles drawn on it.
  band(ctx, frame, 0, head, false);
  band(ctx, frame, frame.height - foot, foot, true);

  // Heading, left. What this is.
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.95)';
  ctx.font = `700 ${Math.round(unit * 1.25)}px system-ui, -apple-system, `
    + 'Segoe UI, sans-serif';
  ctx.fillText(TITLE, pad, Math.round(unit * 2.05));

  // How many, under it.
  const n = marks?.length ?? 0;
  ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
  ctx.font = `500 ${Math.round(unit * 0.8)}px system-ui, -apple-system, `
    + 'Segoe UI, sans-serif';
  ctx.fillText(`${n} ${n === 1 ? 'track' : 'tracks'}`, pad,
    Math.round(unit * 3.1));

  // When, right. A picture of moving things with no time on it is a picture
  // somebody will read as current a day later.
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.72)';
  ctx.font = `600 ${Math.round(unit * 0.86)}px system-ui, -apple-system, `
    + 'Segoe UI, sans-serif';
  ctx.fillText(stampedAt(at), frame.width - pad, Math.round(unit * 2.0));

  // The key, bottom left. Only the kinds actually in the picture: a legend
  // listing things that are not there is a legend nobody trusts.
  let base = frame.height - Math.round(unit * 2.5);
  if (keys.length) {
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    ctx.font = `600 ${Math.round(unit * 0.78)}px system-ui, -apple-system, `
      + 'Segoe UI, sans-serif';
    let x = pad;
    const y = frame.height - Math.round(unit * 2.9);
    for (const key of keys) {
      ctx.beginPath();
      ctx.arc(x + unit * 0.3, y, unit * 0.3, 0, Math.PI * 2);
      ctx.fillStyle = key.colour;
      ctx.fill();
      ctx.fillStyle = 'rgba(255, 255, 255, 0.78)';
      ctx.fillText(key.label, x + unit * 0.85, y + 1);
      x += unit * 0.85 + ctx.measureText(key.label).width + unit * 1.1;
    }
    base = frame.height - Math.round(unit * 1.4);
  }

  // Whose data, bottom left under the key.
  ctx.textAlign = 'left';
  ctx.textBaseline = 'alphabetic';
  ctx.font = `500 ${Math.round(unit * 0.72)}px system-ui, -apple-system, `
    + 'Segoe UI, sans-serif';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.55)';
  ctx.fillText(said, pad, base);

  // And the mark, bottom right.
  ctx.textAlign = 'right';
  ctx.font = `700 ${Math.round(unit * 1.35)}px system-ui, -apple-system, `
    + 'Segoe UI, sans-serif';
  ctx.fillStyle = 'rgba(255, 255, 255, 0.9)';
  ctx.fillText(WATERMARK, frame.width - pad, base);
}

/** A band that fades out into the map rather than ending at a line. */
function band(ctx, frame, top, height, upward) {
  const fade = ctx.createLinearGradient(0, top, 0, top + height);
  fade.addColorStop(upward ? 1 : 0, SCRIM);
  fade.addColorStop(upward ? 0 : 1, 'rgba(8, 10, 15, 0)');
  ctx.fillStyle = fade;
  ctx.fillRect(0, top, frame.width, height);
}

/** The kinds in this picture, each once, in the order they first appear. */
export function legendKeys(marks) {
  const seen = new Map();
  for (const mark of marks ?? []) {
    if (!mark?.label || !mark?.colour || seen.has(mark.label)) continue;
    seen.set(mark.label, { label: mark.label, colour: mark.colour });
  }
  return [...seen.values()].slice(0, 5);
}

/** "16 Sep 2026 · 11:48 UTC". Spelt out, because 09/16 is two dates. */
export function stampedAt(at) {
  const when = at instanceof Date ? at : new Date();
  const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][when.getUTCMonth()];
  const two = (n) => String(n).padStart(2, '0');
  return `${when.getUTCDate()} ${month} ${when.getUTCFullYear()} · `
    + `${two(when.getUTCHours())}:${two(when.getUTCMinutes())} UTC`;
}
