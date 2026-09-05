// The weather, as it is happening: rain where there is radar to see it, and
// cloud from orbit everywhere else.
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

// Ground radar, and only ground radar. It measures the drops themselves,
// updates every ten minutes and can say how hard it is raining. What it cannot
// do is see anywhere without a radar network, which is most of the planet --
// oceans, and much of Africa, Asia and South America are simply blank, and the
// panel says so rather than leaving it looking broken.
//
// There used to be an infrared cloud-top view alongside it, covering the
// blanks from geostationary orbit. It went because it answered a different
// question while sitting in the same control: cold cloud is not falling water,
// and the cloud layer already says where the cloud is, from imagery that
// actually looks like cloud.
const NOTE = 'Ground radar: it measures the drops. Coverage follows national '
  + 'networks, so oceans and much of Africa and Asia are blank.';
const LEGEND = [['#8cf58c', 'drizzle'], ['#33b333', 'light'], ['#f2e33d', 'moderate'],
  ['#f08c28', 'heavy'], ['#e5332e', 'violent'], ['#a94ce0', 'hail or snow']];

// Colour scheme 4 is the familiar green-to-red rainfall ramp. The trailing
// pair is "smooth" and "show snow in its own colour", both of which help at
// the zoom levels people actually look at.
const SCHEME = 4;
const OPTIONS = '1_1';
// RainViewer will render the same tile at 256 or 512 pixels. The 512 version
// covers exactly the same ground at twice the resolution, so it is worth
// asking for on a dense screen and is four times the bytes for nothing on an
// ordinary one. Leaflet still lays each tile out at 256 CSS pixels either way.
const TILE_SIZE = (globalThis.devicePixelRatio ?? 1) > 1.25 ? 512 : 256;

// How long a frame is held before moving on, and the extra pause on the last
// one so the loop reads as ending rather than stuttering.
const SPEEDS = { slow: 800, normal: 460, fast: 240 };
const REST_MS = 1100;

// How long one frame takes to fade into the next. Kept well under the fastest
// step so the fade finishes before the following frame starts, which is the
// difference between a crossfade and a permanent blur of three frames at once.
const FADE_MS = 160;

// Frames go stale: the service publishes every ten minutes, so anything older
// than this is worth replacing even if the tab has been sitting open.
const REFRESH_MS = 5 * 60 * 1000;

// Remembered between visits. Someone who prefers the loop slow and at half
// opacity should not have to say so again every time they open the app. Read
// before any of the state below, since those defaults come from it.
const SETTINGS_KEY = 'earthviewer.weather';
const remembered = (() => {
  try { return JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}'); } catch { return {}; }
})();

let map = null;
let frames = [];          // [{ time, path, forecast }]
let layers = new Map();   // key -> L.TileLayer, built as needed
let at = 0;               // which frame is showing
let enabled = false;
let playing = true;
let opacity = typeof remembered.opacity === 'number' ? remembered.opacity : 0.75;
let speed = SPEEDS[remembered.speed] ? remembered.speed : 'normal';
let timer = null;
// The handle for dropping the frame that has just been faded over.
let fading = null;
let refresher = null;
let failed = '';
let index = null;         // the whole answer from the service
const ready = new Set();  // keys of frames whose tiles are in hand

export function initRadar(leafletMap) {
  map = leafletMap;
  // Above the imagery and the cloud, because rain is the nearest thing to the
  // viewer and the thing being asked about.
  map.createPane('radar').style.zIndex = 460;
  map.getPane('radar').style.pointerEvents = 'none';
  buildDock();
}

// ── The frames ─────────────────────────────────────────────────

async function loadIndex() {
  const resp = await fetch(INDEX, { cache: 'no-store' });
  if (!resp.ok) throw new Error(`the weather service answered ${resp.status}`);
  return resp.json();
}

/** The radar frames, past first, forecast last. */
function framesFor(data) {
  const part = data?.radar;
  const host = data?.host;
  const past = part?.past ?? [];
  const soon = part?.nowcast ?? [];
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
const keyOf = (frame) => String(frame.time);

function layerFor(frame) {
  const key = keyOf(frame);
  const existing = layers.get(key);
  if (existing) return existing;

  const url = `${frame.host}${frame.path}/${TILE_SIZE}/{z}/{x}/{y}/${SCHEME}/${OPTIONS}.png`;
  const layer = L.tileLayer(url, {
    opacity: 0,
    pane: 'radar',
    maxNativeZoom: 12,
    maxZoom: 19,
    className: 'radar-frame',
    // Two extra rings of tiles either side of the viewport, kept rather than
    // discarded. A small pan then reuses what is already fetched instead of
    // blanking every frame in the loop at once. (It costs nothing up front:
    // keepBuffer governs what is retained on a pan, not what is fetched to
    // begin with -- measured, because it was tempting to blame it for the
    // volume of tiles and it is not responsible for any of them.)
    keepBuffer: 4,
    // Fetch while the map is still moving. Waiting for it to settle is what
    // makes radar appear a beat late after every pan.
    updateWhenIdle: false,
  });
  // Knowing when a frame is ready is what lets the panel say so, and what
  // stops the loop running over frames that have nothing in them yet.
  //
  // Listened to for the life of the layer rather than once. A frame is not
  // ready once and for all: pan or zoom and it needs a new set of tiles, and
  // the earlier version -- which latched on the first load and never let go --
  // went on playing at full speed through the gap, so every pan was followed
  // by a lap of half-empty frames that looked like broken radar.
  layer.on('loading', () => { ready.delete(key); });
  layer.on('load', () => { ready.add(key); paintDock(); });
  layer.addTo(map);
  layers.set(key, layer);
  return layer;
}

/**
 * Show one frame, crossfading from the one before at constant coverage.
 *
 * Two translucent layers stacked do not add up the way a crossfade assumes.
 * What shows through the pair is 1 - (1-a)(1-b), so fading one out at the same
 * rate as the other fades in leaves a hole in the middle: between two layers
 * at 0.75 the pair covers 0.61 at the halfway point, the basemap glares
 * through, and the loop strobes once per frame. Measured before this was
 * written -- 153 of 172 sampled frames sat below the steady value.
 *
 * Holding the old one up until the new one has arrived fixes the hole and
 * makes a bulge instead: both at 0.75 covers 0.94, a fifth denser than the
 * frame either side of it, which pulses.
 *
 * So the outgoing opacity is solved for rather than guessed: given the
 * incoming one at `a`, the value that keeps the pair at exactly `opacity` is
 * 1 - (1-opacity)/(1-a). Driven here frame by frame instead of by a CSS
 * transition, because the two curves have to stay in step and only one of
 * them is a straight line.
 */
function showFrame(which) {
  if (!frames.length) return;
  at = (which + frames.length) % frames.length;
  const key = keyOf(frames[at]);
  const arriving = layerFor(frames[at]);

  // Anything older than the frame being replaced goes immediately: only two
  // layers are ever in the sum, so only two can be solved for.
  let leaving = null;
  for (const [other, layer] of layers) {
    if (other === key) continue;
    if (!leaving && layer.options.opacity > 0) leaving = layer;
    else layer.setOpacity(0);
  }

  cancelAnimationFrame(fading);
  if (!leaving || FADE_MS <= 0) {
    arriving.setOpacity(opacity);
    paintDock();
    return;
  }

  const started = performance.now();
  const tick = (now) => {
    const t = Math.min(1, (now - started) / FADE_MS);
    const a = opacity * t;
    arriving.setOpacity(a);
    // The complement: what the layer underneath has to be for the two of them
    // together to come to `opacity` exactly.
    leaving.setOpacity(t >= 1 ? 0 : 1 - (1 - opacity) / (1 - a));
    if (t < 1) fading = requestAnimationFrame(tick);
  };
  fading = requestAnimationFrame(tick);
  paintDock();
}

/**
 * Build every frame invisibly, in the order the loop will want them.
 *
 * The order is the point. A browser opens a handful of connections to a host
 * and queues everything else behind them, so asking for sixteen frames of
 * tiles in catalogue order puts the frame that is about to be shown behind
 * every tile of the frames that will not be needed for another ten seconds.
 * Starting from the frame on screen and wrapping means the queue drains in
 * the order the eye needs it, and the loop can start moving while the far end
 * of it is still arriving.
 */
function preload(from = at) {
  for (let i = 0; i < frames.length; i += 1) {
    layerFor(frames[(from + i) % frames.length]);
  }
}

/** How many of the frames on screen are still fetching their tiles. */
const stillLoading = () => frames.filter((f) => !ready.has(keyOf(f))).length;

// How long to wait for the next frame's tiles before going anyway. Long
// enough that a frame arriving late is waited for rather than skipped; short
// enough that a frame which is never going to arrive -- no radar coverage
// here, a tile server refusing -- does not stop the loop for good.
const PATIENCE_MS = 4000;
let waitingSince = 0;

function step() {
  clearTimeout(timer);
  if (!enabled || !playing || !frames.length) return;

  // Wait for the frame about to be shown, and only that one.
  //
  // This used to wait for every frame to be in hand, which sounds like the
  // careful choice and is the opposite. Sixteen frames over one viewport is
  // several hundred tiles; until the last of them landed the loop would not
  // move at all, so on any ordinary connection the panel sat on a single
  // frame reporting how much was still loading. Measured before this was
  // changed: at 400 ms a tile it reached one frame in twenty-five seconds.
  //
  // The frame after this one is already being fetched, and its tiles are near
  // the front of the queue because of the order preload asks in. So waiting
  // on it alone is enough to keep the loop from playing blanks.
  const next = frames[(at + 1) % frames.length];
  if (!ready.has(keyOf(next))) {
    waitingSince = waitingSince || performance.now();
    if (performance.now() - waitingSince < PATIENCE_MS) {
      timer = setTimeout(step, 120);
      return;
    }
  }
  waitingSince = 0;

  const last = at === frames.length - 1;
  timer = setTimeout(() => {
    showFrame(at + 1);
    step();
  }, last ? REST_MS : SPEEDS[speed]);
}

function clearLayers() {
  for (const layer of layers.values()) layer.remove();
  layers = new Map();
  ready.clear();
}

/** The newest frame that is a measurement rather than an extrapolation. */
function newestObserved() {
  const firstForecast = frames.findIndex((f) => f.forecast);
  return firstForecast > 0 ? firstForecast - 1 : frames.length - 1;
}

async function refresh({ keepPosition = false, fetchIndex = true } = {}) {
  const wasAt = at;
  try {
    if (fetchIndex) index = await loadIndex();
    const next = framesFor(index);
    if (!next.length) throw new Error('no rain radar frames are published');
    failed = '';

    // Rebuilding from scratch on every refresh would re-download two hours of
    // tiles. Only the frames that actually went away are dropped.
    const keys = new Set(next.map(keyOf));
    for (const [key, layer] of layers) {
      if (!keys.has(key)) {
        layer.remove();
        layers.delete(key);
        ready.delete(key);
      }
    }
    frames = next;
    // Which frame is about to be shown decides what order the tiles are
    // asked for in, so it is settled before anything is fetched.
    const start = keepPosition
      ? Math.min(wasAt, frames.length - 1) : newestObserved();
    at = start;
    waitingSince = 0;
    preload(start);
    showFrame(start);
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
  playing = true;
  paintDock();
  try {
    await refresh();
    step();
    refresher = setInterval(() => refresh({ keepPosition: true }).catch(() => {}), REFRESH_MS);
  } catch (err) {
    enabled = false;
    clearInterval(refresher);
    toast(`No live weather — ${err.message}`, 'err');
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
      el('span', { class: 'radar-mark' }, '◈'), 'Weather'),
    el('div', { class: 'radar-body', id: 'radarBody', hidden: true },
      el('div', { class: 'radar-time', id: 'radarTime' }, '—'),

      el('div', { class: 'radar-controls' },
        el('button', {
          class: 'radar-step', title: 'Back a frame (,)',
          onclick: () => { playing = false; clearTimeout(timer); showFrame(at - 1); },
        }, '‹'),
        el('button', {
          class: 'radar-play', id: 'radarPlay',
          onclick: togglePlay,
        }, '⏸'),
        el('button', {
          class: 'radar-step', title: 'On a frame (.)',
          onclick: () => { playing = false; clearTimeout(timer); showFrame(at + 1); },
        }, '›'),
        el('button', {
          class: 'radar-now', id: 'radarNow', title: 'Back to the latest observation',
          onclick: () => { playing = false; clearTimeout(timer); showFrame(newestObserved()); },
        }, 'now')),

      el('input', {
        type: 'range', class: 'radar-scrub', id: 'radarScrub',
        min: 0, max: 0, value: 0,
        oninput: (e) => { playing = false; clearTimeout(timer); showFrame(e.target.valueAsNumber); },
      }),

      el('div', { class: 'radar-speeds' },
        ...Object.keys(SPEEDS).map((key) =>
          el('button', {
            class: `radar-speed${key === speed ? ' is-on' : ''}`,
            dataset: { speed: key },
            onclick: () => { speed = key; step(); remember(); paintDock(); },
          }, key))),

      el('label', { class: 'radar-fade' }, 'Fade',
        el('input', {
          type: 'range', min: 10, max: 100, value: Math.round(opacity * 100),
          oninput: (e) => {
            opacity = e.target.valueAsNumber / 100;
            queueOpacity();
            remember();
          },
        })),

      // What the colours mean. Without it the picture is decorative.
      el('div', { class: 'radar-key', id: 'radarKey' }),
      el('div', { class: 'radar-note', id: 'radarNote' })),
  );
  paintDock();
}

const remember = debounce(() => {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ speed, opacity })); } catch {}
}, 400);

function togglePlay() {
  playing = !playing;
  if (playing) step(); else clearTimeout(timer);
  paintDock();
}

// A tab nobody is looking at should not be fetching tiles or racing the loop
// forward. It picks up where it left off, at the newest frame, rather than
// wherever the timer happened to have got to.
let heldForHidden = false;
addEventListener('visibilitychange', () => {
  if (!enabled) return;
  if (document.hidden) {
    heldForHidden = playing;
    clearTimeout(timer);
  } else if (heldForHidden) {
    heldForHidden = false;
    refresh({ fetchIndex: true }).then(step).catch(() => {});
  }
});

// Keyboard, once the layer is on and the focus is not in a field.
//
// Not the arrow keys, which belong to the map: Leaflet pans with them and
// stops the event before anything else sees it, and taking them would be
// rude even if it worked. Comma and full stop are the video-scrubbing pair
// and nothing else here wants them. Arrows still step the frames when the
// scrubber itself has focus, which is where a hand looking for them lands.
addEventListener('keydown', (e) => {
  if (!enabled || e.metaKey || e.ctrlKey || e.altKey) return;
  if (/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName ?? '')) return;
  const nudge = (by) => { playing = false; clearTimeout(timer); showFrame(at + by); e.preventDefault(); };
  if (e.key === ' ') { togglePlay(); e.preventDefault(); }
  if (e.key === ',') nudge(-1);
  if (e.key === '.') nudge(1);
});

// Dragging the fade slider would otherwise repaint every layer per pixel.
const queueOpacity = debounce(() => {
  if (frames.length) layers.get(keyOf(frames[at]))?.setOpacity(opacity);
}, 60);

function paintDock() {
  const toggleBtn = $('#radarToggle');
  const body = $('#radarBody');
  if (!toggleBtn || !body) return;

  toggleBtn.classList.toggle('is-on', enabled);
  body.hidden = !enabled;
  if (!enabled) return;

  for (const button of document.querySelectorAll('.radar-speed')) {
    button.classList.toggle('is-on', button.dataset.speed === speed);
  }

  const time = $('#radarTime');
  const note = $('#radarNote');
  const key = $('#radarKey');
  const play = $('#radarPlay');
  const scrub = $('#radarScrub');

  if (failed) {
    time.textContent = '—';
    key.replaceChildren();
    note.textContent = failed;
    return;
  }
  if (!frames.length) {
    time.textContent = 'Loading…';
    key.replaceChildren();
    note.textContent = '';
    return;
  }

  const frame = frames[at];
  const when = new Date(frame.time * 1000);
  const minutes = Math.round((when - Date.now()) / 60000);
  const relative = minutes === 0 ? 'now'
    : minutes > 0 ? `in ${minutes} min`
      : `${-minutes} min ago`;

  time.replaceChildren(
    el('b', {}, when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    el('span', { class: 'radar-rel' }, relative),
    ...(frame.forecast ? [el('span', { class: 'radar-soon' }, 'forecast')] : []));

  play.textContent = playing ? '⏸' : '▶';
  play.title = playing ? 'Hold this frame (space)' : 'Run the loop (space)';
  scrub.max = String(frames.length - 1);
  scrub.value = String(at);

  key.replaceChildren(...LEGEND.map(([colour, label]) =>
    el('span', { class: 'radar-swatch', title: label },
      el('i', { style: `background:${colour}` }), label)));

  const still = stillLoading();
  note.textContent = frame.forecast
    ? 'Extrapolated from the recent motion — not an observation.'
    // The count is progress, not a stall: the loop plays as soon as the frame
    // in front of it has arrived, and the rest fill in behind. Saying "still
    // loading" without that made a working animation look stuck.
    : still > 0 ? `${NOTE} ${still} more frames filling in.`
      : NOTE;
}
