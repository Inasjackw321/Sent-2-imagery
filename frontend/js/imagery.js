// The whole app: pick a place, pick a satellite and dates, look at the imagery.
//
// Two satellites fly over the same ground and answer different questions.
// Sentinel-2 photographs it, in daylight, when there is no cloud in the way.
// Sentinel-1 measures it with radar, through cloud and at night. Either can be
// shown, and both can sit on the map at once.
//
// Then two ways to look at whichever one you picked, because they too are
// genuinely different questions. One date is what the satellite saw on a day.
// A merge is what the ground looks like, put together from several passes.

import { api } from './api.js';
import { store, on, setImage } from './store.js';
import { updateOverlay } from './map.js';
import * as adjust from './adjust.js';
import {
  $, $$, el, toast, withBusy, download, loadImage, fmt, sliderBank, debounce,
} from './ui.js';

let renderControls = {};
let enhanceControls = {};
let adjustControls = {};
let cloudControl = null;
let lastRequest = null;
let mode = 'single';                     // 'single' | 'merge'
let satFilter = 'sentinel-2';            // which satellites the dates come from
let satShown = 'sentinel-2';             // which one the next render is of

// The map never shows more than a screenful, so the live preview is built at
// this size however big the render is. Saving uses the full thing.
const PREVIEW_MAX = 1600;

const ENHANCE_SPECS = [
  // Haze and white balance are corrections to light. Radar is not light, so
  // these two are left out of its panel rather than left in doing nothing.
  { key: 'haze_removal', label: 'Haze removal', min: 0, max: 1, step: 0.05, value: 0,
    opticalOnly: true,
    format: (v) => (v ? `${Math.round(v * 100)}%` : 'off') },
  { key: 'adaptive_contrast', label: 'Adaptive contrast', min: 0, max: 5, step: 0.25, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'sharpen', label: 'Detail', min: 0, max: 1.5, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'vibrance', label: 'Vibrance', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'white_balance', label: 'White balance', min: 0, max: 1, step: 0.05, value: 0,
    opticalOnly: true,
    format: (v) => (v ? `${Math.round(v * 100)}%` : 'off') },
  { key: 'denoise', label: 'Denoise', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
];

// Photographic adjustments on the finished picture, live on the map.
const ADJUST_SPECS = [
  { key: 'contrast', label: 'Contrast', min: -60, max: 80, step: 1, value: 0 },
  { key: 'exposure', label: 'Exposure', min: -50, max: 50, step: 1, value: 0 },
  { key: 'saturation', label: 'Saturation', min: -100, max: 100, step: 1, value: 0 },
  { key: 'clarity', label: 'Clarity', min: 0, max: 100, step: 1, value: 0 },
  { key: 'highlights', label: 'Highlights', min: -60, max: 60, step: 1, value: 0 },
  { key: 'shadows', label: 'Shadows', min: -60, max: 60, step: 1, value: 0 },
  { key: 'temperature', label: 'Warmth', min: -60, max: 60, step: 1, value: 0 },
  { key: 'tint', label: 'Tint', min: -60, max: 60, step: 1, value: 0 },
  { key: 'gamma', label: 'Midtones', min: 0.6, max: 1.8, step: 0.02, value: 1,
    format: (v) => v.toFixed(2) },
  { key: 'vignette', label: 'Vignette', min: 0, max: 80, step: 1, value: 0 },
];

// Starting points that suit different jobs. Radar gets its own set: haze
// removal and white balance are corrections to light, and radar is not light.
const ENHANCE_PRESETS = {
  optical: {
    Off: {},
    Balanced: { haze_removal: 0.8, adaptive_contrast: 1.8, sharpen: 0.45, vibrance: 0.2 },
    Punchy: { haze_removal: 1, adaptive_contrast: 3, sharpen: 0.7, vibrance: 0.45 },
    Detail: { haze_removal: 0.9, adaptive_contrast: 2.5, sharpen: 0.9, vibrance: 0.15 },
    Hazy_day: { haze_removal: 1, adaptive_contrast: 2, white_balance: 0.6, sharpen: 0.3 },
    Natural: { haze_removal: 0.5, white_balance: 0.35 },
  },
  radar: {
    Off: {},
    Smooth: { denoise: 0.55, adaptive_contrast: 1.2 },
    Balanced: { denoise: 0.35, adaptive_contrast: 1.8, sharpen: 0.3 },
    Detail: { denoise: 0.15, adaptive_contrast: 2.5, sharpen: 0.6 },
  },
};

const DEFAULT_PRESET = { optical: 'Balanced', radar: 'Balanced' };

const MODE_HINTS = {
  optical: {
    single: 'One pass, exactly as the satellite recorded it. Pick the date you want.',
    merge: 'Several passes solved into one picture — finer than 10 m, and with the '
      + 'cloud from any one date taken out by the others. Tick the dates to use.',
  },
  radar: {
    single: 'One radar pass, exactly as it was measured. Cloud makes no difference to it.',
    merge: 'Several passes averaged. Radar speckle is different every pass, so '
      + 'averaging cancels it — the picture gets cleaner, not finer.',
  },
};

// The one-line promise on each mode card. Merging radar buys something real,
// but not the thing merging optical buys, and the card must not say otherwise.
const MODE_SUB = {
  optical: { single: 'A single pass, as taken', merge: 'Sharper, cloud removed' },
  radar: { single: 'A single pass, as measured', merge: 'Cleaner, speckle averaged' },
};

const SAT_HINTS = {
  'sentinel-2': 'Optical, 10 m, daylight only — and only where there is no cloud.',
  'sentinel-1': 'Radar, ~20 m, through cloud and at night. Brightness is roughness, '
    + 'not colour: still water is black, towns are white.',
  both: 'Every pass from both, newest first. A render uses one satellite; put the '
    + 'other on the map too and fade between them.',
};

/** The satellite the next render will be of, and its record from the config. */
export const activeSat = () => store.config.satellites[satShown]
  ?? store.config.satellites[store.config.default_satellite];

const isRadar = () => activeSat().kind === 'radar';
const family = () => (isRadar() ? 'radar' : 'optical');

export function initImagery() {
  satFilter = satShown = store.config.default_satellite ?? 'sentinel-2';

  const today = new Date();
  const yearAgo = new Date(today.getTime() - 365 * 864e5);
  $('#dateEnd').value = today.toISOString().slice(0, 10);
  $('#dateStart').value = yearAgo.toISOString().slice(0, 10);

  cloudControl = sliderBank($('#cloudSlider'), [{
    key: 'cloud', label: 'Maximum cloud cover', min: 0, max: 100, step: 5, value: 30, unit: '%',
  }], sync);

  buildSatellitePicker();
  buildVisualisationOptions();
  buildRenderSliders();
  buildEnhanceControls();
  buildAdjustControls();

  // Raw Sentinel-2 reads milky and blue-grey: the atmosphere is still in it.
  // Balanced takes that out and gives the colour back, which is what the
  // imagery should look like before anyone touches a slider.
  applyEnhancePreset(DEFAULT_PRESET[family()]);

  $$('#modePicker .mode').forEach((btn) =>
    btn.addEventListener('click', () => setMode(btn.dataset.mode)));
  $('#searchDates').addEventListener('click', runSearch);
  $('#showBtn').addEventListener('click', showImagery);
  $('#downloadPng').addEventListener('click', () => downloadImagery('png'));
  $('#downloadTif').addEventListener('click', () => downloadImagery('geotiff'));
  $('#renderMode').addEventListener('change', () => {
    buildRenderSliders();
    updateHint();
    sync();
  });
  $('#renderSize').addEventListener('change', sync);
  $('#maskClouds').addEventListener('change', sync);
  $('#selectClearest').addEventListener('click', () => tickBest(6));
  $('#selectNone').addEventListener('click', () => tickBest(0));
  for (const id of ['#dateStart', '#dateEnd']) $(id).addEventListener('change', sync);

  // Drawing an area is a clear enough request for the dates over it.
  on('aoi', (info) => {
    sync();
    if (info) runSearch();
  });
  // A new render is a new picture: the adjustments stay, and are re-applied.
  on('image', applyAdjustments);
  // A layer taken off the map changes what there is left to save or copy.
  on('layers', sync);
  setMode('single');
}

// ── Which satellite ────────────────────────────────────────────

function buildSatellitePicker() {
  const host = $('#satPicker');
  host.innerHTML = '';
  const keys = Object.keys(store.config.satellites);
  const options = [...keys.map((k) => ({ key: k, label: store.config.satellites[k].short })),
    { key: 'both', label: 'Both' }];

  for (const opt of options) {
    const dots = opt.key === 'both'
      ? keys.map((k) => el('span', { class: 'layer-dot',
        style: `background:${store.config.satellites[k].colour}` }))
      : [el('span', { class: 'layer-dot',
        style: `background:${store.config.satellites[opt.key].colour}` })];
    host.append(el('button', {
      class: 'sat', dataset: { sat: opt.key },
      onclick: () => setSatellite(opt.key),
    }, el('span', { class: 'sat-dots' }, ...dots), opt.label));
  }
  markSatellite();
}

function setSatellite(key) {
  if (key === satFilter) return;
  satFilter = key;
  // "Both" lists everything but a render is still of one satellite, so the
  // one on show only changes when the choice actually names one.
  if (key !== 'both') satShown = key;
  markSatellite();
  onSatelliteChanged();
  if (store.aoi) runSearch();
  else { store.dates = []; renderDateList(); sync(); }
}

function markSatellite() {
  $$('#satPicker .sat').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.sat === satFilter));
  $('#satHint').textContent = SAT_HINTS[satFilter] ?? '';
}

/** What each mode card promises, which depends on the satellite. */
function paintModeCards() {
  for (const card of $$('#modePicker .mode')) {
    card.querySelector('small').textContent = MODE_SUB[family()][card.dataset.mode];
  }
  $('#modeHint').textContent = MODE_HINTS[family()][mode];
}

/** Everything downstream of the satellite: what to show, and how to tune it. */
function onSatelliteChanged() {
  buildVisualisationOptions();
  buildRenderSliders();
  buildEnhanceControls();
  applyEnhancePreset(DEFAULT_PRESET[family()]);
  paintModeCards();
  // Cloud is a property of light. Filtering radar by it would be meaningless,
  // so the control says so rather than quietly doing nothing.
  $('#cloudSlider').classList.toggle('is-moot', satFilter === 'sentinel-1');
  updateHint();
  sync();
}

// ── One date, or several merged ────────────────────────────────

function setMode(next) {
  mode = next;
  $$('#modePicker .mode').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === next));
  paintModeCards();
  $('#tickRow').hidden = next !== 'merge' || !store.dates.length;

  if (next === 'merge' && store.selected.size < 2) {
    tickBest(Math.min(6, store.dates.length));
  } else if (next === 'single') {
    // Carry the choice over: the clearest ticked date becomes the single one.
    const kept = store.dates.filter((d) => store.selected.has(d.id))
      .sort((a, b) => (a.cloud ?? 100) - (b.cloud ?? 100))[0];
    store.activeDateId = kept?.id ?? store.activeDateId ?? store.dates[0]?.id ?? null;
    store.selected = new Set(store.activeDateId ? [store.activeDateId] : []);
    renderDateList();
  }
  sync();
}

/** The dates this render will use, newest first. */
function chosenDates() {
  if (mode === 'single') {
    const one = store.dates.find((d) => d.id === store.activeDateId);
    return one ? [one] : [];
  }
  return store.dates.filter((d) => store.selected.has(d.id))
    .sort((a, b) => b.date.localeCompare(a.date));
}

const satOf = (date) => date?.satellite ?? store.config.default_satellite;

/**
 * Follow the dates: whichever satellite took them is the one being shown.
 *
 * With both satellites listed, picking a radar date is itself the choice of
 * radar, and everything downstream -- the visualisations on offer, the tuning
 * presets, what a merge is even for -- has to follow it.
 */
function useSatellite(key) {
  if (!key || key === satShown) return;
  satShown = key;
  onSatelliteChanged();
}

/**
 * What a merge of `count` dates would resolve at the current size.
 *
 * The same reasoning the backend applies, so the panel promises what the
 * render will actually deliver. A merge does not make the picture bigger; it
 * makes the size you asked for real. What it can resolve is limited by two
 * things at once: how many dates there are, and whether the output grid is
 * even finer than the satellite's own resolution -- there is nothing hiding
 * between the samples if a pixel covers more ground than one of them.
 *
 * Radar never sharpens. Sentinel-1 is delivered on a grid finer than it can
 * resolve, so there is nothing between its samples to solve for; merging its
 * passes averages the speckle out instead, which is worth just as much.
 */
function mergePlan(count) {
  const sat = activeSat();
  const native = sat.resolution;
  const extent = store.aoiInfo?.extent_m;
  const metresPerPixel = extent ? Math.max(...extent) / renderSize() : native;
  const over = native / metresPerPixel;

  const steps = store.config.superres_steps ?? [[9, 4], [5, 3], [2, 2]];
  const supported = count >= 2 && sat.can_superres
    ? (steps.find(([needed]) => count >= needed)?.[1] ?? 1) : 1;
  const sharpening = count >= 2 && supported > 1 && over > 1.2;
  return {
    sharpening,
    supported,
    metresPerPixel,
    despeckling: count >= 2 && !sat.can_superres,
    resolves: sharpening ? Math.min(over, supported) : 1,
    // What raising the size would buy, when the grid is the limit.
    sizeLimited: count >= 2 && supported > 1 && over < supported,
  };
}

const renderSize = () => parseInt($('#renderSize').value, 10);

// ── What to show ───────────────────────────────────────────────

/** Only what this satellite's bands can actually make. */
function buildVisualisationOptions() {
  const { composites, indices } = store.config;
  const sat = activeSat();
  const wanted = $('#renderMode').value;
  const compGroup = $('#optComposites');
  const idxGroup = $('#optIndices');
  compGroup.innerHTML = idxGroup.innerHTML = '';

  for (const [key, spec] of Object.entries(composites)) {
    if (spec.sat !== sat.key) continue;
    compGroup.append(el('option', { value: `composite:${key}` }, spec.label));
  }
  for (const [key, spec] of Object.entries(indices)) {
    if (spec.sat !== sat.key) continue;
    idxGroup.append(el('option', { value: `index:${key}` }, spec.label));
  }
  idxGroup.hidden = !idxGroup.children.length;

  // Keep the choice across a satellite change where it still exists; fall back
  // to the satellite's own default rather than to whatever sorts first.
  const select = $('#renderMode');
  select.value = wanted;
  if (!select.value) select.value = `composite:${sat.default_composite}`;
  updateHint();
}

function currentMode() {
  const [kind, key] = $('#renderMode').value.split(':');
  return { kind, key };
}

function updateHint() {
  const { kind, key } = currentMode();
  const spec = kind === 'index' ? store.config.indices[key] : store.config.composites[key];
  if (!spec) { $('#renderHint').textContent = ''; return; }
  const detail = kind === 'index' ? spec.formula : `Bands: ${spec.band_labels.join(' · ')}`;
  $('#renderHint').innerHTML = `${spec.hint}<br><span style="opacity:.7">${detail}</span>`;
}

function buildRenderSliders() {
  const { kind, key } = currentMode();
  const host = $('#renderSliders');
  if (kind === 'index') {
    const [lo, hi] = store.config.indices[key].range;
    renderControls = sliderBank(host, [
      { key: 'index_min', label: 'Scale minimum', min: -1, max: 1, step: 0.05, value: lo,
        format: (v) => v.toFixed(2) },
      { key: 'index_max', label: 'Scale maximum', min: -1, max: 1, step: 0.05, value: hi,
        format: (v) => v.toFixed(2) },
    ]);
    const sel = el('select', {},
      ...Object.keys(store.config.colormaps).map((name) =>
        el('option', { value: name, selected: name === store.config.indices[key].colormap }, name)));
    host.append(el('label', { class: 'field' }, 'Colour scheme', sel));
    renderControls.colormap = { get: () => sel.value };
  } else {
    renderControls = sliderBank(host, [
      { key: 'gamma', label: 'Gamma', min: 0.5, max: 2.5, step: 0.05, value: 1.15,
        format: (v) => v.toFixed(2) },
      { key: 'stretch_low', label: 'Shadow clip', min: 0, max: 20, step: 0.5, value: 2, unit: '%' },
      { key: 'stretch_high', label: 'Highlight clip', min: 80, max: 100, step: 0.5, value: 98, unit: '%' },
    ]);
    const sel = el('select', {},
      el('option', { value: '' }, 'Preset default'),
      el('option', { value: 'fixed' }, 'Natural (fixed reflectance)'),
      el('option', { value: 'percentile_linked' }, 'Balanced contrast'),
      el('option', { value: 'percentile' }, 'Maximum contrast'),
      el('option', { value: 'minmax' }, 'Full range'));
    host.append(el('label', { class: 'field' }, 'Tone mapping', sel));
    renderControls.stretch = { get: () => sel.value };
  }
}

function buildEnhanceControls() {
  const specs = ENHANCE_SPECS.filter((s) => !isRadar() || !s.opticalOnly);
  enhanceControls = sliderBank($('#enhanceSliders'), specs, sync);

  const host = $('#enhancePresets');
  host.innerHTML = '';
  for (const name of Object.keys(ENHANCE_PRESETS[family()])) {
    host.append(el('button', {
      class: 'preset',
      onclick: (e) => {
        applyEnhancePreset(name);
        $$('#enhancePresets .preset').forEach((b) => b.classList.toggle('is-active', b === e.target));
      },
    }, name.replace('_', ' ')));
  }
  $('#enhanceReset').onclick = () => {
    applyEnhancePreset('Off');
    $$('#enhancePresets .preset').forEach((b) => b.classList.remove('is-active'));
  };
}

function applyEnhancePreset(name) {
  const preset = ENHANCE_PRESETS[family()][name] ?? {};
  for (const spec of ENHANCE_SPECS) {
    enhanceControls[spec.key]?.set(preset[spec.key] ?? 0);
  }
  $$('#enhancePresets .preset').forEach((b) =>
    b.classList.toggle('is-active', b.textContent === name.replace('_', ' ')));
  sync();
}

// ── Adjusting the picture on screen ────────────────────────────

function buildAdjustControls() {
  adjustControls = sliderBank($('#adjustSliders'), ADJUST_SPECS, queueAdjust);

  const host = $('#adjustPresets');
  host.innerHTML = '';
  for (const name of Object.keys(adjust.PRESETS)) {
    host.append(el('button', {
      class: 'preset',
      onclick: (e) => {
        setAdjustments(adjust.PRESETS[name]);
        $$('#adjustPresets .preset').forEach((b) => b.classList.toggle('is-active', b === e.target));
      },
    }, name));
  }
  $('#adjustReset').addEventListener('click', () => {
    setAdjustments({});
    $$('#adjustPresets .preset').forEach((b) => b.classList.remove('is-active'));
  });
}

function setAdjustments(values) {
  const full = adjust.withDefaults(values);
  for (const spec of ADJUST_SPECS) adjustControls[spec.key]?.set(full[spec.key]);
  applyAdjustments();
  sync();
}

function adjustmentValues() {
  const out = {};
  for (const spec of ADJUST_SPECS) out[spec.key] = adjustControls[spec.key]?.get() ?? spec.value;
  return out;
}

/**
 * Push the current adjustments onto the imagery on the map.
 *
 * The overlay's pixels are swapped in place rather than the overlay replaced,
 * so the map does not move and the fade slider keeps its setting.
 */
function applyAdjustments() {
  const source = store.image?.element;
  const values = adjustmentValues();
  // Kept where anything else that renders the picture can find them -- copying
  // a region has to come out looking like what is on the screen.
  store.adjustments = values;
  if (!source) return;
  if (adjust.isNeutral(values)) {
    updateOverlay(store.image.src);
  } else {
    updateOverlay(adjust.renderToCanvas(source, values, { maxSide: PREVIEW_MAX }).toDataURL());
  }
  sync();
}

// Dragging a slider fires continuously; rebuilding the picture every time
// would make it lag behind the thumb.
const queueAdjust = debounce(applyAdjustments, 90);

function enhancementValues() {
  const values = {};
  for (const spec of ENHANCE_SPECS) {
    const value = enhanceControls[spec.key]?.get() ?? 0;
    if (value > 0) values[spec.key] = value;
  }
  return values;
}

// ── Dates ──────────────────────────────────────────────────────

async function runSearch() {
  if (!store.aoi) return;
  const who = satFilter === 'both' ? 'Sentinel' : activeSat().short;
  try {
    const data = await withBusy(`Looking for ${who} passes…`, () => api.search({
      aoi: store.aoi,
      start: $('#dateStart').value,
      end: $('#dateEnd').value,
      max_cloud: cloudControl.cloud.get(),
      satellites: satFilter === 'both' ? null : [satFilter],
      limit: 60,
    }));
    store.dates = data.scenes;
    store.selected.clear();
    store.activeDateId = data.scenes[0]?.id ?? null;

    if (!data.scenes.length) {
      renderDateList();
      sync();
      toast('No passes matched — widen the dates or allow more cloud');
      return;
    }
    // Land on something sensible: the clearest date, or the best few to merge.
    if (mode === 'merge') tickBest(Math.min(6, data.scenes.length));
    else {
      store.activeDateId = [...data.scenes]
        .sort((a, b) => (a.cloud ?? 100) - (b.cloud ?? 100))[0].id;
      store.selected = new Set([store.activeDateId]);
      renderDateList();
      sync();
    }
    toast(`${data.scenes.length} date${data.scenes.length === 1 ? '' : 's'} found`, 'ok');
  } catch (err) {
    toast(`Search failed: ${err.message}`, 'err');
  }
}

/**
 * Tick the `n` best dates, and untick everything else.
 *
 * Best means least cloudy, and for radar it means most recent, because radar
 * has no cloud to be troubled by. Only one satellite's dates are ever ticked:
 * an optical picture and a radar measurement are different quantities and
 * cannot be merged into one another.
 */
function tickBest(n) {
  const mine = store.dates.filter((d) => satOf(d) === satShown);
  const order = isRadar()
    ? [...mine].sort((a, b) => b.date.localeCompare(a.date))
    : [...mine].sort((a, b) => (a.cloud ?? 100) - (b.cloud ?? 100));
  store.selected = new Set(order.slice(0, n).map((d) => d.id));
  renderDateList();
  sync();
}

function renderDateList() {
  const list = $('#dateList');
  list.innerHTML = '';
  for (const date of store.dates) {
    const chosen = mode === 'single'
      ? date.id === store.activeDateId
      : store.selected.has(date.id);
    const input = el('input', {
      type: mode === 'single' ? 'radio' : 'checkbox',
      name: 'date',
      checked: chosen,
      onclick: (e) => {
        e.stopPropagation();
        pick(date, e.target.checked);
      },
    });
    const spec = store.config.satellites[satOf(date)];
    // Radar records no cloud cover, so it gets the thing that is true of it
    // instead: the pass sees the ground whatever the weather was doing.
    const cloud = spec.cloud_filter
      ? el('div', { class: 'scene-cloud' },
        el('span', { class: 'cloud-dot', style: `background:${cloudColour(date.cloud ?? 0)}` }),
        `${date.cloud ?? 0}%`)
      : el('div', { class: 'scene-cloud is-radar', title: 'Radar is unaffected by cloud' }, 'radar');

    list.append(el('div', {
      class: `scene ${chosen ? 'is-active' : ''}`,
      onclick: () => pick(date, mode === 'single' ? true : !store.selected.has(date.id)),
    },
      input,
      el('div', {},
        el('div', { class: 'scene-date' },
          el('span', { class: 'scene-sat', style: `background:${spec.colour}` }),
          fmt.date(date.date)),
        el('div', { class: 'scene-meta' }, date.tile || date.platform)),
      cloud,
    ));
  }
  $('#dateEmpty').hidden = store.dates.length > 0 || !store.aoi;
  $('#tickRow').hidden = mode !== 'merge' || !store.dates.length;
  // The clearest date is rarely the newest, so it is rarely at the top: show
  // the user what has been picked for them rather than leaving it off-screen.
  list.querySelector('.scene.is-active')?.scrollIntoView({ block: 'nearest' });
}

function pick(date, on) {
  const switching = satOf(date) !== satShown;
  useSatellite(satOf(date));

  if (mode === 'single') {
    store.activeDateId = date.id;
    store.selected = new Set([date.id]);
  } else if (switching) {
    // The ticks belonged to the other satellite, and a merge cannot span both.
    store.selected = new Set([date.id]);
    toast(`Merging ${activeSat().short} now — the other satellite's ticks were cleared`);
  } else if (on) {
    store.selected.add(date.id);
  } else {
    store.selected.delete(date.id);
  }
  renderDateList();
  sync();
}

const cloudColour = (pct) => (pct < 10 ? '#37e0a0' : pct < 30 ? '#ffd166' : '#ff8a5b');

// ── Keeping the panel honest ───────────────────────────────────

function sync() {
  const hasAoi = Boolean(store.aoi);
  const dates = chosenDates();

  $('#stepDates').classList.toggle('is-locked', !hasAoi);
  $('#stepLook').classList.toggle('is-locked', !store.dates.length);
  $('#stepAdjust').classList.toggle('is-locked', !store.image);
  $('#searchDates').disabled = !hasAoi;
  $('#dateEmpty').hidden = store.dates.length > 0 || !hasAoi;
  $('#whenSummary').textContent =
    `${$('#dateStart').value} → ${$('#dateEnd').value} · under ${cloudControl.cloud.get()}% cloud`;

  const tuned = Object.keys(enhancementValues()).length;
  $('#tuningSummary').textContent = tuned ? `${tuned} adjusted` : 'off';
  $('#adjustReset').disabled = adjust.isNeutral(adjustmentValues());

  const plan = mergePlan(dates.length);
  const ready = mode === 'merge' ? dates.length > 1 : dates.length === 1;
  const current = ready && lastRequest
    && JSON.stringify(buildRequest(dates)) === JSON.stringify(lastRequest);

  $('#showBtn').disabled = !ready || current;
  $('#showBtn').textContent = current
    ? 'Showing this now'
    : mode === 'merge'
      ? (ready
        ? (plan.sharpening
          ? `Merge ${dates.length} dates → ${plan.resolves.toFixed(1)}× finer`
          : plan.despeckling
            ? `Average ${dates.length} radar passes`
            : `Merge ${dates.length} dates`)
        : 'Tick at least two dates')
      : (ready ? (store.image ? 'Show this date' : 'Show imagery') : 'Pick a date');
  $('#planSummary').innerHTML = planSummary(dates, plan);
  $('#downloadPng').disabled = $('#downloadTif').disabled = !lastRequest;
  $('#copyRegion').disabled = $('#saveRegion').disabled = !store.image;
}

/** One line above the button saying exactly what pressing it will produce. */
function planSummary(dates, plan) {
  if (!store.aoi) return 'Draw an area on the map to begin.';
  if (!store.dates.length) return 'No dates yet.';
  if (!dates.length) {
    return mode === 'merge' ? 'Tick the dates you want merged.' : 'Pick a date.';
  }
  const sat = activeSat();
  const native = sat.resolution;
  const px = `${renderSize()} px`;
  if (mode === 'single') {
    return `<b>${fmt.date(dates[0].date)}</b> · ${sat.short} · ${px} · `
      + `one pass, ${native} m detail`;
  }
  if (dates.length < 2) return 'A merge needs at least two dates.';

  // Radar cannot be sharpened by merging, but averaging its speckle away is
  // worth as much, so say what it does buy rather than what it cannot.
  if (plan.despeckling) {
    const drop = Math.round((1 - 1 / Math.sqrt(dates.length)) * 100);
    return `<b>${dates.length} radar passes</b> · ${px} · about ${drop}% less speckle, `
      + `still ${native} m detail`;
  }

  // The grid is coarser than the satellite sampled it, so there is nothing
  // finer to find -- but a bigger size would change that, so say so.
  if (!plan.sharpening) {
    return `<b>${dates.length} dates</b> · ${px} · cloud cleared, but a pixel here `
      + `covers ${plan.metresPerPixel.toFixed(0)} m of ground · `
      + '<span class="dim">raise the size for finer detail</span>';
  }
  const detail = `~${(native / plan.resolves).toFixed(1)} m detail`;
  const more = plan.sizeLimited
    ? ' · <span class="dim">a larger size would resolve more</span>'
    : `${(store.config.superres_steps ?? []).find(([, s]) => s === plan.supported + 1)
      ? ` · <span class="dim">more dates would resolve more</span>` : ''}`;
  return `<b>${dates.length} dates → ${detail}</b> · ${px}, against ${native} m `
    + `from one date${more}`;
}

// ── Showing the imagery ────────────────────────────────────────

function buildRequest(dates) {
  const { kind, key } = currentMode();
  const body = {
    aoi: store.aoi,
    scene: dates[0],
    satellite: satOf(dates[0]),
    mode: kind,
    size: renderSize(),
    mask_clouds: $('#maskClouds').checked,
    clip: $('#clipShape').checked,
    ...(renderControls.values ? renderControls.values() : {}),
    ...enhancementValues(),
  };
  if (dates.length > 1) body.scenes = dates;
  if (kind === 'index') {
    body.index = key;
    body.colormap = renderControls.colormap?.get();
  } else {
    body.preset = key;
    const stretch = renderControls.stretch?.get();
    if (stretch) body.stretch = stretch;
  }
  return body;
}

async function showImagery() {
  const dates = chosenDates();
  if (!store.aoi || !dates.length) return;

  const body = buildRequest(dates);
  const what = dates.length > 1
    ? `Merging ${dates.length} dates…`
    : `Fetching ${fmt.date(dates[0].date)}…`;
  try {
    const data = await withBusy(what, () => api.render(body));
    const element = await loadImage(data.image);   // decoded before the map is told
    lastRequest = body;
    setImage({ src: data.image, meta: data.meta, element });
    sync();
    toast(describeResult(data.meta), 'ok');
  } catch (err) {
    toast(`Could not show that: ${err.message}`, 'err');
  }
}

/**
 * What came back, in one line: how big, how sharp, how clear.
 *
 * Pixel size is deliberately not the headline. A small area rendered at 2048 px
 * has half-metre pixels and still cannot show anything Sentinel-2 did not see,
 * so what gets reported is the resolution the imagery actually has.
 */
function describeResult(meta) {
  const size = `${meta.grid.width}×${meta.grid.height} px · ~${meta.effective_res_m} m detail`;
  const sr = meta.superres;
  if (!sr && meta.scenes?.length > 1 && meta.source?.kind === 'radar') {
    return `${meta.scenes.length} radar passes averaged — ${size}`;
  }
  if (!sr) return `${meta.source?.short ?? ''} ${fmt.date(meta.scene.date)} — ${size}`.trim();

  const bits = [`${sr.scale}× merge of ${sr.scenes} dates — ${size}`];
  if (sr.sharpness_gain_pct > 0) bits.push(`+${sr.sharpness_gain_pct}% sharper`);
  else bits.push('no detail gained — try dates closer together');
  if (sr.noise_drop_pct > 0) bits.push(`${sr.noise_drop_pct}% less noise`);
  const clear = meta.composite_report?.combined_pct;
  if (clear != null) bits.push(`${clear}% clear`);
  return bits.join(' · ');
}

async function downloadImagery(format) {
  if (!lastRequest) return;
  const ext = format === 'geotiff' ? 'tif' : 'png';
  const when = lastRequest.scenes?.length
    ? `${lastRequest.scenes.length}dates_${lastRequest.scene.date}`
    : lastRequest.scene.date;
  const who = (lastRequest.satellite ?? 'sentinel-2').replace('-', '');
  const name = `${who}_${when}_${lastRequest.index ?? lastRequest.preset}.${ext}`;

  try {
    // The PNG is what you are looking at, at full size, adjustments included.
    // The GeoTIFF is the measurement, so it goes out as the satellite had it.
    if (format === 'png' && store.image?.element) {
      const canvas = adjust.renderToCanvas(store.image.element, adjustmentValues());
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
      download(blob, name);
      return;
    }
    const blob = await withBusy('Preparing the file…', () =>
      api.renderFile({ ...lastRequest, format }));
    download(blob, name);
  } catch (err) {
    toast(`Download failed: ${err.message}`, 'err');
  }
}
