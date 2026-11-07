// The frame every saved video is laid out in.
//
// A clip of a fire on its own says nothing about where it is or when. So
// each video frame is a template: the imagery on the left, and a panel on the
// right with the product, the date and time of this frame, where it falls in
// the whole span, and a minimap showing where on Earth it was taken.
//
// The minimap is drawn from Natural Earth's land outlines, shipped with the
// app -- not from the basemap, whose tiles come from another server and would
// taint the canvas the video is recorded from. Land only, no national
// borders: a minimap is for finding the place, and a border on it would be a
// claim this app makes nowhere else.

export const FILM_W = 1280;
export const FILM_H = 720;
export const PANEL_W = 360;
const PAD = 16;
const MAP_H = 230;

const INK = '#e9eef7';
const DIM = '#93a1b8';
const FAINT = '#64748b';
const ACCENT = '#ffb74d';
const BG = '#0b0e13';
const PANEL_BG = '#11161e';
const SEA = '#0d1a2b';
const LAND = '#2c3a33';
const COAST = '#4a5d52';

const FONT = 'system-ui, -apple-system, Segoe UI, sans-serif';

// ── Pure geometry, tested in Node ──────────────────────────────

/** Where everything goes, for imagery of a given shape. */
export function layout(imgW, imgH) {
  const room = { x: PAD, y: PAD, w: FILM_W - PANEL_W - PAD * 2, h: FILM_H - PAD * 2 };
  const scale = Math.min(room.w / imgW, room.h / imgH);
  const w = Math.round(imgW * scale);
  const h = Math.round(imgH * scale);
  const panel = { x: FILM_W - PANEL_W, y: 0, w: PANEL_W, h: FILM_H };
  return {
    image: { x: room.x + Math.round((room.w - w) / 2), y: room.y + Math.round((room.h - h) / 2), w, h },
    panel,
    minimap: { x: panel.x + PAD, y: FILM_H - PAD - 64 - MAP_H, w: PANEL_W - PAD * 2, h: MAP_H },
  };
}

/** Web Mercator y for a latitude, in the same units as longitude degrees. */
export function mercY(lat) {
  const clamped = Math.max(-85, Math.min(85, lat));
  return (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + (clamped * Math.PI) / 360));
}

/**
 * What the minimap shows: the area, with enough around it to recognise the
 * place -- at least twelve degrees across, and six times the area itself.
 * In Mercator units, shaped to the minimap.
 */
export function minimapView({ west, south, east, north }, aspect) {
  const cx = (west + east) / 2;
  const cy = (mercY(south) + mercY(north)) / 2;
  let wide = Math.max(12, (east - west) * 6);
  let tall = Math.max(wide / aspect, (mercY(north) - mercY(south)) * 6);
  wide = Math.max(wide, tall * aspect);
  tall = wide / aspect;
  return { x0: cx - wide / 2, x1: cx + wide / 2, y0: cy - tall / 2, y1: cy + tall / 2 };
}

/** A longitude and latitude to a point in a rectangle showing `view`. */
export function toMinimap(view, rect, lon, lat) {
  return {
    x: rect.x + ((lon - view.x0) / (view.x1 - view.x0)) * rect.w,
    y: rect.y + ((view.y1 - mercY(lat)) / (view.y1 - view.y0)) * rect.h,
  };
}

/** How far along the whole span a frame is, 0 to 1. */
export function spanAt(frames, index) {
  if (!frames?.length || frames.length < 2) return 1;
  return Math.max(0, Math.min(1, index / (frames.length - 1)));
}

/** "31.25° N, 34.80° E". */
export function coordSaid(lat, lon) {
  return `${Math.abs(lat).toFixed(2)}° ${lat >= 0 ? 'N' : 'S'}, `
    + `${Math.abs(lon).toFixed(2)}° ${lon >= 0 ? 'E' : 'W'}`;
}

/** "120 × 90 km" for an area. */
export function sizeSaid({ west, south, east, north }) {
  const midLat = ((south + north) / 2) * (Math.PI / 180);
  const km = (deg) => Math.round(deg * 111.32);
  const wide = km((east - west) * Math.cos(midLat));
  const tall = km(north - south);
  return `${wide} × ${tall} km`;
}

/** "6 Nov 2026" and "14:30 UTC" for a frame time. */
export function whenSaid(time) {
  if (!time) return { day: '', clock: '' };
  // A whole-day frame is a range, "start/end", which is not a date on its own.
  const d = new Date(String(time).split('/')[0]);
  const day = d.toLocaleDateString('en-GB',
    { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
  // A whole-day frame (a satellite that flies over) has no single clock time.
  const ranged = String(time).includes('/');
  return { day, clock: ranged ? 'whole day, UTC' : `${String(time).slice(11, 16)} UTC` };
}

/** {west, south, east, north} out of a Leaflet LatLngBounds. */
export function boundsOf(latLngBounds) {
  return {
    west: latLngBounds.getWest(), south: latLngBounds.getSouth(),
    east: latLngBounds.getEast(), north: latLngBounds.getNorth(),
  };
}

/**
 * The region the middle of an area is in, by name, or '' if nobody knows.
 * Asked of this app's backend, which asks the gazetteer once per place.
 */
export async function placeOf(latLngBounds) {
  const c = latLngBounds.getCenter();
  try {
    const got = await (await fetch(`/api/whereis?${new URLSearchParams({
      lat: c.lat.toFixed(4), lon: c.lng.toFixed(4) })}`)).json();
    return got.region ?? '';
  } catch {
    return '';
  }
}

// ── The land, fetched once ─────────────────────────────────────

let land = null;

export async function loadLand() {
  if (land) return land;
  try {
    const got = await (await fetch('/vendor/naturalearth/land.json')).json();
    land = got.rings ?? [];
  } catch {
    // The minimap loses its coastlines and keeps its box and coordinates.
    land = [];
  }
  return land;
}

// ── Drawing ────────────────────────────────────────────────────

function text(ctx, said, x, y, { size = 14, weight = 500, colour = INK, align = 'left' } = {}) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.fillStyle = colour;
  ctx.textAlign = align;
  ctx.textBaseline = 'alphabetic';
  ctx.fillText(said, x, y);
}

/** Lines of `said` no wider than `width`, at most `most` of them. */
function wrap(ctx, said, width, most) {
  const words = String(said ?? '').split(/\s+/).filter(Boolean);
  const lines = [];
  let line = '';
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (ctx.measureText(next).width > width && line) {
      lines.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  if (lines.length > most) {
    lines.length = most;
    lines[most - 1] = `${lines[most - 1].replace(/\s+\S*$/, '')}…`;
  }
  return lines;
}

function drawMinimap(ctx, rect, bounds, rings) {
  const view = minimapView(bounds, rect.w / rect.h);
  ctx.save();
  ctx.beginPath();
  ctx.rect(rect.x, rect.y, rect.w, rect.h);
  ctx.clip();
  ctx.fillStyle = SEA;
  ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
  ctx.fillStyle = LAND;
  ctx.strokeStyle = COAST;
  ctx.lineWidth = 1;
  for (const ring of rings) {
    ctx.beginPath();
    for (let i = 0; i < ring.length; i += 2) {
      const at = toMinimap(view, rect, ring[i], ring[i + 1]);
      if (i === 0) ctx.moveTo(at.x, at.y); else ctx.lineTo(at.x, at.y);
    }
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
  }
  // The area, never smaller than a few pixels, with a crosshair to find it.
  const a = toMinimap(view, rect, bounds.west, bounds.north);
  const b = toMinimap(view, rect, bounds.east, bounds.south);
  const cx = (a.x + b.x) / 2;
  const cy = (a.y + b.y) / 2;
  const w = Math.max(8, b.x - a.x);
  const h = Math.max(8, b.y - a.y);
  ctx.strokeStyle = 'rgba(255,183,77,.45)';
  ctx.beginPath();
  ctx.moveTo(rect.x, cy); ctx.lineTo(cx - w / 2 - 4, cy);
  ctx.moveTo(cx + w / 2 + 4, cy); ctx.lineTo(rect.x + rect.w, cy);
  ctx.moveTo(cx, rect.y); ctx.lineTo(cx, cy - h / 2 - 4);
  ctx.moveTo(cx, cy + h / 2 + 4); ctx.lineTo(cx, rect.y + rect.h);
  ctx.stroke();
  ctx.fillStyle = 'rgba(255,183,77,.25)';
  ctx.fillRect(cx - w / 2, cy - h / 2, w, h);
  ctx.strokeStyle = ACCENT;
  ctx.lineWidth = 2;
  ctx.strokeRect(cx - w / 2, cy - h / 2, w, h);
  ctx.restore();
  ctx.strokeStyle = '#263043';
  ctx.lineWidth = 1;
  ctx.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.w - 1, rect.h - 1);
}

/**
 * Draw one whole video frame.
 *
 *   picture    a canvas or image holding this frame's imagery
 *   title      the product, e.g. "MTG FCI Fire Temperature RGB"
 *   frames     every frame in the video, for the timeline
 *   index      which one this is
 *   bounds     {west, south, east, north} of what the imagery shows
 *   where      a place name for it, if one is known
 *   note       a line under the title, e.g. "Heat contrast on"
 *   credit     the data credit
 */
export function drawTemplate(ctx, {
  picture, title, frames, index, bounds, where = '', note = '', credit = '', rings = [],
}) {
  const width = picture.naturalWidth || picture.width;
  const height = picture.naturalHeight || picture.height;
  const at = layout(width, height);
  const frame = frames[index];

  ctx.save();
  ctx.fillStyle = BG;
  ctx.fillRect(0, 0, FILM_W, FILM_H);
  ctx.imageSmoothingQuality = 'high';
  ctx.drawImage(picture, at.image.x, at.image.y, at.image.w, at.image.h);
  ctx.strokeStyle = '#263043';
  ctx.strokeRect(at.image.x - 0.5, at.image.y - 0.5, at.image.w + 1, at.image.h + 1);

  const p = at.panel;
  ctx.fillStyle = PANEL_BG;
  ctx.fillRect(p.x, p.y, p.w, p.h);
  ctx.fillStyle = ACCENT;
  ctx.fillRect(p.x, 0, 3, FILM_H);
  const left = p.x + PAD + 4;
  const inner = p.w - PAD * 2 - 4;

  text(ctx, 'EARTHVIEWER', left, 34, { size: 11, weight: 700, colour: FAINT });
  ctx.font = `650 19px ${FONT}`;
  const lines = wrap(ctx, title, inner, 2);
  lines.forEach((line, i) => text(ctx, line, left, 62 + i * 24, { size: 19, weight: 650 }));
  let y = 62 + lines.length * 24;
  if (note) { text(ctx, note, left, y, { size: 12, colour: DIM }); y += 20; }

  const when = whenSaid(frame?.time);
  text(ctx, when.day, left, y + 46, { size: 30, weight: 700 });
  text(ctx, when.clock, left, y + 78, { size: 22, weight: 600, colour: ACCENT });

  // The whole span, and where this frame is in it.
  const ty = y + 112;
  const share = spanAt(frames, index);
  ctx.fillStyle = '#1b2230';
  ctx.fillRect(left, ty, inner, 6);
  ctx.fillStyle = ACCENT;
  ctx.fillRect(left, ty, Math.max(3, inner * share), 6);
  ctx.beginPath();
  ctx.arc(left + inner * share, ty + 3, 6, 0, Math.PI * 2);
  ctx.fill();
  const first = whenSaid(frames[0]?.time);
  const last = whenSaid(frames[frames.length - 1]?.time);
  text(ctx, `${first.day}`, left, ty + 24, { size: 11, colour: DIM });
  text(ctx, `${last.day}`, left + inner, ty + 24, { size: 11, colour: DIM, align: 'right' });
  text(ctx, first.clock.replace(/,? UTC$/, ''), left, ty + 39, { size: 11, colour: FAINT });
  text(ctx, last.clock.replace(/,? UTC$/, ''), left + inner, ty + 39,
    { size: 11, colour: FAINT, align: 'right' });
  text(ctx, `frame ${index + 1} of ${frames.length}`, left + inner / 2, ty + 39,
    { size: 11, colour: FAINT, align: 'center' });

  // Where.
  const m = at.minimap;
  if (bounds) {
    drawMinimap(ctx, m, bounds, rings);
    const lat = (bounds.south + bounds.north) / 2;
    const lon = (bounds.west + bounds.east) / 2;
    ctx.font = `600 14px ${FONT}`;
    const place = where ? wrap(ctx, where, inner, 1)[0] : coordSaid(lat, lon);
    text(ctx, place, left, m.y - 26, { size: 14, weight: 600 });
    text(ctx, where ? `${coordSaid(lat, lon)} · ${sizeSaid(bounds)}` : sizeSaid(bounds),
      left, m.y - 9, { size: 11, colour: DIM });
  }

  // The credit at the foot, the watermark above it.
  ctx.font = `400 10.5px ${FONT}`;
  const credits = wrap(ctx, credit, inner, 2);
  const foot = FILM_H - PAD;
  credits.forEach((line, i) => text(ctx, line, left,
    foot - (credits.length - 1 - i) * 13, { size: 10.5, colour: FAINT }));
  text(ctx, '@Kaldockhi', left, foot - credits.length * 13 - 6,
    { size: 15, weight: 700, colour: ACCENT });
  ctx.restore();
}
