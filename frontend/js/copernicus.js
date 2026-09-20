// The satellites that cannot be imagery, as live layers.
//
// The imagery flow is: draw an area, pick a date, get a rendered scene.
// Sentinel-1, Sentinel-2 and Landsat live there. These cannot join them, and
// the reason is worth saying rather than leaving as a gap in the picker.
//
// Sentinel-3 and Sentinel-5P are published as NetCDF granules -- a whole swath
// in one file, not tiled and not cloud-optimised. Pulling a hundred-kilometre
// box out of one means downloading the entire granule, and this app's pipeline
// is built on windowed reads of COGs. Different data, different pipeline. The
// weather satellites are further from it still: a geostationary full disc is
// not a scene over your area at all.
//
// EUMETSAT serves all of them as ordinary WMS with no account and no key,
// which is how they arrive here: the current picture, drawn under the imagery.
// Coarse on purpose, and the coarseness is the point. Sentinel-2 sees a field
// once every five days; Sentinel-3 sees the whole planet every day; Meteosat
// sees Europe every ten minutes, including at night.
import { api } from './api.js';
import { $, el } from './ui.js';

// How often to ask the backend what the newest frame is. Meteosat publishes
// every ten minutes and the rest a few times a day, so this is about a tab
// that has been open a while catching up rather than about keeping pace --
// and a quarter of an hour of a ten-minute cadence is at most one frame
// behind, which the bar says out loud as an age.
const REFRESH_MS = 15 * 60 * 1000;

let map = null;
let drawn = null;
let enabled = false;
let catalogue = null;
let chosen = null;      // { family, layer }
let opacity = 0.8;
// Which of the moments on offer is showing: an index into the chosen layer's
// frames, or null for the newest. null STAYS the newest -- a refresh that
// brings a fresher frame moves the picture on rather than leaving it behind,
// which is what "live" has to mean for a panel that reloads itself every
// quarter of an hour.
let frameAt = null;
// The loop: a timer id while it is playing, null while it is not.
let playing = null;
// Whether the whole catalogue is showing, rather than the handful of products
// per satellite that the backend marks as the everyday ones.
let showAll = false;
let timer = null;
let problem = '';

export function initCopernicus(leafletMap) {
  map = leafletMap;
  // Under the cloud mask and the radar, over the basemap. This is a picture
  // of the ground and the air above it, so it belongs at the bottom of the
  // things drawn on top of the map.
  map.createPane('copernicus').style.zIndex = 420;
  map.getPane('copernicus').style.pointerEvents = 'none';
  // Once, on the bar itself rather than on its contents, so it survives the
  // bar being rebuilt -- and passive: false, because a wheel handler that
  // cannot preventDefault scrolls the page instead of the times.
  $('#copBar')?.addEventListener('wheel', onWheel, { passive: false });
  // Panning or zooming makes every frame already fetched the wrong tiles, so
  // the walk starts again over the new view. On moveend rather than on move,
  // or a drag would start forty-eight walks across one gesture.
  map.on('moveend', () => {
    if (enabled && chosen?.layer?.animates) warm();
  });
  buildDock();
}

// ── The layer ──────────────────────────────────────────────────

function show() {
  drawn?.remove();
  drawn = null;
  if (!enabled || !chosen || !catalogue) return;

  let missing = 0;
  drawn = L.tileLayer.wms(catalogue.wms, {
    layers: chosen.layer.id,
    format: 'image/png',
    transparent: true,
    // 1.3.0 is what the capabilities are read at, and mixing versions is how
    // you get axis order wrong and the picture mirrored.
    version: '1.3.0',
    // Whichever moment the bar is showing. What that IS depends on the orbit,
    // and the backend has already decided: a whole day for a satellite that
    // flies over, an instant for one that stares.
    //
    // One instant from a polar orbiter is one orbit strip -- a few hundred
    // kilometres of the planet and nothing else, which on a world map reads
    // as a broken layer rather than as a satellite that has not been over the
    // rest of the world yet. A WMS given a TIME range draws everything inside
    // it, so a day is every pass that day, and at three hundred metres that
    // is the globe.
    ...(timeParam() ? { time: timeParam() } : {}),
    pane: 'copernicus',
    opacity,
    attribution: catalogue.attribution,
  });
  // A swath does not cover the globe, so missing tiles are ordinary: a polar
  // orbiter has simply not been over that ground today. A run of them
  // everywhere means the layer has moved, which is worth saying rather than
  // showing a blank.
  drawn.on('tileerror', () => {
    missing += 1;
    if (missing === 16) { problem = `${chosen.layer.id} is not answering.`; paint(); }
  });
  drawn.on('tileload', () => {
    if (!problem) return;
    problem = '';
    paint();
  });
  drawn.addTo(map);
}

async function load() {
  try {
    catalogue = await api.copernicus();
    problem = '';
    // Keep what was chosen if it is still on offer; otherwise take the first
    // live layer of the first family that has one.
    const all = (catalogue.families ?? []).flatMap(
      (family) => family.layers.map((layer) => ({ family, layer })));
    // An everyday one by preference, so the panel does not open on a product
    // that is only visible after pressing "all products".
    chosen = all.find((c) => c.layer.id === chosen?.layer?.id)
      ?? all.find((c) => c.layer.everyday !== false) ?? all[0] ?? null;
    if (!all.length) problem = nothingLive(catalogue);
  } catch (err) {
    problem = err.message;
    catalogue = null;
    chosen = null;
  }
  buildDock();
  show();
  // Ahead of being asked for, so the loop does not stutter through its first
  // pass. Only where there is something to animate: a week of orbit strips is
  // seven pictures nobody is going to play.
  if (enabled && chosen?.layer?.animates) warm();
}

/** Which frame is showing: the newest unless scrubbed back. */
export function frameIndex(frames, at) {
  if (!frames?.length) return 0;
  if (at == null) return frames.length - 1;
  return Math.min(Math.max(at, 0), frames.length - 1);
}

/** Where a step of the scrubber lands, with the newest frame meaning "live".
 *
 * Returning null rather than the last index is the whole of what keeps the
 * panel live: an index would pin the picture to whatever the newest frame was
 * when you scrubbed, and ten minutes later that is the one before last.
 */
export function stepFrame(frames, at, step) {
  const last = (frames?.length ?? 0) - 1;
  if (last < 0) return null;
  const next = frameIndex(frames, at) + step;
  return next >= last ? null : Math.max(0, next);
}

/** The next frame of a loop: round the end and back to the start.
 *
 * Deliberately NOT stepFrame. A loop that resolved its last frame to null
 * would land on "live", and live is a moving target -- a loop left running
 * would silently start following the clock instead of replaying the same few
 * hours, and the animation would drift a frame later every time the panel
 * refreshed.
 */
export function nextFrame(frames, at) {
  const last = (frames?.length ?? 0) - 1;
  if (last < 0) return null;
  const next = frameIndex(frames, at) + 1;
  return next > last ? 0 : next;
}

/** How many frames make up about this many minutes. Always at least one. */
export function framesPer(minutes, stepMinutes) {
  // An unknown cadence makes "an hour" meaningless, so the dial moves one
  // frame. Treating it as one minute would make the hour dial jump sixty
  // frames past the end of a list that holds twenty-four.
  if (!(stepMinutes > 0)) return 1;
  return Math.max(1, Math.round(minutes / stepMinutes));
}

/** The TIME parameter for whichever frame is selected. */
export function wmsTime(layer, at) {
  const frames = layer?.frames ?? [];
  if (!frames.length) return layer?.time_default ?? null;
  return frames[frameIndex(frames, at)].time;
}

function timeParam() {
  return wmsTime(chosen?.layer, frameAt);
}

/** Which of a family's products the panel lists.
 *
 * The everyday handful, unless everything was asked for -- and whatever is
 * currently drawn, always, because a list that can hide the thing it is
 * showing leaves no way back to it.
 */
export function productsShown(layers, all, chosenId) {
  return (layers ?? []).filter(
    (layer) => all || layer.everyday !== false || layer.id === chosenId);
}

/** What to say when EUMETSAT answered but nothing it holds is current. */
function nothingLive(got) {
  const stale = (got.families ?? []).reduce((n, f) => n + (f.stale ?? 0), 0);
  const hours = Math.round(got.live_within_hours ?? 36);
  if (stale) {
    return `EUMETSAT has ${stale} matching layers but none from the last `
      + `${hours} h. Shown only when current.`;
  }
  return 'No Sentinel, Meteosat or Metop layers at EUMETSAT'
    + `${got.catalogue_size ? ` among ${got.catalogue_size} layers` : ''}.`;
}

// ── The panel ──────────────────────────────────────────────────

function buildDock() {
  const dock = $('#copernicusDock');
  if (!dock) return;
  const families = (catalogue?.families ?? []).filter((f) => f.layers.length);
  const held = families.reduce((n, f) => n + f.layers.length, 0);
  const listed = families.reduce(
    (n, f) => n + productsShown(f.layers, showAll, chosen?.layer?.id).length, 0);
  dock.innerHTML = '';
  dock.append(
    el('button', { class: 'cop-toggle', id: 'copToggle', onclick: toggle },
      el('span', { class: 'cop-mark' }, '🛰'), 'Live satellites'),
    el('div', { class: 'cop-body', id: 'copBody', hidden: !enabled },
      ...families.map((family) => el('div', { class: 'cop-family' },
        el('div', { class: 'cop-name' },
          el('i', { style: `background:${family.colour}` }),
          family.short,
          el('small', {}, family.resolution),
          // Said out loud, because the list below is a shortlist now.
          el('small', { class: 'cop-many' }, (() => {
            const shown = productsShown(
              family.layers, showAll, chosen?.layer?.id).length;
            return shown < family.layers.length
              ? `${shown} of ${family.layers.length}`
              : `${shown} product${shown === 1 ? '' : 's'}`;
          })())),
        // The everyday handful rather than the whole catalogue. EUMETSAT
        // serves a dozen or more products per satellite, and a panel that
        // opens with forty buttons on it opens with nothing in particular on
        // it. The rest are behind one button at the bottom.
        el('div', { class: 'cop-layers' },
          ...productsShown(family.layers, showAll, chosen?.layer?.id)
            .map((layer) => el('button', {
              class: `cop-layer${layer.id === chosen?.layer?.id ? ' is-on' : ''}`,
              title: `${layer.title} — ${layer.id}`,
              onclick: () => {
        chosen = { family, layer };
        pause();
        stopWarming();
        buildDock();
        show();
        if (layer.animates) warm();
      },
            }, shortName(layer)))))),
      listed < held || showAll
        ? el('button', {
          class: `cop-more${showAll ? ' is-on' : ''}`,
          title: 'Every product EUMETSAT is serving from these satellites',
          onclick: () => { showAll = !showAll; buildDock(); },
        }, showAll ? `Just the everyday ${listedEveryday(families)}`
          : `All ${held} products`)
        : null,
      families.length
        ? el('label', { class: 'cop-fade' }, 'Fade',
          el('input', {
            type: 'range', min: '20', max: '100', value: String(opacity * 100),
            oninput: (e) => {
              opacity = Number(e.target.value) / 100;
              drawn?.setOpacity(opacity);
            },
          }))
        : null,
      el('div', { class: 'cop-count', id: 'copCount' }, 'Loading…'),
      el('div', { class: 'cop-note', id: 'copNote' }, '')));
  paint();
  buildBar();
}

// ── The time bar ───────────────────────────────────────────────
//
// Out of the side panel and onto the map, because it is not a setting: it is
// what you are looking at. A geostationary satellite publishes a frame every
// ten minutes, so the interesting thing about it is not which product is
// selected but which minute is on screen -- and the way you find out what a
// front is doing is to play it.

// How long each frame is held when the loop is running. Faster than this and
// a frame is gone before the tiles for it have arrived; slower and it stops
// reading as motion.
const FRAME_MS = 550;

function buildBar() {
  const bar = $('#copBar');
  if (!bar) return;
  const layer = chosen?.layer;
  const frames = layer?.frames ?? [];
  bar.innerHTML = '';
  bar.hidden = !enabled || frames.length < 2;
  if (bar.hidden) { pause(); return; }

  const now = frames[frameIndex(frames, frameAt)];
  const hop = (by) => () => {
    pause();
    frameAt = stepFrame(frames, frameAt, by);
    retime();
    refreshBar();
  };
  // An hour at a time on the left dial, one frame on the right, which is what
  // the two dials mean on every weather map that has them.
  const anHour = framesPer(60, layer.step_minutes);

  // Filtered, because append() is the DOM's and not el()'s: handed a null it
  // inserts the text "null" rather than skipping it, which is how a bar with
  // no clock on it came to read "nullnull".
  bar.append(...[
    layer.animates
      ? el('button', {
        class: `cop-play${playing ? ' is-on' : ''}`, id: 'copPlay',
        title: playing ? 'Stop' : 'Play the last few hours, on a loop',
        onclick: () => (playing ? pause() : play()),
      }, playing ? '❚❚' : '▶')
      // A satellite that flies over has no animation to offer: consecutive
      // frames are two different strips of the planet a day apart, and a loop
      // of those is a slideshow, not weather moving.
      : el('span', { class: 'cop-noplay', title: 'Only Meteosat updates fast '
        + 'enough to animate — these frames are a day apart' }, '·'),
    el('span', { class: 'cop-date', id: 'copDate' }, dateLabel(now)),
    dial(layer.animates ? anHour : 1, hop),
    layer.animates ? el('span', { class: 'cop-colon' }, ':') : null,
    layer.animates ? dial(1, hop, true) : null,
    el('span', { class: 'cop-time', id: 'copTime' }, clockLabel(layer, now)),
    el('button', {
      class: `cop-latest${frameAt == null ? ' is-on' : ''}`, id: 'copLatest',
      title: 'The newest frame, and it stays the newest frame',
      onclick: () => { pause(); frameAt = null; retime(); refreshBar(); },
    }, '⏭'),
    // How much of the eight hours is in hand. Shown only while it is still
    // arriving: a bar that reads "48/48" forever is furniture.
    el('span', { class: 'cop-warm', id: 'copWarm' }, warmSaid()),
    layer.animates
      ? el('button', {
        class: `cop-rec${filming ? ' is-on' : ''}`, id: 'copRec',
        title: filming ? 'Stop and save' : 'Record the loop as a video',
        onclick: () => (filming ? stopFilm() : startFilm()),
      }, filming ? '■' : '●')
      : null,
  ].filter((node) => node != null));
}

/** One up/down pair, stepping by a fixed number of frames. */
function dial(by, hop, minor = false) {
  return el('span', { class: `cop-dial${minor ? ' is-minor' : ''}` },
    el('button', { class: 'cop-nudge', title: 'Later', onclick: hop(by) }, '⌃'),
    el('button', { class: 'cop-nudge', title: 'Earlier', onclick: hop(-by) }, '⌄'));
}

/** "20 Sept", from the day a frame falls on. */
export function dateLabel(frame) {
  const on = frame?.at;
  if (!on) return '';
  const when = new Date(`${on}T12:00:00Z`);
  return Number.isNaN(when.valueOf()) ? on
    : when.toLocaleDateString('en-GB', { day: 'numeric', month: 'short',
                                         timeZone: 'UTC' });
}

/** "18:00" for a satellite that stares, and nothing for one that flies over.
 *
 * A whole day of orbit strips has no clock reading. Showing 00:00 for it
 * would be a time nobody can act on -- it is not when the satellite passed
 * over, it is where the range this app asked for happens to start.
 */
export function clockLabel(layer, frame) {
  return layer?.animates ? (frame?.label ?? '') : 'all day';
}

/** "12/48" while the frames are still arriving, and nothing once they are. */
function warmSaid() {
  if (!warmOf || warmed >= warmOf) return '';
  return `${warmed}/${warmOf}`;
}

function refreshBar() {
  const layer = chosen?.layer;
  const frames = layer?.frames ?? [];
  if (!frames.length) return;
  const now = frames[frameIndex(frames, frameAt)];
  const date = $('#copDate');
  const time = $('#copTime');
  if (date) date.textContent = dateLabel(now);
  if (time) time.textContent = clockLabel(layer, now);
  $('#copLatest')?.classList.toggle('is-on', frameAt == null);
  const warmNode = $('#copWarm');
  if (warmNode) warmNode.textContent = warmSaid();
  const rec = $('#copRec');
  if (rec) {
    rec.textContent = filming ? '■' : '●';
    rec.classList.toggle('is-on', Boolean(filming));
    rec.title = filming ? 'Stop and save' : 'Record the loop as a video';
  }
  const play = $('#copPlay');
  if (play) {
    play.textContent = playing ? '❚❚' : '▶';
    play.classList.toggle('is-on', Boolean(playing));
    play.title = playing ? 'Stop' : 'Play the last few hours, on a loop';
  }
  paint();
}

// ── Fetching the frames before they are wanted ─────────────────
//
// Asked for: have the last eight hours in hand so it runs smooth. A loop that
// fetches each frame as it reaches it stutters on the first pass through --
// worse over a slow connection, and worst exactly when somebody is watching
// something happen.
//
// So the frames are walked once, ahead of time, one at a time. One at a time
// rather than all at once on purpose: forty-eight frames of a dozen tiles is
// six hundred requests, and firing those together would compete with the
// frame actually on screen and hammer a free service. Sequential is polite,
// bounded, and easy to stop.
//
// Each frame is drawn into its own layer at zero opacity and removed once it
// has loaded. Removing it does not lose anything: the tiles are in the
// browser's cache and in this app's own cache behind it, so when the loop
// reaches that frame the pictures are already there.

let warming = null;      // the run in progress, so a new one can cancel it
let warmed = 0;          // how many frames of the current layer are in hand
let warmOf = 0;

/** Walk the frames, fetching each one, until they are all in hand. */
async function warm() {
  const layer = chosen?.layer;
  const frames = layer?.frames ?? [];
  if (!map || !enabled || !catalogue || frames.length < 2) return;
  // Whatever was walking is told to stop before this one starts. Two walks
  // over one view is twice the requests for the same tiles.
  if (warming) warming.stop = true;
  const run = { id: layer.id, stop: false };
  warming = run;
  warmed = 0;
  warmOf = frames.length;
  refreshBar();

  for (const frame of frames) {
    if (run.stop || !enabled) break;
    // eslint-disable-next-line no-await-in-loop
    await oneFrame(frame);
    if (run.stop) break;
    warmed += 1;
    refreshBar();
  }
  if (warming === run) warming = null;
  if (!filming) coolOven();
  refreshBar();
}

/** The hidden layer the walk and the recorder both fetch through.
 *
 * ONE layer, retimed, rather than one per frame. Adding and removing
 * forty-eight layers leaves tiles in flight whose layer has gone, and when
 * one of those lands Leaflet reaches for a map that is no longer there --
 * measured: twenty-four "_fadeAnimated of null" errors per walk, one per
 * frame. Retiming has nothing to orphan.
 */
let oven = null;

function heatOven() {
  if (oven || !map || !catalogue || !chosen) return oven;
  oven = L.tileLayer.wms(catalogue.wms, {
    layers: chosen.layer.id,
    format: 'image/png',
    transparent: true,
    version: '1.3.0',
    pane: 'copernicus',
    opacity: 0,
  });
  oven.addTo(map);
  return oven;
}

function coolOven() {
  oven?.remove();
  oven = null;
}

/** Fetch one frame invisibly, and hand back its loaded tile images. */
function oneFrame(frame) {
  return new Promise((done) => {
    const layer = heatOven();
    if (!layer) { done([]); return; }
    let settled = false;
    const bell = setTimeout(finish, WARM_WAIT_MS);
    function finish() {
      if (settled) return;
      settled = true;
      clearTimeout(bell);
      layer.off('load', finish);
      // Handed over while the layer is still on the page. A detached <img>
      // still draws, but its bounding box collapses to nothing and every
      // tile would land in the top-left corner.
      done([...(layer._container?.querySelectorAll('img') ?? [])]);
    }
    layer.on('load', finish);
    try {
      layer.setParams({ time: frame.time });
    } catch {
      finish();
    }
  });
}

/** How long to wait on one frame before moving to the next. */
const WARM_WAIT_MS = 8000;

/** Stop any walk in progress. */
function stopWarming() {
  if (warming) warming.stop = true;
  warming = null;
  warmed = 0;
  warmOf = 0;
  if (!filming) coolOven();
}

// ── Recording the loop ─────────────────────────────────────────
//
// A video of the eight hours, made from the same frames the loop plays.
//
// This is only possible because the tiles come through this app now. A canvas
// with a cross-origin image drawn into it is tainted, and a tainted canvas
// refuses to hand back its pixels -- captureStream included. Fetched straight
// from EUMETSAT, every one of these frames would poison the recording.
//
// What goes in it: the satellite, the borders, and the time. NOT the basemap,
// which comes from somebody else's tile server and would taint the canvas
// after all -- so the coastlines in the video are the satellite's own, which
// at true colour is most of what a basemap was drawing anyway.

// How long each satellite frame is held in the video. Slower than the live
// loop: a video is watched rather than glanced at, and a front crossing the
// country at eight frames a second is a flicker.
const FILM_HOLD_MS = 260;
const FILM_FPS = 25;

let filming = null;

function filmType() {
  for (const kind of ['video/webm;codecs=vp9', 'video/webm;codecs=vp8',
                      'video/webm', 'video/mp4']) {
    if (window.MediaRecorder?.isTypeSupported?.(kind)) return kind;
  }
  return '';
}

async function startFilm() {
  const layer = chosen?.layer;
  const frames = layer?.frames ?? [];
  if (filming || !map || frames.length < 2) return;
  if (!window.MediaRecorder) {
    problem = 'This browser cannot record video.';
    paint();
    return;
  }
  pause();

  const box = map.getContainer().getBoundingClientRect();
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(box.width);
  canvas.height = Math.round(box.height);
  const ctx = canvas.getContext('2d');
  const chunks = [];
  const kind = filmType();
  const rec = new MediaRecorder(canvas.captureStream(FILM_FPS),
                                kind ? { mimeType: kind } : undefined);
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const done = new Promise((ready) => { rec.onstop = ready; });
  filming = { stop: false, rec };
  refreshBar();
  rec.start();

  const outlines = await bordersForFilm();
  for (const frame of frames) {
    if (filming?.stop) break;
    // eslint-disable-next-line no-await-in-loop
    await filmFrame(ctx, canvas, frame, outlines, box);
  }
  rec.stop();
  await done;
  filming = null;
  if (!warming) coolOven();
  refreshBar();
  if (chunks.length) {
    save(new Blob(chunks, { type: kind || 'video/webm' }), layer, frames);
  }
}

function stopFilm() {
  if (filming) filming.stop = true;
}

/** Draw one frame of the video and hold it for its share of the running time. */
async function filmFrame(ctx, canvas, frame, outlines, box) {
  const tiles = await oneFrame(frame);
  ctx.fillStyle = '#05070b';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (const img of tiles) {
    const at = img.getBoundingClientRect();
    if (!img.complete || !img.naturalWidth) continue;
    try {
      ctx.drawImage(img, at.left - box.left, at.top - box.top,
                    at.width, at.height);
    } catch { /* one tile that will not draw is not a reason to lose the film */ }
  }
  drawFilmBorders(ctx, outlines);
  stampFilm(ctx, canvas, frame);
  await held(FILM_HOLD_MS);
}

async function bordersForFilm() {
  try {
    const got = await api.trackerOutlines();
    return (got.outlines ?? []).filter((o) => o.level === 'country');
  } catch {
    // The satellite is the subject; borders are an aid. A video without them
    // is worth more than no video.
    return [];
  }
}

function drawFilmBorders(ctx, outlines) {
  if (!outlines.length) return;
  ctx.save();
  ctx.strokeStyle = 'rgba(255, 96, 92, 0.85)';
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  for (const outline of outlines) {
    const shape = outline.shape;
    const parts = shape?.type === 'Polygon' ? shape.coordinates
      : shape?.type === 'MultiPolygon' ? shape.coordinates.flat() : [];
    for (const ring of parts) {
      ctx.beginPath();
      ring.forEach(([lon, lat], i) => {
        const at = map.latLngToContainerPoint([lat, lon]);
        if (i === 0) ctx.moveTo(at.x, at.y);
        else ctx.lineTo(at.x, at.y);
      });
      ctx.stroke();
    }
  }
  ctx.restore();
}

function stampFilm(ctx, canvas, frame) {
  const text = `${dateLabel(frame)}  ${frame.label ?? ''}`.trim();
  const size = Math.max(14, Math.round(canvas.width * 0.016));
  ctx.save();
  ctx.font = `600 ${size}px system-ui, -apple-system, Segoe UI, sans-serif`;
  const wide = ctx.measureText(text).width;
  ctx.fillStyle = 'rgba(5, 7, 11, 0.72)';
  ctx.fillRect(14, canvas.height - size * 2.6, wide + size, size * 1.9);
  ctx.fillStyle = '#e9eef7';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 14 + size / 2, canvas.height - size * 1.65);
  ctx.restore();
}

function held(ms) {
  return new Promise((done) => setTimeout(done, ms));
}

function save(blob, layer, frames) {
  const from = frames[0]?.time?.slice(0, 16).replace(/[:T]/g, '') ?? '';
  const to = frames[frames.length - 1]?.time?.slice(11, 16).replace(':', '') ?? '';
  const name = `${(layer?.id ?? 'satellite').split(':').pop()}_${from}-${to}`
    + `_kaldockhi.${blob.type.includes('mp4') ? 'mp4' : 'webm'}`;
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function play() {
  const frames = chosen?.layer?.frames ?? [];
  if (playing || frames.length < 2) return;
  // Start from the oldest frame rather than from wherever the scrubber sits,
  // so pressing play always gives the whole loop instead of the tail of it.
  frameAt = 0;
  retime();
  playing = setInterval(() => {
    const on = chosen?.layer?.frames ?? [];
    if (on.length < 2) { pause(); return; }
    frameAt = nextFrame(on, frameAt);
    retime();
    refreshBar();
  }, FRAME_MS);
  refreshBar();
}

function pause() {
  if (!playing) return;
  clearInterval(playing);
  playing = null;
  refreshBar();
}

/** How many products the short list holds, for the button that goes back. */
function listedEveryday(families) {
  return families.reduce(
    (n, f) => n + productsShown(f.layers, false, chosen?.layer?.id).length, 0);
}

/** Retime the drawn layer in place.
 *
 * setParams rather than a rebuild: scrubbing across a week is seven steps, and
 * tearing the layer down and building it again at each one flashes the map
 * empty seven times.
 */
function retime() {
  if (!drawn) { show(); return; }
  const when = timeParam();
  drawn.setParams(when ? { time: when } : {});
}

/** Scrolling over the bar walks the frames: down is back in time. */
function onWheel(e) {
  const frames = chosen?.layer?.frames ?? [];
  if (frames.length < 2) return;
  // Otherwise the page scrolls underneath instead, which is the one thing a
  // reader cannot mean by scrolling on a row of times.
  e.preventDefault();
  pause();
  frameAt = stepFrame(frames, frameAt, e.deltaY > 0 ? -1 : 1);
  retime();
  refreshBar();
}

/** A label that fits, from a title that does not. */
function shortName(layer) {
  return layer.title
    // The satellite's name is already the heading above the list, so it is
    // only taking room away from the part that says which product this is.
    .replace(/^(sentinel[-\s]?[35]p?|metop[-\s]?[abc]?|meteosat|msg|mtg|fci|seviri|avhrr)\s*/i, '')
    .replace(/\s*\(.*\)\s*$/, '')
    .trim()
    .slice(0, 18) || layer.id.split(':').pop().slice(0, 18);
}

function toggle() {
  enabled = !enabled;
  $('#copToggle').classList.toggle('is-on', enabled);
  $('#copBody').hidden = !enabled;
  if (enabled) {
    load();
    timer = setInterval(load, REFRESH_MS);
  } else {
    clearInterval(timer);
    timer = null;
    pause();
    stopWarming();
    drawn?.remove();
    drawn = null;
  }
  paint();
  buildBar();
}

function paint() {
  const count = $('#copCount');
  const note = $('#copNote');
  if (!count || !note || !enabled) return;

  if (!chosen) {
    count.textContent = catalogue ? 'Nothing current' : 'Loading…';
    note.textContent = problem;
    return;
  }

  const age = chosen.layer.age_minutes;
  count.textContent = `${chosen.family.short} · ${
    age == null ? 'live' : age < 60 ? `${age} min ago`
      : `${Math.round(age / 60)} h ago`}`;

  const lines = [
    chosen.layer.animates
      ? (playing
        ? 'Playing the last few hours on a loop.'
        : 'A frame every '
          + `${chosen.layer.step_minutes ?? 10} minutes. Press play, or scroll `
          + 'the times to go back.')
      : frameAt == null
        ? 'Today: every pass so far, which at this scale is the whole Earth. '
          + 'One instant would be a single orbit strip. Scroll the dates to go '
          + 'back.'
        : 'One whole day: every pass that day.',
    chosen.family.about,
  ];
  if (catalogue.attribution === 'synthetic') {
    lines.push('Demo mode: the tiles are stand-ins.');
  } else {
    // Said once, because it is the question the picker raises: why are these
    // two not in the list of satellites you can search by date?
    lines.push('Published as NetCDF rather than tiled GeoTIFF, so these are '
      + 'live layers rather than scenes you can pick a date for.');
    lines.push(catalogue.attribution);
  }
  if (problem) lines.push(problem);
  note.textContent = lines.filter(Boolean).join(' ');
}
