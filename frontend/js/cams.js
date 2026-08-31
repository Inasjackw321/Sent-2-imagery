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

  // The names below are read off the coordinates rather than given with them,
  // so the position is exact and the label is a best reading of where that is.
  {
    id: 'kingisepp', name: 'Kingisepp', place: 'Leningrad Oblast, Russia',
    lat: 59.3750, lon: 28.5965, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1744011569/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'lomonosov', name: 'Lomonosov', place: 'Gulf of Finland, Russia',
    lat: 59.9104, lon: 29.7756, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1623730451/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'ndbc-44014', name: 'NOAA buoy 44014', place: '64 NM east of Virginia Beach, USA',
    lat: 36.6030, lon: -74.8370, precision: 'given position',
    // The buoy's own camera: a six-panel panorama of the horizon, remade
    // hourly. A stable URL, so it needs no date arithmetic.
    kind: 'still', src: 'https://www.ndbc.noaa.gov/buoycam.php?station=44014',
    host: 'ndbc.noaa.gov',
  },
  {
    id: 'kinmen-air', name: 'Kinmen air quality', place: 'Kinmen, Taiwan',
    lat: 24.4321, lon: 118.3123, precision: 'given position',
    // The address carries the minute it was taken, so a fixed one is a
    // photograph of a moment rather than a camera. The template is filled in
    // from the clock and walked backwards until a picture answers.
    kind: 'dated',
    template: 'https://airtw.moenv.gov.tw/AirSitePic/{YYYY}{MM}{DD}/077-{YYYY}{MM}{DD}{HH}{mm}.jpg',
    // Taiwan is UTC+8 all year -- no daylight saving to track.
    tzOffsetMinutes: 8 * 60,
    stepMinutes: 10, backSteps: 12,
    host: 'airtw.moenv.gov.tw',
  },
  // Both of these sit on Baengnyeong island, the South Korean island closest
  // to the North Korean coast -- a few miles south of the Northern Limit Line
  // and about a dozen from the mainland opposite.
  {
    id: 'baengnyeong-east', name: 'Baengnyeong east', place: 'Ongjin, South Korea',
    lat: 37.9553, lon: 124.7354, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793910208/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'baengnyeong-west', name: 'Baengnyeong west', place: 'Ongjin, South Korea',
    lat: 37.9747, lon: 124.6189, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1762140071/original.jpg',
    host: 'windy.com',
  },
  // Western Russia, roughly north-east from the Sea of Azov to the Volga.
  // Each is placed exactly where its position says; the labels are the nearest
  // recognisable place to that point rather than a claim about what the camera
  // is pointed at.
  {
    id: 'azov-coast', name: 'Sea of Azov coast', place: 'Krasnodar Krai, Russia',
    lat: 46.6647, lon: 37.7529, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1689404487/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'kursk', name: 'Kursk', place: 'Kursk Oblast, Russia',
    lat: 51.7299, lon: 36.1327, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1610395347/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'volgograd', name: 'Volgograd', place: 'Volgograd Oblast, Russia',
    lat: 48.7552, lon: 44.5065, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1747888923/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'kaluga', name: 'Kaluga', place: 'Kaluga Oblast, Russia',
    lat: 54.5293, lon: 36.2754, precision: 'given position',
    // A fixed filename that the host overwrites, so it refreshes like any
    // other snapshot rather than being a single frame.
    kind: 'still', src: 'https://pics.starvisor.net/galleries/orig/cap_klg.jpg',
    host: 'starvisor.net',
  },
  {
    id: 'kolomna', name: 'Kolomna', place: 'Moscow Oblast, Russia',
    lat: 55.0958, lon: 38.7644, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793907411/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'moscow-oblast-nw', name: 'Moscow Oblast north-west', place: 'Russia',
    lat: 56.0960, lon: 36.5520, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793903294/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'konakovo', name: 'Konakovo', place: 'Tver Oblast, Russia',
    lat: 56.7205, lon: 36.7719, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1624789022/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'tver-west', name: 'Tver Oblast west', place: 'Russia',
    lat: 56.2278, lon: 32.7650, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793868708/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'nizhny-novgorod', name: 'Nizhny Novgorod', place: 'Nizhny Novgorod Oblast, Russia',
    lat: 56.2375, lon: 43.9596, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1731429998/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'udmurtia', name: 'Udmurtia', place: 'Russia',
    lat: 56.0600, lon: 53.0500, precision: 'given position',
    kind: 'still', src: 'https://pics.starvisor.net/galleries/orig/cap_azv.jpg',
    host: 'starvisor.net',
  },
  {
    id: 'tuapse', name: 'Tuapse', place: 'Krasnodar Krai, Russia',
    lat: 44.0978, lon: 39.0534, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1457258031/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'krasnodar', name: 'Krasnodar', place: 'Krasnodar Krai, Russia',
    lat: 45.0464, lon: 39.0281, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1746027972/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'pyatigorsk', name: 'Pyatigorsk', place: 'Stavropol Krai, Russia',
    lat: 44.0332, lon: 43.0506, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793899677/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'saratov', name: 'Saratov', place: 'Saratov Oblast, Russia',
    lat: 51.5276, lon: 46.0597, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1793901790/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'moscow-southeast', name: 'Moscow south-east', place: 'Russia',
    lat: 55.7074, lon: 37.7669, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1662277766/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'moscow-centre', name: 'Moscow centre', place: 'Russia',
    lat: 55.7262, lon: 37.5636, precision: 'given position',
    // EarthCam's own video host rather than the page that frames it, so this
    // is a playlist for the player here instead of somebody else's embed.
    kind: 'hls',
    src: 'https://videos-3.earthcam.com/fecnetwork/moscowHD1.flv/playlist.m3u8',
    host: 'earthcam.com',
  },
  {
    id: 'east-sussex', name: 'East Sussex', place: 'England',
    lat: 50.9725, lon: 0.9677, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1474904378/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'odesa-coast', name: 'Odesa Oblast coast', place: 'Ukraine',
    lat: 46.4129, lon: 30.1209, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1668778986/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'tallinn-cam024', name: 'Tallinn junction camera', place: 'Tallinn, Estonia',
    lat: 59.4178, lon: 24.7648, precision: 'given position',
    // The city's traffic cameras publish at a fixed "last" address that is
    // overwritten in place, so unlike the Estonian road frames this replaced
    // it stays current and is worth asking for again.
    kind: 'still', src: 'https://ristmikud.tallinn.ee/last/cam024.jpg',
    host: 'ristmikud.tallinn.ee',
  },
  // Two views of the same spot on the Narva river, at the same given position:
  // the town, and the castle facing Ivangorod fortress across the water. Both
  // are pages built to be framed, so they are embeds rather than stills.
  {
    id: 'narva-town', name: 'Narva', place: 'Ida-Viru, Estonia',
    lat: 59.3934, lon: 28.1429, precision: 'given position',
    kind: 'embed',
    src: 'https://balticlivecam.com/cameras/estonia/narva/narva/?embed',
    host: 'balticlivecam.com',
  },
  {
    id: 'narva-castle', name: 'Narva castle and Ivangorod fortress',
    place: 'Ida-Viru, Estonia',
    lat: 59.3934, lon: 28.1429, precision: 'given position',
    kind: 'embed',
    src: 'https://balticlivecam.com/cameras/estonia/narva/narva-castle-ivangorod-fortress/?embed',
    host: 'balticlivecam.com',
  },
  {
    id: 'german-bight', name: 'German Bight', place: 'North Sea',
    lat: 54.1532, lon: 6.8243, precision: 'given position',
    kind: 'still', src: 'https://imgproxy.windy.com/_/full/plain/current/1759328266/original.jpg',
    host: 'windy.com',
  },
  {
    id: 'st-petersburg-north', name: 'St Petersburg north', place: 'Russia',
    lat: 60.0040, lon: 30.4680, precision: 'given position',
    // VK's video_ext player is built to be framed, so this one is an embed
    // rather than a still or a playlist.
    kind: 'embed',
    src: 'https://vkvideo.ru/video_ext.php?oid=1025087646&id=456239017&hash=42c649342965ddbe&hd=3',
    host: 'vkvideo.ru',
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

/**
 * How far apart to draw pins that share a position, in pixels.
 *
 * Two cameras can genuinely be at the same place -- two views from one spot,
 * given the same coordinates. Drawn on top of each other the upper pin hides
 * the lower one completely, and the one underneath can never be opened from
 * the map at all: the same shape of bug as a canvas swallowing a click, one
 * layer up.
 *
 * They are nudged apart on screen only. The position itself is untouched, so
 * the panel and the window still report exactly what was given -- the drawing
 * moves, the claim does not.
 */
const SPREAD_PX = 15;

/** Where in a group of pins sharing a position this one is drawn. */
function spread(index, total) {
  if (total < 2) return [0, 0];
  const angle = (2 * Math.PI * index) / total - Math.PI / 2;
  return [Math.round(Math.cos(angle) * SPREAD_PX), Math.round(Math.sin(angle) * SPREAD_PX)];
}

function marker(cam, index = 0, total = 1) {
  const [dx, dy] = spread(index, total);
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
      // The anchor is what moves the drawing: Leaflet places the icon by it,
      // so shifting it slides the pin without touching the point it marks.
      iconSize: [26, 26], iconAnchor: [13 - dx, 13 - dy],
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
  // Grouped by position first, so cameras sharing one can be fanned out and
  // each still reached. Note this cannot be a plain CAMS.map(marker): that
  // hands map's index and the whole array to the second and third arguments,
  // which are the position within a group and the size of it.
  const groups = new Map();
  for (const cam of CAMS) {
    const key = `${cam.lat},${cam.lon}`;
    groups.set(key, [...(groups.get(key) ?? []), cam]);
  }
  const pins = [];
  for (const group of groups.values()) {
    group.forEach((cam, i) => pins.push(marker(cam, i, group.length)));
  }
  layer = L.layerGroup(pins, { pane: 'cams' });
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
    badge: cam.frozen
      ? { text: 'FRAME', className: 'is-frozen' }
      : {
        text: cam.kind === 'still' || cam.kind === 'dated' ? 'STILL' : 'LIVE',
        className: cam.kind === 'still' || cam.kind === 'dated' ? 'is-still' : 'is-live',
      },
    link: cam.src,
    body: frame,
    // Small, half the screen, then nearly all of it. A webcam is the one thing
    // here worth filling a monitor with, and the panel it started in is a
    // thumbnail of a video.
    sizes: [420, 'min(760px, calc(100vw - 40px))', 'calc(100vw - 40px)'],
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
  if (cam.kind === 'dated') return dated(cam);
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
    // A camera page needs to run a player and nothing else. Without this it
    // also gets to open windows, submit forms, and -- given a click anywhere
    // inside it -- navigate the whole tab somewhere else. allow-same-origin
    // is granted because the players load their own streams and storage, and
    // withholding it breaks them; it does not give the frame access to this
    // page, which is a different origin.
    sandbox: 'allow-scripts allow-same-origin allow-presentation',
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
  const carrier = cam.frozen
    ? `A single frame from ${cam.host}, at the moment its address names. It does `
      + 'not refresh: each picture there has its own address and there is no way '
      + 'to ask for the newest one.'
    : cam.kind === 'dated'
      ? `Stills from ${cam.host}, one every ${cam.stepMinutes} minutes. Each has its `
        + 'own dated address, so the newest few minutes are tried in turn — the '
        + 'time in the corner is the frame actually on screen.'
      : cam.kind === 'still'
        ? `A still from ${cam.host}, refreshed every ${STILL_SECONDS} seconds — not continuous video.`
        : `${cam.offsite ? 'Hosted by' : 'Streamed by'} ${cam.host}.`;
  const placed = cam.precision === 'given position'
    ? 'Pinned to the position it was given, so the marker is the camera.'
    : `Pinned to the ${cam.precision} — the player carries no coordinates, so `
      + 'the marker is the place, not the lens.';
  return `${carrier} ${placed}`;
}

/**
 * Fill a dated address in from the clock.
 *
 * Some hosts publish each frame at its own address, stamped with the minute it
 * was taken. There is no "latest" to ask for, so the address has to be worked
 * out -- and since publishing lags capture by an unknown few minutes, the only
 * honest approach is to try the newest slot and walk backwards until one
 * answers. `back` is how many slots to step back from now.
 */
/**
 * The instant of the slot `back` steps before now, on the host's clock.
 *
 * Their clock, not yours. The frames are stamped in the host's own timezone,
 * so a viewer in London asking a camera in Taiwan from their own local time
 * would name an hour eight hours in the past and get nothing but 404s for
 * every slot it tried -- which looks exactly like a dead camera.
 *
 * The returned Date is shifted so that its *UTC* fields read as the host's
 * local ones; read it back with getUTC*, never getHours.
 */
function datedSlot(cam, back = 0) {
  const step = cam.stepMinutes ?? 10;
  const shifted = new Date(
    Date.now() + (cam.tzOffsetMinutes ?? 0) * 60000 - back * step * 60000);
  shifted.setUTCMinutes(Math.floor(shifted.getUTCMinutes() / step) * step, 0, 0);
  return shifted;
}

/** The real instant a slot was captured, for showing to the viewer. */
function datedAt(cam, back = 0) {
  return new Date(datedSlot(cam, back).getTime() - (cam.tzOffsetMinutes ?? 0) * 60000);
}

function datedUrl(cam, back = 0) {
  const at = datedSlot(cam, back);
  const pad = (n) => String(n).padStart(2, '0');
  return cam.template
    .replace(/\{YYYY\}/g, String(at.getUTCFullYear()))
    .replace(/\{MM\}/g, pad(at.getUTCMonth() + 1))
    .replace(/\{DD\}/g, pad(at.getUTCDate()))
    .replace(/\{HH\}/g, pad(at.getUTCHours()))
    .replace(/\{mm\}/g, pad(at.getUTCMinutes()));
}

/**
 * A camera whose frames each have their own dated address.
 *
 * Walks back through the recent slots until one loads. Whatever it finds, the
 * footer says which minute it is showing, because "a few minutes ago" and
 * "two hours ago" look identical in a photograph of a hillside.
 */
function dated(cam) {
  const waiting = el('div', { class: 'cam-wait' },
    el('span', {}, `Looking for the newest frame from ${cam.host}…`),
    el('small', {}, 'Each picture has its own address, so the recent minutes are tried in turn.'));

  const image = el('div', { class: 'cam-still-wrap' });
  const stamp = el('span', { class: 'cam-stamp' }, '');
  const limit = cam.backSteps ?? 12;

  let back = 0;
  const attempt = () => {
    const url = datedUrl(cam, back);
    const img = el('img', { class: 'cam-still', referrerpolicy: 'no-referrer',
      alt: `${cam.name} — ${cam.place}` });
    img.addEventListener('load', () => {
      image.replaceChildren(img);
      waiting.remove();
      // The slot's own instant, shown in the viewer's timezone -- so the
      // clock time on screen is one they can compare against their own.
      const when = datedAt(cam, back);
      const old = Math.round((Date.now() - when.getTime()) / 60000);
      stamp.textContent = old < (cam.stepMinutes ?? 10)
        ? when.toLocaleTimeString()
        : `${when.toLocaleTimeString()} · ${old} min old`;
    }, { once: true });
    img.addEventListener('error', () => {
      back += 1;
      if (back <= limit) { attempt(); return; }
      waiting.replaceChildren(
        el('span', {}, `No frame found from ${cam.host}`),
        el('small', {}, `Tried the last ${limit * (cam.stepMinutes ?? 10)} minutes. `
          + 'The camera may be down, or the address pattern may have changed.'));
    }, { once: true });
    img.src = url;
  };
  attempt();

  // Re-resolve on the same timer as an ordinary snapshot, from the top.
  playing.set(cam.id, {
    timer: setInterval(() => { back = 0; attempt(); }, STILL_SECONDS * 1000),
  });
  return [image, stamp, waiting];
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

  const image = el('img', {
    class: 'cam-still', alt: `${cam.name} — ${cam.place}`,
    // The host has no business knowing which page asked for the picture.
    referrerpolicy: 'no-referrer',
  });
  const stamp = el('span', { class: 'cam-stamp' }, '');

  const pull = () => {
    // A frozen frame is fetched once and wants the cache, not a way around
    // it -- and a host that signs its addresses may refuse an extra parameter.
    image.src = cam.frozen
      ? cam.src
      : `${cam.src}${cam.src.includes('?') ? '&' : '?'}t=${Date.now()}`;
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
  // A frame with a fixed address is not a camera: asking again returns the
  // same picture until the host stops answering at all, and a timer would
  // only make that look like activity.
  if (!cam.frozen) {
    playing.set(cam.id, { timer: setInterval(pull, STILL_SECONDS * 1000) });
  }
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
    // Safari plays this itself, so there is no hls.js instance to destroy on
    // close -- the element is the thing holding the stream open, and it has to
    // be handed over or closing the window leaves it pulling video.
    playing.set(cam.id, { video });
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
  // The native path has no player to destroy, so the element is stopped
  // directly: paused, emptied, and told to let go of what it had buffered.
  if (held.video) {
    held.video.pause();
    held.video.removeAttribute('src');
    held.video.load();
  }
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
