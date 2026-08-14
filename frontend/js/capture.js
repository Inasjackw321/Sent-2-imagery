// Capture tab: scene search, visualisation options, rendering and change detection.

import { api } from './api.js';
import { store, emit, on, addCapture } from './store.js';
import { $, $$, el, toast, withBusy, download, loadImage, fmt, sliderBank } from './ui.js';

let renderControls = {};
let changeControls = {};
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
  Radar: { denoise: 0.8, adaptive_contrast: 2, sharpen: 0.2 },
  Natural: { haze_removal: 0.5, white_balance: 0.35 },
};

export function initCapture() {
  const today = new Date();
  const yearAgo = new Date(today.getTime() - 365 * 864e5);
  $('#dateEnd').value = today.toISOString().slice(0, 10);
  $('#dateStart').value = yearAgo.toISOString().slice(0, 10);

  cloudControl = sliderBank($('#cloudSlider'), [{
    key: 'cloud', label: 'Maximum cloud cover', min: 0, max: 100, step: 5, value: 30, unit: '%',
  }]);

  buildSatellitePicker();
  buildVisualisationOptions();
  buildRenderSliders();
  buildEnhanceControls();

  changeControls = sliderBank($('#changeSliders'), [
    { key: 'threshold', label: 'Change threshold', min: 0.02, max: 0.5, step: 0.01, value: 0.1,
      format: (v) => `±${v.toFixed(2)}` },
    { key: 'diff_limit', label: 'Colour scale limit', min: 0.1, max: 1, step: 0.05, value: 0.4,
      format: (v) => `±${v.toFixed(2)}` },
  ]);
  $('#changeBtn').addEventListener('click', runChange);
  $('#searchScenes').addEventListener('click', runSearch);
  $('#renderBtn').addEventListener('click', () => runRender());
  $('#downloadPng').addEventListener('click', () => downloadRender('png'));
  $('#downloadTif').addEventListener('click', () => downloadRender('geotiff'));
  $('#renderMode').addEventListener('change', () => {
    buildRenderSliders();
    updateHint();
  });
  $('#renderSize').addEventListener('change', syncEnabled);
  $('#satellite').addEventListener('change', () => selectSource($('#satellite').value));
  $('#useComposite').addEventListener('change', (e) => {
    $('#compositeMethodRow').hidden = !e.target.checked;
    // Compositing removes cloud by dropping the cloudy pixels first, so it is
    // pointless without the mask. Turn it on rather than quietly doing nothing.
    if (e.target.checked && currentSource().has_cloud_mask && !$('#maskClouds').checked) {
      $('#maskClouds').checked = true;
      toast('Cloud masking switched on — that is what the composite merges around');
    }
    syncEnabled();
  });
  $('#superres').addEventListener('change', (e) => {
    // Super-resolution has nothing to fuse without several dates, so it turns
    // the composite on rather than quietly doing nothing.
    if (e.target.checked && !$('#useComposite').checked) {
      $('#useComposite').checked = true;
      $('#useComposite').dispatchEvent(new Event('change'));
    }
    syncEnabled();
  });
  $('#superresScale').addEventListener('change', syncEnabled);
  on('selection', syncEnabled);

  on('aoi', syncEnabled);
  on('scenes', renderSceneList);
  syncEnabled();
}

function currentSource() {
  return store.config.sources.find((s) => s.key === store.source)
    ?? store.config.sources[0];
}

function buildSatellitePicker() {
  const select = $('#satellite');
  select.innerHTML = '';
  const groups = new Map();
  for (const source of store.config.sources) {
    const kind = { optical: 'Optical', sar: 'Radar', dem: 'Terrain',
                   landcover: 'Land cover', thermal: 'Thermal' }[source.kind] ?? 'Other';
    if (!groups.has(kind)) groups.set(kind, el('optgroup', { label: kind }));
    groups.get(kind).append(el('option', { value: source.key }, source.label));
  }
  for (const group of groups.values()) select.append(group);

  store.source = store.config.default_source;
  select.value = store.source;
  describeSource();
}

function selectSource(key) {
  store.source = key;
  const source = currentSource();
  describeSource();
  buildVisualisationOptions();
  buildRenderSliders();
  updateHint();

  // A different satellite means different scenes.
  store.scenes = [];
  store.selected.clear();
  store.activeSceneId = null;
  emit('scenes', []);

  $('#renderSize').value = String(source.default_size || 1024);
  $('#pansharpenRow').hidden = !source.pan;
  $('#panPill').textContent = source.pan_resolution
    ? `${source.resolution} m → ${source.pan_resolution} m` : '';
  $('#maskClouds').disabled = !source.has_cloud_mask;
  $('#maskClouds').parentElement.style.opacity = source.has_cloud_mask ? '1' : '.45';
  syncEnabled();
}

function describeSource() {
  const source = currentSource();
  const bits = [
    `${source.resolution} m`,
    source.revisit,
    `from ${source.since.slice(0, 4)}`,
  ].filter(Boolean);
  $('#satelliteInfo').innerHTML = `
    <div><b>${source.platform}</b> · ${bits.join(' · ')}</div>
    ${source.swath_hint ? `<div class="dim">${source.swath_hint}</div>` : ''}
    ${source.notes ? `<div class="dim">${source.notes}</div>` : ''}
    <div class="dim">via ${source.provider}</div>`;
}

function buildVisualisationOptions() {
  const { composites, indices } = store.config;
  const source = currentSource();
  const compGroup = $('#optComposites');
  const idxGroup = $('#optIndices');
  const changeSel = $('#changeIndex');
  compGroup.innerHTML = idxGroup.innerHTML = changeSel.innerHTML = '';

  // Only offer what this satellite actually carries the bands for.
  for (const key of source.composites) {
    compGroup.append(el('option', { value: `composite:${key}` }, composites[key].label));
  }
  for (const key of source.indices) {
    idxGroup.append(el('option', { value: `index:${key}` }, indices[key].label));
    changeSel.append(el('option', { value: key }, indices[key].label));
  }
  compGroup.hidden = !source.composites.length;
  idxGroup.hidden = !source.indices.length;

  if (source.categorical) {
    compGroup.append(el('option', { value: 'categorical:landcover' }, 'Land-cover classes'));
    compGroup.hidden = false;
  }
  const preferred = source.categorical ? 'categorical:landcover'
    : source.default_composite ? `composite:${source.default_composite}`
      : `index:${source.indices[0]}`;
  $('#renderMode').value = preferred;
  updateHint();
}

function currentMode() {
  const [mode, key] = $('#renderMode').value.split(':');
  return { mode, key };
}

function updateHint() {
  const { mode, key } = currentMode();
  if (mode === 'categorical') {
    $('#renderHint').textContent = 'Eleven land-cover classes, with the area of each.';
    return;
  }
  const spec = mode === 'index' ? store.config.indices[key] : store.config.composites[key];
  if (!spec) { $('#renderHint').textContent = ''; return; }
  const detail = mode === 'index'
    ? `${spec.formula}`
    : `Bands: ${spec.band_labels.join(' · ')}`;
  $('#renderHint').innerHTML = `${spec.hint}<br><span style="opacity:.7">${detail}</span>`;
}

function buildRenderSliders() {
  const { mode, key } = currentMode();
  const host = $('#renderSliders');
  if (mode === 'categorical') {
    host.innerHTML = '';
    renderControls = {};
    return;
  }
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
  if ($('#pansharpen').checked && currentSource().pan) values.pansharpen = 1.0;
  return values;
}

/** The super-resolution factor asked for, once it is actually usable. */
function superresScale() {
  if (!$('#superres').checked || !$('#useComposite').checked) return 1;
  if (store.selected.size < 2) return 1;
  const wanted = parseInt($('#superresScale').value, 10);
  const size = parseInt($('#renderSize').value, 10);
  const max = store.config.max_size ?? 4096;
  const capped = Math.min(wanted, store.config.max_superres ?? 4,
    Math.max(1, Math.floor(max / size)));
  return capped;
}

function describeSuperres() {
  const on = $('#superres').checked;
  $('#superresScaleRow').hidden = !on;
  $('#superresHint').hidden = !on;
  if (!on) return;

  const wanted = parseInt($('#superresScale').value, 10);
  const scale = superresScale();
  const size = parseInt($('#renderSize').value, 10);
  const dates = store.selected.size;
  const needed = wanted * wanted;
  if (dates < 2 || !$('#useComposite').checked) {
    $('#superresHint').textContent =
      'Tick at least two dates above — the detail comes from the differences between them.';
    return;
  }
  const bits = [];
  if (scale < wanted) {
    bits.push(`Capped at ${scale}× — ${wanted}× of ${size} px would pass the `
      + `${store.config.max_size ?? 4096} px limit.`);
  }
  bits.push(dates >= needed
    ? `${dates} dates ticked — enough to fill a ${wanted}× finer grid.`
    : `${dates} of the ${needed} dates a ${wanted}× grid wants. Fewer still sharpens, `
      + 'with less to work from.');
  bits.push(`Output ${size * scale} px. Each date is read onto the fine grid, `
    + 'aligned to a fraction of a pixel, then fused. Dates close together work '
    + 'best — the ground has to still be the same ground.');
  $('#superresHint').innerHTML = bits.join('<br>');
}

function syncEnabled() {
  const hasAoi = Boolean(store.aoi);
  $('#searchScenes').disabled = !hasAoi;
  const compositing = $('#useComposite').checked;
  $('#renderBtn').disabled = !hasAoi
    || (compositing ? store.selected.size < 2 : !store.activeSceneId);
  const scale = superresScale();
  $('#renderBtn').textContent = scale > 1
    ? `Fuse ${store.selected.size} scenes at ${scale}×`
    : compositing
      ? `Build composite of ${store.selected.size} scenes`
      : 'Download imagery';
  describeSuperres();
  $('#changeBtn').disabled = !hasAoi || store.selected.size !== 2;
  $('#downloadPng').disabled = $('#downloadTif').disabled = !lastRequest;
}

// ── Search ─────────────────────────────────────────────────────

async function runSearch() {
  if (!store.aoi) return;
  try {
    const source = currentSource();
    const data = await withBusy(`Searching ${source.label}…`, () => api.search({
      aoi: store.aoi,
      source: store.source,
      start: $('#dateStart').value,
      end: $('#dateEnd').value,
      max_cloud: cloudControl.cloud.get(),
      limit: 60,
    }));
    store.scenes = data.scenes;
    store.selected.clear();
    store.activeSceneId = data.scenes[0]?.id ?? null;
    emit('scenes', data.scenes);
    syncEnabled();
    toast(data.scenes.length
      ? `${data.scenes.length} scene${data.scenes.length === 1 ? '' : 's'} found`
      : 'No scenes matched — widen the dates or allow more cloud', data.scenes.length ? 'ok' : '');
  } catch (err) {
    toast(`Search failed: ${err.message}`, 'err');
  }
}

function renderSceneList(scenes) {
  const list = $('#sceneList');
  list.innerHTML = '';
  if (!scenes.length) return;

  for (const scene of scenes) {
    const check = el('input', {
      type: 'checkbox',
      checked: store.selected.has(scene.id),
      onclick: (e) => {
        e.stopPropagation();
        if (e.target.checked) store.selected.add(scene.id);
        else store.selected.delete(scene.id);
        emit('selection', store.selected);
        syncEnabled();
      },
    });
    const row = el('div', {
      class: `scene ${scene.id === store.activeSceneId ? 'is-active' : ''}`,
      onclick: () => {
        store.activeSceneId = scene.id;
        $$('#sceneList .scene').forEach((n, i) =>
          n.classList.toggle('is-active', scenes[i].id === scene.id));
        syncEnabled();
      },
    },
      check,
      el('div', {},
        el('div', { class: 'scene-date' }, fmt.date(scene.date)),
        el('div', { class: 'scene-meta' }, `${scene.platform}${scene.tile ? ` · ${scene.tile}` : ''}`)),
      scene.cloud == null
        ? el('div', { class: 'scene-cloud dim' }, scene.instrument ?? '')
        : el('div', { class: 'scene-cloud' },
          el('span', { class: 'cloud-dot', style: `background:${cloudColour(scene.cloud)}` }),
          `${scene.cloud}%`),
    );
    list.append(row);
  }
}

const cloudColour = (pct) => (pct < 10 ? '#37e0a0' : pct < 30 ? '#ffd166' : '#ff8a5b');

// ── Render ─────────────────────────────────────────────────────

export function buildRenderRequest(scene, overrides = {}) {
  const { mode, key } = currentMode();
  const values = renderControls.values ? renderControls.values() : {};
  const body = {
    aoi: store.aoi,
    source: store.source,
    scene,
    mode,
    size: parseInt($('#renderSize').value, 10),
    mask_clouds: $('#maskClouds').checked && !$('#maskClouds').disabled,
    clip: $('#clipShape').checked,
    ...values,
    ...enhancementValues(),
    ...overrides,
  };
  if (mode === 'index') {
    body.index = key;
    body.colormap = renderControls.colormap?.get();
  } else if (mode === 'categorical') {
    body.mode = 'categorical';
  } else {
    body.preset = key;
    const stretch = renderControls.stretch?.get();
    if (stretch) body.stretch = stretch;
    else delete body.stretch;
  }
  return body;
}

/** What the fusion measured, said plainly — including when it did not help. */
function describeGain(sr) {
  const cleaner = sr.noise_drop_pct > 0 ? `, ${sr.noise_drop_pct}% less noise` : '';
  if (sr.sharpness_gain_pct > 0) return `+${sr.sharpness_gain_pct}% detail${cleaner}`;
  // The dates disagreed about the ground rather than about its sampling: a
  // year of growth averages out into less detail, not more.
  return `no detail gained${cleaner} — the dates differ too much, try ones closer together`;
}

/** Scenes to merge when the cloud-free composite is switched on. */
function compositeScenes() {
  if (!$('#useComposite').checked || store.selected.size < 2) return null;
  return store.scenes
    .filter((s) => store.selected.has(s.id))
    .sort((a, b) => b.date.localeCompare(a.date));
}

async function runRender() {
  const merged = compositeScenes();
  const scene = merged ? merged[0] : store.scenes.find((s) => s.id === store.activeSceneId);
  if (!scene || !store.aoi) return;

  const body = buildRenderRequest(scene);
  const scale = superresScale();
  if (merged) {
    body.scenes = merged;
    body.composite_method = $('#compositeMethod').value;
    if (scale > 1) body.superres = scale;
  }
  const what = scale > 1
    ? `Fusing ${merged.length} scenes at ${scale}×…`
    : merged
      ? `Merging ${merged.length} scenes…`
      : `Downloading imagery for ${fmt.date(scene.date)}…`;
  try {
    const data = await withBusy(what, () => api.render(body));
    const image = await loadImage(data.image);
    lastRequest = body;
    addCapture({ src: data.image, image, meta: data.meta });
    syncEnabled();
    const report = data.meta.composite_report;
    const sr = data.meta.superres;
    toast(sr
      ? `${sr.scale}× from ${sr.scenes} dates — ${data.meta.grid.width}×${data.meta.grid.height} px `
        + `at ${data.meta.grid.ground_res_m} m/px · ${describeGain(sr)}`
      : report
        ? `Composite of ${report.scenes} scenes — ${report.combined_pct}% clear `
          + `(best single date: ${report.best_single_pct}%)`
        : `Rendered ${data.meta.grid.width}×${data.meta.grid.height} px `
          + `at ${data.meta.grid.ground_res_m} m/px`, 'ok');
  } catch (err) {
    toast(`Render failed: ${err.message}`, 'err');
  }
}

async function downloadRender(format) {
  if (!lastRequest) return;
  try {
    const body = { ...lastRequest, format };
    const blob = await withBusy('Preparing download…', () =>
      (body.mode === 'change' ? api.changeFile(body) : api.renderFile(body)));
    const ext = format === 'geotiff' ? 'tif' : 'png';
    const when = body.mode === 'change'
      ? `${body.scene_a.date}_to_${body.scene_b.date}`
      : (body.scene?.date ?? 'scene');
    download(blob, `sentinel2_${when}_${body.index ?? body.preset}.${ext}`);
  } catch (err) {
    toast(`Download failed: ${err.message}`, 'err');
  }
}

// ── Change detection ───────────────────────────────────────────

async function runChange() {
  if (store.selected.size !== 2) {
    toast('Tick exactly two scenes to compare', 'err');
    return;
  }
  const [a, b] = [...store.selected].map((id) => store.scenes.find((s) => s.id === id));
  const body = {
    aoi: store.aoi,
    source: store.source,
    scene_a: a,
    scene_b: b,
    index: $('#changeIndex').value,
    size: parseInt($('#renderSize').value, 10),
    mask_clouds: $('#maskClouds').checked,
    clip: $('#clipShape').checked,
    ...changeControls.values(),
    ...enhancementValues(),
  };
  try {
    const data = await withBusy('Comparing dates…', () => api.change(body));
    const image = await loadImage(data.image);
    lastRequest = { ...body, mode: 'change' };
    addCapture({ src: data.image, image, meta: data.meta });
    syncEnabled();
    const { classes } = data.meta;
    toast(`${classes[2].percent}% gain · ${classes[0].percent}% loss over ${data.meta.days_apart} days`, 'ok');
  } catch (err) {
    toast(`Comparison failed: ${err.message}`, 'err');
  }
}
