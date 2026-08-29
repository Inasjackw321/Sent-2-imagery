// Live cameras, pinned to the ground they are looking at.
//
// Everything else here is the view from orbit, taken days or weeks apart. A
// webcam is the opposite: one fixed angle, from the ground, right now. Putting
// the two on the same map is the point -- a Sentinel-2 pass tells you the
// harbour was full on Tuesday, and the camera tells you what it looks like
// while you are reading that.
//
// Three kinds, because "webcam" covers three unrelated things and pretending
// otherwise breaks two of them:
//
//   embed  a page built to be framed, which plays itself
//   still  one JPEG the host overwrites every few minutes, fetched on a timer
//   hls    a playlist of video segments, which needs an actual video player
//
// The positions are uneven and the interface says so. Cameras given
// coordinates are pinned where the camera is; the rest are pinned to the place
// in their title, because an embedded player carries no coordinates and a
// town-level pin honestly labelled beats a precise-looking guess.

import { $, el } from './ui.js';

export const CAMS = [
  {
    id: 'tarifa',
    name: 'Kite surfing beach',
    place: 'Tarifa, Spain',
    lat: 36.0290, lon: -5.6180,
    // Playa de los Lances, the beach the camera is named for.
    precision: 'beach',
    src: 'https://ipcamlive.com/player/player.php?alias=kite&mute=1',
    host: 'ipcamlive.com',
  },
  {
    id: 'dubai-marina',
    name: 'Dubai Marina',
    place: 'Dubai, United Arab Emirates',
    lat: 25.0805, lon: 55.1403,
    precision: 'district',
    src: 'https://ipcamlive.com/player/player.php?alias=60902b0a40947&mute=1',
    host: 'ipcamlive.com',
  },
  {
    id: 'slovyansk',
    name: 'Slovyansk',
    place: 'Slovyansk, Ukraine',
    lat: 48.8531, lon: 37.6069,
    precision: 'town',
    src: 'https://rtsp.me/embed/7yyGSRHn/',
    host: 'rtsp.me',
  },
  {
    id: 'belgorod',
    name: 'Belgorod',
    place: 'Belgorod, Russia',
    lat: 50.5952, lon: 36.5872,
    precision: 'town',
    src: 'https://rtsp.me/embed/2fyb9tn3/',
    host: 'rtsp.me',
  },
  {
    id: 'romankiv',
    name: 'Romankiv',
    place: 'Romankiv, Ukraine',
    // A village south of Kyiv, on the Dnipro. Placed from the name alone and
    // the least certain of the six -- worth correcting if you know better.
    lat: 50.1900, lon: 30.6800,
    precision: 'village, approximate',
    src: 'https://rtsp.me/embed/7dh4ra77/',
    host: 'rtsp.me',
  },
  {
    id: 'moscow-progress-city',
    name: 'Progress City',
    place: 'Moscow, Russia',
    lat: 55.7558, lon: 37.6173,
    precision: 'city',
    src: 'https://rtsp.me/embed/2EeYnYti/',
    host: 'rtsp.me',
  },
  {
    id: 'moscow-earthcam',
    name: 'Moscow HD',
    place: 'Moscow, Russia',
    lat: 55.7520, lon: 37.6175,
    precision: 'city',
    // EarthCam serves a page, not a player, and refuses to be framed by
    // anyone else. Embedding it would give a permanently blank panel, so this
    // one opens on its own site instead of pretending to play here.
    src: 'https://www.earthcam.com/world/russia/moscow/?cam=moscow_hd',
    host: 'earthcam.com',
    offsite: true,
  },
  {
    id: 'novi-petrivtsi',
    name: 'Novi Petrivtsi 559',
    place: 'Novi Petrivtsi, Ukraine',
    lat: 50.5628, lon: 30.4472,
    precision: 'village',
    src: 'https://rtsp.me/embed/5R3EQY32/',
    host: 'rtsp.me',
  },

  // ── Cameras given by position ────────────────────────────────
  //
  // These came with coordinates, so unlike the ones above their pins are
  // where the camera actually is rather than where its title says.
  //
  // They are also not embeddable pages, which is why they are not iframes.
  // Six are single JPEGs -- a webcam snapshot the host overwrites every few
  // minutes -- and framing one gives a picture that is right once and then
  // silently wrong for the rest of the day, so they are fetched again on a
  // timer. Two are HLS playlists, which are a list of video segments rather
  // than anything a frame can display, and need a player.
  {
    id: 'gibraltar-bay', name: 'Bay of Gibraltar', place: 'Gibraltar',
    lat: 36.1390, lon: -5.3413, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1645095187/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'europa-point', name: 'Europa Point', place: 'Gibraltar',
    lat: 36.1153, lon: -5.3495, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1644919197/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'tarifa-strait', name: 'Strait of Gibraltar', place: 'Tarifa, Spain',
    lat: 36.0519, lon: -5.6481, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1499427214/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'hampton-roads', name: 'Hampton Roads', place: 'Norfolk, Virginia, USA',
    lat: 36.9626, lon: -76.2700, precision: 'given position',
    kind: 'hls', src: 'https://media-sfs4.vdotcameras.com/rtplive/HamptonRoads782/playlist_sfm4s.m3u8',
    host: 'vdotcameras.com',
  },
  {
    id: 'temryuk', name: 'Taman peninsula', place: 'Temryuk, Russia',
    lat: 45.3281, lon: 37.2623, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793909890/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'oresund', name: 'Øresund', place: 'Denmark',
    lat: 55.5753, lon: 12.8264, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1638710999/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'panama-canal', name: 'Panama Canal', place: 'Panama City, Panama',
    lat: 8.9966, lon: -79.5917, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1511843094/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'bosphorus', name: 'Bosphorus', place: 'Istanbul, Türkiye',
    lat: 41.0800, lon: 29.0517, precision: 'given position',
    kind: 'hls', src: 'https://601a43eea2819.streamlock.net/hls/268.stream/playlist.m3u8',
    host: 'streamlock.net',
  },
];

// How often a snapshot camera is asked for again. Windy's hosts overwrite the
// image every few minutes, so this is roughly the rate at which there is
// anything new to see -- often enough to feel live, rarely enough to be
// a reasonable way to treat somebody else's bandwidth.
const STILL_SECONDS = 60;

// hls.js, fetched only when an HLS camera is first opened. Browsers other than
// Safari cannot play a playlist on their own, and loading a video library on
// every page view to serve two of sixteen cameras would be rude.
const HLS_LIBRARY = 'https://cdn.jsdelivr.net/npm/hls.js@1.5.13/dist/hls.min.js';

let map = null;
let layer = null;
let enabled = false;
let watching = null;     // the cam whose player is open
// Whatever the open camera left running -- a snapshot timer, an HLS player --
// so that closing the panel can actually stop it.
let playing = {};

export function initCams(leafletMap) {
  map = leafletMap;
  // Above the ships: a camera is a place you are looking at, and should not
  // end up underneath a passing tanker.
  map.createPane('cams').style.zIndex = 480;
  buildDock();
  buildViewer();
}

// ── On the map ─────────────────────────────────────────────────

function marker(cam) {
  const pin = L.marker([cam.lat, cam.lon], {
    pane: 'cams',
    riseOnHover: true,
    title: `${cam.name} — ${cam.place}`,
    icon: L.divIcon({
      className: 'cam-pin',
      html: `<svg viewBox="0 0 24 24" aria-hidden="true">
               <circle cx="12" cy="12" r="10" fill="#0d1015" stroke="#ff5f8d" stroke-width="2"/>
               <path d="M7 9.5h6.5v5H7z M14.5 11l3-1.8v5.6l-3-1.8z" fill="#ff5f8d"/>
             </svg>`,
      iconSize: [26, 26], iconAnchor: [13, 13],
    }),
  });
  pin.on('click', () => watch(cam.id));
  pin.bindTooltip(cam.name, { direction: 'top', offset: [0, -12], className: 'cam-label' });
  return pin;
}

function drawPins() {
  layer?.remove();
  layer = L.layerGroup(CAMS.map(marker), { pane: 'cams' });
  layer.addTo(map);
}

// ── The player ─────────────────────────────────────────────────

function buildViewer() {
  if ($('#camViewer')) return;
  document.body.append(
    el('div', { class: 'cam-viewer', id: 'camViewer', hidden: true },
      el('div', { class: 'cam-bar' },
        el('span', { class: 'cam-live', id: 'camLive' }, 'LIVE'),
        el('b', { id: 'camTitle' }, ''),
        el('span', { class: 'cam-where', id: 'camWhere' }, ''),
        el('a', {
          class: 'cam-out', id: 'camOut', target: '_blank', rel: 'noopener noreferrer',
          title: 'Open the stream in a new tab',
        }, '↗'),
        el('button', { class: 'cam-close', title: 'Close', onclick: () => watch(null) }, '×')),
      el('div', { class: 'cam-frame', id: 'camFrame' }),
      el('div', { class: 'cam-foot', id: 'camFoot' })),
  );
}

/**
 * Show one camera, or none.
 *
 * Whatever is playing is created and destroyed rather than hidden: leaving a
 * player in the document keeps a video stream running, or a snapshot timer
 * ticking, for a window nobody is looking at -- which costs bandwidth all
 * afternoon and is rude to whoever is hosting the camera for nothing.
 */
function watch(id) {
  const cam = CAMS.find((c) => c.id === id) ?? null;
  watching = cam;
  const viewer = $('#camViewer');
  const frame = $('#camFrame');
  if (!viewer || !frame) return;

  stopPlaying();
  frame.replaceChildren();
  if (!cam) {
    viewer.hidden = true;
    paintDock();
    return;
  }

  if (cam.kind === 'still') {
    frame.append(...snapshot(cam));
    describe(cam);
    reveal(cam);
    return;
  }

  if (cam.kind === 'hls') {
    frame.append(...stream(cam));
    describe(cam);
    reveal(cam);
    return;
  }

  if (cam.offsite) {
    // No iframe at all: this host refuses to be framed, so an embed here would
    // be a panel that is blank for ever with nothing to explain itself.
    frame.append(el('div', { class: 'cam-wait' },
      el('span', {}, `${cam.host} does not allow embedding`),
      el('small', {}, 'It plays on its own site rather than in here.'),
      el('a', {
        class: 'cam-go', href: cam.src, target: '_blank', rel: 'noopener noreferrer',
      }, `Watch on ${cam.host} ↗`)));
    describe(cam);
    reveal(cam);
    return;
  }

  // Covers the frame until the player answers. It has to sit on top rather
  // than behind: an iframe paints its own background, including the browser's
  // error page, so anything underneath is never seen.
  const waiting = el('div', { class: 'cam-wait' },
    el('span', {}, `Connecting to ${cam.host}…`),
    el('small', {}, 'If nothing appears, the stream is offline or blocked. ↗ opens it directly.'));

  const player = el('iframe', {
    src: cam.src,
    title: `${cam.name} live webcam — ${cam.place}`,
    allow: 'autoplay; encrypted-media; fullscreen',
    allowfullscreen: '',
    referrerpolicy: 'no-referrer',
    loading: 'eager',
  });
  // The frame is cross-origin, so there is no telling a stream from an error
  // page. Either way something is now on screen and the notice is in the way.
  player.addEventListener('load', () => waiting.remove(), { once: true });
  frame.append(player, waiting);

  describe(cam);
  reveal(cam);
}

/**
 * A snapshot camera: one JPEG, fetched again on a timer.
 *
 * The cache-busting stamp is not optional. The host serves the same URL for
 * every new picture, so without it the browser answers every refresh out of
 * its own cache and the panel shows this morning's weather until the tab is
 * reloaded -- which looks exactly like a working live camera.
 */
function snapshot(cam) {
  const waiting = el('div', { class: 'cam-wait' },
    el('span', {}, `Fetching from ${cam.host}…`),
    el('small', {}, 'If nothing appears, the camera is offline or blocked. ↗ opens it directly.'));

  const image = el('img', { class: 'cam-still', alt: `${cam.name} — ${cam.place}` });
  const stamp = el('span', { class: 'cam-stamp' }, '');

  const pull = () => {
    image.src = `${cam.src}${cam.src.includes('?') ? '&' : '?'}t=${Date.now()}`;
  };
  image.addEventListener('load', () => {
    waiting.remove();
    stamp.textContent = new Date().toLocaleTimeString();
  });
  image.addEventListener('error', () => {
    waiting.replaceChildren(
      el('span', {}, `${cam.host} did not send a picture`),
      el('small', {}, 'The camera may be offline, or the host may be refusing '
        + 'requests from other sites. ↗ opens it directly.'));
  });

  pull();
  playing = { timer: setInterval(pull, STILL_SECONDS * 1000) };
  return [image, stamp, waiting];
}

/**
 * An HLS camera: a playlist of video segments, which needs a real player.
 *
 * Safari plays these natively; nothing else does, so hls.js is fetched the
 * first time one is opened. Loading a video library on every page view to
 * serve two of sixteen cameras would be paying for it sixteen times over.
 */
function stream(cam) {
  const waiting = el('div', { class: 'cam-wait' },
    el('span', {}, `Connecting to ${cam.host}…`),
    el('small', {}, 'Live video takes a moment to start.'));

  const video = el('video', {
    class: 'cam-video', controls: true, playsinline: true,
    // Muted because a panel that starts shouting when you click a pin is not
    // a feature, and because autoplay is blocked outright without it.
    muted: true, autoplay: true,
  });
  video.muted = true;

  const failed = (why) => waiting.replaceChildren(
    el('span', {}, `${cam.name} could not be played`),
    el('small', {}, why));

  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = cam.src;
    video.addEventListener('loadeddata', () => waiting.remove(), { once: true });
    video.addEventListener('error', () => failed('The stream is offline or unreachable.'), { once: true });
    playing = {};
    return [video, waiting];
  }

  playing = {};
  loadHls().then((Hls) => {
    // Opened, then closed again before the library arrived: attaching now
    // would start a stream into a panel nobody is looking at.
    if (watching?.id !== cam.id) return;
    if (!Hls?.isSupported()) {
      failed('This browser cannot play HLS video.');
      return;
    }
    const player = new Hls({ liveDurationInfinity: true });
    player.loadSource(cam.src);
    player.attachMedia(video);
    player.on(Hls.Events.MANIFEST_PARSED, () => {
      waiting.remove();
      video.play().catch(() => { /* the browser declined to autoplay */ });
    });
    player.on(Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) failed('The stream is offline or unreachable.');
    });
    playing.player = player;
  }).catch(() => failed('The video player could not be loaded. ↗ opens the stream directly.'));

  return [video, waiting];
}

let hlsLoading = null;

/** Fetch hls.js once, and hand the same promise to everyone after that. */
function loadHls() {
  if (window.Hls) return Promise.resolve(window.Hls);
  hlsLoading ??= new Promise((resolve, reject) => {
    const script = el('script', { src: HLS_LIBRARY });
    script.addEventListener('load', () => resolve(window.Hls));
    script.addEventListener('error', () => {
      // Let a later attempt try again rather than remembering the failure for
      // the life of the page: this is usually a network that has since come
      // back, not a library that has ceased to exist.
      hlsLoading = null;
      reject(new Error('hls.js did not load'));
    });
    document.head.append(script);
  });
  return hlsLoading;
}

/** Stop whatever the last camera left running. */
function stopPlaying() {
  if (playing.timer) clearInterval(playing.timer);
  // Without this the player keeps pulling video segments off the host for a
  // panel that is no longer on screen.
  playing.player?.destroy();
  playing = {};
}

/** Fill in the bar and the footer for whichever camera is open. */
function describe(cam) {
  $('#camTitle').textContent = cam.name;
  $('#camWhere').textContent = cam.place;
  $('#camOut').href = cam.src;
  // A still is not a live view, and calling one "live" would be the single
  // most misleading thing this panel could say: a snapshot from before an
  // event looks exactly like a snapshot from after it.
  const carrier = cam.kind === 'still'
    ? `A still from ${cam.host}, refreshed every ${STILL_SECONDS} seconds — not continuous video.`
    : `${cam.offsite ? 'Hosted by' : 'Streamed by'} ${cam.host}.`;
  const placed = cam.precision === 'given position'
    ? 'Pinned to the position it was given, so the marker is the camera.'
    : `Pinned to the ${cam.precision} — the player carries no coordinates, so `
      + 'the marker is the place, not the lens.';
  $('#camFoot').textContent = `${carrier} ${placed}`;
  $('#camLive').textContent = cam.kind === 'still' ? 'STILL' : 'LIVE';
  $('#camLive').classList.toggle('is-still', cam.kind === 'still');
}

/** Show the panel, and bring the camera into view if it is off screen. */
function reveal(cam) {
  $('#camViewer').hidden = false;
  if (!map.getBounds().contains([cam.lat, cam.lon])) {
    map.flyTo([cam.lat, cam.lon], Math.max(map.getZoom(), 11), { duration: 0.8 });
  }
  paintDock();
}

// ── The panel ──────────────────────────────────────────────────

function toggle() {
  enabled = !enabled;
  if (enabled) {
    drawPins();
  } else {
    layer?.remove();
    layer = null;
    watch(null);
  }
  paintDock();
}

function buildDock() {
  const dock = $('#camDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'cam-toggle', id: 'camToggle', onclick: toggle },
      el('span', { class: 'cam-mark' }, '◉'), 'Live cams'),
    el('div', { class: 'cam-body', id: 'camBody', hidden: true },
      el('div', { class: 'cam-list', id: 'camList' },
        ...CAMS.map((cam) => el('button', {
          class: 'cam-item', dataset: { cam: cam.id },
          onclick: () => watch(watching?.id === cam.id ? null : cam.id),
        }, el('b', {}, cam.name, cam.offsite ? el('i', { class: 'cam-away' }, '↗') : null),
           el('span', {}, cam.place)))),
      el('div', { class: 'cam-note' },
        `${CAMS.length} public cameras, played from their own hosts. `
        + 'The ones given a position are pinned to the camera; the rest are '
        + 'town-level, because an embedded player carries no coordinates. '
        + '↗ marks one that only plays on its own site.')),
  );
  paintDock();
}

function paintDock() {
  const button = $('#camToggle');
  const body = $('#camBody');
  if (!button || !body) return;
  button.classList.toggle('is-on', enabled);
  body.hidden = !enabled;
  for (const item of document.querySelectorAll('.cam-item')) {
    item.classList.toggle('is-on', item.dataset.cam === watching?.id);
  }
}
