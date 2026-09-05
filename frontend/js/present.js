// Presentation mode: the cameras as a wall, with the reports running under it.
//
// The rest of this app is for working: you pick an area, search it, compare
// dates, read a panel. This is the opposite -- a screen nobody is holding a
// mouse in front of. It goes on a second monitor or a wall and is read from
// across a room, so everything here is bigger, quieter and unattended.
//
// It is built out of what already exists rather than beside it: the same
// camera list the map pins, the same air-threat feed the map draws. What is
// different is the framing. A grid of cameras answers "what does it look like
// there now", the ticker answers "what is being reported", and a clock says
// when -- which matters more than usual, because someone glancing at a wall
// display has no idea how old what they are seeing is.
//
// Three things are deliberate:
//
//   Only stills and streams go on the wall. Embedded players are pages that
//   run their own scripts, and a dozen of them in a grid is a dozen video
//   players fighting over one connection. They stay on the map.
//
//   Everything says its own age. A camera that stopped answering an hour ago
//   looks exactly like one that is working, which on an unattended screen is
//   the worst failure available, so the tiles carry the time of the frame and
//   grey out when it stops being now.
//
//   Nothing here can change anything. It is a display; there is no control on
//   it that touches the map underneath.

import { api } from './api.js';
import { CAMS } from './cams.js';
import { $, el } from './ui.js';

// How often each still is fetched again. Slower than the map's panel: this
// runs unattended for hours and there is no reason to pull twelve cameras
// every minute all afternoon.
const STILL_SECONDS = 90;

// When a tile stops claiming to be live. Nothing on a wall display should look
// current when it is not.
const STALE_SECONDS = STILL_SECONDS * 3;

// How often to ask for new reports, and how often to move the ticker on.
const FEED_MS = 60000;
const TICKER_MS = 6000;

// How long before the grid moves to the next set of cameras. Long enough to
// actually watch something, short enough that a wall gets round them all.
const ROTATE_MS = 45000;

const LAYOUTS = { '2×2': 4, '3×2': 6, '3×3': 9, '4×3': 12 };

const HLS_LIBRARY = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js';

// Only what can be dropped into a grid without bringing a whole page with it.
const SHOWABLE = CAMS.filter((c) => c.kind === 'still' || c.kind === 'hls');

let root = null;
let open = false;
let layout = '3×3';
let page = 0;
let rotating = true;
let feed = null;
let alertAt = 0;
const running = new Map();      // cam id -> { timer } | { player } | { video }
let feedTimer = null;
let tickTimer = null;
let rotateTimer = null;
let clockTimer = null;

export function initPresent() {
  document.addEventListener('keydown', (e) => {
    if (!open) return;
    if (e.key === 'Escape') { close(); return; }
    if (e.key === ' ') { e.preventDefault(); rotating = !rotating; paintBar(); return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); turn(1); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); turn(-1); }
  });
  $('#presentBtn')?.addEventListener('click', () => (open ? close() : show()));
}

const pages = () => Math.max(1, Math.ceil(SHOWABLE.length / LAYOUTS[layout]));

/** The cameras on the wall right now. */
function onScreen() {
  const size = LAYOUTS[layout];
  page = ((page % pages()) + pages()) % pages();
  return SHOWABLE.slice(page * size, page * size + size);
}

// ── Opening and closing ────────────────────────────────────────

function show() {
  if (open) return;
  open = true;
  root = el('div', { class: 'wall', id: 'wall', role: 'region', 'aria-label': 'Presentation mode' });
  document.body.append(root);
  document.body.classList.add('is-walled');
  build();

  loadFeed();
  feedTimer = setInterval(loadFeed, FEED_MS);
  tickTimer = setInterval(nextAlert, TICKER_MS);
  rotateTimer = setInterval(() => { if (rotating) turn(1); }, ROTATE_MS);
  clockTimer = setInterval(paintClock, 1000);

  // Asked for, never insisted on: a browser may refuse, and a wall that works
  // in a window is better than one that does nothing because it could not
  // have the whole screen.
  document.documentElement.requestFullscreen?.().catch(() => {});
}

function close() {
  if (!open) return;
  open = false;
  stopAll();
  clearInterval(feedTimer);
  clearInterval(tickTimer);
  clearInterval(rotateTimer);
  clearInterval(clockTimer);
  feedTimer = tickTimer = rotateTimer = clockTimer = null;
  root?.remove();
  root = null;
  document.body.classList.remove('is-walled');
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
}

function turn(by) {
  page += by;
  build();
}

// ── The wall ───────────────────────────────────────────────────

function build() {
  if (!root) return;
  stopAll();
  const cams = onScreen();
  root.replaceChildren(
    el('div', { class: 'wall-head' },
      el('div', { class: 'wall-title' },
        el('span', { class: 'wall-dot' }),
        el('b', {}, 'EarthViewer'),
        el('span', { class: 'wall-sub', id: 'wallSub' },
          `${cams.length} cameras · page ${page + 1} of ${pages()}`)),
      el('div', { class: 'wall-clock', id: 'wallClock' }),
      el('div', { class: 'wall-tools' },
        ...Object.keys(LAYOUTS).map((name) => el('button', {
          class: `wall-btn${name === layout ? ' is-on' : ''}`,
          onclick: () => { layout = name; page = 0; build(); },
        }, name)),
        el('button', { class: 'wall-btn', onclick: () => turn(-1), title: 'Previous (←)' }, '‹'),
        el('button', {
          class: `wall-btn${rotating ? ' is-on' : ''}`, id: 'wallRotate',
          onclick: () => { rotating = !rotating; paintBar(); },
          title: 'Cycle through the cameras (space)',
        }, '⟳'),
        el('button', { class: 'wall-btn', onclick: () => turn(1), title: 'Next (→)' }, '›'),
        el('button', { class: 'wall-btn wall-exit', onclick: close, title: 'Leave (Esc)' }, '✕'))),

    el('div', { class: `wall-grid is-${layout.replace('×', 'x')}`, id: 'wallGrid' },
      ...cams.map(tile)),

    el('div', { class: 'wall-foot' },
      el('div', { class: 'wall-status', id: 'wallStatus' }, 'Reports loading…'),
      el('div', { class: 'wall-ticker', id: 'wallTicker' },
        el('div', { class: 'wall-tick', id: 'wallTick' }, 'Waiting for reports…'))));

  paintClock();
  paintFeed();
}

/** One camera on the wall. */
function tile(cam) {
  const stamp = el('span', { class: 'tile-stamp' }, '—');
  const shade = el('div', { class: 'tile-wait' }, `Fetching from ${cam.host}…`);
  const body = cam.kind === 'hls' ? stream(cam, shade, stamp) : still(cam, shade, stamp);

  return el('figure', { class: 'tile', id: `tile-${cam.id}` },
    body, shade,
    el('figcaption', { class: 'tile-cap' },
      el('b', {}, cam.name),
      el('span', {}, cam.place),
      stamp));
}

function still(cam, shade, stamp) {
  const image = el('img', {
    class: 'tile-img', alt: `${cam.name} — ${cam.place}`, referrerpolicy: 'no-referrer',
  });
  let last = 0;

  const pull = () => {
    // Cache-busted, as on the map: without it the browser answers every
    // refresh out of its own cache and a wall display shows this morning,
    // indefinitely, looking exactly like a working camera.
    image.src = `${cam.src}${cam.src.includes('?') ? '&' : '?'}t=${Date.now()}`;
  };
  image.addEventListener('load', () => {
    last = Date.now();
    shade.remove();
    image.classList.remove('is-stale');
    stamp.textContent = new Date(last).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  });
  image.addEventListener('error', () => {
    shade.textContent = `${cam.host} is not answering`;
    shade.classList.add('is-bad');
    if (!image.isConnected) return;
    image.parentNode?.prepend(shade);
  });

  pull();
  running.set(cam.id, {
    timer: setInterval(() => {
      // A camera whose last good frame has aged out says so rather than
      // sitting there looking current. This is the whole reason the wall
      // tracks its own timestamps instead of trusting that a picture on
      // screen means a picture from now.
      if (last && Date.now() - last > STALE_SECONDS * 1000) {
        image.classList.add('is-stale');
        stamp.textContent = `${Math.round((Date.now() - last) / 60000)} min old`;
      }
      pull();
    }, STILL_SECONDS * 1000),
  });
  return image;
}

function stream(cam, shade, stamp) {
  const video = el('video', {
    class: 'tile-img', playsinline: true, muted: true, autoplay: true, loop: true,
  });
  video.muted = true;
  const ok = () => {
    shade.remove();
    stamp.textContent = 'live';
  };
  const bad = () => {
    shade.textContent = `${cam.name} is not playing`;
    shade.classList.add('is-bad');
  };

  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = cam.src;
    video.addEventListener('loadeddata', ok, { once: true });
    video.addEventListener('error', bad, { once: true });
    running.set(cam.id, { video });
    return video;
  }

  running.set(cam.id, {});
  loadHls().then((Hls) => {
    // Turned to another page, or left the wall entirely, before the library
    // arrived: attaching now would start a stream nobody is looking at.
    if (!running.has(cam.id)) return;
    if (!Hls?.isSupported()) { bad(); return; }
    const player = new Hls({ liveDurationInfinity: true });
    player.loadSource(cam.src);
    player.attachMedia(video);
    player.on(Hls.Events.MANIFEST_PARSED, () => {
      ok();
      video.play().catch(() => { /* the browser declined to autoplay */ });
    });
    player.on(Hls.Events.ERROR, (_e, data) => { if (data.fatal) bad(); });
    const held = running.get(cam.id);
    if (held) held.player = player;
    else player.destroy();
  }).catch(bad);
  return video;
}

let hlsLoading = null;

function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  hlsLoading ??= new Promise((done, fail) => {
    const tag = document.createElement('script');
    tag.src = HLS_LIBRARY;
    tag.onload = () => done(window.Hls);
    tag.onerror = () => fail(new Error('hls.js could not be loaded'));
    document.head.append(tag);
  });
  return hlsLoading;
}

/**
 * Stop everything the current page of tiles started.
 *
 * Called before every rebuild, not only on the way out. Turning the page
 * without this leaves a timer pulling a JPEG and an HLS player pulling video
 * for a tile that is no longer on screen -- which after an hour of cycling is
 * a dozen streams running at once for a wall showing nine.
 */
function stopAll() {
  for (const held of running.values()) {
    clearInterval(held.timer);
    held.player?.destroy?.();
    if (held.video) { held.video.pause?.(); held.video.removeAttribute('src'); held.video.load?.(); }
  }
  running.clear();
}

// ── The clock ──────────────────────────────────────────────────

function paintClock() {
  const clock = $('#wallClock');
  if (!clock) return;
  const now = new Date();
  clock.replaceChildren(
    el('b', {}, now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })),
    // UTC alongside local, because a wall display is read by people comparing
    // it to reports that are timestamped in UTC.
    el('span', {}, `${now.toISOString().slice(11, 16)} UTC`));
}

function paintBar() {
  $('#wallRotate')?.classList.toggle('is-on', rotating);
  const sub = $('#wallSub');
  if (sub) {
    sub.textContent = `${onScreen().length} cameras · page ${page + 1} of ${pages()}`
      + (rotating ? '' : ' · paused');
  }
}

// ── The reports ────────────────────────────────────────────────

async function loadFeed() {
  try {
    feed = await api.osint();
    alertAt = 0;
  } catch (err) {
    feed = { alerts: [], count: 0, state: err.message };
  }
  paintFeed();
}

function paintFeed() {
  const status = $('#wallStatus');
  if (!status) return;
  const alerts = feed?.alerts ?? [];
  const air = feed?.count ?? 0;
  const worst = alerts.length ? Math.max(...alerts.map((a) => a.rank ?? 0)) : 0;

  status.replaceChildren(
    el('span', { class: `wall-pip rank-${worst}` }),
    el('b', {}, air ? `${air} tracked` : 'nothing tracked'),
    el('span', {}, alerts.length
      ? `${alerts.length} reports · last ${feed.alert_minutes ?? 90} min`
      : (feed?.state ?? 'no reports')));
  nextAlert();
}

/**
 * Move the ticker on one report.
 *
 * A crawl was the obvious thing and is the wrong one: a line sliding past at a
 * readable speed fits about one report a minute, and on a night when six
 * things are reported at once the sixth appears five minutes late. One at a
 * time, held long enough to read, gets through all of them.
 */
function nextAlert() {
  const tick = $('#wallTick');
  if (!tick) return;
  const alerts = feed?.alerts ?? [];
  if (!alerts.length) {
    tick.replaceChildren(el('span', { class: 'tick-quiet' },
      feed?.state?.startsWith('demo')
        ? 'Demo mode — these reports are invented.'
        : 'Nothing reported.'));
    return;
  }

  const item = alerts[alertAt % alerts.length];
  alertAt += 1;
  const kind = feed?.kinds?.[item.kind] ?? {};
  const mins = Math.max(0, Math.round((Date.now() / 1000 - item.seen) / 60));

  tick.replaceChildren(
    el('span', { class: 'tick-when' }, mins < 1 ? 'now' : `${mins} min`),
    el('span', {
      class: `tick-kind rank-${item.rank ?? 1}`,
      style: kind.colour ? `color:${kind.colour}` : '',
    }, kind.label ?? item.kind),
    el('span', { class: 'tick-what' }, item.summary),
    // A report that could not be placed still reaches the wall, and says so.
    // The alternative -- dropping it -- is how the previous version made a
    // patchy night look identical to a broken feed.
    item.placed
      ? el('span', { class: 'tick-where' }, item.place ?? '')
      : el('span', { class: 'tick-where is-unplaced' }, 'not mapped'),
    el('span', { class: 'tick-src' }, item.channel ?? ''));
  // Restart the fade so each report arrives rather than swapping in place.
  tick.classList.remove('is-in');
  void tick.offsetWidth;
  tick.classList.add('is-in');
}
