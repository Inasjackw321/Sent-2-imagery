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

// Two ways of watching the weather, and they answer different questions.
//
// Ground radar is the sharp one: it measures the drops themselves, updates
// every ten minutes and can say how hard it is raining. What it cannot do is
// see anywhere without a radar network, which is most of the planet -- oceans,
// and much of Africa, Asia and South America are simply blank.
//
// The satellite view has the opposite shape. It is infrared from
// geostationary orbit, so it covers everything, everywhere, all the time --
// but it sees cloud tops rather than rain, and cold cloud is not the same
// thing as falling water.
//
// Offering only the first is what made this feel broken over half the world.
const MODES = {
  radar: {
    label: 'Rain radar', part: 'radar',
    note: 'Ground radar: it measures the drops. Coverage follows national '
      + 'networks, so oceans and much of Africa and Asia are blank.',
    legend: [['#8cf58c', 'drizzle'], ['#33b333', 'light'], ['#f2e33d', 'moderate'],
      ['#f08c28', 'heavy'], ['#e5332e', 'violent'], ['#a94ce0', 'hail or snow']],
  },
  satellite: {
    label: 'Cloud tops', part: 'satellite',
    note: 'Infrared from geostationary orbit: everywhere at once, but it shows '
      + 'cloud top temperature, not rain. Cold and bright is tall storm cloud.',
    legend: [['#1b2a3a', 'thin'], ['#5b7590', 'low cloud'], ['#b9c6d4', 'thick'],
      ['#ffffff', 'cold tops']],
  },
};

// Colour scheme 4 is the familiar green-to-red rainfall ramp. The trailing
// pair is "smooth" and "show snow in its own colour", both of which help at
// the zoom levels people actually look at.
const SCHEME = 4;
const OPTIONS = '1_1';
const TILE_SIZE = 256;

// How long a frame is held before moving on, and the extra pause on the last
// one so the loop reads as ending rather than stuttering.
const SPEEDS = { slow: 800, normal: 460, fast: 240 };
const REST_MS = 1100;

// Frames go stale: the service publishes every ten minutes, so anything older
// than this is worth replacing even if the tab has been sitting open.
const REFRESH_MS = 5 * 60 * 1000;

// Remembered between visits. Someone who prefers the satellite view at half
// opacity should not have to say so again every time they open the app. Read
// before any of the state below, since three of those defaults come from it.
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
let mode = MODES[remembered.mode] ? remembered.mode : 'radar';
let speed = SPEEDS[remembered.speed] ? remembered.speed : 'normal';
let timer = null;
let refresher = null;
let failed = '';
let index = null;         // the whole answer from the service, both modes
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

/** The frames for whichever mode is showing, past first, forecast last. */
function framesFor(data, which) {
  const part = data?.[MODES[which].part];
  const host = data?.host;
  const past = part?.past ?? part?.infrared ?? [];
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
const keyOf = (frame) => `${mode}:${frame.time}`;

function layerFor(frame) {
  const key = keyOf(frame);
  const existing = layers.get(key);
  if (existing) return existing;

  // The satellite tiles use a different scheme: 0 is the plain infrared ramp,
  // and asking for the rainfall colours there would paint cloud as downpour.
  const scheme = mode === 'radar' ? SCHEME : 0;
  const url = `${frame.host}${frame.path}/${TILE_SIZE}/{z}/{x}/{y}/${scheme}/${OPTIONS}.png`;
  const layer = L.tileLayer(url, {
    opacity: 0,
    pane: 'radar',
    maxNativeZoom: mode === 'radar' ? 12 : 8,
    maxZoom: 19,
    attribution: '<a href="https://www.rainviewer.com/">RainViewer</a>',
  });
  // Knowing when a frame is ready is what lets the panel say so. Recorded
  // against the frame rather than counted, because a layer built for an
  // earlier visit is already loaded and will never fire the event again --
  // counting would leave "loading" on screen for good after switching modes.
  layer.once('load', () => { ready.add(key); paintDock(); });
  layer.addTo(map);
  layers.set(key, layer);
  return layer;
}

function showFrame(which) {
  if (!frames.length) return;
  at = (which + frames.length) % frames.length;
  const frame = frames[at];
  const key = keyOf(frame);
  layerFor(frame).setOpacity(opacity);
  for (const [other, layer] of layers) {
    if (other !== key) layer.setOpacity(0);
  }
  paintDock();
}

/**
 * Build every frame at once, invisibly, before the loop starts.
 *
 * The first version built one frame ahead, which meant the first pass of the
 * loop was really a download and looked like it. Two hours of tiles over one
 * viewport is a small ask, and doing it up front is the difference between a
 * loop that runs and one that lurches.
 */
function preload() {
  for (const frame of frames) layerFor(frame);
}

/** How many of the frames on screen are still fetching their tiles. */
const stillLoading = () => frames.filter((f) => !ready.has(keyOf(f))).length;

function step() {
  clearTimeout(timer);
  if (!enabled || !playing || !frames.length) return;
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
    const next = framesFor(index, mode);
    if (!next.length) throw new Error(`no ${MODES[mode].label.toLowerCase()} frames are published`);
    failed = '';

    // Rebuilding from scratch on every refresh would re-download two hours of
    // tiles. Only the frames that actually went away are dropped.
    const keys = new Set(next.map((f) => `${mode}:${f.time}`));
    for (const [key, layer] of layers) {
      if (key.startsWith(`${mode}:`) && !keys.has(key)) {
        layer.remove();
        layers.delete(key);
        ready.delete(key);
      }
    }
    frames = next;
    preload();
    showFrame(keepPosition ? Math.min(wasAt, frames.length - 1) : newestObserved());
  } catch (err) {
    failed = err.message;
    frames = [];
    clearLayers();
    paintDock();
    throw err;
  }
}

/** Switch between ground radar and the satellite view. */
async function setMode(next) {
  if (next === mode) return;
  mode = next;
  remember();
  // The other mode's layers are kept: switching back should be instant, and
  // they are already paid for.
  for (const [key, layer] of layers) {
    if (!key.startsWith(`${mode}:`)) layer.setOpacity(0);
  }
  if (!enabled) { paintDock(); return; }
  try {
    await refresh({ fetchIndex: !index });
    step();
  } catch {
    /* refresh has already said what went wrong. */
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
      // Which of the two views. Radar is sharp where it reaches; the satellite
      // reaches everywhere.
      el('div', { class: 'radar-modes' },
        ...Object.entries(MODES).map(([key, spec]) =>
          el('button', {
            class: `radar-mode${key === mode ? ' is-on' : ''}`,
            dataset: { mode: key },
            onclick: () => setMode(key),
          }, spec.label))),

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
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify({ mode, speed, opacity })); } catch {}
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

  for (const button of document.querySelectorAll('.radar-mode')) {
    button.classList.toggle('is-on', button.dataset.mode === mode);
  }
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

  key.replaceChildren(...MODES[mode].legend.map(([colour, label]) =>
    el('span', { class: 'radar-swatch', title: label },
      el('i', { style: `background:${colour}` }), label)));

  const still = stillLoading();
  note.textContent = frame.forecast
    ? 'Extrapolated from the recent motion — not an observation.'
    : still > 0 ? `${MODES[mode].note} Loading ${still} more frames…`
      : MODES[mode].note;
}
