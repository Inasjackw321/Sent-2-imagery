// Live cameras, pinned to the ground they are looking at.
//
// Everything else here is the view from orbit, taken days or weeks apart. A
// webcam is the opposite: one fixed angle, from the ground, right now. Putting
// the two on the same map is the point -- a Sentinel-2 pass tells you the
// harbour was full on Tuesday, and the camera tells you what it looks like
// while you are reading that.
//
// The positions are the honest weak part. An embedded player carries no
// coordinates, so each camera is pinned to the place in its title rather than
// to wherever the lens actually sits. That is a town-level pin, and the popup
// says so instead of implying a surveyed position.

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
    id: 'novi-petrivtsi',
    name: 'Novi Petrivtsi 559',
    place: 'Novi Petrivtsi, Ukraine',
    lat: 50.5628, lon: 30.4472,
    precision: 'village',
    src: 'https://rtsp.me/embed/5R3EQY32/',
    host: 'rtsp.me',
  },
];

let map = null;
let layer = null;
let enabled = false;
let watching = null;     // the cam whose player is open

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
        el('span', { class: 'cam-live' }, 'LIVE'),
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
 * The iframe is created and destroyed rather than hidden: leaving a player in
 * the document keeps a video stream running for a window nobody is looking
 * at, which costs bandwidth all afternoon and is rude to whoever is hosting
 * the camera for nothing.
 */
function watch(id) {
  const cam = CAMS.find((c) => c.id === id) ?? null;
  watching = cam;
  const viewer = $('#camViewer');
  const frame = $('#camFrame');
  if (!viewer || !frame) return;

  frame.replaceChildren();
  if (!cam) {
    viewer.hidden = true;
    paintDock();
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

  $('#camTitle').textContent = cam.name;
  $('#camWhere').textContent = cam.place;
  $('#camOut').href = cam.src;
  $('#camFoot').textContent =
    `Streamed by ${cam.host}. Pinned to the ${cam.precision} — the player carries `
    + 'no coordinates, so the marker is the place, not the lens.';
  viewer.hidden = false;

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
        }, el('b', {}, cam.name), el('span', {}, cam.place)))),
      el('div', { class: 'cam-note' },
        `${CAMS.length} public cameras, played from their own hosts. `
        + 'Pins are town-level: an embedded player carries no coordinates.')),
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
