// Edit tab: non-destructive adjustments, geometry, undo/redo and export.

import { store, on, emit, activeCapture, setActiveCapture } from './store.js';
import {
  DEFAULTS, PRESETS, applyAdjustments, isNeutral, transformCanvas, withDefaults,
} from './adjust.js';
import { $, $$, el, toast, download, sliderBank, fmt, debounce } from './ui.js';

const SPECS = [
  { key: 'exposure', label: 'Exposure', min: -100, max: 100, step: 1, value: 0 },
  { key: 'contrast', label: 'Contrast', min: -100, max: 100, step: 1, value: 0 },
  { key: 'saturation', label: 'Saturation', min: -100, max: 100, step: 1, value: 0 },
  { key: 'highlights', label: 'Highlights', min: -100, max: 100, step: 1, value: 0 },
  { key: 'shadows', label: 'Shadows', min: -100, max: 100, step: 1, value: 0 },
  { key: 'temperature', label: 'Temperature', min: -100, max: 100, step: 1, value: 0 },
  { key: 'tint', label: 'Tint', min: -100, max: 100, step: 1, value: 0 },
  { key: 'gamma', label: 'Gamma', min: 0.4, max: 2.5, step: 0.02, value: 1, format: (v) => v.toFixed(2) },
  { key: 'clarity', label: 'Clarity', min: 0, max: 100, step: 1, value: 0 },
  { key: 'vignette', label: 'Vignette', min: 0, max: 100, step: 1, value: 0 },
];

let controls = {};
let canvas;
let ctx;
let history = [];
let historyAt = -1;
let cropping = false;
let cropRect = null;
let dragStart = null;

export const editState = (capture) => {
  if (!capture) return null;
  capture.edit ??= { adj: { ...DEFAULTS }, rotate: 0, flipH: false, flipV: false, crop: null };
  return capture.edit;
};

export function initEditor() {
  canvas = $('#editCanvas');
  ctx = canvas.getContext('2d');

  controls = sliderBank($('#editSliders'), SPECS, debounce(() => {
    const state = editState(activeCapture());
    if (!state) return;
    Object.assign(state.adj, controls.values());
    draw();
  }, 40));

  const presetHost = $('#editPresets');
  for (const name of Object.keys(PRESETS)) {
    presetHost.append(el('button', {
      class: 'preset', onclick: () => applyPreset(name),
    }, name));
  }

  $('#editSource').addEventListener('change', (e) => setActiveCapture(e.target.value));
  $('#editUndo').addEventListener('click', undo);
  $('#editRedo').addEventListener('click', redo);
  $('#editReset').addEventListener('click', () => {
    const state = editState(activeCapture());
    if (!state) return;
    pushHistory();
    Object.assign(state, { adj: { ...DEFAULTS }, rotate: 0, flipH: false, flipV: false, crop: null });
    syncControls();
    draw();
  });

  $$('[data-transform]').forEach((btn) => btn.addEventListener('click', () => {
    const state = editState(activeCapture());
    if (!state) return;
    const action = btn.dataset.transform;
    if (action === 'crop') {
      cropping = !cropping;
      btn.classList.toggle('is-active', cropping);
      canvas.classList.toggle('is-cropping', cropping);
      cropRect = null;
      $('#cropActions').hidden = !cropping;
      draw();
      return;
    }
    pushHistory();
    if (action === 'rot-left') state.rotate -= 90;
    if (action === 'rot-right') state.rotate += 90;
    if (action === 'flip-h') state.flipH = !state.flipH;
    if (action === 'flip-v') state.flipV = !state.flipV;
    state.crop = null;
    draw();
  }));

  $('#cropApply').addEventListener('click', applyCrop);
  $('#cropCancel').addEventListener('click', () => {
    cropRect = null;
    cropping = false;
    canvas.classList.remove('is-cropping');
    $('#cropActions').hidden = true;
    $$('[data-transform="crop"]').forEach((b) => b.classList.remove('is-active'));
    draw();
  });

  canvas.addEventListener('pointerdown', onPointerDown);
  canvas.addEventListener('pointermove', onPointerMove);
  window.addEventListener('pointerup', onPointerUp);

  $('#editExport').addEventListener('click', () => exportImage('png'));
  $('#editExportJpg').addEventListener('click', () => exportImage('jpeg'));
  $('#editApplyAll').addEventListener('click', applyToAll);

  on('captures', syncSources);
  on('activeCapture', () => { syncSources(); syncControls(); resetHistory(); draw(); });
}

// ── Source list ────────────────────────────────────────────────

function syncSources() {
  const select = $('#editSource');
  select.innerHTML = '';
  for (const capture of store.captures) {
    select.append(el('option', {
      value: capture.id, selected: capture.id === store.activeCaptureId,
    }, capture.name));
  }
  if (!store.captures.length) {
    select.append(el('option', { value: '' }, 'Nothing captured yet'));
  }
}

function syncControls() {
  const state = editState(activeCapture());
  const adj = withDefaults(state?.adj);
  for (const spec of SPECS) controls[spec.key]?.set(adj[spec.key]);
  $$('#editPresets .preset').forEach((b) => b.classList.remove('is-active'));
}

function applyPreset(name) {
  const state = editState(activeCapture());
  if (!state) return;
  pushHistory();
  state.adj = { ...DEFAULTS, ...PRESETS[name] };
  syncControls();
  $$('#editPresets .preset').forEach((b) => b.classList.toggle('is-active', b.textContent === name));
  draw();
}

// ── History ────────────────────────────────────────────────────

const snapshot = (state) => JSON.stringify(state);

function resetHistory() {
  const state = editState(activeCapture());
  history = state ? [snapshot(state)] : [];
  historyAt = history.length - 1;
  syncHistoryButtons();
}

function pushHistory() {
  const state = editState(activeCapture());
  if (!state) return;
  history = history.slice(0, historyAt + 1);
  history.push(snapshot(state));
  if (history.length > 40) history.shift();
  historyAt = history.length - 1;
  syncHistoryButtons();
}

function restore(index) {
  const capture = activeCapture();
  if (!capture || !history[index]) return;
  capture.edit = JSON.parse(history[index]);
  historyAt = index;
  syncControls();
  syncHistoryButtons();
  draw();
}

const undo = () => historyAt > 0 && restore(historyAt - 1);
const redo = () => historyAt < history.length - 1 && restore(historyAt + 1);

function syncHistoryButtons() {
  $('#editUndo').disabled = historyAt <= 0;
  $('#editRedo').disabled = historyAt >= history.length - 1;
}

// ── Rendering ──────────────────────────────────────────────────

/** The finished, edited image for a capture — shared with timelapse and graphic. */
export function editedCanvas(capture, { adjustmentsOverride } = {}) {
  if (!capture) return null;
  const state = editState(capture);
  const base = document.createElement('canvas');
  base.width = capture.image.naturalWidth;
  base.height = capture.image.naturalHeight;
  base.getContext('2d').drawImage(capture.image, 0, 0);

  const transformed = transformCanvas(base, state);
  const adj = adjustmentsOverride ?? state.adj;
  if (isNeutral(adj)) return transformed;

  const c2 = transformed.getContext('2d', { willReadFrequently: true });
  const data = applyAdjustments(c2.getImageData(0, 0, transformed.width, transformed.height), adj);
  c2.putImageData(data, 0, 0);
  return transformed;
}

function draw() {
  const capture = activeCapture();
  const foot = $('#editFoot');
  if (!capture) {
    canvas.width = canvas.height = 0;
    foot.textContent = 'Render an image on the Capture tab to start editing.';
    return;
  }
  const out = editedCanvas(capture);
  canvas.width = out.width;
  canvas.height = out.height;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(out, 0, 0);

  if (cropping && cropRect) {
    ctx.save();
    ctx.fillStyle = 'rgba(6,10,16,.55)';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.clearRect(cropRect.x, cropRect.y, cropRect.w, cropRect.h);
    ctx.drawImage(out, cropRect.x, cropRect.y, cropRect.w, cropRect.h,
      cropRect.x, cropRect.y, cropRect.w, cropRect.h);
    ctx.strokeStyle = '#4cc2ff';
    ctx.lineWidth = Math.max(2, canvas.width / 400);
    ctx.setLineDash([canvas.width / 60, canvas.width / 90]);
    ctx.strokeRect(cropRect.x, cropRect.y, cropRect.w, cropRect.h);
    ctx.restore();
  }

  const meta = capture.meta;
  foot.innerHTML = '';
  const chips = [
    `${out.width} × ${out.height} px`,
    meta.grid ? `${meta.grid.ground_res_m} m/px` : null,
    meta.label,
    meta.scene?.date ? fmt.date(meta.scene.date) : null,
    meta.cloud_masked_pct ? `${meta.cloud_masked_pct}% cloud masked` : null,
    meta.stats?.mean != null ? `mean ${meta.stats.mean}` : null,
  ].filter(Boolean);
  for (const chip of chips) foot.append(el('span', { class: 'stat-chip' }, chip));
  emit('edited', capture);
}

// ── Crop interaction ───────────────────────────────────────────

function canvasPoint(e) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((e.clientX - rect.left) / rect.width) * canvas.width,
    y: ((e.clientY - rect.top) / rect.height) * canvas.height,
  };
}

function onPointerDown(e) {
  if (!cropping || !activeCapture()) return;
  dragStart = canvasPoint(e);
  cropRect = { x: dragStart.x, y: dragStart.y, w: 0, h: 0 };
}

function onPointerMove(e) {
  if (!dragStart) return;
  const p = canvasPoint(e);
  cropRect = {
    x: Math.min(dragStart.x, p.x),
    y: Math.min(dragStart.y, p.y),
    w: Math.abs(p.x - dragStart.x),
    h: Math.abs(p.y - dragStart.y),
  };
  draw();
}

function onPointerUp() {
  dragStart = null;
}

function applyCrop() {
  const state = editState(activeCapture());
  if (!state || !cropRect || cropRect.w < 8 || cropRect.h < 8) {
    toast('Drag a crop rectangle on the image first', 'err');
    return;
  }
  pushHistory();
  // Crops compose: a second crop is relative to the already-cropped image.
  const prev = state.crop;
  state.crop = prev
    ? { x: prev.x + cropRect.x, y: prev.y + cropRect.y, w: cropRect.w, h: cropRect.h }
    : { ...cropRect };
  cropRect = null;
  cropping = false;
  canvas.classList.remove('is-cropping');
  $('#cropActions').hidden = true;
  $$('[data-transform="crop"]').forEach((b) => b.classList.remove('is-active'));
  draw();
}

// ── Export ─────────────────────────────────────────────────────

function exportImage(format) {
  const capture = activeCapture();
  if (!capture) return;
  const out = editedCanvas(capture);
  const type = format === 'jpeg' ? 'image/jpeg' : 'image/png';
  const flat = format === 'jpeg' ? flatten(out) : out;
  flat.toBlob((blob) => {
    download(blob, `${slug(capture.name)}.${format === 'jpeg' ? 'jpg' : 'png'}`);
  }, type, 0.94);
}

function flatten(source) {
  const c = document.createElement('canvas');
  c.width = source.width;
  c.height = source.height;
  const g = c.getContext('2d');
  g.fillStyle = '#0d1015';
  g.fillRect(0, 0, c.width, c.height);
  g.drawImage(source, 0, 0);
  return c;
}

function applyToAll() {
  const state = editState(activeCapture());
  if (!state) return;
  let count = 0;
  for (const capture of store.captures) {
    if (capture.id === store.activeCaptureId) continue;
    editState(capture).adj = { ...state.adj };
    count++;
  }
  toast(count ? `Adjustments copied to ${count} other image${count === 1 ? '' : 's'}` : 'No other images yet');
  emit('captures', store.captures);
}

export const slug = (s) => s.replace(/[^\w.-]+/g, '_').replace(/^_|_$/g, '').slice(0, 80) || 'image';

export function refreshEditor() {
  syncSources();
  syncControls();
  draw();
}
