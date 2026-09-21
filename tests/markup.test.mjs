// Tests for marking up a picture before sending it to somebody.
//
// Run with:  node --test tests/markup.test.mjs
//
// A satellite picture answers "what does it look like". It does not answer
// "look HERE", which is almost always what it is being sent for -- so the
// arrow is the point, and the arrow has to land where it was drawn.
//
// The one that would be invisible and wrong: marks are stored as a fraction
// of the picture, not as screen pixels, because the box shows the picture at
// whatever size fits and the file is saved at full size. Screen pixels would
// put every mark in the wrong place in the file that actually gets sent.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { COLOURS, at, paint, undone, worthKeeping }
  from '../frontend/js/markup.js';

const BOX = { left: 100, top: 50, width: 400, height: 300 };

// ── Where a mark lands ─────────────────────────────────────────

test('a mark is stored as a fraction of the picture', () => {
  assert.deepEqual(at({ clientX: 300, clientY: 200 }, BOX), { x: 0.5, y: 0.5 });
});

test('the top-left corner is the origin', () => {
  assert.deepEqual(at({ clientX: 100, clientY: 50 }, BOX), { x: 0, y: 0 });
});

test('so the same mark lands in the same place at any size', () => {
  // The whole reason fractions are stored. The box shows the picture at
  // whatever fits and the file is saved at full size: screen pixels would
  // move every mark in the file that actually gets sent.
  const small = at({ clientX: 300, clientY: 200 }, BOX);
  const large = at({ clientX: 500, clientY: 350 },
                   { left: 100, top: 50, width: 800, height: 600 });
  assert.deepEqual(small, large);
});

// ── What counts as a mark ──────────────────────────────────────

test('a dragged-out arrow is kept', () => {
  assert.equal(worthKeeping({ kind: 'arrow', from: { x: 0.1, y: 0.1 },
                              to: { x: 0.6, y: 0.6 } }), true);
});

test('a stray click is not', () => {
  // Leaving those in fills the picture with specks nobody meant.
  assert.equal(worthKeeping({ kind: 'arrow', from: { x: 0.5, y: 0.5 },
                              to: { x: 0.505, y: 0.5 } }), false);
});

test('text with words is kept and text without is not', () => {
  assert.equal(worthKeeping({ kind: 'text', said: 'revetment' }), true);
  assert.equal(worthKeeping({ kind: 'text', said: '   ' }), false);
  assert.equal(worthKeeping({ kind: 'text' }), false);
});

test('nothing is not a mark', () => {
  assert.equal(worthKeeping(null), false);
});

// ── Undo ───────────────────────────────────────────────────────

test('undo takes the last mark off', () => {
  assert.deepEqual(undone([1, 2, 3]), [1, 2]);
});

test('and undoing an empty picture is not an error', () => {
  assert.deepEqual(undone([]), []);
});

test('undo does not touch the picture underneath', () => {
  // It returns a new list rather than mutating, so the original is still
  // there -- a drawing tool that bakes as you go cannot be corrected.
  const all = [1, 2, 3];
  undone(all);
  assert.deepEqual(all, [1, 2, 3]);
});

// ── Drawing ────────────────────────────────────────────────────

/** A stand-in 2D context that records what it was asked to do. */
function fakeCtx() {
  const calls = [];
  const record = (name) => (...args) => calls.push([name, ...args]);
  return {
    calls,
    set strokeStyle(v) { calls.push(['strokeStyle', v]); },
    set fillStyle(v) { calls.push(['fillStyle', v]); },
    set lineWidth(v) { calls.push(['lineWidth', v]); },
    set lineCap(v) {}, set lineJoin(v) {}, set font(v) {}, set textBaseline(v) {},
    beginPath: record('beginPath'), moveTo: record('moveTo'),
    lineTo: record('lineTo'), stroke: record('stroke'), fill: record('fill'),
    closePath: record('closePath'), strokeRect: record('strokeRect'),
    fillRect: record('fillRect'), fillText: record('fillText'),
    measureText: () => ({ width: 60 }),
  };
}

const ARROW = { kind: 'arrow', from: { x: 0.25, y: 0.5 }, to: { x: 0.75, y: 0.5 },
                colour: '#ff4a44' };

test('an arrow is drawn in the picture\'s own pixels', () => {
  const ctx = fakeCtx();
  paint(ctx, [ARROW], 400, 200);
  const moves = ctx.calls.filter(([n]) => n === 'moveTo');
  assert.deepEqual(moves[0], ['moveTo', 100, 100]);
});

test('and the same arrow scales with the picture', () => {
  // Saved at full size, previewed at whatever fits: one drawing routine, two
  // sizes. Two routines would drift and the one that drifted would be the
  // one nobody looked at until it was sent.
  const ctx = fakeCtx();
  paint(ctx, [ARROW], 800, 400);
  const moves = ctx.calls.filter(([n]) => n === 'moveTo');
  assert.deepEqual(moves[0], ['moveTo', 200, 200]);
});

test('and so does the weight of the mark, not only its endpoints', () => {
  // The endpoints come from the picture's width directly, so they scale
  // whatever happens. The stroke and the arrowhead come from the smaller
  // side, and a fixed value there gives a hairline on a 4096 px picture and
  // a blob on a 512 px one -- neither of which the preview would show.
  // Above the floor at both sizes. The stroke has a two-pixel minimum, so a
  // small enough picture scales by the floor rather than by the picture and
  // would hide the very thing this is checking.
  const small = fakeCtx();
  const large = fakeCtx();
  paint(small, [ARROW], 1000, 500);
  paint(large, [ARROW], 4000, 2000);
  const widthOf = (c) => c.calls.find(([n]) => n === 'lineWidth')[1];
  assert.ok(widthOf(large) > widthOf(small) * 3,
    `${widthOf(small)} -> ${widthOf(large)}`);
  // The head too: its points are placed from the same unit.
  const headSpan = (c) => {
    // The shaft contributes one lineTo, the head the next two. Measured
    // across the head, which for a horizontal arrow is its Y spread -- the
    // two barbs share an X, so comparing those is always zero and always
    // passes.
    const pts = c.calls.filter(([n]) => n === 'lineTo').slice(1);
    return Math.abs(pts[0][2] - pts[1][2]);
  };
  assert.ok(headSpan(large) > headSpan(small) * 3,
    `${headSpan(small)} -> ${headSpan(large)}`);
});

test('an arrow has a head as well as a shaft', () => {
  const ctx = fakeCtx();
  paint(ctx, [ARROW], 400, 200);
  assert.ok(ctx.calls.some(([n]) => n === 'fill'), 'the head is filled');
  assert.ok(ctx.calls.some(([n]) => n === 'stroke'), 'the shaft is stroked');
});

test('the shaft stops short of the point', () => {
  // Otherwise the line sticks out through the head and leaves a spike.
  const ctx = fakeCtx();
  paint(ctx, [ARROW], 400, 200);
  const shaft = ctx.calls.filter(([n]) => n === 'lineTo')[0];
  assert.ok(shaft[1] < 300, `shaft ended at ${shaft[1]}, tip is at 300`);
});

test('a box is drawn as a rectangle', () => {
  const ctx = fakeCtx();
  paint(ctx, [{ kind: 'box', from: { x: 0.25, y: 0.25 },
                to: { x: 0.75, y: 0.5 }, colour: '#ffd23f' }], 400, 200);
  const rect = ctx.calls.find(([n]) => n === 'strokeRect');
  assert.deepEqual(rect, ['strokeRect', 100, 50, 200, 50]);
});

test('text is drawn on a plate', () => {
  // These are satellite pictures: white text over snow, or red over a rusted
  // roof, is not there at all.
  const ctx = fakeCtx();
  paint(ctx, [{ kind: 'text', from: { x: 0.1, y: 0.1 }, said: 'two ships',
                colour: '#ffffff' }], 400, 200);
  assert.ok(ctx.calls.some(([n]) => n === 'fillRect'), 'plate');
  assert.ok(ctx.calls.some(([n]) => n === 'fillText'), 'words');
});

test('each mark keeps its own colour', () => {
  const ctx = fakeCtx();
  paint(ctx, [{ ...ARROW, colour: '#ff4a44' },
              { ...ARROW, colour: '#4ce0b3' }], 400, 200);
  const used = ctx.calls.filter(([n]) => n === 'strokeStyle').map(([, v]) => v);
  assert.deepEqual(used, ['#ff4a44', '#4ce0b3']);
});

test('nothing to draw draws nothing', () => {
  const ctx = fakeCtx();
  paint(ctx, [], 400, 200);
  assert.equal(ctx.calls.filter(([n]) => n === 'stroke').length, 0);
  paint(ctx, null, 400, 200);
});

test('a mark of a kind nobody knows is skipped rather than thrown on', () => {
  const ctx = fakeCtx();
  assert.doesNotThrow(() => paint(ctx, [{ kind: 'spiral', from: { x: 0, y: 0 },
                                          to: { x: 1, y: 1 } }], 400, 200));
});

test('every colour on offer is a real one', () => {
  assert.ok(COLOURS.length >= 3);
  for (const c of COLOURS) assert.match(c, /^#[0-9a-f]{6}$/i);
});

// ── The wiring ─────────────────────────────────────────────────

const SOURCE = readFileSync(
  new URL('../frontend/js/markup.js', import.meta.url), 'utf8');
const PANEL = readFileSync(
  new URL('../frontend/js/imagery.js', import.meta.url), 'utf8');

test('what is saved is drawn at full size, not scraped off the screen', () => {
  const at2 = SOURCE.indexOf('function save()');
  const block = SOURCE.slice(at2, at2 + 700);
  assert.match(block, /picture\.naturalWidth/);
  assert.match(block, /paint\(ctx, marks, out\.width, out\.height\)/);
});

test('the editor opens on the picture that is on screen', () => {
  // Not a fresh render: what gets annotated has to be what was being looked
  // at, adjustments and all.
  assert.match(PANEL, /openMarkup\(store\.image\.src, exportStem\(\)\)/);
});

test('annotating needs a picture and animating needs two dates', () => {
  // Different conditions, so they are not the same disabled check.
  assert.match(PANEL, /#markupBtn'\)\.disabled = !store\.image\?\.src/);
  assert.match(PANEL, /#animateBtn'\)\.disabled = chosenDates\(\)\.length < 2/);
});

test('an animation of one date is refused before anything is rendered', () => {
  const at3 = PANEL.indexOf('async function makeAnimation');
  const block = PANEL.slice(at3, at3 + 900);
  assert.match(block, /dates\.length < 2/);
});
