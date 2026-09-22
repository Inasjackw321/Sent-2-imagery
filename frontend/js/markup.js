// Marking up a picture before sending it to somebody.
//
// A satellite picture on its own answers "what does it look like". It does not
// answer "look HERE", and that is almost always what it is being sent for --
// the revetment at the end of the runway, the two ships that were not there
// last week. Without a way to point, the picture goes out with a paragraph
// underneath saying "middle left, just above the road", which is slower to
// write and worse to read than an arrow.
//
// So: the picture opens in a box, you put arrows and text on it, and what you
// save is the picture with the marks burnt in. Burnt in rather than kept
// beside it, because the file is what gets forwarded and a caption that lives
// in this app reaches nobody.
//
// Three things are deliberate.
//
//   THE MARKS ARE IN THE PICTURE'S OWN COORDINATES, not the screen's. The box
//   is shown at whatever size fits, and a mark placed on a picture shown at
//   half size has to land in the same place at full size. Storing screen
//   pixels would move every mark when the window was resized.
//
//   NOTHING IS DESTRUCTIVE. Every mark can be undone and the picture
//   underneath is never touched, so the export can be repeated and the
//   original is still there. A drawing tool that bakes as you go is one that
//   cannot be corrected.
//
//   IT DOES NOT TOUCH THE MAP. This is a picture editor over a still. The map
//   underneath keeps whatever was on it.

import { $, el, savePicture } from './ui.js';

// What a mark looks like, as a fraction of the picture's smaller side, so a
// mark on a 512 px picture and on a 4096 px one are the same weight.
const STROKE = 0.004;
const HEAD = 0.022;
const TEXT = 0.035;

export const COLOURS = ['#ff4a44', '#ffd23f', '#4ce0b3', '#7fc4ff', '#ffffff'];

let host = null;          // the dialog
let picture = null;       // the HTMLImageElement being marked up
let canvas = null;
let marks = [];
let tool = 'arrow';
let colour = COLOURS[0];
let drawing = null;       // the mark being dragged out
let name = 'imagery';

/** Where a mark sits, as a fraction of the picture. */
export function at(event, box) {
  return {
    x: (event.clientX - box.left) / box.width,
    y: (event.clientY - box.top) / box.height,
  };
}

/** Whether a dragged-out mark is big enough to have been meant. */
export function worthKeeping(mark) {
  if (!mark) return false;
  if (mark.kind === 'text') return Boolean(mark.said?.trim());
  const long = Math.hypot(mark.to.x - mark.from.x, mark.to.y - mark.from.y);
  // A click is not a drag. Below about a fiftieth of the picture it is a
  // stray press, and leaving those in fills the picture with specks.
  return long > 0.02;
}

/** The marks, with the last one removed. Pure, so undo is testable. */
export function undone(all) {
  return all.slice(0, -1);
}

/**
 * Draw every mark onto a context sized to the picture.
 *
 * Exported and taking its size, because the same function draws the preview
 * on screen and the file that gets saved -- at two different sizes. Two
 * drawing routines would drift, and the one that drifted would be the one
 * nobody looked at until it was sent to somebody.
 */
export function paint(ctx, all, wide, tall) {
  const unit = Math.min(wide, tall);
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const mark of all ?? []) {
    ctx.strokeStyle = mark.colour;
    ctx.fillStyle = mark.colour;
    ctx.lineWidth = Math.max(2, unit * STROKE);
    if (mark.kind === 'arrow') arrow(ctx, mark, wide, tall, unit);
    else if (mark.kind === 'box') {
      const x = mark.from.x * wide;
      const y = mark.from.y * tall;
      ctx.strokeRect(x, y, (mark.to.x - mark.from.x) * wide,
                     (mark.to.y - mark.from.y) * tall);
    } else if (mark.kind === 'text') {
      label(ctx, mark, wide, tall, unit);
    }
  }
}

function arrow(ctx, mark, wide, tall, unit) {
  const x0 = mark.from.x * wide;
  const y0 = mark.from.y * tall;
  const x1 = mark.to.x * wide;
  const y1 = mark.to.y * tall;
  const head = unit * HEAD;
  const angle = Math.atan2(y1 - y0, x1 - x0);
  // The shaft stops short of the point, so the line does not stick out
  // through the head and leave a spike on the tip.
  const stop = Math.max(0, Math.hypot(x1 - x0, y1 - y0) - head * 0.9);
  ctx.beginPath();
  ctx.moveTo(x0, y0);
  ctx.lineTo(x0 + Math.cos(angle) * stop, y0 + Math.sin(angle) * stop);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x1 - Math.cos(angle - 0.4) * head, y1 - Math.sin(angle - 0.4) * head);
  ctx.lineTo(x1 - Math.cos(angle + 0.4) * head, y1 - Math.sin(angle + 0.4) * head);
  ctx.closePath();
  ctx.fill();
}

function label(ctx, mark, wide, tall, unit) {
  const size = Math.max(11, unit * TEXT);
  ctx.font = `600 ${size}px system-ui, -apple-system, Segoe UI, sans-serif`;
  ctx.textBaseline = 'top';
  const x = mark.from.x * wide;
  const y = mark.from.y * tall;
  const pad = size * 0.35;
  const width = ctx.measureText(mark.said).width;
  // On a plate. These are satellite pictures: white text over snow or a
  // sunlit roof is not there at all, and neither is red over a rusted roof.
  ctx.fillStyle = 'rgba(8, 11, 16, 0.72)';
  ctx.fillRect(x - pad, y - pad, width + pad * 2, size + pad * 2);
  ctx.fillStyle = mark.colour;
  ctx.fillText(mark.said, x, y);
}

// ── The box ────────────────────────────────────────────────────

/** Open the editor over a picture. `src` may be any same-origin image. */
export function openMarkup(src, saveAs = 'imagery') {
  if (!src) return;
  name = saveAs;
  marks = [];
  drawing = null;
  picture = new Image();
  picture.onload = () => { build(); redraw(); };
  picture.src = src;
}

export function closeMarkup() {
  host?.remove();
  host = null;
  canvas = null;
  marks = [];
  drawing = null;
}

function build() {
  host?.remove();
  const stage = el('div', { class: 'mk-stage' });
  canvas = el('canvas', { class: 'mk-canvas' });
  canvas.width = picture.naturalWidth;
  canvas.height = picture.naturalHeight;
  stage.append(canvas);

  const pick = (key, text) => el('button', {
    class: `mk-tool${tool === key ? ' is-on' : ''}`, type: 'button',
    'data-tool': key,
    onclick: () => {
      tool = key;
      host.querySelectorAll('.mk-tool').forEach(
        (b) => b.classList.toggle('is-on', b.dataset.tool === key));
    },
  }, text);

  host = el('div', { class: 'mk-host', id: 'markupHost' },
    el('div', { class: 'mk-box' },
      el('div', { class: 'mk-bar' },
        pick('arrow', '↗ Arrow'),
        pick('box', '▭ Box'),
        pick('text', 'T Text'),
        el('div', { class: 'mk-colours' },
          ...COLOURS.map((c) => el('button', {
            class: `mk-colour${c === colour ? ' is-on' : ''}`, type: 'button',
            style: `background:${c}`, 'data-colour': c,
            title: c,
            onclick: () => {
              colour = c;
              host.querySelectorAll('.mk-colour').forEach(
                (b) => b.classList.toggle('is-on', b.dataset.colour === c));
            },
          }))),
        el('button', {
          class: 'mk-act', id: 'markupUndo', type: 'button',
          onclick: () => { marks = undone(marks); redraw(); },
        }, 'Undo'),
        el('button', {
          class: 'mk-act', type: 'button',
          onclick: () => { marks = []; redraw(); },
        }, 'Clear'),
        el('button', {
          class: 'mk-act is-go', id: 'markupSave', type: 'button',
          onclick: save,
        }, 'Save PNG'),
        el('button', {
          class: 'mk-act', id: 'markupClose', type: 'button',
          onclick: closeMarkup,
        }, 'Close')),
      stage,
      el('div', { class: 'mk-hint' },
        'Drag to draw. Text asks for the words, then click where it goes. '
        + 'The marks are saved into the picture.')));
  document.body.append(host);

  canvas.addEventListener('pointerdown', down);
  canvas.addEventListener('pointermove', move);
  canvas.addEventListener('pointerup', up);
  canvas.addEventListener('pointercancel', up);
}

function down(event) {
  const box = canvas.getBoundingClientRect();
  const spot = at(event, box);
  if (tool === 'text') {
    // eslint-disable-next-line no-alert
    const said = window.prompt('What should it say?');
    if (said?.trim()) {
      marks.push({ kind: 'text', from: spot, said: said.trim(), colour });
      redraw();
    }
    return;
  }
  canvas.setPointerCapture?.(event.pointerId);
  drawing = { kind: tool, from: spot, to: spot, colour };
}

function move(event) {
  if (!drawing) return;
  drawing.to = at(event, canvas.getBoundingClientRect());
  redraw();
}

function up() {
  if (drawing && worthKeeping(drawing)) marks.push(drawing);
  drawing = null;
  redraw();
}

function redraw() {
  if (!canvas || !picture) return;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(picture, 0, 0, canvas.width, canvas.height);
  paint(ctx, drawing ? [...marks, drawing] : marks, canvas.width, canvas.height);
}

function save() {
  // Drawn again at full size into a canvas of its own rather than exported
  // from the one on screen, so what is saved does not depend on how big the
  // window happened to be.
  const out = document.createElement('canvas');
  out.width = picture.naturalWidth;
  out.height = picture.naturalHeight;
  const ctx = out.getContext('2d');
  ctx.drawImage(picture, 0, 0);
  paint(ctx, marks, out.width, out.height);
  // Through the one save path the rest of the app uses. A bare anchor click
  // -- which this was -- is a request a browser may simply drop, and a
  // marked-up picture that does not arrive looks exactly like a button that
  // does nothing.
  out.toBlob((blob) => {
    savePicture(blob, `${name}_marked_kaldockhi.png`,
      { title: 'Imagery', what: 'The marked-up picture' });
  }, 'image/png');
}
