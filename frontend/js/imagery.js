// The whole app: pick a place, pick dates, look at the imagery.
//
// Two ways to look at it, chosen up front because they are genuinely different
// questions. One date is what the satellite saw on a day. A merge is what the
// ground looks like, put together from several passes: sharper, and without
// the cloud any single pass happened to have over it.

import { api } from './api.js';
import { store, on, setImage } from './store.js';
import { $, $$, el, toast, withBusy, download, loadImage, fmt, sliderBank } from './ui.js';

let renderControls = {};
let enhanceControls = {};
let cloudControl = null;
let lastRequest = null;
let mode = 'single';                     // 'single' | 'merge'

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

const MODE_HINTS = {
  single: 'One pass, exactly as the satellite recorded it. Pick the date you want.',
  merge: 'Several passes solved into one picture — finer than 10 m, and with the '
    + 'cloud from any one date taken out by the others. Tick the dates to use.',
};

export function initImagery() {
  const today = new Date();
  const yearAgo = new Date(today.getTime() - 365 * 864e5);
  $('#dateEnd').value = today.toISOString().slice(0, 10);
  $('#dateStart').value = yearAgo.toISOString().slice(0, 10);

  cloudControl = sliderBank($('#cloudSlider'), [{
    key: 'cloud', label: 'Maximum cloud cover', min: 0, max: 100, step: 5, value: 30, unit: '%',
  }], sync);

  buildVisualisationOptions();
  buildRenderSliders();
  buildEnhanceControls();

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
  setMode('single');
}

// ── One date, or several merged ────────────────────────────────

function setMode(next) {
  mode = next;
  $$('#modePicker .mode').forEach((b) => b.classList.toggle('is-active', b.dataset.mode === next));
  $('#modeHint').textContent = MODE_HINTS[next];
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
  const fits = Math.max(1, Math.floor((store.config.max_size ?? 4096) / renderSize()));
  return Math.min(wanted, fits);
}

const renderSize = () => parseInt($('#renderSize').value, 10);

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
  enhanceControls = sliderBank($('#enhanceSliders'), ENHANCE_SPECS, sync);

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
  sync();
}

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
  try {
    const data = await withBusy('Looking for Sentinel-2 passes…', () => api.search({
      aoi: store.aoi,
      start: $('#dateStart').value,
      end: $('#dateEnd').value,
      max_cloud: cloudControl.cloud.get(),
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

/** Tick the `n` least cloudy dates, and untick everything else. */
function tickBest(n) {
  store.selected = new Set([...store.dates]
    .sort((a, b) => (a.cloud ?? 100) - (b.cloud ?? 100))
    .slice(0, n)
    .map((d) => d.id));
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
    list.append(el('div', {
      class: `scene ${chosen ? 'is-active' : ''}`,
      onclick: () => pick(date, mode === 'single' ? true : !store.selected.has(date.id)),
    },
      input,
      el('div', {},
        el('div', { class: 'scene-date' }, fmt.date(date.date)),
        el('div', { class: 'scene-meta' }, date.tile || date.platform)),
      el('div', { class: 'scene-cloud' },
        el('span', { class: 'cloud-dot', style: `background:${cloudColour(date.cloud ?? 0)}` }),
        `${date.cloud ?? 0}%`),
    ));
  }
  $('#dateEmpty').hidden = store.dates.length > 0 || !store.aoi;
  $('#tickRow').hidden = mode !== 'merge' || !store.dates.length;
  // The clearest date is rarely the newest, so it is rarely at the top: show
  // the user what has been picked for them rather than leaving it off-screen.
  list.querySelector('.scene.is-active')?.scrollIntoView({ block: 'nearest' });
}

function pick(date, on) {
  if (mode === 'single') {
    store.activeDateId = date.id;
    store.selected = new Set([date.id]);
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
  $('#searchDates').disabled = !hasAoi;
  $('#dateEmpty').hidden = store.dates.length > 0 || !hasAoi;
  $('#whenSummary').textContent =
    `${$('#dateStart').value} → ${$('#dateEnd').value} · under ${cloudControl.cloud.get()}% cloud`;

  const tuned = Object.keys(enhancementValues()).length;
  $('#tuningSummary').textContent = tuned ? `${tuned} adjusted` : 'off';

  const scale = mode === 'merge' ? mergeScale(dates.length) : 1;
  const ready = mode === 'merge' ? dates.length > 1 : dates.length === 1;
  const current = ready && lastRequest
    && JSON.stringify(buildRequest(dates)) === JSON.stringify(lastRequest);

  $('#showBtn').disabled = !ready || current;
  $('#showBtn').textContent = current
    ? 'Showing this now'
    : mode === 'merge'
      ? (ready ? `Merge ${dates.length} dates → ${scale}×` : 'Tick at least two dates')
      : (ready ? (store.image ? 'Show this date' : 'Show imagery') : 'Pick a date');
  $('#planSummary').innerHTML = planSummary(dates, scale);
  $('#downloadPng').disabled = $('#downloadTif').disabled = !lastRequest;
}

/** One line above the button saying exactly what pressing it will produce. */
function planSummary(dates, scale) {
  if (!store.aoi) return 'Draw an area on the map to begin.';
  if (!store.dates.length) return 'No dates yet.';
  if (!dates.length) {
    return mode === 'merge' ? 'Tick the dates you want merged.' : 'Pick a date.';
  }
  const px = `${renderSize() * scale} px`;
  if (mode === 'single') return `<b>${fmt.date(dates[0].date)}</b> · ${px} · one pass`;
  if (dates.length < 2) return 'A merge needs at least two dates.';

  const steps = store.config.superres_steps ?? [];
  const next = steps.find(([, s]) => s === scale + 1);
  const more = next && scale < (store.config.max_superres ?? 4)
    ? ` · <span class="dim">${next[0]} dates would reach ${next[1]}×</span>` : '';
  return `<b>${dates.length} dates → ${scale}× detail</b> · ${px}${more}`;
}

// ── Showing the imagery ────────────────────────────────────────

function buildRequest(dates) {
  const { kind, key } = currentMode();
  const body = {
    aoi: store.aoi,
    scene: dates[0],
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
  if (!sr) return `${fmt.date(meta.scene.date)} — ${size}`;

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
    const blob = await withBusy('Preparing the file…', () =>
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
