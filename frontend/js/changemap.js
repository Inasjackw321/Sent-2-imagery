// Change map: where the ground changed between two dates, from Sentinel-1
// radar -- the Satellite Change Map app, inside EarthViewer.
//
// Radar sees through cloud and at night, and the same ground seen twice from
// the same orbit returns the same signal unless something on it changed: a
// building went, rubble came, a field was ploughed, a flood came in. The
// backend (backend/changemap) averages several passes on each side and runs a
// statistical test per 10 m pixel, so what is shown is change that noise
// cannot explain.
//
// The panel is three plain questions -- where, when, how careful -- and a
// check of exactly which images will be used before anything is downloaded.
// Then the map, its spots, how far to trust it, and one row of buttons to
// save it in every useful form.

import { api } from './api.js';
import { store } from './store.js';
import { $, el, toast, debounce, savePicture } from './ui.js';
import { openWindow, closeWindow, isOpen } from './windows.js';

let map = null;
let pickArea = null;
const WIN = 'changemap';

// What the panel holds between renders.
const state = {
  bounds: null,           // [west, south, east, north]
  label: '',
  start: '',
  end: '',
  images: 3,
  plan: null,             // the last answer from /api/change/plan
  planning: false,
  planError: '',
  job: null,              // the running comparison, polled
  result: null,           // the result on the map
  preset: 'Balanced',
  view: 'changes',        // what the map shows: changes, before, after
  blend: 1,               // 0 = before radar, 1 = after radar
  background: 'satellite',
  watermark: '@Kaldockhi',
  saved: [],
};

// Map layers, in a pane of their own above the imagery.
const layers = { outline: null, overlay: null, before: null, after: null, spots: null };

// ── Pure pieces, tested in Node ────────────────────────────────

/** YYYY-MM-DD in UTC. */
export function isoDay(d) {
  return new Date(d).toISOString().slice(0, 10);
}

/** The quick date choices, ending today. */
export function datePresets(today = new Date()) {
  const end = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate()));
  const back = (days) => isoDay(end.getTime() - days * 86400e3);
  return {
    month: { label: 'Last month', start: back(30), end: isoDay(end) },
    quarter: { label: 'Last 3 months', start: back(91), end: isoDay(end) },
    year: { label: 'Last year', start: back(365), end: isoDay(end) },
  };
}

/** What a number of images per side means, in a line. */
export function imagesSaid(n) {
  if (n <= 1) return '1 image each side: quickest, smallest download, only big changes show.';
  if (n <= 3) return `${n} images each side: a good balance of detail and download.`;
  if (n <= 8) return `${n} images each side: finds smaller changes; a bigger download.`;
  return `${n} images each side: the most sensitive; a large download reaching months back.`;
}

/** "12 Jun 2026" from YYYY-MM-DD. */
export function daySaid(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso ?? '';
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** "4.3 × 4.4 km" for a box. */
export function areaSaid([west, south, east, north]) {
  const lat = ((south + north) / 2) * Math.PI / 180;
  const w = (east - west) * 111.32 * Math.cos(lat);
  const h = (north - south) * 110.57;
  const f = (v) => (v >= 10 ? v.toFixed(0) : v.toFixed(1));
  return `${f(w)} × ${f(h)} km`;
}

/** The plan in two short lines. */
export function planSaid(plan) {
  const list = (days) => days.map(daySaid).join(', ');
  const size = plan.download_mb >= 1000
    ? `${(plan.download_mb / 1000).toFixed(1)} GB` : `${plan.download_mb} MB`;
  return {
    before: `Before: ${plan.before.length} pass${plan.before.length === 1 ? '' : 'es'} — ${list(plan.before)}`,
    after: `After: ${plan.after.length} pass${plan.after.length === 1 ? '' : 'es'} — ${list(plan.after)}`,
    size: plan.saved ? 'Already made on this computer: it opens instantly.'
      : `About ${size} to download from Microsoft Planetary Computer (free, no account).`,
  };
}

/** The address of one saved file. */
export function exportUrl(key, kind, { preset = 'Balanced', background = 'satellite',
  watermark = '@Kaldockhi', numbers = true } = {}) {
  const q = new URLSearchParams({ kind, preset, background, watermark, numbers: String(numbers) });
  return `/api/change/export/${encodeURIComponent(key)}?${q}`;
}

/** A filename for a saved file. */
export function fileName(result, kind) {
  const ext = { png: 'png', jpeg: 'jpg', csv: 'csv', geojson: 'geojson', tif: 'tif', zip: 'zip' }[kind];
  const label = String(result?.label ?? 'change').replace(/[^\w.-]+/g, '_').replace(/^_+|_+$/g, '');
  const p = result?.params ?? {};
  return `change_${label || 'area'}_${p.start ?? ''}_${p.end ?? ''}.${ext}`;
}

export const SAVES = [
  ['png', '🖼 Picture', 'The map with numbered spots, legend, scale bar and credits (PNG)'],
  ['csv', '📄 Spots', 'Every changed spot: where, how big, how much, how often, when (CSV)'],
  ['geojson', '🌍 GeoJSON', 'The spots as points, for QGIS, Google Earth or any GIS'],
  ['tif', '🗺 GeoTIFF', 'The change itself on its 10 m grid: detected change and dB'],
  ['zip', '📦 Everything', 'All of the above in one zip, with a note saying what each is'],
];

// ── Wiring ─────────────────────────────────────────────────────

export function initChangeMap(leafletMap, { pickArea: pick } = {}) {
  map = leafletMap;
  pickArea = pick ?? null;
  map.createPane('changemap').style.zIndex = 445;
  layers.spots = L.layerGroup([], { pane: 'changemap' });
  const presets = datePresets();
  state.start = presets.quarter.start;
  state.end = presets.quarter.end;
  const dock = $('#changeDock');
  if (dock) {
    dock.append(el('button', { class: 'fire-toggle change-toggle', id: 'changeToggle', onclick: toggle },
      el('span', { class: 'fire-flame' }, '🛰'), 'Change map'));
  }
  map.on('zoomend', crisp);
}

function toggle() {
  if (isOpen(WIN)) { closeWindow(WIN); return; }
  open();
}

function open() {
  openWindow({
    id: WIN, title: 'Change map', where: 'radar change between two dates', sizes: true,
    body: el('div', { class: 'cm', id: 'cmBody' }),
    onClose: () => $('#changeToggle')?.classList.remove('is-on'),
  });
  $('#changeToggle')?.classList.add('is-on');
  loadSaved();
  render();
}

const planSoon = debounce(() => checkPlan(), 500);

function setArea(bounds, label = '') {
  state.bounds = bounds.map((v) => Math.round(v * 1e5) / 1e5);
  state.label = label;
  state.plan = null;
  drawOutline();
  render();
  planSoon();
  nameArea();
}

async function nameArea() {
  const [w, s, e, n] = state.bounds;
  try {
    const got = await fetch(`/api/whereis?lat=${(s + n) / 2}&lon=${(w + e) / 2}`).then((r) => r.json());
    if (got?.region && !state.label) {
      state.label = got.region;
      render();
    }
  } catch { /* coordinates are a fine name */ }
}

function drawArea() {
  if (!pickArea) return;
  const armed = pickArea((bounds) => {
    setArea([bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()]);
  }, 'box');
  if (armed) toast('Drag a box over the area to compare.');
}

function useImageryArea() {
  const aoi = store.aoi;
  if (!aoi) return;
  const layer = L.geoJSON(aoi.type === 'Feature' ? aoi : { type: 'Feature', geometry: aoi });
  const b = layer.getBounds();
  if (!b.isValid()) return;
  setArea([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()]);
}

function drawOutline() {
  layers.outline?.remove();
  if (!state.bounds) return;
  const [w, s, e, n] = state.bounds;
  layers.outline = L.rectangle([[s, w], [n, e]], {
    pane: 'changemap', color: '#ffffff', weight: 1.5, fill: false, dashArray: '5 4', interactive: false,
  }).addTo(map);
}

async function checkPlan() {
  if (!state.bounds || !state.start || !state.end) return;
  const asked = body();
  state.planning = true;
  state.planError = '';
  render();
  try {
    const plan = await api.changePlan(asked);
    if (JSON.stringify(asked) !== JSON.stringify(body())) return;   // superseded
    state.plan = plan;
    if (plan.note) toast(plan.note);
  } catch (err) {
    state.plan = null;
    state.planError = err.message;
  } finally {
    state.planning = false;
    render();
  }
}

function body() {
  return { bounds: state.bounds, start: state.start, end: state.end, images: state.images,
    label: state.label };
}

async function findChanges() {
  if (!state.plan) return;
  if (state.plan.saved) { await showResult(state.plan.key); return; }
  try {
    state.job = await api.changeRun(body());
  } catch (err) {
    toast(`Could not start: ${err.message}`, 'err');
    return;
  }
  render();
  for (;;) {
    // eslint-disable-next-line no-await-in-loop
    await new Promise((done) => setTimeout(done, 700));
    let job;
    try {
      // eslint-disable-next-line no-await-in-loop
      job = await api.changeJob();
    } catch { continue; }
    state.job = job;
    renderProgress();
    if (job.state === 'done') {
      state.job = null;
      await showResult(job.key);
      loadSaved();
      return;
    }
    if (job.state === 'failed') {
      state.job = null;
      render();
      toast(`The change map could not be made: ${job.error}`, 'err');
      return;
    }
  }
}

async function showResult(key, { fit = true } = {}) {
  try {
    state.result = await api.changeResult(key, state.preset);
  } catch (err) {
    toast(`Could not open the change map: ${err.message}`, 'err');
    return;
  }
  const r = state.result;
  state.bounds = r.params.bounds;
  state.start = r.params.start;
  state.end = r.params.end;
  state.images = r.params.images;
  state.label = r.label;
  drawOutline();
  drawResult();
  if (fit) map.fitBounds(r.bounds, { padding: [40, 40] });
  render();
}

async function setPreset(name) {
  state.preset = name;
  if (state.result) await showResult(state.result.key, { fit: false });
}

function drawResult() {
  const r = state.result;
  for (const k of ['overlay', 'before', 'after']) { layers[k]?.remove(); layers[k] = null; }
  layers.spots.clearLayers();
  if (!r) return;
  for (const side of ['before', 'after']) {
    const got = r.layers?.[side];
    if (got) layers[side] = L.imageOverlay(got.image, got.bounds, { pane: 'changemap', opacity: 0 }).addTo(map);
  }
  layers.overlay = L.imageOverlay(r.overlay, r.bounds, { pane: 'changemap' }).addTo(map);
  for (const s of r.spots.slice(0, 300)) {
    const colour = s.kind === 'decrease' ? r.colours.decrease : r.colours.increase;
    L.marker([s.lat, s.lon], {
      pane: 'changemap',
      icon: L.divIcon({ className: 'cm-badge', html: `<span style="border-color:${colour}">${s.times}</span>`,
        iconSize: null, iconAnchor: [-6, 9] }),
    }).bindPopup(spotPopup(s), { maxWidth: 260 }).addTo(layers.spots);
  }
  layers.spots.addTo(map);
  applyView();
  crisp();
}

function spotPopup(s) {
  const kind = s.kind === 'decrease' ? 'Radar signal decreased' : 'Radar signal increased';
  const seen = s.times
    ? `Seen changing ${s.times} time${s.times === 1 ? '' : 's'} between passes:<br>${s.when.join('<br>')}`
    : 'No single pass-to-pass change stood out: it changed gradually.';
  return `<div class="fire-popup"><div class="fire-popup-head">${kind} by ${Math.abs(s.change_db).toFixed(1)} dB</div>`
    + `<div>${s.area_m2.toLocaleString()} m² · ${s.lat.toFixed(5)}, ${s.lon.toFixed(5)}</div>`
    + `<div class="dim">${seen}</div></div>`;
}

/** Which layers show, and the before/after blend. */
function applyView() {
  const showRadar = state.view !== 'changes';
  layers.before?.setOpacity(showRadar ? 1 - state.blend : 0);
  layers.after?.setOpacity(showRadar ? state.blend : 0);
  layers.overlay?.setOpacity(state.view === 'radar' ? 0 : 1);
  if (state.view === 'radar') layers.spots.remove(); else layers.spots.addTo(map);
}

/** Sharp 10 m pixels when zoomed in, smooth when zoomed out. */
function crisp() {
  const sharp = map.getZoom() >= 14;
  document.querySelectorAll('.leaflet-changemap-pane img').forEach((img) => {
    img.style.imageRendering = sharp ? 'pixelated' : 'auto';
  });
}

async function save(kind) {
  const r = state.result;
  if (!r) return;
  toast(kind === 'zip' || kind === 'png' ? 'Making the files…' : 'Saving…');
  try {
    const res = await fetch(exportUrl(r.key, kind, {
      preset: state.preset, background: state.background, watermark: state.watermark }));
    if (!res.ok) {
      let said = `${res.status}`;
      try { said = (await res.json()).detail ?? said; } catch { /* not JSON */ }
      throw new Error(said);
    }
    const blob = await res.blob();
    await savePicture(blob, fileName(r, kind), { title: r.label, what: 'Change map' });
  } catch (err) {
    toast(`Could not save: ${err.message}`, 'err');
  }
}

async function loadSaved() {
  try {
    state.saved = (await api.changeSaved()).saved ?? [];
  } catch { state.saved = []; }
  if (isOpen(WIN)) render();
}

async function forget(key) {
  try {
    state.saved = (await api.changeForget(key)).saved ?? [];
    if (state.result?.key === key) clearResult();
  } catch (err) {
    toast(`Could not delete: ${err.message}`, 'err');
  }
  render();
}

function clearResult() {
  state.result = null;
  drawResult();
  layers.outline?.remove();
}

// ── The panel ──────────────────────────────────────────────────

function section(n, title, ...children) {
  return el('div', { class: 'cm-step' },
    el('div', { class: 'cm-step-head' }, el('span', { class: 'cm-n' }, String(n)), title),
    ...children);
}

function render() {
  const host = $('#cmBody');
  if (!host) return;
  host.innerHTML = '';
  const presets = datePresets();
  const running = state.job?.state === 'running';

  host.append(
    el('p', { class: 'cm-intro' },
      'Where did the ground change between two dates? Sentinel-1 radar sees through cloud '
      + 'and at night; red is where its signal fell, cyan where it rose.'),

    section(1, 'Where',
      el('div', { class: 'cm-row' },
        el('button', { class: 'btn btn-ghost btn-sm', onclick: drawArea }, '▭ Draw a box'),
        store.aoi ? el('button', { class: 'btn btn-ghost btn-sm', onclick: useImageryArea },
          'Use my imagery area') : null),
      el('div', { class: 'cm-said' }, state.bounds
        ? `${state.label || 'Your area'} · ${areaSaid(state.bounds)}`
        : 'Drag a box on the map round the place to compare.')),

    section(2, 'When',
      el('div', { class: 'cm-chips' },
        ...Object.values(presets).map((p) => el('button', {
          class: `cm-chip${state.start === p.start && state.end === p.end ? ' is-on' : ''}`,
          onclick: () => { state.start = p.start; state.end = p.end; state.plan = null; render(); planSoon(); },
        }, p.label))),
      el('div', { class: 'cm-dates' },
        el('label', {}, 'Before', el('input', { type: 'date', value: state.start, max: state.end,
          onchange: (e) => { state.start = e.target.value; state.plan = null; render(); planSoon(); } })),
        el('span', { class: 'cm-arrow' }, '→'),
        el('label', {}, 'After', el('input', { type: 'date', value: state.end, max: isoDay(new Date()),
          onchange: (e) => { state.end = e.target.value; state.plan = null; render(); planSoon(); } })))),

    section(3, 'How careful',
      el('input', { type: 'range', min: '1', max: '20', value: String(state.images), class: 'cm-range',
        oninput: (e) => {
          state.images = Number(e.target.value);
          $('#cmImagesSaid').textContent = imagesSaid(state.images);
        },
        onchange: () => { state.plan = null; render(); planSoon(); } }),
      el('div', { class: 'cm-said', id: 'cmImagesSaid' }, imagesSaid(state.images))),

    planBox(),

    el('button', {
      class: 'btn btn-primary cm-go', disabled: !state.plan || running || state.planning,
      onclick: findChanges,
    }, running ? 'Making the change map…' : state.plan?.saved ? 'Open the change map' : 'Find changes'),
    el('div', { class: 'cm-progress', id: 'cmProgress', hidden: !running },
      el('div', { class: 'loop-track' }, el('div', { class: 'loop-fill', id: 'cmFill' })),
      el('div', { class: 'cm-said', id: 'cmMessage' }, '')),

    state.result ? resultBox() : null,
    savedBox(),
  );
  renderProgress();
}

function planBox() {
  if (!state.bounds) return null;
  if (state.planning) return el('div', { class: 'cm-plan' }, 'Checking which radar images there are…');
  if (state.planError) return el('div', { class: 'cm-plan is-bad' }, state.planError);
  if (!state.plan) return null;
  const said = planSaid(state.plan);
  return el('div', { class: 'cm-plan' },
    el('div', {}, said.before), el('div', {}, said.after),
    el('div', { class: 'cm-dim' }, said.size),
    ...state.plan.warnings.map((w) => el('div', { class: 'cm-warn' }, `⚠ ${w}`)));
}

function renderProgress() {
  const job = state.job;
  const box = $('#cmProgress');
  if (!box) return;
  box.hidden = job?.state !== 'running';
  if (!job) return;
  $('#cmFill').style.width = `${Math.round((job.fraction ?? 0) * 100)}%`;
  $('#cmMessage').textContent = job.message ?? '';
}

function resultBox() {
  const r = state.result;
  const s = r.summary;
  const noise = s.noise;
  const km = (v) => (v < 0.01 ? `${Math.round(v * 1e6).toLocaleString()} m²` : `${v.toFixed(2)} km²`);
  return el('div', { class: 'cm-result' },
    el('div', { class: 'cm-step-head' }, el('span', { class: 'cm-n cm-n-done' }, '✓'),
      `${r.label}: ${daySaid(r.params.start)} → ${daySaid(r.params.end)}`),
    el('div', { class: 'cm-dim' }, `Before ${r.before_said} · After ${r.after_said} · orbit ${r.orbit}`),
    el('div', { class: 'cm-stats' },
      el('div', { class: 'cm-stat' }, el('b', { style: `color:${r.colours.decrease}` }, km(s.decrease_km2)),
        el('span', {}, 'signal fell')),
      el('div', { class: 'cm-stat' }, el('b', { style: `color:${r.colours.increase}` }, km(s.increase_km2)),
        el('span', {}, 'signal rose')),
      el('div', { class: 'cm-stat' }, el('b', {}, s.spots.toLocaleString()),
        el('span', {}, `spot${s.spots === 1 ? '' : 's'}`))),
    el('div', { class: `cm-noise is-${noise.level}` }, noise.said),

    el('div', { class: 'cm-label' }, 'What counts as a change'),
    el('div', { class: 'cm-chips' },
      ...['Sensitive', 'Balanced', 'Strict'].map((name) => el('button', {
        class: `cm-chip${state.preset === name ? ' is-on' : ''}`, title: r.presets[name],
        onclick: () => setPreset(name),
      }, name))),
    el('div', { class: 'cm-dim' }, r.presets[state.preset] ?? ''),

    el('div', { class: 'cm-label' }, 'Show'),
    el('div', { class: 'cm-chips' },
      ...[['changes', 'Changes'], ['both', 'Changes on radar'], ['radar', 'Radar only']].map(([v, label]) =>
        el('button', { class: `cm-chip${state.view === v ? ' is-on' : ''}`,
          onclick: () => { state.view = v; applyView(); render(); } }, label))),
    state.view !== 'changes' ? el('div', { class: 'cm-blend' },
      el('span', {}, 'Before'),
      el('input', { type: 'range', min: '0', max: '100', value: String(Math.round(state.blend * 100)),
        oninput: (e) => { state.blend = Number(e.target.value) / 100; applyView(); } }),
      el('span', {}, 'After')) : null,

    r.spots.length ? el('div', { class: 'cm-label' },
      `Biggest spots (${r.spots_total.toLocaleString()} in all) — click to go there`) : null,
    el('div', { class: 'cm-spots' },
      ...r.spots.slice(0, 8).map((sp) => el('button', {
        class: 'cm-spot', onclick: () => map.flyTo([sp.lat, sp.lon], Math.max(map.getZoom(), 15)),
      },
      el('i', { style: `background:${sp.kind === 'decrease' ? r.colours.decrease : r.colours.increase}` }),
      `${sp.area_m2.toLocaleString()} m² · ${sp.change_db > 0 ? '+' : ''}${sp.change_db.toFixed(1)} dB`
        + (sp.times ? ` · changed ${sp.times}×` : '')))),

    el('div', { class: 'cm-label' }, 'Save'),
    el('div', { class: 'cm-saves' },
      ...SAVES.map(([kind, label, title]) => el('button', {
        class: `btn btn-ghost btn-sm${kind === 'zip' ? ' cm-save-all' : ''}`, title, onclick: () => save(kind),
      }, label))),
    el('div', { class: 'cm-row cm-opts' },
      el('label', {}, 'Picture over ',
        el('select', { onchange: (e) => { state.background = e.target.value; } },
          el('option', { value: 'satellite', selected: state.background === 'satellite' }, 'satellite photo'),
          el('option', { value: 'radar', selected: state.background === 'radar' }, 'radar image'))),
      el('label', {}, 'Mark ',
        el('input', { type: 'text', value: state.watermark, maxlength: '60', class: 'cm-mark',
          oninput: (e) => { state.watermark = e.target.value; } }))));
}

function savedBox() {
  if (!state.saved.length) return null;
  return el('details', { class: 'cm-saved', open: !state.result },
    el('summary', {}, `Saved change maps (${state.saved.length})`),
    ...state.saved.map((s) => el('div', { class: 'cm-saved-row' },
      el('button', { class: 'cm-saved-open', onclick: () => showResult(s.key) }, s.said),
      el('button', { class: 'cm-saved-x', title: 'Delete it', onclick: () => forget(s.key) }, '✕'))));
}
