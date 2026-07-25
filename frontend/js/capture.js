// Capture tab: scene search, visualisation options, rendering and change detection.

import { api } from './api.js';
import { store, emit, on, addCapture } from './store.js';
import { $, $$, el, toast, withBusy, download, loadImage, fmt, sliderBank } from './ui.js';

let renderControls = {};
let changeControls = {};
let cloudControl = null;
let lastRequest = null;

export function initCapture() {
  const today = new Date();
  const yearAgo = new Date(today.getTime() - 365 * 864e5);
  $('#dateEnd').value = today.toISOString().slice(0, 10);
  $('#dateStart').value = yearAgo.toISOString().slice(0, 10);

  cloudControl = sliderBank($('#cloudSlider'), [{
    key: 'cloud', label: 'Maximum cloud cover', min: 0, max: 100, step: 5, value: 30, unit: '%',
  }]);

  buildVisualisationOptions();
  buildRenderSliders();

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

  on('aoi', syncEnabled);
  on('scenes', renderSceneList);
  syncEnabled();
}

function buildVisualisationOptions() {
  const { composites, indices } = store.config;
  const compGroup = $('#optComposites');
  const idxGroup = $('#optIndices');
  const changeSel = $('#changeIndex');
  compGroup.innerHTML = idxGroup.innerHTML = changeSel.innerHTML = '';

  for (const [key, spec] of Object.entries(composites)) {
    compGroup.append(el('option', { value: `composite:${key}` }, spec.label));
  }
  for (const [key, spec] of Object.entries(indices)) {
    idxGroup.append(el('option', { value: `index:${key}` }, spec.label));
    changeSel.append(el('option', { value: key }, spec.label));
  }
  updateHint();
}

function currentMode() {
  const [mode, key] = $('#renderMode').value.split(':');
  return { mode, key };
}

function updateHint() {
  const { mode, key } = currentMode();
  const spec = mode === 'index' ? store.config.indices[key] : store.config.composites[key];
  const detail = mode === 'index'
    ? `${spec.formula}`
    : `Bands: ${spec.band_labels.join(' · ')}`;
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

function syncEnabled() {
  const hasAoi = Boolean(store.aoi);
  $('#searchScenes').disabled = !hasAoi;
  $('#renderBtn').disabled = !hasAoi || !store.activeSceneId;
  $('#changeBtn').disabled = !hasAoi || store.selected.size !== 2;
  $('#downloadPng').disabled = $('#downloadTif').disabled = !lastRequest;
}

// ── Search ─────────────────────────────────────────────────────

async function runSearch() {
  if (!store.aoi) return;
  try {
    const data = await withBusy('Searching the Sentinel-2 catalogue…', () => api.search({
      aoi: store.aoi,
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
      el('div', { class: 'scene-cloud' },
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
    scene,
    mode,
    size: parseInt($('#renderSize').value, 10),
    mask_clouds: $('#maskClouds').checked,
    clip: $('#clipShape').checked,
    ...values,
    ...overrides,
  };
  if (mode === 'index') {
    body.index = key;
    body.colormap = renderControls.colormap?.get();
  } else {
    body.preset = key;
    const stretch = renderControls.stretch?.get();
    if (stretch) body.stretch = stretch;
    else delete body.stretch;
  }
  return body;
}

async function runRender() {
  const scene = store.scenes.find((s) => s.id === store.activeSceneId);
  if (!scene || !store.aoi) return;
  const body = buildRenderRequest(scene);
  try {
    const data = await withBusy(`Downloading imagery for ${fmt.date(scene.date)}…`,
      () => api.render(body));
    const image = await loadImage(data.image);
    lastRequest = body;
    addCapture({ src: data.image, image, meta: data.meta });
    syncEnabled();
    toast(`Rendered ${data.meta.grid.width}×${data.meta.grid.height} px at ${data.meta.grid.ground_res_m} m/px`, 'ok');
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
    scene_a: a,
    scene_b: b,
    index: $('#changeIndex').value,
    size: parseInt($('#renderSize').value, 10),
    mask_clouds: $('#maskClouds').checked,
    clip: $('#clipShape').checked,
    ...changeControls.values(),
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
