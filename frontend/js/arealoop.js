// The history of a live satellite product over one place you mark out:
// Meteosat's last 48 hours at ten minutes, or for a satellite that flies over
// once a day -- Sentinel-3, Sentinel-5P, Metop -- its last two weeks of days.
//
// The live layer's loop is the last eight hours over whatever is on screen.
// This answers the other question: a fire you want to watch start, spread and
// die down -- which is what the Fire Temperature RGB is for -- over the last
// 48 hours, played as a loop over the box you drew, and saved as a video.
//
// One picture of the area per frame rather than map tiles: 288 small images
// for two days instead of tens of thousands of tiles. They come through this
// app's own proxy, which holds them once fetched and keeps them same-origin,
// so the canvas the video is recorded from is not tainted.

import { api } from './api.js';
import { el, savePicture, toast } from './ui.js';
import { openVideoEditor } from './videoeditor.js';
import {
  FILM_H, FILM_W, boundsOf, drawTemplate, loadLand, placeOf,
} from './filmtemplate.js';

// How many frames are asked for at once. Enough to keep the line busy, few
// enough not to queue in front of the live layer's own tiles.
export const AT_ONCE = 6;

// The picture's longest side, in pixels, and its shortest. Meteosat's fire
// channels are a couple of kilometres a pixel, so a pixel per half-kilometre
// of ground is already finer than what was measured; the ceiling keeps two
// days of frames to a few tens of megabytes.
export const MOST_PX = 768;
export const LEAST_PX = 256;

// Frames a second when playing and in the saved video.
export const SPEEDS = [1, 2, 4, 8, 12, 16];
const DEFAULT_SPEED = 8;
// A fortnight of days at eight a second is under two seconds of video.
const DAILY_SPEED = 2;

// The data credit EUMETSAT asks for, carried in every saved video.
const CREDIT = 'Contains modified Copernicus data · EUMETSAT View';

let map = null;
let loop = null;   // the one loop open at a time

export function initAreaLoop(leafletMap) {
  map = leafletMap;
  // Above the live layer it sits inside, below everything drawn on the map.
  map.createPane('arealoop').style.zIndex = 430;
  map.getPane('arealoop').style.pointerEvents = 'none';
}

// ── Pure pieces, tested in Node ────────────────────────────────

/** The picture size for an area, from its Web Mercator box and the ground it covers. */
export function areaSize([x0, y0, x1, y1], groundKm) {
  const wide = Math.abs(x1 - x0);
  const tall = Math.abs(y1 - y0);
  const longest = Math.min(MOST_PX, Math.max(LEAST_PX, Math.round(groundKm * 2)));
  return wide >= tall
    ? { width: longest, height: Math.max(16, Math.round(longest * tall / wide)) }
    : { width: Math.max(16, Math.round(longest * wide / tall)), height: longest };
}

/** The address of one frame of the area, through this app's WMS proxy. */
export function frameUrl(wms, layerId, box, size, time, format = 'image/jpeg') {
  const q = new URLSearchParams({
    service: 'WMS', request: 'GetMap', version: '1.3.0',
    layers: layerId, styles: '',
    // JPEG: a whole-area picture has no holes to be transparent, and it is
    // a tenth of the size of a PNG of the same frame.
    // PNG where the picture has gaps to see through (a day of orbit strips).
    format, transparent: format === 'image/png' ? 'true' : 'false',
    crs: 'EPSG:3857', bbox: box.map((v) => v.toFixed(1)).join(','),
    width: String(size.width), height: String(size.height),
    time,
  });
  return `${wms}?${q}`;
}

/** Newest first, so the latest picture of the place is on screen soonest. */
export function fetchOrder(count) {
  return Array.from({ length: count }, (_, i) => count - 1 - i);
}

/** "123 of 288" and what is missing, for the loading bar. */
export function progressSaid(done, missing, total) {
  const gap = missing ? ` · ${missing} not served` : '';
  return `${done + missing} of ${total}${gap}`;
}

/** The next frame to show that actually arrived, wrapping round. -1 if none has. */
export function nextHeld(frames, at, step = 1) {
  const n = frames.length;
  for (let k = 1; k <= n; k += 1) {
    const i = (((at + step * k) % n) + n) % n;
    if (frames[i].img) return i;
  }
  return -1;
}

// ── Heat contrast ──────────────────────────────────────────────
//
// In the Fire Temperature RGB a fire is a few red-to-white pixels in a
// picture that is otherwise mostly dark, and at the scale of a box over a
// region a small fire is easy to miss. So the hottest pixels are pushed apart
// from everything else: the scene is dimmed, and whatever is clearly hotter
// than the rest of its own frame is redrawn on a flame scale -- red, then
// yellow, then white as it gets hotter -- the same order the RGB itself uses
// for fire intensity, only louder.
//
// "Hotter than the ground around it", and not a fixed level, because the red
// channel is the 3.8 micron brightness temperature and the whole scene moves
// with the day: sunlit desert at noon is warmer than a fire at night. A fixed
// threshold would light up every afternoon.
//
// And around it, not across the whole box. It used to be judged against the
// box's own 97th percentile, which made the answer depend on what else the
// box held: drawn a little wider over warm ground, that percentile rose past
// the fire, and the fire vanished from a picture of the same moment. Each
// pixel is now compared with the average of the ground within LOCAL_KM of it
// -- a fire is a few pixels hotter than its surroundings, a warm landscape is
// warm all the way across -- and that neighbourhood is measured in
// kilometres, so a small box and a large one judge the same fire alike.

// How far above the surrounding ground a pixel must be before it counts, and
// over how many levels it goes from first glow to full white.
export const HEAT_MARGIN = 25;
export const HEAT_SPAN = 60;
// How dark the rest of the picture goes, so the heat stands out against it.
export const HEAT_DIM = 0.5;
// The neighbourhood a pixel is compared with: this far each way.
export const LOCAL_KM = 20;
const LEAST_RADIUS = 4;
const MOST_RADIUS = 96;

/** The neighbourhood radius in pixels, for a picture at `pxPerKm`. */
export function localRadius(pxPerKm) {
  const r = Math.round(LOCAL_KM * (pxPerKm > 0 ? pxPerKm : 1));
  return Math.min(MOST_RADIUS, Math.max(LEAST_RADIUS, r));
}

// Where the ground round a pixel varies a lot -- the edge between warm land
// and cold sea, say -- an average says little, and the warm side of the edge
// sits well above it without being anything. So a pixel must also stand this
// many spreads (standard deviations) above its surroundings. A fire on even
// ground clears it easily; one side of a coastline does not.
export const HEAT_SIGMA = 2;
// But never more than this far above the mean: a big fire raises the spread
// of its own surroundings, and uncapped it would hide itself. A coast's warm
// side (land 140 levels over sea) still stays under it.
export const HEAT_SPREAD_CAP = 90;

/**
 * The mean of the red channel over a (2r+1)-pixel square round every pixel,
 * clipped at the edges, and its spread. One pass to sum, one to read: the
 * same cost for any radius.
 */
export function localStats(data, width, height, radius) {
  const w1 = width + 1;
  const sums = new Float64Array(w1 * (height + 1));
  const squares = new Float64Array(w1 * (height + 1));
  for (let y = 0; y < height; y += 1) {
    let row = 0;
    let rowSq = 0;
    for (let x = 0; x < width; x += 1) {
      const v = data[(y * width + x) * 4];
      row += v;
      rowSq += v * v;
      sums[(y + 1) * w1 + x + 1] = sums[y * w1 + x + 1] + row;
      squares[(y + 1) * w1 + x + 1] = squares[y * w1 + x + 1] + rowSq;
    }
  }
  const mean = new Float32Array(width * height);
  const spread = new Float32Array(width * height);
  const box = (table, x0, y0, x1, y1) => table[y1 * w1 + x1] - table[y0 * w1 + x1]
    - table[y1 * w1 + x0] + table[y0 * w1 + x0];
  for (let y = 0; y < height; y += 1) {
    const y0 = Math.max(0, y - radius);
    const y1 = Math.min(height, y + radius + 1);
    for (let x = 0; x < width; x += 1) {
      const x0 = Math.max(0, x - radius);
      const x1 = Math.min(width, x + radius + 1);
      const n = (y1 - y0) * (x1 - x0);
      const m = box(sums, x0, y0, x1, y1) / n;
      mean[y * width + x] = m;
      spread[y * width + x] = Math.sqrt(Math.max(0, box(squares, x0, y0, x1, y1) / n - m * m));
    }
  }
  return { mean, spread };
}

/** Just the mean: see localStats. */
export function localRed(data, width, height, radius) {
  return localStats(data, width, height, radius).mean;
}

/** The red level a pixel must reach to count as heat, given its surroundings. */
function heatFloor(mean, spread) {
  return mean + Math.max(HEAT_MARGIN, Math.min(HEAT_SPREAD_CAP, HEAT_SIGMA * spread));
}

/** 0 to 1 across [lo, hi], eased at both ends. */
function smooth(lo, hi, x) {
  const t = Math.min(1, Math.max(0, (x - lo) / (hi - lo)));
  return t * t * (3 - 2 * t);
}

/** The flame scale: red, then yellow, then white, for 0 to 1. */
export function flame(h) {
  if (h < 0.5) {
    const t = h / 0.5;
    return [230 + 25 * t, 60 + 160 * t, 30 * t];
  }
  const t = (h - 0.5) / 0.5;
  return [255, 220 + 35 * t, 30 + 205 * t];
}

/**
 * Push a frame's hot pixels apart from the rest, in place, on the RGBA bytes
 * of a `width` x `height` picture, each judged against the ground within
 * `radius` pixels of it. Returns how many pixels counted as heat.
 */
export function heatContrast(data, width, height, radius = LEAST_RADIUS) {
  const { mean, spread } = localStats(data, width, height, radius);
  let hot = 0;
  for (let p = 0; p < width * height; p += 1) {
    const i = p * 4;
    const lo = Math.min(254, heatFloor(mean[p], spread[p]));
    const h = smooth(lo, Math.min(255, Math.max(lo + 1, lo + HEAT_SPAN)), data[i]);
    if (h <= 0) {
      data[i] *= HEAT_DIM;
      data[i + 1] *= HEAT_DIM;
      data[i + 2] *= HEAT_DIM;
      continue;
    }
    hot += 1;
    // Even a faint fire is drawn clearly: most of the way to the flame
    // colour by the time it is a third of the way up the scale.
    const a = Math.min(1, h * 2.5);
    const [r, g, b] = flame(h);
    data[i] = data[i] * HEAT_DIM * (1 - a) + r * a;
    data[i + 1] = data[i + 1] * HEAT_DIM * (1 - a) + g * a;
    data[i + 2] = data[i + 2] * HEAT_DIM * (1 - a) + b * a;
  }
  return { hot, radius };
}

/** The share of a frame's pixels with anything drawn on them (not transparent). */
export function drawnShare(data) {
  let drawn = 0;
  for (let i = 3; i < data.length; i += 4) if (data[i] > 8) drawn += 1;
  return data.length ? drawn / (data.length / 4) : 0;
}

/** Why a loop whose every frame came back empty is empty. */
export function emptySaid(title, frames, daily) {
  const span = daily ? `${frames} days` : `${frames} frames`;
  if (/fire|frp|hotspot/i.test(String(title ?? ''))) {
    return `${title} draws only the pixels where the satellite detected a fire, `
      + `and it found none in this box over these ${span}. An empty box here means `
      + 'no fires were seen, not a broken layer. Draw a larger box, or try the '
      + 'Meteosat Fire Temperature product, which shows heat every ten minutes.';
  }
  return `${title} has nothing over this box in these ${span}: the satellite's `
    + 'passes or the product\'s coverage miss it. Try a larger box or another product.';
}

// ── Thermal view ───────────────────────────────────────────────
//
// The Fire Temperature RGB's red channel is Meteosat's 3.8 micron channel:
// the one fires show up in, because a fire radiates far more at 3.8 microns
// than the ground around it. Drawn on its own, on a blue-to-yellow scale --
// navy for cold, through crimson and red to orange, yellow and white for the
// hottest -- it reads as a heat map: cold sea and cloud blue, warm ground red
// and orange, a fire a yellow-white point.
//
// The scale is fixed, the same for every frame and every box, so a loop does
// not flicker as its brightest pixel changes and two pictures can be
// compared. Hot spots -- pixels well above the ground around them, by the
// same rule heat contrast uses -- are ringed in white and counted.

export const VIEWS = [['thermal', '🌡 Thermal'], ['contrast', '🔥 Heat contrast'],
  ['picture', 'Picture']];

export const THERMAL_STOPS = [
  [0, [6, 6, 60]], [0.22, [20, 20, 130]], [0.42, [100, 20, 120]],
  [0.58, [200, 35, 45]], [0.74, [250, 115, 20]], [0.88, [255, 205, 40]],
  [1, [255, 255, 225]],
];

/** The thermal scale's colour for 0 (coldest) to 1 (hottest). */
export function thermalColour(t) {
  const x = Math.min(1, Math.max(0, t));
  for (let k = 1; k < THERMAL_STOPS.length; k += 1) {
    const [hi, top] = THERMAL_STOPS[k];
    if (x <= hi) {
      const [lo, bottom] = THERMAL_STOPS[k - 1];
      const f = (x - lo) / (hi - lo);
      return bottom.map((c, i) => Math.round(c + (top[i] - c) * f));
    }
  }
  return THERMAL_STOPS[THERMAL_STOPS.length - 1][1];
}

const THERMAL_LUT = Array.from({ length: 256 }, (_, v) => thermalColour(v / 255));

/** Redraw RGBA bytes as a heat map of their red channel, in place. */
export function thermalMap(data) {
  for (let i = 0; i < data.length; i += 4) {
    if (data[i + 3] === 0) continue;
    const [r, g, b] = THERMAL_LUT[data[i]];
    data[i] = r;
    data[i + 1] = g;
    data[i + 2] = b;
  }
}

// At most this many spots ringed per frame: past it, it is warm ground.
export const MOST_SPOTS = 25;

/**
 * The hot spots in a frame: pixels well above the ground within `radius`
 * pixels (see heatFloor), with red leading (sunlit cloud is
 * brighter in the short-wave channels), joined into patches. Positions are
 * fractions of the width and height of the hottest pixel. Hottest first.
 */
export function hotSpots(data, width, height, radius = LEAST_RADIUS) {
  const { mean: around, spread } = localStats(data, width, height, radius);
  const n = width * height;
  const hot = new Uint8Array(n);
  let total = 0;
  for (let p = 0; p < n; p += 1) {
    const i = p * 4;
    if (data[i + 3] > 8 && data[i] >= data[i + 1]
        && data[i] >= heatFloor(around[p], spread[p])) {
      hot[p] = 1;
      total += 1;
    }
  }
  // Hot across a fifth of the frame is a warm landscape, not fires.
  if (total > n * 0.2) return [];
  const spots = [];
  const stack = [];
  for (let start = 0; start < n; start += 1) {
    if (!hot[start]) continue;
    hot[start] = 0;
    stack.push(start);
    let pixels = 0; let peak = -1; let px = 0; let py = 0; let above = 0;
    while (stack.length) {
      const p = stack.pop();
      const x = p % width;
      const y = (p - x) / width;
      pixels += 1;
      const red = data[p * 4];
      if (red > peak) { peak = red; px = x; py = y; above = red - around[p]; }
      if (x > 0 && hot[p - 1]) { hot[p - 1] = 0; stack.push(p - 1); }
      if (x < width - 1 && hot[p + 1]) { hot[p + 1] = 0; stack.push(p + 1); }
      if (y > 0 && hot[p - width]) { hot[p - width] = 0; stack.push(p - width); }
      if (y < height - 1 && hot[p + width]) { hot[p + width] = 0; stack.push(p + width); }
    }
    spots.push({ x: (px + 0.5) / width, y: (py + 0.5) / height, pixels, peak,
      above: Math.round(above) });
  }
  spots.sort((a, b) => b.peak - a.peak || b.pixels - a.pixels);
  return spots.slice(0, MOST_SPOTS);
}

/** "3 hot spots", for the bar. */
export function spotsSaid(spots) {
  if (!spots) return '';
  if (!spots.length) return 'No hot spots';
  return `🔥 ${spots.length} hot spot${spots.length === 1 ? '' : 's'}`;
}

/** How many hours a run of frames covers, first to last. */
export function spanHours(frames) {
  if (!frames?.length) return 0;
  const ms = new Date(frames[frames.length - 1].time) - new Date(frames[0].time);
  return Math.round(ms / 3600000);
}

/** What the saved video is called. */
export function filmName(layerId, frames) {
  const from = frames[0]?.time?.slice(0, 16).replace(/[:T-]/g, '') ?? '';
  const to = frames[frames.length - 1]?.time?.slice(0, 16).replace(/[:T-]/g, '') ?? '';
  return `${(layerId ?? 'meteosat').split(':').pop()}_area_${from}-${to}_kaldockhi`;
}

/** "6 Nov 14:30 UTC" for a frame. */
export function frameSaid(frame) {
  if (!frame?.time) return '';
  // A whole-day frame is a range, "start/end", which is not a date on its own.
  const d = new Date(frame.time.split('/')[0]);
  const day = d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  // A whole day of passes has no clock time of its own.
  // So has a plain date, which is how NASA names its days.
  if (frame.time.includes('/') || frame.time.length === 10) return `${day} · whole day`;
  return `${day} ${frame.time.slice(11, 16)} UTC`;
}

/**
 * Whether a product is a fire-heat picture, so the Thermal view starts on for
 * it: Meteosat's Fire Temperature RGB, a fire radiative power product. Not a
 * product that already draws a measurement on its own colour scale -- a
 * land surface temperature map recoloured by its red channel would be
 * nonsense -- nor NASA's fire detections, which are coloured dots.
 */
export function isHeat(title) {
  const t = String(title ?? '');
  if (/land surface|surface temp|thermal anomal|carbon|anomalies/i.test(t)) return false;
  return /fire temp|firetemp|fire_temp|frp|fire radiative|hotspot/i.test(t);
}

/** "Last 48 hours" or "Last 14 days", from the plan the backend sent. */
export function spanSaid(plan) {
  const span = plan?.span || 'last 48 hours';
  return span.charAt(0).toUpperCase() + span.slice(1);
}

// ── Loading ────────────────────────────────────────────────────

/**
 * Fetch every frame of `layer` over `bounds` for the last `hours`, and play it.
 * Replaces any loop already open.
 */
export async function startAreaLoop({ layerId, title, bounds, hours = 48 }) {
  closeLoop();
  const sw = L.CRS.EPSG3857.project(bounds.getSouthWest());
  const ne = L.CRS.EPSG3857.project(bounds.getNorthEast());
  const box = [sw.x, sw.y, ne.x, ne.y];
  const groundKm = map.distance(bounds.getSouthWest(), bounds.getSouthEast()) / 1000;
  const size = areaSize(box, Math.max(groundKm,
    map.distance(bounds.getSouthWest(), bounds.getNorthWest()) / 1000));
  // Pixels per kilometre of ground, so heat contrast's neighbourhood is the
  // same stretch of ground whatever size of box was drawn.
  const pxPerKm = size.width / Math.max(groundKm, 0.001);

  const abort = new AbortController();
  loop = {
    layerId, title, bounds, box, size, pxPerKm, abort, frames: [], at: -1,
    done: 0, missing: 0, playing: null, speed: DEFAULT_SPEED, filming: null,
    // On from the start for a heat product, where the hottest pixels are the
    // point; off for anything else, whose colours it would only distort.
    // How the frames are drawn: 'thermal' (the heat on a blue-to-yellow
    // scale, hot spots ringed), 'contrast' (the picture dimmed, the hottest
    // pixels red to white) or 'picture' (as EUMETSAT drew it). Thermal for a
    // heat product, where finding the fire is the point.
    view: isHeat(title) ? 'thermal' : 'picture',
    daily: false, span: '', format: 'image/jpeg',
    outline: L.rectangle(bounds, {
      color: '#ffb74d', weight: 1.5, fill: false, dashArray: '4 4', interactive: false,
    }).addTo(map),
    overlay: null,
  };
  const mine = loop;
  // The live layer hides its own fire marks while a loop is open: two sets,
  // for two different moments, over the same ground would contradict.
  window.dispatchEvent(new Event('arealoop'));
  buildLoader();

  let plan;
  try {
    plan = await api.copernicusHistory(layerId, hours);
  } catch (err) {
    toast(`Could not list the frames: ${err.message}`, 'err');
    closeLoop();
    return;
  }
  if (loop !== mine) return;
  mine.frames = plan.frames.map((f) => ({ ...f, url: null, img: null, failed: false }));
  mine.wms = plan.wms;
  mine.title = plan.title || title;
  mine.view = isHeat(mine.title) ? 'thermal' : 'picture';
  mine.daily = Boolean(plan.daily);
  mine.span = spanSaid(plan);
  mine.format = plan.format || 'image/jpeg';
  if (mine.daily) mine.speed = DAILY_SPEED;
  if (!mine.frames.length) {
    toast('EUMETSAT is not holding any frames of that product to fetch.', 'err');
    closeLoop();
    return;
  }
  buildLoader();
  buildBar();

  const order = fetchOrder(mine.frames.length);
  let next = 0;
  const worker = async () => {
    while (next < order.length && loop === mine) {
      const i = order[next];
      next += 1;
      // eslint-disable-next-line no-await-in-loop
      await fetchFrame(mine, i);
    }
  };
  await Promise.all(Array.from({ length: AT_ONCE }, worker));
  if (loop !== mine) return;
  hideLoader();
  const held = mine.done;
  // Every frame arrived and every one is empty: say why, rather than leave a
  // box of nothing that looks like a broken layer.
  const empty = held > 0 && mine.frames.every((f) => !f.img || f.drawn === 0);
  if (empty) {
    toast(emptySaid(mine.title, held, mine.daily), 'warn');
  } else {
    toast(`${held} of ${mine.frames.length} frames over your area`
      + `${mine.missing ? ` — ${mine.missing} EUMETSAT did not serve` : ''}. `
      + 'Press play, or save it as a video.', held ? 'ok' : 'err');
  }
  buildBar();
}

async function fetchFrame(mine, i) {
  const frame = mine.frames[i];
  try {
    const res = await fetch(frameUrl(mine.wms, mine.layerId, mine.box, mine.size, frame.time, mine.format),
      { signal: mine.abort.signal });
    if (!res.ok) throw new Error(String(res.status));
    const blob = await res.blob();
    if (!blob.type.startsWith('image/')) throw new Error(blob.type);
    const raw = await picture(blob);
    const { hot, thermal, spots, drawn } = await heated(raw.img, mine.format, mine.pxPerKm);
    if (loop !== mine) {
      for (const copy of [raw, hot, thermal]) if (copy) URL.revokeObjectURL(copy.url);
      return;
    }
    frame.raw = raw;
    frame.hot = hot ?? raw;
    frame.thermal = thermal ?? raw;
    frame.spots = spots;
    frame.drawn = drawn;
    Object.assign(frame, copyFor(frame, mine.view));
    mine.done += 1;
    // The newest frame in hand goes on the map as it arrives, until somebody
    // plays, steps or scrubs -- after which the picture is theirs to choose.
    if (!mine.touched && i > mine.at) show(i);
  } catch (err) {
    if (err.name === 'AbortError' || loop !== mine) return;
    frame.failed = true;
    mine.missing += 1;
  }
  updateLoader();
  refreshBar();
}

async function picture(blob) {
  const url = URL.createObjectURL(blob);
  const img = new Image();
  img.src = url;
  await img.decode();
  return { url, img };
}

/**
 * A frame's other two drawings -- heat-contrasted and thermal, each null if
 * the browser will not make it -- its hot spots, and how much of it has
 * anything drawn on it at all.
 */
async function heated(img, format = 'image/jpeg', pxPerKm = 1) {
  let drawn = null;
  let spots = [];
  try {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const radius = localRadius(pxPerKm);
    drawn = drawnShare(pixels.data);
    // Found on the frame as it came, before either drawing changes it.
    spots = hotSpots(pixels.data, canvas.width, canvas.height, radius);
    const original = new Uint8ClampedArray(pixels.data);
    // Saved as it came: a PNG keeps the gaps between a day's swaths clear.
    const encode = async () => {
      const blob = await new Promise((done) => canvas.toBlob(done, format, 0.92));
      return blob ? picture(blob) : null;
    };
    heatContrast(pixels.data, canvas.width, canvas.height, radius);
    ctx.putImageData(pixels, 0, 0);
    const hot = await encode();
    pixels.data.set(original);
    thermalMap(pixels.data);
    ctx.putImageData(pixels, 0, 0);
    ringSpots(ctx, spots, canvas.width, canvas.height);
    const thermal = await encode();
    return { hot, thermal, spots, drawn };
  } catch {
    // The plain frame is still the frame; losing a drawing of it is not a
    // reason to lose the picture.
    return { hot: null, thermal: null, spots, drawn };
  }
}

/** Ring each hot spot on a thermal frame, in white, sized by the spot. */
function ringSpots(ctx, spots, width, height) {
  ctx.save();
  ctx.strokeStyle = '#ffffff';
  ctx.lineWidth = Math.max(1.5, Math.min(width, height) / 300);
  for (const spot of spots) {
    ctx.beginPath();
    ctx.arc(spot.x * width, spot.y * height, 6 + Math.sqrt(spot.pixels) * 1.5, 0, Math.PI * 2);
    ctx.stroke();
  }
  ctx.restore();
}

/** Which drawing of a frame a view shows. */
function copyFor(frame, view) {
  if (view === 'thermal') return frame.thermal ?? frame.raw;
  if (view === 'contrast') return frame.hot ?? frame.raw;
  return frame.raw;
}

/** Switch every frame to another drawing, keeping the place. */
function setView(view) {
  if (!loop) return;
  loop.view = view;
  for (const frame of loop.frames) {
    if (frame.raw) Object.assign(frame, copyFor(frame, view));
  }
  if (loop.at >= 0) show(loop.at);
  refreshBar();
}

// ── The loading bar, at the side of the map ────────────────────

function buildLoader() {
  document.querySelector('#loopLoad')?.remove();
  if (!loop) return;
  const total = loop.frames.length;
  const card = el('div', { class: 'loop-load', id: 'loopLoad' },
    el('div', { class: 'loop-load-head' },
      el('b', {}, loop.title || 'Meteosat'),
      el('button', { class: 'loop-x', title: 'Stop and close', onclick: closeLoop }, '✕')),
    el('div', { class: 'loop-load-sub' }, `${loop.span || 'Fetching the history'} over your area`),
    el('div', { class: 'loop-track' }, el('div', { class: 'loop-fill', id: 'loopFill' })),
    el('div', { class: 'loop-count', id: 'loopCount' },
      total ? progressSaid(0, 0, total) : 'Listing the frames…'));
  document.querySelector('.stage')?.append(card);
  updateLoader();
}

function updateLoader() {
  if (!loop) return;
  const total = loop.frames.length || 1;
  const fill = document.querySelector('#loopFill');
  const count = document.querySelector('#loopCount');
  if (fill) fill.style.width = `${Math.round(((loop.done + loop.missing) / total) * 100)}%`;
  if (count && loop.frames.length) {
    count.textContent = progressSaid(loop.done, loop.missing, loop.frames.length);
  }
}

function hideLoader() {
  document.querySelector('#loopLoad')?.remove();
}

// ── Showing and playing ────────────────────────────────────────

function show(i) {
  if (!loop || !loop.frames[i]?.img) return;
  loop.at = i;
  const url = loop.frames[i].url;
  if (!loop.overlay) {
    loop.overlay = L.imageOverlay(url, loop.bounds, { pane: 'arealoop', interactive: false })
      .addTo(map);
  } else {
    loop.overlay.setUrl(url);
  }
  refreshBar();
}

function play() {
  if (!loop || loop.playing || loop.done < 2) return;
  loop.touched = true;
  loop.playing = setInterval(() => {
    const i = nextHeld(loop.frames, loop.at);
    if (i >= 0) show(i);
  }, 1000 / loop.speed);
  refreshBar();
}

function pause() {
  if (!loop?.playing) return;
  clearInterval(loop.playing);
  loop.playing = null;
  refreshBar();
}

export function closeLoop() {
  if (!loop) return;
  loop.abort.abort();
  if (loop.playing) clearInterval(loop.playing);
  if (loop.filming) loop.filming.stop = true;
  loop.overlay?.remove();
  loop.outline?.remove();
  for (const frame of loop.frames) {
    for (const copy of new Set([frame.raw, frame.hot, frame.thermal])) {
      if (copy?.url) URL.revokeObjectURL(copy.url);
    }
  }
  loop = null;
  hideLoader();
  document.querySelector('#loopBar')?.remove();
  window.dispatchEvent(new Event('arealoop'));
}

/** Whether an area loop is open. */
export function loopOpen() {
  return loop !== null;
}

function buildBar() {
  document.querySelector('#loopBar')?.remove();
  if (!loop || !loop.frames.length) return;
  const frames = loop.frames;
  const step = (by) => () => {
    pause();
    loop.touched = true;
    const i = nextHeld(frames, Math.max(0, loop.at), by);
    if (i >= 0) show(i);
  };
  // Built once and updated in place: rebuilt on every frame, the slider
  // would be replaced under the pointer and a drag would end at once.
  const parts = {
    play: el('button', { class: 'cop-play', onclick: () => (loop.playing ? pause() : play()) }),
    back: el('button', { class: 'cop-play', title: 'One frame back', onclick: step(-1) }, '‹'),
    scrub: el('input', {
      class: 'loop-scrub', type: 'range', min: '0', max: String(frames.length - 1),
      oninput: (e) => {
        pause();
        loop.touched = true;
        const want = Number(e.target.value);
        const i = frames[want]?.img ? want : nextHeld(frames, want, 1);
        if (i >= 0) show(i);
      },
    }),
    on: el('button', { class: 'cop-play', title: 'One frame on', onclick: step(1) }, '›'),
    when: el('span', { class: 'cop-time loop-when' }),
    speed: el('select', {
      class: 'loop-speed', title: 'Frames a second, playing and in the video',
      onchange: (e) => {
        loop.speed = Number(e.target.value);
        if (loop.playing) { pause(); play(); }
      },
    }, ...SPEEDS.map((sp) => el('option', { value: String(sp), selected: sp === loop.speed },
      `${sp} fps`))),
    heat: el('select', {
      class: 'loop-speed loop-view',
      title: 'Thermal: the heat on a blue-to-yellow scale, hot spots ringed. '
        + 'Heat contrast: the scene dimmed, the hottest pixels red to white. '
        + 'Picture: as EUMETSAT drew it.',
      onchange: (e) => setView(e.target.value),
    }, ...VIEWS.map(([value, label]) => el('option', { value, selected: value === loop.view }, label))),
    spots: el('span', { class: 'loop-spots' }),
    rec: el('button', {
      class: 'cop-rec',
      onclick: () => (loop.filming ? (loop.filming.stop = true) : film()),
    }),
  };
  loop.bar = parts;
  const bar = el('div', { class: 'loop-bar', id: 'loopBar' },
    el('span', { class: 'loop-tag', title: loop.title },
      loop.daily ? `${loop.frames.length} days · area` : '48 h · area'),
    parts.play, parts.back, parts.scrub, parts.on, parts.when, parts.speed, parts.heat,
    parts.spots, parts.rec,
    el('button', { class: 'loop-x', title: 'Close the area loop', onclick: closeLoop }, '✕'));
  document.querySelector('.stage')?.append(bar);
  refreshBar();
}

function refreshBar() {
  const parts = loop?.bar;
  if (!parts) return;
  const at = Math.max(0, loop.at);
  const filming = Boolean(loop.filming);
  parts.play.textContent = loop.playing ? '❚❚' : '▶';
  parts.play.title = loop.playing ? 'Stop' : `Play the ${loop.span.toLowerCase()} on a loop`;
  parts.play.classList.toggle('is-on', Boolean(loop.playing));
  parts.play.disabled = loop.done < 2 || filming;
  for (const b of [parts.back, parts.on, parts.scrub]) b.disabled = filming;
  parts.scrub.value = String(at);
  parts.when.textContent = frameSaid(loop.frames[at]);
  if (!filming) parts.rec.textContent = '⏺ Video';
  parts.rec.title = filming ? 'Stop preparing the video'
    : `Make a video of the ${loop.span.toLowerCase()}: trim, crop and save it as MP4`;
  parts.rec.classList.toggle('is-on', filming);
  parts.rec.disabled = loop.done < 2;
  parts.heat.value = loop.view;
  parts.heat.disabled = filming;
  const spots = loop.frames[at]?.spots;
  parts.spots.hidden = loop.view === 'picture' || !spots;
  parts.spots.textContent = spotsSaid(spots);
  parts.spots.classList.toggle('is-hot', Boolean(spots?.length));
}

// ── The video ──────────────────────────────────────────────────

async function film() {
  if (!loop || loop.filming || loop.done < 2) return;
  pause();
  const mine = loop;
  const held = mine.frames.filter((f) => f.img);
  // Laid out in the video template: the imagery, and beside it the date, the
  // timeline and a minimap of where it is. See filmtemplate.js.
  const canvas = document.createElement('canvas');
  canvas.width = FILM_W;
  canvas.height = FILM_H;
  const ctx = canvas.getContext('2d');
  mine.filming = { stop: false };
  refreshBar();
  const [rings, where] = await Promise.all([loadLand(), placeOf(mine.bounds)]);
  const bounds = boundsOf(mine.bounds);
  // Every frame drawn once, exactly, and kept as a still: the editor then
  // trims, crops and encodes them -- nothing is recorded in real time.
  const stills = [];
  for (const [index, frame] of held.entries()) {
    if (mine.filming.stop || loop !== mine) break;
    drawTemplate(ctx, {
      picture: frame.img, title: mine.title, frames: held, index, bounds, where, rings,
      note: `${mine.daily ? `${held.length} days` : `${spanHours(held)} h`} over the highlighted area`
        + (mine.view === 'thermal' ? ' · thermal view, hot spots ringed'
          : mine.view === 'contrast' ? ' · heat contrast on' : ''),
      credit: CREDIT,
    });
    // eslint-disable-next-line no-await-in-loop
    const still = await new Promise((done) => canvas.toBlob(done, 'image/jpeg', 0.92));
    if (still) stills.push(still);
    if (mine.bar) mine.bar.rec.textContent = `■ ${index + 1} / ${held.length}`;
  }
  const stopped = mine.filming.stop;
  if (loop === mine) {
    mine.filming = null;
    refreshBar();
  }
  if (stopped || !stills.length) return;
  await openVideoEditor({
    frames: stills, fps: mine.speed, name: filmName(mine.layerId, held),
    title: `Edit video · ${mine.title}`,
    save: (blob, filename) => savePicture(blob, filename, { title: mine.title, what: 'Video' }),
  });
}

