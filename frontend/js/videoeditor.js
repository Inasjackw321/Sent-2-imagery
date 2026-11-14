// A small video editor that opens after every recording: trim it, crop it,
// change its speed and its look, and save it as an MP4.
//
// The recordings hand over their frames as stills rather than as a finished
// video, which is what makes this possible. Nothing is recorded in real time
// any more -- every frame is drawn once, exactly, and kept as a JPEG -- so
// the editor can show any frame, play any stretch, and encode exactly the
// frames asked for at exactly the speed asked for.
//
// MP4 (H.264), the format every phone, browser and editor opens, is written
// with the browser's own encoder (WebCodecs) and a small muxer (vendor/
// mp4-muxer, MIT): frame-perfect and faster than real time. A browser
// without that encoder records the edited frames as they play instead, as
// MP4 if it can and WebM if it cannot, and says which.

import { el, toast } from './ui.js';

export const SPEEDS = [1, 2, 4, 6, 8, 12, 16, 24, 30];
export const ASPECTS = [
  ['free', 'Free'], ['16:9', '16:9'], ['1:1', 'Square'], ['9:16', '9:16 phone'], ['4:5', '4:5'],
];
const MAX_WIDTH = 1920;
const MAX_HEIGHT = 1920;
const CACHE_FRAMES = 40;

// ── Pure pieces, tested in Node ────────────────────────────────

/** The largest crop of `aspect` centred in a w x h frame. Free is the whole frame. */
export function cropFor(aspect, w, h) {
  if (aspect === 'free' || !aspect) return { x: 0, y: 0, w, h };
  const [a, b] = aspect.split(':').map(Number);
  const want = a / b;
  let cw = w;
  let ch = Math.round(w / want);
  if (ch > h) { ch = h; cw = Math.round(h * want); }
  return { x: Math.round((w - cw) / 2), y: Math.round((h - ch) / 2), w: cw, h: ch };
}

/** A crop kept inside the frame and at least `least` pixels each way. */
export function clampCrop(crop, w, h, least = 32) {
  const cw = Math.max(least, Math.min(w, Math.round(crop.w)));
  const ch = Math.max(least, Math.min(h, Math.round(crop.h)));
  return {
    x: Math.max(0, Math.min(w - cw, Math.round(crop.x))),
    y: Math.max(0, Math.min(h - ch, Math.round(crop.y))),
    w: cw, h: ch,
  };
}

/** The video's size for a crop: even (H.264 needs it), and no bigger than 1920. */
export function outputSize(crop) {
  const shrink = Math.min(1, MAX_WIDTH / crop.w, MAX_HEIGHT / crop.h);
  const even = (v) => Math.max(2, Math.floor((v * shrink) / 2) * 2);
  return { w: even(crop.w), h: even(crop.h) };
}

/** The canvas filter for the look sliders (each a percentage, 100 = as is). */
export function filterFor({ brightness = 100, contrast = 100, saturation = 100 } = {}) {
  const parts = [];
  if (brightness !== 100) parts.push(`brightness(${brightness}%)`);
  if (contrast !== 100) parts.push(`contrast(${contrast}%)`);
  if (saturation !== 100) parts.push(`saturate(${saturation}%)`);
  return parts.join(' ') || 'none';
}

/** "12 frames · 3.0 s". */
export function lengthSaid(count, fps) {
  const secs = count / Math.max(fps, 1);
  return `${count} frame${count === 1 ? '' : 's'} · ${secs < 10 ? secs.toFixed(1) : Math.round(secs)} s`;
}

/** The filename with the right extension for what was made. */
export function withExtension(name, type) {
  const stem = String(name || 'video').replace(/\.(mp4|webm|mov)$/i, '');
  return `${stem}.${String(type).includes('mp4') ? 'mp4' : 'webm'}`;
}

/** H.264 configurations to try, best first. */
export function codecsFor(w, h) {
  const big = w * h > 1280 * 720;
  return [
    big ? 'avc1.640033' : 'avc1.640028',   // High
    big ? 'avc1.4d0033' : 'avc1.4d0028',   // Main
    big ? 'avc1.420033' : 'avc1.42001f',   // Baseline
  ];
}

/** Every encoder to try, best first, with what the MP4 calls it. H.264 opens
 * everywhere; VP9 in MP4 is the fallback for a browser built without an
 * H.264 encoder -- it plays in browsers, VLC, Windows and Android, not on an
 * iPhone, and the editor says so when it is used. */
export function encodersFor(w, h) {
  return [
    ...codecsFor(w, h).map((codec) => ({ codec, mux: 'avc', name: 'H.264' })),
    { codec: 'vp09.00.40.08', mux: 'vp9', name: 'VP9' },
  ];
}

// ── The editor ─────────────────────────────────────────────────

/**
 * Open the editor on a list of frames (Blobs of JPEG or PNG, all one size).
 * Resolves when it closes, to the saved Blob or null.
 *
 *   frames   the stills, in order
 *   fps      how fast they were meant to play
 *   name     the filename to offer, without extension
 *   save     called with (blob, filename) to hand the file over
 */
export function openVideoEditor({ frames, fps = 8, name = 'video', title = 'Edit video', save }) {
  return new Promise((resolve) => {
    if (!frames?.length) { resolve(null); return; }
    const ed = new Editor({ frames, fps, name, title, save, done: resolve });
    ed.start();
  });
}

class Editor {
  constructor({ frames, fps, name, title, save, done }) {
    Object.assign(this, { frames, name, title, save, done });
    this.fps = SPEEDS.includes(fps) ? fps : SPEEDS.reduce((a, b) => (Math.abs(b - fps) < Math.abs(a - fps) ? b : a));
    this.cache = new Map();
    this.at = 0;
    this.trim = [0, frames.length - 1];
    this.adjust = { brightness: 100, contrast: 100, saturation: 100 };
    this.aspect = 'free';
    this.playing = null;
    this.busy = false;
  }

  async start() {
    const first = await this.decode(0);
    this.w = first.width;
    this.h = first.height;
    this.crop = { x: 0, y: 0, w: this.w, h: this.h };
    this.build();
    this.draw();
    document.addEventListener('keydown', this.onKey = (e) => {
      if (e.key === 'Escape' && !this.busy) this.close(null);
      if (e.key === ' ' && e.target === document.body) { e.preventDefault(); this.togglePlay(); }
    });
  }

  async decode(i) {
    if (this.cache.has(i)) return this.cache.get(i);
    const bmp = await createImageBitmap(this.frames[i]);
    this.cache.set(i, bmp);
    if (this.cache.size > CACHE_FRAMES) {
      const [old, gone] = this.cache.entries().next().value;
      if (old !== i) { gone.close?.(); this.cache.delete(old); }
    }
    return bmp;
  }

  build() {
    const chip = (group, value, label, on, act) => el('button', {
      class: `cm-chip${on ? ' is-on' : ''}`, dataset: { group, value }, onclick: act,
    }, label);
    const slider = (key, label) => el('label', { class: 've-slider' },
      el('span', {}, label),
      el('input', { type: 'range', min: '40', max: '180', value: String(this.adjust[key]),
        oninput: (e) => { this.adjust[key] = Number(e.target.value); this.draw(); this.sayOut(); } }),
      el('b', { dataset: { key } }, `${this.adjust[key]}%`));

    this.canvas = el('canvas', { class: 've-canvas', width: String(this.w), height: String(this.h) });
    this.cropBox = el('div', { class: 've-crop' },
      ...['nw', 'ne', 'sw', 'se'].map((c) => el('i', { class: `ve-h ve-${c}`, dataset: { corner: c } })));
    this.stage = el('div', { class: 've-stage' }, this.canvas, this.cropBox);
    this.scrub = el('input', { type: 'range', class: 've-scrub', min: '0', max: String(this.frames.length - 1),
      value: '0', oninput: (e) => { this.pause(); this.seek(Number(e.target.value)); } });
    this.playBtn = el('button', { class: 'cop-play', onclick: () => this.togglePlay() }, '▶');
    this.when = el('span', { class: 've-when' });
    this.trimFrom = el('input', { type: 'range', min: '0', max: String(this.frames.length - 1), value: '0',
      oninput: (e) => this.setTrim(0, Number(e.target.value)) });
    this.trimTo = el('input', { type: 'range', min: '0', max: String(this.frames.length - 1),
      value: String(this.frames.length - 1), oninput: (e) => this.setTrim(1, Number(e.target.value)) });
    this.out = el('div', { class: 've-out' });
    this.progress = el('div', { class: 've-progress', hidden: true },
      el('div', { class: 'loop-track' }, el('div', { class: 'loop-fill' })), el('span', {}, ''));
    this.saveBtn = el('button', { class: 'btn btn-primary', onclick: () => this.export() }, 'Save MP4');

    this.node = el('div', { class: 've-backdrop' },
      el('div', { class: 've', role: 'dialog', 'aria-label': 'Edit video' },
        el('div', { class: 've-head' }, el('b', {}, this.title),
          el('span', { class: 've-name' }, withExtension(this.name, 'mp4')),
          el('button', { class: 'win-close', title: 'Close without saving', onclick: () => this.close(null) }, '×')),
        this.stage,
        el('div', { class: 've-transport' }, this.playBtn, this.scrub, this.when),
        el('div', { class: 've-grid' },
          el('div', { class: 've-group' },
            el('div', { class: 'cm-label' }, 'Trim'),
            el('label', { class: 've-slider' }, el('span', {}, 'Start'), this.trimFrom),
            el('label', { class: 've-slider' }, el('span', {}, 'End'), this.trimTo)),
          el('div', { class: 've-group' },
            el('div', { class: 'cm-label' }, 'Crop — drag the box or its corners'),
            el('div', { class: 'cm-chips' }, ...ASPECTS.map(([v, label]) =>
              chip('aspect', v, label, v === this.aspect, () => this.setAspect(v))))),
          el('div', { class: 've-group' },
            el('div', { class: 'cm-label' }, 'Look'),
            slider('brightness', 'Brightness'), slider('contrast', 'Contrast'),
            slider('saturation', 'Saturation'),
            el('button', { class: 'btn btn-ghost btn-sm', onclick: () => this.resetLook() }, 'Reset look')),
          el('div', { class: 've-group' },
            el('div', { class: 'cm-label' }, 'Speed'),
            el('div', { class: 'cm-chips' }, ...SPEEDS.map((s) =>
              chip('speed', String(s), `${s} fps`, s === this.fps, () => this.setSpeed(s)))))),
        el('div', { class: 've-foot' }, this.out, this.progress,
          el('button', { class: 'btn btn-ghost', onclick: () => this.close(null) }, 'Cancel'),
          this.saveBtn)));
    document.body.append(this.node);
    this.dragCrop();
    this.layoutCrop();
    this.sayOut();
    this.sayWhen();
    new ResizeObserver(() => this.layoutCrop()).observe(this.canvas);
  }

  async draw(i = this.at) {
    const bmp = await this.decode(i);
    const ctx = this.canvas.getContext('2d');
    ctx.filter = filterFor(this.adjust);
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.drawImage(bmp, 0, 0);
    ctx.filter = 'none';
  }

  seek(i) {
    this.at = Math.max(this.trim[0], Math.min(this.trim[1], i));
    this.scrub.value = String(this.at);
    this.draw();
    this.sayWhen();
  }

  sayWhen() {
    const n = this.at - this.trim[0];
    const total = this.trim[1] - this.trim[0] + 1;
    this.when.textContent = `${n + 1} / ${total}`;
  }

  togglePlay() { if (this.playing) this.pause(); else this.play(); }

  play() {
    if (this.playing) return;
    this.playBtn.textContent = '❚❚';
    const tick = async () => {
      if (!this.playing) return;
      const next = this.at >= this.trim[1] ? this.trim[0] : this.at + 1;
      this.seek(next);
      this.playing = setTimeout(tick, 1000 / this.fps);
    };
    this.playing = setTimeout(tick, 1000 / this.fps);
  }

  pause() {
    clearTimeout(this.playing);
    this.playing = null;
    if (this.playBtn) this.playBtn.textContent = '▶';
  }

  setTrim(which, value) {
    if (which === 0) this.trim[0] = Math.min(value, this.trim[1]);
    else this.trim[1] = Math.max(value, this.trim[0]);
    this.trimFrom.value = String(this.trim[0]);
    this.trimTo.value = String(this.trim[1]);
    this.seek(which === 0 ? this.trim[0] : this.trim[1]);
    this.sayOut();
  }

  setAspect(aspect) {
    this.aspect = aspect;
    this.crop = cropFor(aspect, this.w, this.h);
    this.node.querySelectorAll('[data-group="aspect"]').forEach((b) =>
      b.classList.toggle('is-on', b.dataset.value === aspect));
    this.layoutCrop();
    this.sayOut();
  }

  setSpeed(fps) {
    this.fps = fps;
    this.node.querySelectorAll('[data-group="speed"]').forEach((b) =>
      b.classList.toggle('is-on', Number(b.dataset.value) === fps));
    if (this.playing) { this.pause(); this.play(); }
    this.sayOut();
  }

  resetLook() {
    this.adjust = { brightness: 100, contrast: 100, saturation: 100 };
    this.node.querySelectorAll('.ve-slider input[type=range][min="40"]').forEach((input) => { input.value = '100'; });
    this.node.querySelectorAll('[data-key]').forEach((b) => { b.textContent = '100%'; });
    this.draw();
    this.sayOut();
  }

  sayOut() {
    for (const [key, value] of Object.entries(this.adjust)) {
      const label = this.node?.querySelector(`[data-key="${key}"]`);
      if (label) label.textContent = `${value}%`;
    }
    const size = outputSize(this.crop);
    const count = this.trim[1] - this.trim[0] + 1;
    this.out.textContent = `${size.w}×${size.h} · ${lengthSaid(count, this.fps)} · MP4`;
  }

  /** Where the crop sits on screen, from where it sits in the frame. */
  layoutCrop() {
    const shown = this.canvas.getBoundingClientRect();
    const stage = this.stage.getBoundingClientRect();
    const k = shown.width / this.w;
    Object.assign(this.cropBox.style, {
      left: `${shown.left - stage.left + this.crop.x * k}px`,
      top: `${shown.top - stage.top + this.crop.y * k}px`,
      width: `${this.crop.w * k}px`,
      height: `${this.crop.h * k}px`,
    });
  }

  dragCrop() {
    let from = null;
    const ratio = () => {
      if (this.aspect === 'free') return null;
      const [a, b] = this.aspect.split(':').map(Number);
      return a / b;
    };
    this.cropBox.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      this.cropBox.setPointerCapture(e.pointerId);
      from = { x: e.clientX, y: e.clientY, crop: { ...this.crop }, corner: e.target.dataset.corner ?? null };
    });
    this.cropBox.addEventListener('pointermove', (e) => {
      if (!from) return;
      const k = this.canvas.getBoundingClientRect().width / this.w;
      const dx = (e.clientX - from.x) / k;
      const dy = (e.clientY - from.y) / k;
      const c = { ...from.crop };
      if (!from.corner) {
        c.x += dx; c.y += dy;
      } else {
        const left = from.corner.includes('w');
        const top = from.corner.includes('n');
        let w = c.w + (left ? -dx : dx);
        let h = c.h + (top ? -dy : dy);
        const r = ratio();
        if (r) { if (Math.abs(dx) > Math.abs(dy)) h = w / r; else w = h * r; }
        if (left) c.x = from.crop.x + from.crop.w - w;
        if (top) c.y = from.crop.y + from.crop.h - h;
        c.w = w; c.h = h;
      }
      this.crop = clampCrop(c, this.w, this.h);
      this.layoutCrop();
      this.sayOut();
    });
    const end = () => { from = null; };
    this.cropBox.addEventListener('pointerup', end);
    this.cropBox.addEventListener('pointercancel', end);
  }

  setBusy(on, said = '') {
    this.busy = on;
    this.progress.hidden = !on;
    this.saveBtn.disabled = on;
    this.progress.querySelector('span').textContent = said;
  }

  showProgress(fraction, said) {
    this.progress.querySelector('.loop-fill').style.width = `${Math.round(fraction * 100)}%`;
    this.progress.querySelector('span').textContent = said;
  }

  async export() {
    if (this.busy) return;
    this.pause();
    this.setBusy(true, 'Making the MP4…');
    try {
      const job = {
        count: this.trim[1] - this.trim[0] + 1,
        frame: (k) => this.decode(this.trim[0] + k),
        crop: { ...this.crop }, adjust: { ...this.adjust }, fps: this.fps,
        progress: (f, said) => this.showProgress(f, said),
      };
      const blob = await encodeVideo(job);
      const filename = withExtension(this.name, blob.type);
      if (!blob.type.includes('mp4')) {
        toast('This browser cannot write MP4, so the video was saved as WebM.', 'warn');
      } else if (blob.codecName === 'VP9') {
        toast('Saved as MP4 with VP9 video: this browser has no H.264 encoder. It plays in '
          + 'browsers, VLC, Windows and Android, but not on an iPhone.', 'warn');
      }
      await this.save?.(blob, filename);
      this.close(blob);
    } catch (err) {
      this.setBusy(false);
      toast(`The video could not be made: ${err.message}`, 'err');
    }
  }

  close(result) {
    this.pause();
    document.removeEventListener('keydown', this.onKey);
    for (const bmp of this.cache.values()) bmp.close?.();
    this.cache.clear();
    this.node?.remove();
    this.done(result);
  }
}

// ── Encoding ───────────────────────────────────────────────────

/** Draw one edited frame onto a canvas of the output size. */
function paintFrame(ctx, bmp, crop, size, adjust) {
  ctx.filter = filterFor(adjust);
  ctx.drawImage(bmp, crop.x, crop.y, crop.w, crop.h, 0, 0, size.w, size.h);
  ctx.filter = 'none';
}

async function workingConfig(size, fps) {
  if (!window.VideoEncoder || !window.VideoFrame) return null;
  for (const choice of encodersFor(size.w, size.h)) {
    const config = {
      codec: choice.codec, width: size.w, height: size.h, framerate: fps,
      bitrate: Math.round(size.w * size.h * 6),
      ...(choice.mux === 'avc' ? { avc: { format: 'avc' } } : {}),
    };
    try {
      // eslint-disable-next-line no-await-in-loop
      const { supported } = await VideoEncoder.isConfigSupported(config);
      if (supported) return { config, ...choice };
    } catch { /* try the next */ }
  }
  return null;
}

/**
 * The edited frames as a video Blob: MP4 through WebCodecs where the browser
 * has an H.264 encoder, otherwise recorded as they play.
 */
export async function encodeVideo({ count, frame, crop, adjust, fps, progress = () => {} }) {
  const size = outputSize(crop);
  const canvas = document.createElement('canvas');
  canvas.width = size.w;
  canvas.height = size.h;
  const ctx = canvas.getContext('2d');
  const found = await workingConfig(size, fps);
  if (!found) return recordVideo({ count, frame, crop, adjust, fps, progress, canvas, ctx, size });
  const { config } = found;

  const { Muxer, ArrayBufferTarget } = await import('/vendor/mp4-muxer/mp4-muxer.esm.js');
  const muxer = new Muxer({
    target: new ArrayBufferTarget(),
    video: { codec: found.mux, width: size.w, height: size.h, frameRate: fps },
    fastStart: 'in-memory',
  });
  let failure = null;
  const encoder = new VideoEncoder({
    output: (chunk, meta) => muxer.addVideoChunk(chunk, meta),
    error: (e) => { failure = e; },
  });
  encoder.configure(config);
  const step = 1e6 / fps;
  for (let k = 0; k < count; k += 1) {
    if (failure) throw failure;
    // eslint-disable-next-line no-await-in-loop
    paintFrame(ctx, await frame(k), crop, size, adjust);
    const vf = new VideoFrame(canvas, { timestamp: Math.round(k * step), duration: Math.round(step) });
    encoder.encode(vf, { keyFrame: k % Math.max(1, Math.round(fps * 2)) === 0 });
    vf.close();
    if (encoder.encodeQueueSize > 6) {
      // eslint-disable-next-line no-await-in-loop
      await new Promise((ready) => encoder.addEventListener('dequeue', ready, { once: true }));
    }
    progress((k + 1) / count, `Encoding frame ${k + 1} of ${count}`);
  }
  await encoder.flush();
  encoder.close();
  if (failure) throw failure;
  muxer.finalize();
  const blob = new Blob([muxer.target.buffer], { type: 'video/mp4' });
  blob.codecName = found.name;
  return blob;
}

function recorderType() {
  for (const kind of ['video/mp4;codecs=avc1', 'video/mp4', 'video/webm;codecs=vp9',
                      'video/webm;codecs=vp8', 'video/webm']) {
    if (window.MediaRecorder?.isTypeSupported?.(kind)) return kind;
  }
  return '';
}

/** The fallback: the edited frames played onto a canvas and recorded. */
async function recordVideo({ count, frame, crop, adjust, fps, progress, canvas, ctx, size }) {
  if (!window.MediaRecorder) throw new Error('this browser cannot make videos');
  const kind = recorderType();
  const rec = new MediaRecorder(canvas.captureStream(30), kind ? { mimeType: kind } : undefined);
  const chunks = [];
  rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  const stopped = new Promise((ready) => { rec.onstop = ready; });
  paintFrame(ctx, await frame(0), crop, size, adjust);
  rec.start();
  for (let k = 0; k < count; k += 1) {
    // eslint-disable-next-line no-await-in-loop
    paintFrame(ctx, await frame(k), crop, size, adjust);
    progress((k + 1) / count, `Recording frame ${k + 1} of ${count}`);
    // eslint-disable-next-line no-await-in-loop
    await new Promise((ready) => setTimeout(ready, 1000 / fps));
  }
  rec.stop();
  await stopped;
  return new Blob(chunks, { type: (kind || 'video/webm').split(';')[0] });
}
