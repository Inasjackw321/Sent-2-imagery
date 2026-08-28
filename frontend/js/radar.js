// Rain, as it is falling, and where it is about to fall.
//
// This is a different kind of layer to everything else on the map. Sentinel
// passes over twice a week; NASA's cloud mosaic is once a day. Precipitation
// changes in minutes, so this comes from ground weather radar rather than from
// orbit -- RainViewer stitches national radar networks into world tiles every
// ten minutes, and publishes a couple of hours of past frames alongside a
// short forecast built by extrapolating the recent motion.
//
// Which makes it the natural companion to the cloud layer. Cloud says whether
// a Sentinel-2 pass had any chance of seeing the ground. Radar says what that
// cloud is actually doing.

import { $, el, toast, debounce } from './ui.js';

// Free, no key, no account -- in keeping with everything else here.
const INDEX = 'https://api.rainviewer.com/public/weather-maps.json';

// Colour scheme 4 is the familiar green-to-red rainfall ramp. The trailing
// pair is "smooth" and "show snow in its own colour", both of which help at
// the zoom levels people actually look at.
const SCHEME = 4;
const OPTIONS = '1_1';
const TILE_SIZE = 256;

// How long a frame is held before moving on, and the extra pause on the last
// one so the loop reads as ending rather than stuttering.
const FRAME_MS = 460;
const REST_MS = 1100;

// Frames go stale: the service publishes every ten minutes, so anything older
// than this is worth replacing even if the tab has been sitting open.
const REFRESH_MS = 5 * 60 * 1000;

let map = null;
let frames = [];          // [{ time, path, forecast }]
let layers = new Map();   // time -> L.TileLayer, built as needed
let at = 0;               // which frame is showing
let enabled = false;
let playing = true;
let opacity = 0.75;
let timer = null;
let refresher = null;
let failed = '';

export function initRadar(leafletMap) {
  map = leafletMap;
  // Above the imagery and the cloud, because rain is the nearest thing to the
  // viewer and the thing being asked about.
  map.createPane('radar').style.zIndex = 460;
  map.getPane('radar').style.pointerEvents = 'none';
  buildDock();
}

// ── The frames ─────────────────────────────────────────────────

async function loadFrames() {
  const resp = await fetch(INDEX, { cache: 'no-store' });
  if (!resp.ok) throw new Error(`the radar service answered ${resp.status}`);
  const data = await resp.json();
  const host = data.host;
  const past = data.radar?.past ?? [];
  const soon = data.radar?.nowcast ?? [];
  if (!past.length && !soon.length) throw new Error('the radar service returned no frames');

  return [
    ...past.map((f) => ({ ...f, host, forecast: false })),
    ...soon.map((f) => ({ ...f, host, forecast: true })),
  ];
}

/**
 * The tile layer for one frame.
 *
 * Built when a frame is first shown and then kept, so running the loop a
 * second time costs nothing. Every layer but the current one is left at zero
 * opacity rather than removed: taking a layer off the map throws its tiles
 * away, and the loop would flicker its way through a re-download on each pass.
 */
function layerFor(frame) {
  const existing = layers.get(frame.time);
  if (existing) return existing;

  const url = `${frame.host}${frame.path}/${TILE_SIZE}/{z}/{x}/{y}/${SCHEME}/${OPTIONS}.png`;
  const layer = L.tileLayer(url, {
    opacity: 0,
    pane: 'radar',
    maxNativeZoom: 12,
    maxZoom: 19,
    attribution: '<a href="https://www.rainviewer.com/">RainViewer</a>',
  });
  layer.addTo(map);
  layers.set(frame.time, layer);
  return layer;
}

function showFrame(index) {
  if (!frames.length) return;
  at = (index + frames.length) % frames.length;
  const frame = frames[at];
  layerFor(frame).setOpacity(opacity);
  for (const [time, layer] of layers) {
    if (time !== frame.time) layer.setOpacity(0);
  }
  // The next frame is built a beat early so its tiles are in the cache by the
  // time the loop reaches it, which is the difference between a loop that
  // runs and one that stutters on first play.
  layerFor(frames[(at + 1) % frames.length]);
  paintDock();
}

function step() {
  clearTimeout(timer);
  if (!enabled || !playing || !frames.length) return;
  const last = at === frames.length - 1;
  timer = setTimeout(() => {
    showFrame(at + 1);
    step();
  }, last ? REST_MS : FRAME_MS);
}

function clearLayers() {
  for (const layer of layers.values()) layer.remove();
  layers = new Map();
}

async function refresh({ keepPosition = false } = {}) {
  const wasAt = at;
  try {
    const next = await loadFrames();
    failed = '';
    // Rebuilding from scratch on every refresh would re-download two hours of
    // tiles. Only the frames that actually went away are dropped.
    const times = new Set(next.map((f) => f.time));
    for (const [time, layer] of layers) {
      if (!times.has(time)) { layer.remove(); layers.delete(time); }
    }
    frames = next;
    // Landing on the most recent observation rather than the forecast is the
    // honest default: it is the frame that is a measurement.
    const newest = frames.findIndex((f) => f.forecast);
    showFrame(keepPosition ? Math.min(wasAt, frames.length - 1)
      : (newest > 0 ? newest - 1 : frames.length - 1));
  } catch (err) {
    failed = err.message;
    frames = [];
    clearLayers();
    paintDock();
    throw err;
  }
}

// ── Turning it on and off ──────────────────────────────────────

async function toggle() {
  enabled = !enabled;
  if (!enabled) {
    clearTimeout(timer);
    clearInterval(refresher);
    clearLayers();
    paintDock();
    return;
  }
  paintDock();
  try {
    await refresh();
    step();
    refresher = setInterval(() => refresh({ keepPosition: true }).catch(() => {}), REFRESH_MS);
  } catch (err) {
    enabled = false;
    clearInterval(refresher);
    toast(`No live rain radar — ${err.message}`, 'err');
    paintDock();
  }
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#radarDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'radar-toggle', id: 'radarToggle', onclick: toggle },
      el('span', { class: 'radar-mark' }, '◈'), 'Rain radar'),
    el('div', { class: 'radar-body', id: 'radarBody', hidden: true },
      el('div', { class: 'radar-time', id: 'radarTime' }, '—'),
      el('div', { class: 'radar-controls' },
        el('button', {
          class: 'radar-step', title: 'Back a frame',
          onclick: () => { playing = false; showFrame(at - 1); step(); },
        }, '‹'),
        el('button', {
          class: 'radar-play', id: 'radarPlay',
          onclick: () => { playing = !playing; playing ? step() : clearTimeout(timer); paintDock(); },
        }, '⏸'),
        el('button', {
          class: 'radar-step', title: 'On a frame',
          onclick: () => { playing = false; showFrame(at + 1); step(); },
        }, '›')),
      el('input', {
        type: 'range', class: 'radar-scrub', id: 'radarScrub',
        min: 0, max: 0, value: 0,
        oninput: (e) => { playing = false; clearTimeout(timer); showFrame(e.target.valueAsNumber); },
      }),
      el('label', { class: 'radar-fade' }, 'Fade',
        el('input', {
          type: 'range', min: 10, max: 100, value: Math.round(opacity * 100),
          oninput: (e) => {
            opacity = e.target.valueAsNumber / 100;
            queueOpacity();
          },
        })),
      el('div', { class: 'radar-note', id: 'radarNote' })),
  );
  paintDock();
}

// Dragging the fade slider would otherwise repaint every layer per pixel.
const queueOpacity = debounce(() => {
  if (frames.length) layers.get(frames[at].time)?.setOpacity(opacity);
}, 60);

function paintDock() {
  const toggleBtn = $('#radarToggle');
  const body = $('#radarBody');
  if (!toggleBtn || !body) return;

  toggleBtn.classList.toggle('is-on', enabled);
  body.hidden = !enabled;
  if (!enabled) return;

  const time = $('#radarTime');
  const note = $('#radarNote');
  const play = $('#radarPlay');
  const scrub = $('#radarScrub');

  if (failed) {
    time.textContent = '—';
    note.textContent = failed;
    return;
  }
  if (!frames.length) {
    time.textContent = 'Loading…';
    note.textContent = '';
    return;
  }

  const frame = frames[at];
  const when = new Date(frame.time * 1000);
  const minutes = Math.round((when - Date.now()) / 60000);
  const relative = minutes === 0 ? 'now'
    : minutes > 0 ? `in ${minutes} min`
      : `${-minutes} min ago`;

  time.innerHTML = '';
  time.append(
    el('b', {}, when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    el('span', { class: 'radar-rel' }, relative),
    ...(frame.forecast ? [el('span', { class: 'radar-soon' }, 'forecast')] : []));

  play.textContent = playing ? '⏸' : '▶';
  play.title = playing ? 'Hold this frame' : 'Run the loop';
  scrub.max = String(frames.length - 1);
  scrub.value = String(at);

  const observed = frames.filter((f) => !f.forecast).length;
  note.textContent = frame.forecast
    ? 'Extrapolated from the recent motion — not an observation.'
    : `${observed} frames of ground radar. Coverage follows national networks, `
      + 'so oceans and much of Africa and Asia are blank.';
}
