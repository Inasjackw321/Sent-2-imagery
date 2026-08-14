// The whole app: find dates over an area, merge them, look at the imagery.

import { api } from './api.js';
import { store, emit, on, setImage } from './store.js';
import { $, $$, el, toast, withBusy, download, loadImage, fmt, sliderBank } from './ui.js';

let renderControls = {};
let enhanceControls = {};
let cloudControl = null;
let lastRequest = null;

const ENHANCE_SPECS = [
  { key: 'haze_removal', label: 'Haze removal', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? `${Math.round(v * 100)}%` : 'off') },
  { key: 'adaptive_contrast', label: 'Adaptive contrast', min: 0, max: 5, step: 0.25, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'sharpen', label: 'Detail', min: 0, max: 1.5, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'vibrance', label: 'Vibrance', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
  { key: 'white_balance', label: 'White balance', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? `${Math.round(v * 100)}%` : 'off') },
  { key: 'denoise', label: 'Denoise', min: 0, max: 1, step: 0.05, value: 0,
    format: (v) => (v ? v.toFixed(2) : 'off') },
];

// Starting points that suit different jobs.
const ENHANCE_PRESETS = {
  Off: {},
  Balanced: { haze_removal: 0.8, adaptive_contrast: 1.5, sharpen: 0.25, vibrance: 0.2 },
  Punchy: { haze_removal: 1, adaptive_contrast: 3, sharpen: 0.5, vibrance: 0.45 },
  Hazy_day: { haze_removal: 1, adaptive_contrast: 2, white_balance: 0.6, sharpen: 0.3 },
  Natural: { haze_removal: 0.5, white_balance: 0.35 },
};

export function initImagery() {
  const today = new Date();
  const yearAgo = new Date(today.getTime() - 365 * 864e5);
  $('#dateEnd').value = today.toISOString().slice(0, 10);
  $('#dateStart').value = yearAgo.toISOString().slice(0, 10);

  cloudControl = sliderBank($('#cloudSlider'), [{
    key: 'cloud', label: 'Maximum cloud cover', min: 0, max: 100, step: 5, value: 30, unit: '%',
  }]);

  buildVisualisationOptions();
  buildRenderSliders();
  buildEnhanceControls();

  $('#searchDates').addEventListener('click', runSearch);
  $('#showBtn').addEventListener('click', showImagery);
  $('#downloadPng').addEventListener('click', () => downloadImagery('png'));
  $('#downloadTif').addEventListener('click', () => downloadImagery('geotiff'));
  $('#renderMode').addEventListener('change', () => {
    buildRenderSliders();
    updateHint();
  });
  $('#renderSize').addEventListener('change', sync);
  $('#selectClearest').addEventListener('click', () => tickBest(6));
  $('#selectNone').addEventListener('click', () => tickBest(0));

  on('aoi', sync);
  on('dates', renderDateList);
  sync();
}

// ── What to show ───────────────────────────────────────────────

function buildVisualisationOptions() {
  const { composites, indices, satellite } = store.config;
  const compGroup = $('#optComposites');
  const idxGroup = $('#optIndices');
  compGroup.innerHTML = idxGroup.innerHTML = '';

  for (const [key, spec] of Object.entries(composites)) {
    compGroup.append(el('option', { value: `composite:${key}` }, spec.label));
  }
  for (const [key, spec] of Object.entries(indices)) {
    idxGroup.append(el('option', { value: `index:${key}` }, spec.label));
  }
  $('#renderMode').value = `composite:${satellite.default_composite}`;
  updateHint();
}

function currentMode() {
  const [mode, key] = $('#renderMode').value.split(':');
  return { mode, key };
}

function updateHint() {
  const { mode, key } = currentMode();
  const spec = mode === 'index' ? store.config.indices[key] : store.config.composites[key];
  if (!spec) { $('#renderHint').textContent = ''; return; }
  const detail = mode === 'index' ? spec.formula : `Bands: ${spec.band_labels.join(' · ')}`;
  $('#renderHint').innerHTML = `${spec.hint}<br><span style="opacity:.7">${detail}</span>`;
}

function buildRenderSliders() {
  const { mode, key } = currentMode();
  const host = $('#renderSliders');
  if (mode === 'index') {
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
  enhanceControls = sliderBank($('#enhanceSliders'), ENHANCE_SPECS);

  const host = $('#enhancePresets');
  host.innerHTML = '';
  for (const name of Object.keys(ENHANCE_PRESETS)) {
    host.append(el('button', {
      class: 'preset',
      onclick: (e) => {
        applyEnhancePreset(name);
        $$('#enhancePresets .preset').forEach((b) => b.classList.toggle('is-active', b === e.target));
      },
    }, name.replace('_', ' ')));
  }
  $('#enhanceReset').addEventListener('click', () => {
    applyEnhancePreset('Off');
    $$('#enhancePresets .preset').forEach((b) => b.classList.remove('is-active'));
  });
}

function applyEnhancePreset(name) {
  const preset = ENHANCE_PRESETS[name] ?? {};
  for (const spec of ENHANCE_SPECS) {
    enhanceControls[spec.key]?.set(preset[spec.key] ?? 0);
  }
}

function enhancementValues() {
  const values = {};
  for (const spec of ENHANCE_SPECS) {
    const value = enhanceControls[spec.key]?.get() ?? 0;
    if (value > 0) values[spec.key] = value;
  }
  return values;
}

// ── Merging ────────────────────────────────────────────────────

/** The dates ticked for merging, newest first. */
function mergeDates() {
  return store.dates.filter((d) => store.selected.has(d.id))
    .sort((a, b) => b.date.localeCompare(a.date));
}

/**
 * How much finer a merge of `count` dates will be sampled.
 *
 * The same rule the backend applies, so the panel can promise what the render
 * will actually deliver -- including the cap that stops a big area from asking
 * for more pixels than an image is allowed to have.
 */
function mergeScale(count) {
  if (count < 2) return 1;
  const steps = store.config.superres_steps ?? [[9, 4], [5, 3], [2, 2]];
  const wanted = steps.find(([needed]) => count >= needed)?.[1] ?? 1;
  const size = parseInt($('#renderSize').value, 10);
  const fits = Math.max(1, Math.floor((store.config.max_size ?? 4096) / size));
  return Math.min(wanted, fits);
}

function describeMerge() {
  const count = store.selected.size;
  const box = $('#mergeHint');
  if (count < 2) {
    box.innerHTML = 'Tick two or more dates and they are merged into one picture: '
      + 'sharper, and with the cloud taken out.';
    return;
  }
  const scale = mergeScale(count);
  const size = parseInt($('#renderSize').value, 10);
  const next = (store.config.superres_steps ?? []).find(([, s]) => s === scale + 1);
  const bits = [
    `<b>${count} dates → ${scale}× detail</b>, ${size * scale} px across.`,
    'Cloud in any one date is taken out by the others, and the sub-pixel '
    + 'differences between them are solved for detail no single date holds.',
  ];
  if (next && scale < (store.config.max_superres ?? 4)) {
    bits.push(`<span class="dim">${next[0]} dates would reach ${next[1]}×.</span>`);
  }
  box.innerHTML = bits.join(' ');
}

// ── Search ─────────────────────────────────────────────────────

async function runSearch() {
  if (!store.aoi) return;
  try {
    const data = await withBusy('Searching Sentinel-2…', () => api.search({
      aoi: store.aoi,
      start: $('#dateStart').value,
      end: $('#dateEnd').value,
      max_cloud: cloudControl.cloud.get(),
      limit: 60,
    }));
    store.dates = data.scenes;
    store.selected.clear();
    store.activeDateId = data.scenes[0]?.id ?? null;
    emit('dates', data.scenes);
    // Most people want the best few merged, so start them there.
    tickBest(Math.min(6, data.scenes.length));
    toast(data.scenes.length
      ? `${data.scenes.length} date${data.scenes.length === 1 ? '' : 's'} found`
      : 'Nothing found — widen the dates or allow more cloud',
    data.scenes.length ? 'ok' : '');
  } catch (err) {
    toast(`Search failed: ${err.message}`, 'err');
  }
}

/** Tick the `n` least cloudy dates, and untick everything else. */
function tickBest(n) {
  const best = [...store.dates]
    .sort((a, b) => (a.cloud ?? 100) - (b.cloud ?? 100))
    .slice(0, n)
    .map((d) => d.id);
  store.selected = new Set(best);
  renderDateList(store.dates);
  sync();
}

function renderDateList(dates) {
  const list = $('#dateList');
  list.innerHTML = '';
  for (const date of dates) {
    const check = el('input', {
      type: 'checkbox',
      checked: store.selected.has(date.id),
      onclick: (e) => {
        e.stopPropagation();
        if (e.target.checked) store.selected.add(date.id);
        else store.selected.delete(date.id);
        store.activeDateId = date.id;
        sync();
      },
    });
    list.append(el('div', {
      class: `scene ${store.selected.has(date.id) ? 'is-active' : ''}`,
      onclick: (e) => {
        if (e.target !== check) check.click();
      },
    },
      check,
      el('div', {},
        el('div', { class: 'scene-date' }, fmt.date(date.date)),
        el('div', { class: 'scene-meta' }, date.tile || date.platform)),
      el('div', { class: 'scene-cloud' },
        el('span', { class: 'cloud-dot', style: `background:${cloudColour(date.cloud ?? 0)}` }),
        `${date.cloud ?? 0}%`),
    ));
  }
}

const cloudColour = (pct) => (pct < 10 ? '#37e0a0' : pct < 30 ? '#ffd166' : '#ff8a5b');

// ── Showing the imagery ────────────────────────────────────────

function sync() {
  const hasAoi = Boolean(store.aoi);
  const count = store.selected.size;
  $('#searchDates').disabled = !hasAoi;
  $('#showBtn').disabled = !hasAoi || (!count && !store.activeDateId);
  $('#showBtn').textContent = count > 1
    ? `Merge ${count} dates → ${mergeScale(count)}×`
    : 'Show imagery';
  $('#downloadPng').disabled = $('#downloadTif').disabled = !lastRequest;
  $$('#dateList .scene').forEach((row, i) =>
    row.classList.toggle('is-active', store.selected.has(store.dates[i]?.id)));
  describeMerge();
}

function buildRequest(dates) {
  const { mode, key } = currentMode();
  const body = {
    aoi: store.aoi,
    scene: dates[0],
    mode,
    size: parseInt($('#renderSize').value, 10),
    mask_clouds: $('#maskClouds').checked,
    clip: $('#clipShape').checked,
    ...(renderControls.values ? renderControls.values() : {}),
    ...enhancementValues(),
  };
  if (dates.length > 1) body.scenes = dates;
  if (mode === 'index') {
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
  if (!store.aoi) return;
  const merged = mergeDates();
  const dates = merged.length > 1
    ? merged
    : [store.dates.find((d) => d.id === store.activeDateId) ?? merged[0] ?? store.dates[0]];
  if (!dates[0]) return;

  const body = buildRequest(dates);
  const what = dates.length > 1
    ? `Merging ${dates.length} dates…`
    : `Fetching ${fmt.date(dates[0].date)}…`;
  try {
    const data = await withBusy(what, () => api.render(body));
    await loadImage(data.image);          // decoded before the map is told
    lastRequest = body;
    setImage({ src: data.image, meta: data.meta });
    sync();
    toast(describeResult(data.meta), 'ok');
  } catch (err) {
    toast(`Could not show that: ${err.message}`, 'err');
  }
}

/** What came back, in one line: how big, how sharp, how clear. */
function describeResult(meta) {
  const size = `${meta.grid.width}×${meta.grid.height} px at ${meta.grid.ground_res_m} m/px`;
  const sr = meta.superres;
  if (!sr) return `${size} from ${fmt.date(meta.scene.date)}`;

  const bits = [`${sr.scale}× merge of ${sr.scenes} dates — ${size}`];
  if (sr.sharpness_gain_pct > 0) bits.push(`+${sr.sharpness_gain_pct}% detail`);
  else bits.push('no detail gained — try dates closer together');
  if (sr.noise_drop_pct > 0) bits.push(`${sr.noise_drop_pct}% less noise`);
  const clear = meta.composite_report?.combined_pct;
  if (clear != null) bits.push(`${clear}% clear`);
  return bits.join(' · ');
}

async function downloadImagery(format) {
  if (!lastRequest) return;
  try {
    const blob = await withBusy('Preparing download…', () =>
      api.renderFile({ ...lastRequest, format }));
    const ext = format === 'geotiff' ? 'tif' : 'png';
    const when = lastRequest.scenes?.length
      ? `${lastRequest.scenes.length}dates_${lastRequest.scene.date}`
      : lastRequest.scene.date;
    download(blob, `sentinel2_${when}_${lastRequest.index ?? lastRequest.preset}.${ext}`);
  } catch (err) {
    toast(`Download failed: ${err.message}`, 'err');
  }
}
