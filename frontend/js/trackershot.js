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
const MARK_PX = 28;

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

const INK = '#0b0e13';
const BORDER = 'rgba(255, 255, 255, 0.16)';
const LAND = 'rgba(255, 255, 255, 0.022)';

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
  return {
    north: Math.min(bounds.north, north),
    south: Math.max(bounds.south, south),
    west: Math.max(bounds.west, west),
    east: Math.min(bounds.east, east),
  };
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
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${MARK_PX}"`
    + ` height="${MARK_PX}" viewBox="0 0 18 18">${part.body}</svg>`;
  const image = new Image(MARK_PX, MARK_PX);
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
export async function drawShot({ bounds, marks, outlines, credit }) {
  const frame = framing(closeIn(bounds, marks));
  const canvas = document.createElement('canvas');
  canvas.width = frame.width;
  canvas.height = frame.height;
  const ctx = canvas.getContext('2d');

  ctx.fillStyle = INK;
  ctx.fillRect(0, 0, frame.width, frame.height);

  // The borders, faint. They are what makes a mark mean anything: an arrow on
  // an empty field is a dot, and an arrow inside Poltava oblast is a report.
  ctx.lineWidth = 1.2;
  ctx.strokeStyle = BORDER;
  ctx.fillStyle = LAND;
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

  // The marks, in the same artwork the map draws, turned to their own course.
  for (const mark of marks) {
    const image = await glyphImage(mark);
    if (!image) continue;
    const [x, y] = frame.at(mark.lat, mark.lon);
    ctx.save();
    ctx.translate(x, y);
    if (mark.turn != null) ctx.rotate((mark.turn * Math.PI) / 180);
    ctx.drawImage(image, -MARK_PX / 2, -MARK_PX / 2, MARK_PX, MARK_PX);
    ctx.restore();
  }

  stamp(ctx, frame, credit);
  return canvas;
}

/**
 * The mark in the corner, and the credit beside it.
 *
 * The credit is not decoration and not optional, which is why it cannot be
 * left out: an empty one falls back to CREDIT rather than to nothing. It used
 * to return early on a missing credit, so a picture taken before the first
 * feed arrived went out with NEPTUN's data on it and their name nowhere.
 */
function stamp(ctx, frame, credit) {
  const said = credit || CREDIT;
  const size = Math.max(16, Math.round(frame.width * 0.019));
  const pad = Math.round(size * 0.9);

  ctx.textBaseline = 'bottom';
  ctx.font = `700 ${size}px system-ui, -apple-system, Segoe UI, sans-serif`;
  ctx.textAlign = 'right';
  ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
  ctx.fillText(WATERMARK, frame.width - pad + 2, frame.height - pad + 2);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
  ctx.fillText(WATERMARK, frame.width - pad, frame.height - pad);

  const small = Math.max(11, Math.round(size * 0.62));
  ctx.font = `500 ${small}px system-ui, -apple-system, Segoe UI, sans-serif`;
  ctx.textAlign = 'left';
  ctx.fillStyle = 'rgba(0, 0, 0, 0.55)';
  ctx.fillText(said, pad + 2, frame.height - pad + 2);
  ctx.fillStyle = 'rgba(255, 255, 255, 0.6)';
  ctx.fillText(said, pad, frame.height - pad);
}
