// Tests for how tight the exported picture is drawn.
//
// Run with:  node --test tests/shot.test.mjs
//
// The picture used to be framed by the map view. A map view is chosen for
// looking at with panels down one side, and what is in the air is usually
// clustered in one part of it -- so the thing being sent was a few specks in
// a lot of empty dark. These are about the two ways of overcorrecting: a
// crop so tight there is no landmark left in it, and a crop that quietly
// shows more ground than was asked for.

import test from 'node:test';
import assert from 'node:assert/strict';

import { closeIn } from '../frontend/js/trackershot.js';

const VIEW = { north: 52.5, south: 45.5, west: 22.0, east: 40.0 };
const mark = (lat, lon) => ({ lat, lon });

test('a cluster in one corner crops to the cluster', () => {
  const got = closeIn(VIEW, [mark(50.0, 31.0), mark(50.3, 32.0),
                             mark(49.7, 32.5)]);
  // Comfortably inside the view on every side.
  assert.ok(got.north < VIEW.north && got.south > VIEW.south);
  assert.ok(got.west > VIEW.west && got.east < VIEW.east);
  // And a small fraction of the ground: the view is 18 degrees across.
  assert.ok(got.east - got.west < 3, `${got.east - got.west} degrees wide`);
});

test('the marks all stay inside the crop', () => {
  const marks = [mark(50.0, 31.0), mark(50.3, 32.0), mark(49.7, 32.5)];
  const got = closeIn(VIEW, marks);
  for (const m of marks) {
    assert.ok(m.lat <= got.north && m.lat >= got.south, `${m.lat} off frame`);
    assert.ok(m.lon >= got.west && m.lon <= got.east, `${m.lon} off frame`);
  }
});

test('there is room around them rather than a crop through them', () => {
  const got = closeIn(VIEW, [mark(50.0, 31.0), mark(51.0, 33.0)]);
  assert.ok(got.north > 51.0, 'the top mark is on the edge');
  assert.ok(got.south < 50.0, 'the bottom mark is on the edge');
});

test('one mark on its own does not become a picture of nothing', () => {
  // Without a floor this is a crop of zero degrees at infinite magnification,
  // with no landmark in it to say where it was taken.
  const got = closeIn(VIEW, [mark(50.0, 31.0)]);
  assert.ok(got.north - got.south >= 0.7, `${got.north - got.south} tall`);
  assert.ok(got.east - got.west >= 0.7, `${got.east - got.west} wide`);
});

test('two marks a mile apart get the same floor', () => {
  const got = closeIn(VIEW, [mark(50.000, 31.000), mark(50.008, 31.012)]);
  assert.ok(got.north - got.south >= 0.7);
  assert.ok(got.east - got.west >= 0.7);
});

test('it never shows more ground than was asked for', () => {
  // "The whole country" has to keep meaning the country. A crop that grew
  // past the view would be the picture answering a question nobody asked.
  const spread = [mark(52.4, 22.1), mark(45.6, 39.9)];
  const got = closeIn(VIEW, spread);
  assert.ok(got.north <= VIEW.north && got.south >= VIEW.south);
  assert.ok(got.west >= VIEW.west && got.east <= VIEW.east);
});

test('the floor does not push a crop past the view either', () => {
  // A single mark inside a view smaller than the floor: the crop is the
  // view, not the floor.
  const tiny = { north: 50.1, south: 50.0, west: 31.0, east: 31.1 };
  const got = closeIn(tiny, [mark(50.05, 31.05)]);
  assert.deepEqual(got, tiny);
});

test('no marks means the view, unchanged', () => {
  assert.equal(closeIn(VIEW, []), VIEW);
  assert.equal(closeIn(VIEW, undefined), VIEW);
});

// The wiring and the size, read off the source. Neither is arithmetic that
// can be called from here -- drawShot needs a canvas -- but both are a line
// that can be deleted without a single test above noticing, which is exactly
// what a mutation run showed.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const SOURCE = readFileSync(
  fileURLToPath(new URL('../frontend/js/trackershot.js', import.meta.url)),
  'utf8');

test('the picture is actually drawn from the tightened frame', () => {
  assert.ok(SOURCE.includes('framing(closeIn(bounds, marks))'),
    'drawShot frames the whole view again');
});

test('a mark is small enough to say where it is', () => {
  // A glyph wide enough to cover the town it is over has stopped being a
  // position. Forty-four was chosen against a picture of a whole country;
  // the frame is much tighter now, so the same glyph covers far more ground.
  const size = Number(/const MARK_PX = (\d+)/.exec(SOURCE)?.[1]);
  assert.ok(size > 0, 'MARK_PX is gone');
  assert.ok(size <= 32, `a mark is ${size}px on a ${1600}px picture`);
});
