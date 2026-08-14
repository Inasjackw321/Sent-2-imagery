// Timelapse tab: render a stack of dates, preview the animation, export GIF/WebP/APNG.

import { api } from './api.js';
import { store, on, emit, addCapture, getCapture, isDemo } from './store.js';
import { buildRenderRequest } from './capture.js';
import { editedCanvas, editState, slug } from './editor.js';
import * as chrome from './overlay.js';
import { $, el, toast, withBusy, download, loadImage, fmt, sliderBank } from './ui.js';

let controls = {};
let canvas;
let ctx;
let playTimer = 0;
let cursor = 0;

export function initTimelapse() {
  canvas = $('#animCanvas');
  ctx = canvas.getContext('2d');

  controls = sliderBank($('#animSliders'), [
    { key: 'fps', label: 'Speed', min: 0.5, max: 12, step: 0.5, value: 2,
      format: (v) => `${v} fps` },
    { key: 'width', label: 'Frame width', min: 400, max: 1600, step: 50, value: 900,
      format: (v) => `${v} px` },
    { key: 'colors', label: 'GIF colours', min: 32, max: 256, step: 32, value: 256 },
  ], () => { drawFrame(cursor); });

  $('#buildFrames').addEventListener('click', buildFrames);
  $('#animPlay').addEventListener('click', play);
  $('#animStop').addEventListener('click', stop);
  $('#animExport').addEventListener('click', exportAnimation);
  $('#sheetExport').addEventListener('click', exportContactSheet);
  $('#animScrub').addEventListener('input', (e) => {
    stop();
    drawFrame(parseInt(e.target.value, 10));
  });
  for (const id of ['#animDate', '#animBar', '#animScale', '#animTitleOn', '#animEdits']) {
    $(id).addEventListener('change', () => drawFrame(cursor));
  }
  $('#animTitle').addEventListener('input', () => drawFrame(cursor));

  on('selection', syncEnabled);
  on('frames', () => { syncFrameList(); syncEnabled(); drawFrame(0); });
  on('edited', () => drawFrame(cursor));
  syncEnabled();
}

function syncEnabled() {
  $('#buildFrames').disabled = store.selected.size < 2 || !store.aoi;
  const ready = store.frames.length >= 2;
  $('#animExport').disabled = !ready;
  $('#sheetExport').disabled = store.frames.length < 2;
  $('#animScrub').max = Math.max(0, store.frames.length - 1);
}

// ── Building frames ────────────────────────────────────────────

async function buildFrames() {
  const scenes = store.scenes
    .filter((s) => store.selected.has(s.id))
    .sort((a, b) => a.date.localeCompare(b.date));
  if (scenes.length < 2) {
    toast('Tick at least two dates on the Capture tab', 'err');
    return;
  }

  const progress = $('#frameProgress');
  const bar = progress.firstElementChild;
  const label = progress.lastElementChild;
  progress.hidden = false;
  store.frames = [];

  let failed = 0;
  for (const [i, scene] of scenes.entries()) {
    label.textContent = `Rendering ${fmt.date(scene.date)} — ${i + 1} of ${scenes.length}`;
    bar.style.width = `${(i / scenes.length) * 100}%`;
    try {
      const data = await api.render(buildRenderRequest(scene));
      const image = await loadImage(data.image);
      const capture = addCapture({ src: data.image, image, meta: data.meta });
      store.frames.push({ captureId: capture.id, date: scene.date });
    } catch (err) {
      failed++;
      console.warn('frame failed', scene.id, err);
    }
  }

  bar.style.width = '100%';
  label.textContent = '';
  progress.hidden = true;
  emit('frames', store.frames);
  if (failed) toast(`${failed} frame${failed === 1 ? '' : 's'} could not be rendered`, 'err');
  if (store.frames.length >= 2) toast(`${store.frames.length} frames ready`, 'ok');
}

function syncFrameList() {
  const list = $('#frameList');
  list.innerHTML = '';
  store.frames.forEach((frame, i) => {
    const capture = getCapture(frame.captureId);
    if (!capture) return;
    list.append(el('div', { class: 'frame-item' },
      el('img', { src: capture.src, alt: '' }),
      el('span', {}, fmt.date(frame.date)),
      el('button', {
        class: 'x', title: 'Remove frame',
        onclick: () => {
          store.frames.splice(i, 1);
          emit('frames', store.frames);
        },
      }, '×')));
  });
}

// ── Frame composition ──────────────────────────────────────────

function frameSize() {
  const first = getCapture(store.frames[0]?.captureId);
  if (!first) return { width: 900, height: 600 };
  const source = editedCanvas(first);
  const width = Math.round(controls.width.get());
  return { width, height: Math.round((source.height / source.width) * width) };
}

/** Draw one composed frame (image + overlays) at export resolution. */
export function composeFrame(index, size = frameSize()) {
  const frame = store.frames[index];
  const capture = getCapture(frame?.captureId);
  if (!capture) return null;

  const useEdits = $('#animEdits').checked;
  const source = useEdits
    ? editedCanvas(capture)
    : editedCanvas(capture, { adjustmentsOverride: {} });

  const out = document.createElement('canvas');
  out.width = size.width;
  out.height = size.height;
  const g = out.getContext('2d');
  const theme = chrome.THEMES.dark;

  g.fillStyle = '#0a0d12';
  g.fillRect(0, 0, out.width, out.height);

  // Contain-fit so mixed aspect ratios never distort.
  const scale = Math.min(out.width / source.width, out.height / source.height);
  const dw = source.width * scale;
  const dh = source.height * scale;
  const dx = (out.width - dw) / 2;
  const dy = (out.height - dh) / 2;
  g.imageSmoothingQuality = 'high';
  g.drawImage(source, dx, dy, dw, dh);

  const s = out.width / 900;
  const pad = 16 * s;

  if ($('#animTitleOn').checked && $('#animTitle').value.trim()) {
    chrome.plate(g, pad, pad, out.width - pad * 2, 34 * s, theme, 8 * s);
    chrome.shadowText(g, $('#animTitle').value.trim(), out.width / 2, pad + 22 * s, {
      size: 16 * s, weight: 700, colour: theme.ink, align: 'center', shadow: null,
    });
  }

  if ($('#animDate').checked) {
    const text = fmt.date(frame.date);
    g.font = chrome.font(19 * s, 700);
    const width = g.measureText(text).width + 22 * s;
    chrome.plate(g, out.width - pad - width, out.height - pad - 40 * s, width, 32 * s, theme, 8 * s);
    chrome.shadowText(g, text, out.width - pad - width / 2, out.height - pad - 19 * s, {
      size: 19 * s, weight: 700, colour: theme.ink, align: 'center', shadow: null,
    });
  }

  if ($('#animScale').checked) {
    const mpp = chrome.metresPerPixel(capture, dw, source.width);
    chrome.drawScaleBar(g, pad + 8 * s, out.height - pad - 22 * s, 150 * s, mpp, theme, s);
  }

  if ($('#animBar').checked) {
    const progress = store.frames.length > 1 ? index / (store.frames.length - 1) : 1;
    chrome.drawTimelineBar(g, pad, out.height - pad + 4 * s,
      out.width - pad * 2, progress, theme, s);
  }

  if (capture.meta.demo || isDemo()) chrome.drawWatermark(g, out.width, out.height, s);

  return out;
}

function drawFrame(index) {
  cursor = Math.max(0, Math.min(index, store.frames.length - 1));
  const label = $('#animLabel');
  if (!store.frames.length) {
    canvas.width = canvas.height = 0;
    label.textContent = 'Tick dates on the Capture tab, then render them here.';
    return;
  }
  const composed = composeFrame(cursor);
  if (!composed) return;
  canvas.width = composed.width;
  canvas.height = composed.height;
  ctx.drawImage(composed, 0, 0);
  $('#animScrub').value = cursor;
  label.innerHTML = '';
  label.append(el('b', {}, fmt.date(store.frames[cursor].date)),
    ` — frame ${cursor + 1} of ${store.frames.length}`);
}

// ── Playback ───────────────────────────────────────────────────

function play() {
  stop();
  if (store.frames.length < 2) return;
  const interval = 1000 / controls.fps.get();
  const bounce = $('#animBoomerang').checked;
  let direction = 1;
  playTimer = setInterval(() => {
    let next = cursor + direction;
    if (next >= store.frames.length) {
      if (bounce) { direction = -1; next = store.frames.length - 2; }
      else next = 0;
    } else if (next < 0) {
      direction = 1;
      next = 1;
    }
    drawFrame(next);
  }, interval);
}

function stop() {
  clearInterval(playTimer);
  playTimer = 0;
}

// ── Export ─────────────────────────────────────────────────────

const toDataUrl = (c) => c.toDataURL('image/png');

async function collectFrames() {
  const size = frameSize();
  const frames = [];
  for (let i = 0; i < store.frames.length; i++) {
    const composed = composeFrame(i, size);
    if (composed) frames.push(toDataUrl(composed));
    // Yield so the progress spinner keeps painting on long stacks.
    if (i % 4 === 3) await new Promise((r) => setTimeout(r, 0));
  }
  return frames;
}

async function exportAnimation() {
  stop();
  try {
    const blob = await withBusy('Building animation…', async () => {
      const frames = await collectFrames();
      return api.animate({
        frames,
        fps: controls.fps.get(),
        loop: $('#animLoop').checked ? 0 : 1,
        boomerang: $('#animBoomerang').checked,
        format: $('#animFormat').value,
        colors: controls.colors.get(),
        filename: animationName(),
      });
    });
    const ext = { gif: 'gif', webp: 'webp', apng: 'png' }[$('#animFormat').value];
    download(blob, `${animationName()}.${ext}`);
    toast(`Animation ready — ${fmt.bytes(blob.size)}`, 'ok');
  } catch (err) {
    toast(`Animation failed: ${err.message}`, 'err');
  }
}

async function exportContactSheet() {
  try {
    const blob = await withBusy('Building contact sheet…', async () =>
      api.contactSheet({ frames: await collectFrames(), columns: 3, filename: animationName() }));
    download(blob, `${animationName()}_contact_sheet.png`);
  } catch (err) {
    toast(`Contact sheet failed: ${err.message}`, 'err');
  }
}

function animationName() {
  const title = $('#animTitle').value.trim();
  if (title) return slug(title);
  const first = store.frames[0]?.date ?? '';
  const last = store.frames.at(-1)?.date ?? '';
  return slug(`timelapse_${first}_${last}`);
}

export function refreshTimelapse() {
  syncFrameList();
  syncEnabled();
  drawFrame(cursor);
}
