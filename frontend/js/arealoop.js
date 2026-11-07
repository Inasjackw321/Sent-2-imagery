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
import { el, toast } from './ui.js';
import {
  FILM_H, FILM_W, boundsOf, drawTemplate, loadLand, placeOf,
} from './filmtemplate.js';
import {
  HOLD_HOURS, MATCH_KM, clusterIncidents, drawMarksOn, hotspots, incidentSaid, marksFor,
  paintMarks, whenSaid,
} from './incidents.js';

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
const FILM_FPS = 25;

// The data credit EUMETSAT asks for, carried in every saved video.
const CREDIT = 'Contains modified Copernicus data · EUMETSAT View';

let map = null;
let loop = null;   // the one loop open at a time

export function initAreaLoop(leafletMap) {
  map = leafletMap;
  // Above the live layer it sits inside, below everything drawn on the map.
  map.createPane('arealoop').style.zIndex = 430;
  map.getPane('arealoop').style.pointerEvents = 'none';
  // The fire marks over it, clickable, under the popups.
  map.createPane('loopfires').style.zIndex = 615;
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
// "Hotter than the rest of its own frame", and not a fixed level, because the
// red channel is the 3.8 micron brightness temperature and the whole scene
// moves with the day: sunlit desert at noon is warmer than a fire at night.
// A fixed threshold would light up every afternoon. The frame's own 97th
// percentile is its background, and only pixels a clear margin above it count
// as heat, so a frame with no fire in it stays plain rather than inventing
// speckle out of noise.

// How far above the frame's background a pixel must be before it counts, and
// over how many levels it goes from first glow to full white.
export const HEAT_MARGIN = 25;
export const HEAT_SPAN = 60;
// How dark the rest of the picture goes, so the heat stands out against it.
export const HEAT_DIM = 0.5;
const BACKGROUND_PERCENTILE = 0.97;

/** The red channel's value below which `share` of the pixels fall. */
export function redPercentile(data, share) {
  const counts = new Uint32Array(256);
  for (let i = 0; i < data.length; i += 4) counts[data[i]] += 1;
  const want = (data.length / 4) * share;
  let seen = 0;
  for (let v = 0; v < 256; v += 1) {
    seen += counts[v];
    if (seen >= want) return v;
  }
  return 255;
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
 * Push a frame's hot pixels apart from the rest, in place, on RGBA bytes.
 * Returns the levels used and how many pixels counted as heat.
 */
export function heatContrast(data) {
  const background = redPercentile(data, BACKGROUND_PERCENTILE);
  const lo = Math.min(254, background + HEAT_MARGIN);
  const hi = Math.min(255, Math.max(lo + 1, lo + HEAT_SPAN));
  let hot = 0;
  for (let i = 0; i < data.length; i += 4) {
    const h = smooth(lo, hi, data[i]);
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
  return { background, lo, hi, hot };
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
  if (frame.time.includes('/')) return `${day} · whole day`;
  return `${day} ${frame.time.slice(11, 16)} UTC`;
}

/** Whether a product is about heat, so heat contrast starts on for it. */
export function isHeat(title) {
  return /fire|frp|temperature|hotspot|thermal/i.test(String(title ?? ''));
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

  const abort = new AbortController();
  loop = {
    layerId, title, bounds, box, size, abort, frames: [], at: -1,
    done: 0, missing: 0, playing: null, speed: DEFAULT_SPEED, filming: null,
    // On from the start for a heat product, where the hottest pixels are the
    // point; off for anything else, whose colours it would only distort.
    contrast: isHeat(title),
    daily: false, span: '', format: 'image/jpeg',
    outline: L.rectangle(bounds, {
      color: '#ffb74d', weight: 1.5, fill: false, dashArray: '4 4', interactive: false,
    }).addTo(map),
    overlay: null,
    // Detected fires (NASA FIRMS, with their megawatts) and the incidents
    // they group into; drawn for heat products. See incidents.js.
    fires: [], incidents: [], firesSaid: '', showFires: isHeat(title),
    marks: L.layerGroup([], { pane: 'loopfires' }).addTo(map),
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
  mine.contrast = isHeat(mine.title);
  mine.daily = Boolean(plan.daily);
  mine.span = spanSaid(plan);
  mine.format = plan.format || 'image/jpeg';
  if (mine.daily) mine.speed = DAILY_SPEED;
  mine.showFires = mine.contrast;
  if (mine.contrast) loadFires(mine, mine.daily ? 168 : hours);
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
  toast(`${held} of ${mine.frames.length} frames over your area`
    + `${mine.missing ? ` — ${mine.missing} EUMETSAT did not serve` : ''}. `
    + 'Press play, or save it as a video.', held ? 'ok' : 'err');
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
    const { hot, spots } = await heated(raw.img, mine.format);
    if (loop !== mine) {
      URL.revokeObjectURL(raw.url);
      if (hot) URL.revokeObjectURL(hot.url);
      return;
    }
    frame.raw = raw;
    frame.hot = hot ?? raw;
    frame.spots = spots;
    Object.assign(frame, mine.contrast ? frame.hot : frame.raw);
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
 * The heat-contrasted copy of a frame (null if the browser will not make
 * one), and the frame's own hot patches, found before the contrast changes it.
 */
async function heated(img, format = 'image/jpeg') {
  let spots = [];
  try {
    const canvas = document.createElement('canvas');
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height);
    spots = hotspots(pixels.data, canvas.width, canvas.height);
    heatContrast(pixels.data);
    ctx.putImageData(pixels, 0, 0);
    // Saved as it came: a PNG keeps the gaps between a day's swaths clear.
    const blob = await new Promise((done) => canvas.toBlob(done, format, 0.92));
    return { hot: blob ? await picture(blob) : null, spots };
  } catch {
    // The plain frame is still the frame; losing the contrast is not a
    // reason to lose the picture.
    return { hot: null, spots };
  }
}

/** Switch every frame between plain and heat-contrasted, keeping the place. */
function setContrast(on) {
  if (!loop) return;
  loop.contrast = on;
  for (const frame of loop.frames) {
    if (frame.raw) Object.assign(frame, on ? frame.hot : frame.raw);
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
  paintFires();
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
  loop.marks?.remove();
  document.querySelector('#loopFires')?.remove();
  for (const frame of loop.frames) {
    for (const copy of new Set([frame.raw, frame.hot])) {
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
    fires: el('button', {
      class: 'loop-heat',
      title: 'Detected fires with their megawatts (NASA FIRMS), and hot patches no '
        + 'detection confirms',
      onclick: () => setFires(!loop.showFires),
    }, '🎯 Fires'),
    heat: el('button', {
      class: 'loop-heat',
      title: 'Dim the scene and draw the hottest pixels red to white, so fires stand out',
      onclick: () => setContrast(!loop.contrast),
    }, '🔥 Heat contrast'),
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
    parts.fires, parts.rec,
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
  parts.rec.textContent = filming ? '■ Recording' : '⏺ Video';
  parts.rec.title = filming ? 'Stop recording' : `Save the ${loop.span.toLowerCase()} as a video`;
  parts.rec.classList.toggle('is-on', filming);
  parts.rec.disabled = loop.done < 2;
  parts.heat.classList.toggle('is-on', loop.contrast);
  parts.heat.disabled = filming;
  parts.fires.classList.toggle('is-on', loop.showFires);
  parts.fires.textContent = loop.incidents.length ? `🎯 Fires · ${loop.incidents.length}` : '🎯 Fires';
}

// ── The video ──────────────────────────────────────────────────

function filmType() {
  for (const kind of ['video/webm;codecs=vp9', 'video/webm;codecs=vp8',
                      'video/webm', 'video/mp4']) {
    if (window.MediaRecorder?.isTypeSupported?.(kind)) return kind;
  }
  return '';
}

async function film() {
  if (!loop || loop.filming || loop.done < 2) return;
  if (!window.MediaRecorder) {
    toast('This browser cannot record video.', 'err');
    return;
  }
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
  const kind = filmType();
  const chunks = [];
  const rec = new MediaRecorder(canvas.captureStream(FILM_FPS),
    kind ? { mimeType: kind } : undefined);
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const stopped = new Promise((ready) => { rec.onstop = ready; });
  rec.start();
  for (const [index, frame] of held.entries()) {
    if (mine.filming.stop || loop !== mine) break;
    drawTemplate(ctx, {
      picture: mine.showFires ? withMarks(mine, frame) : frame.img, title: mine.title, frames: held, index, bounds, where, rings,
      note: `${mine.daily ? `${held.length} days` : `${spanHours(held)} h`} over the highlighted area`
        + (mine.contrast ? ' · heat contrast on' : '')
        + (mine.showFires && mine.fires.length ? ' · 🔥 = NASA FIRMS detection, MW' : ''),
      credit: CREDIT,
    });
    show(mine.frames.indexOf(frame));
    // eslint-disable-next-line no-await-in-loop
    await new Promise((done) => setTimeout(done, 1000 / mine.speed));
  }
  rec.stop();
  await stopped;
  if (loop === mine) {
    mine.filming = null;
    refreshBar();
  }
  if (!chunks.length) return;
  const blob = new Blob(chunks, { type: kind || 'video/webm' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = `${filmName(mine.layerId, held)}.${blob.type.includes('mp4') ? 'mp4' : 'webm'}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

// ── Detected fires and incidents ───────────────────────────────

function loopBox(mine) {
  return {
    west: mine.bounds.getWest(), south: mine.bounds.getSouth(),
    east: mine.bounds.getEast(), north: mine.bounds.getNorth(),
  };
}

/** Every FIRMS detection in the box over the loop's span, grouped into incidents. */
async function loadFires(mine, hours) {
  mine.firesSaid = 'Looking for satellite fire detections…';
  buildFires();
  try {
    const got = await api.fires({ ...loopBox(mine), hours: Math.min(168, Math.max(1, hours)) });
    if (loop !== mine) return;
    mine.fires = got.fires ?? [];
    mine.incidents = clusterIncidents(mine.fires);
    mine.firesSaid = '';
  } catch (err) {
    if (loop !== mine) return;
    mine.firesSaid = `Fire detections unavailable: ${err.message}`;
  }
  buildFires();
  paintFires();
  refreshBar();
}

function setFires(on) {
  if (!loop) return;
  loop.showFires = on;
  if (on && !loop.fires.length && !loop.firesSaid) loadFires(loop, loop.daily ? 168 : 48);
  buildFires();
  paintFires();
  refreshBar();
}

function currentMarks(mine, frame) {
  return marksFor(mine.fires, frame?.spots, frame?.time, loopBox(mine));
}

function paintFires() {
  if (!loop?.marks) return;
  const frame = loop.frames[loop.at];
  if (!loop.showFires || !frame) { loop.marks.clearLayers(); return; }
  const marks = currentMarks(loop, frame);
  paintMarks(loop.marks, marks, { autoPanPaddingBottomRight: [260, 120] });
  const line = document.querySelector('#loopFiresNow');
  if (line) {
    const bits = [];
    bits.push(marks.detections.length
      ? `${marks.detections.length} detected fire${marks.detections.length === 1 ? '' : 's'} on this frame`
      : 'No detected fire on this frame');
    if (marks.unconfirmed.length) {
      bits.push(`${marks.unconfirmed.length} unconfirmed hot patch${marks.unconfirmed.length === 1 ? '' : 'es'}`);
    }
    line.textContent = bits.join(' · ');
  }
}

/** A copy of the frame with its fire marks drawn on, for the video. */
function withMarks(mine, frame) {
  const canvas = document.createElement('canvas');
  canvas.width = frame.img.naturalWidth;
  canvas.height = frame.img.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(frame.img, 0, 0);
  drawMarksOn(ctx, canvas.width, canvas.height, currentMarks(mine, frame), loopBox(mine));
  return canvas;
}

/** Go to the frame nearest a moment, and to the place. */
function jumpTo(incident) {
  if (!loop) return;
  pause();
  loop.touched = true;
  const want = Date.parse(incident.peakAt);
  let best = -1;
  let gap = Infinity;
  loop.frames.forEach((f, i) => {
    if (!f.img) return;
    const t = Date.parse(String(f.time).split('/')[0]);
    // The first frame at or after the detection, so the fire is on it.
    const d = t >= want ? t - want : (want - t) * 4;
    if (d < gap) { gap = d; best = i; }
  });
  if (best >= 0) show(best);
  map.panTo([incident.lat, incident.lon]);
}

/** The incidents card: what was detected in the box, biggest first. */
function buildFires() {
  document.querySelector('#loopFires')?.remove();
  if (!loop || !loop.showFires) return;
  const rows = loop.incidents.slice(0, 8).map((inc) => {
    const said = incidentSaid(inc);
    return el('button', { class: 'loop-fire', onclick: () => jumpTo(inc),
      title: `Go to ${whenSaid(inc.peakAt)}, when it was strongest` },
      el('b', {}, `🔥 ${said.head}`),
      el('span', {}, said.sub),
      el('span', { class: 'dim' }, `${inc.lat.toFixed(3)}, ${inc.lon.toFixed(3)}`
        + ` · ${inc.totalMw ? `${inc.totalMw} MW summed` : ''}`));
  });
  const more = loop.incidents.length > 8 ? el('div', { class: 'loop-fires-note' },
    `and ${loop.incidents.length - 8} smaller`) : null;
  const empty = !loop.incidents.length && !loop.firesSaid
    ? el('div', { class: 'loop-fires-note' },
      'No satellite fire detection in this box over this time. A bright patch in '
      + 'the picture with no detection is not a confirmed fire.')
    : null;
  const card = el('div', { class: 'loop-fires', id: 'loopFires' },
    el('div', { class: 'loop-load-head' },
      el('b', {}, loop.incidents.length
        ? `Fire incidents · ${loop.incidents.length}` : 'Fire incidents'),
      el('button', { class: 'loop-x', title: 'Hide the fire marks', onclick: () => setFires(false) }, '✕')),
    el('div', { class: 'loop-load-sub', id: 'loopFiresNow' }, loop.firesSaid || ''),
    ...rows, more, empty,
    el('div', { class: 'loop-fires-note' },
      `🔥 a NASA FIRMS thermal detection, with its fire radiative power, shown for ${HOLD_HOURS} h `
      + `after it was made. ? heat: a hot patch in the frame with no detection within ${MATCH_KM} km.`));
  document.querySelector('.stage')?.append(card);
}
