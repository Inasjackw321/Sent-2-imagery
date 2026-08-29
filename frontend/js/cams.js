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
import { openWindow, closeWindow, closeAll, isOpen, openIds } from './windows.js';

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

// Windows are keyed by camera id with this in front, so the seismographs'
// windows and these cannot collide in the same register.
const WIN = 'cam:';

let map = null;
let layer = null;
let enabled = false;
// What each open camera left running -- a snapshot timer, an HLS player --
// keyed by camera id, so closing one window stops that one and no other.
const playing = new Map();

export function initCams(leafletMap) {
  map = leafletMap;
  // Above the ships: a camera is a place you are looking at, and should not
  // end up underneath a passing tanker.
  map.createPane('cams').style.zIndex = 480;
  buildDock();
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
  pin.on('add', () => {
    // Tag the element so the dock can light the pins whose windows are open.
    pin.getElement()?.setAttribute('data-cam', cam.id);
    paintDock();
  });
  pin.bindTooltip(cam.name, { direction: 'top', offset: [0, -12], className: 'cam-label' });
  return pin;
}

function drawPins() {
  layer?.remove();
  layer = L.layerGroup(CAMS.map(marker), { pane: 'cams' });
  layer.addTo(map);
}

// ── The player ─────────────────────────────────────────────────

/**
 * Open a camera, or close it if it is already open.
 *
 * Several can be open at once: the reason to put cameras on a map of
 * satellite imagery is to compare them, and comparing means seeing more than
 * one at a time. Each gets its own window.
 *
 * Whatever is playing is created and destroyed with the window rather than
 * hidden. Leaving a player in the document keeps a video stream running, or a
 * snapshot timer ticking, for a window nobody is looking at -- which costs
 * bandwidth all afternoon and is rude to whoever is hosting the camera.
 */
function watch(id) {
  const cam = CAMS.find((c) => c.id === id);
  if (!cam) return;
  if (isOpen(WIN + cam.id)) {
    closeWindow(WIN + cam.id);
    return;
  }

  const frame = el('div', { class: 'cam-frame' });
  frame.append(...body(cam));

  openWindow({
    id: WIN + cam.id,
    title: cam.name,
    where: cam.place,
    badge: {
      text: cam.kind === 'still' ? 'STILL' : 'LIVE',
      className: cam.kind === 'still' ? 'is-still' : 'is-live',
    },
    link: cam.src,
    body: frame,
    foot: footnote(cam),
    onClose: () => { stopPlaying(cam.id); paintDock(); },
  });

  if (!map.getBounds().contains([cam.lat, cam.lon])) {
    map.flyTo([cam.lat, cam.lon], Math.max(map.getZoom(), 11), { duration: 0.8 });
  }
  paintDock();
}

/** Whatever plays this particular kind of camera. */
function body(cam) {
  if (cam.kind === 'still') return snapshot(cam);
  if (cam.kind === 'hls') return stream(cam);
  if (cam.offsite) {
    // No iframe at all: this host refuses to be framed, so an embed here would
    // be a panel that is blank for ever with nothing to explain itself.
    return [el('div', { class: 'cam-wait' },
      el('span', {}, `${cam.host} does not allow embedding`),
      el('small', {}, 'It plays on its own site rather than in here.'),
      el('a', {
        class: 'cam-go', href: cam.src, target: '_blank', rel: 'noopener noreferrer',
      }, `Watch on ${cam.host} ↗`))];
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
  return [player, waiting];
}

/**
 * What the window says about itself underneath the picture.
 *
 * A still is not a live view, and calling one "live" would be the single most
 * misleading thing this could say: a snapshot from before an event looks
 * exactly like a snapshot from after it.
 */
function footnote(cam) {
  const carrier = cam.kind === 'still'
    ? `A still from ${cam.host}, refreshed every ${STILL_SECONDS} seconds — not continuous video.`
    : `${cam.offsite ? 'Hosted by' : 'Streamed by'} ${cam.host}.`;
  const placed = cam.precision === 'given position'
    ? 'Pinned to the position it was given, so the marker is the camera.'
    : `Pinned to the ${cam.precision} — the player carries no coordinates, so `
      + 'the marker is the place, not the lens.';
  return `${carrier} ${placed}`;
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
  playing.set(cam.id, { timer: setInterval(pull, STILL_SECONDS * 1000) });
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
    playing.set(cam.id, {});
    return [video, waiting];
  }

  playing.set(cam.id, {});
  loadHls().then((Hls) => {
    // Opened, then closed again before the library arrived: attaching now
    // would start a stream into a window nobody is looking at.
    if (!isOpen(WIN + cam.id)) return;
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
    const held = playing.get(cam.id);
    if (held) held.player = player;
    else player.destroy();   // closed while the manifest was being parsed
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

/** Stop whatever one camera left running. */
function stopPlaying(id) {
  const held = playing.get(id);
  if (!held) return;
  playing.delete(id);
  if (held.timer) clearInterval(held.timer);
  // Without this the player keeps pulling video segments off the host for a
  // window that is no longer on screen.
  held.player?.destroy();
}

// ── The panel ──────────────────────────────────────────────────

function toggle() {
  enabled = !enabled;
  if (enabled) {
    drawPins();
  } else {
    layer?.remove();
    layer = null;
    // Turning the layer off takes its windows with it. Leaving them behind
    // would mean streams playing for pins that are no longer on the map.
    closeAll((id) => id.startsWith(WIN));
  }
  paintDock();
}

/**
 * The dock: a switch and a count, and nothing else.
 *
 * There used to be a list of every camera here. Twelve of them made a column
 * taller than the window, which pushed the other panels off the bottom of the
 * screen and buried the thing it was listing. The map already shows where each
 * camera is, which is the useful half of a list of places -- so the pins are
 * the list, and this is only the switch that puts them there.
 */
function buildDock() {
  const dock = $('#camDock');
  if (!dock) return;
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'cam-toggle', id: 'camToggle', onclick: toggle },
      el('span', { class: 'cam-mark' }, '◉'), 'Live cams'),
    el('div', { class: 'cam-body', id: 'camBody', hidden: true },
      el('div', { class: 'cam-count', id: 'camCount' }, ''),
      // With the list gone the map is the index, which leaves nothing to find
      // a camera that is off screen. This is the list's one useful job kept.
      el('button', { class: 'cam-fit', id: 'camFit', onclick: fitAll },
        'Zoom out to all of them'),
      el('div', { class: 'cam-note' },
        `${CAMS.length} public cameras, played from their own hosts. Click a `
        + 'pin to open one, and again to close it — several can be open at once. '
        + 'The ones given a position are pinned to the camera; the rest are '
        + 'town-level, because an embedded player carries no coordinates.')),
  );
  paintDock();
}

/** Pull the map back until every camera is on it. */
function fitAll() {
  if (!CAMS.length) return;
  map.fitBounds(L.latLngBounds(CAMS.map((c) => [c.lat, c.lon])), { padding: [60, 60] });
}

function paintDock() {
  const button = $('#camToggle');
  const body = $('#camBody');
  if (!button || !body) return;
  button.classList.toggle('is-on', enabled);
  body.hidden = !enabled;

  const watching = openIds().filter((id) => id.startsWith(WIN)).length;
  const count = $('#camCount');
  if (count) {
    count.textContent = watching
      ? `${watching} open of ${CAMS.length}`
      : `${CAMS.length} on the map`;
  }
  // A pin whose window is open is lit, so the map says which is which without
  // a list to cross-reference against.
  for (const pin of document.querySelectorAll('.cam-pin')) {
    pin.classList.toggle('is-on', isOpen(WIN + pin.dataset.cam));
  }
}
