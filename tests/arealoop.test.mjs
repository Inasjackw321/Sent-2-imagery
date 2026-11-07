// Two days of a Meteosat product over a box you draw: the arithmetic of it.
//
// The fetching and the video need a browser; what is checked here is what
// decides whether they are right -- how big each picture is, what it asks the
// proxy for, which order the frames come in, and which frame plays next when
// some never arrived.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  AT_ONCE, LEAST_PX, MOST_PX, SPEEDS, areaSize, fetchOrder, filmName, frameSaid,
  frameUrl, nextHeld, progressSaid,
} from '../frontend/js/arealoop.js';

test('a picture keeps the shape of the box', () => {
  const wide = areaSize([0, 0, 200000, 100000], 300);
  assert.equal(wide.width, 600);
  assert.equal(wide.height, 300);
  const tall = areaSize([0, 0, 100000, 200000], 300);
  assert.deepEqual([tall.width, tall.height], [300, 600]);
});

test('no bigger than the ceiling, no smaller than the floor', () => {
  assert.equal(areaSize([0, 0, 1, 1], 5000).width, MOST_PX);
  assert.equal(areaSize([0, 0, 1, 1], 5).width, LEAST_PX);
});

test('each frame is one picture of the box at one time, through this app', () => {
  const url = new URL(frameUrl('/api/copernicus/wms', 'mtg_fd:rgb_firetemperature',
    [100, 200, 300, 400], { width: 512, height: 256 }, '2026-11-07T12:00:00Z'),
  'http://here');
  assert.equal(url.pathname, '/api/copernicus/wms');
  const q = Object.fromEntries(url.searchParams);
  assert.equal(q.request, 'GetMap');
  assert.equal(q.layers, 'mtg_fd:rgb_firetemperature');
  assert.equal(q.crs, 'EPSG:3857');
  assert.equal(q.bbox, '100.0,200.0,300.0,400.0');
  assert.equal(q.width, '512');
  assert.equal(q.height, '256');
  assert.equal(q.time, '2026-11-07T12:00:00Z');
  // A tenth of the size of a PNG, and a whole-area picture has no holes.
  assert.equal(q.format, 'image/jpeg');
});

test('the newest is fetched first, so the latest picture is up soonest', () => {
  assert.deepEqual(fetchOrder(4), [3, 2, 1, 0]);
  assert.equal(fetchOrder(288).length, 288);
});

test('the loading bar says how far it has got, and what was not served', () => {
  assert.equal(progressSaid(120, 0, 288), '120 of 288');
  assert.equal(progressSaid(120, 3, 288), '123 of 288 · 3 not served');
});

test('playing skips a frame that never arrived, and wraps round', () => {
  const frames = [{ img: 1 }, {}, { img: 1 }, { img: 1 }];
  assert.equal(nextHeld(frames, 0), 2);
  assert.equal(nextHeld(frames, 3), 0);
  assert.equal(nextHeld(frames, 2, -1), 0);
  assert.equal(nextHeld([{}, {}], 0), -1);
});

test('the video is named for the product and the two days it covers', () => {
  assert.equal(filmName('mtg_fd:rgb_firetemperature',
    [{ time: '2026-11-05T12:10:00Z' }, { time: '2026-11-07T12:00:00Z' }]),
  'rgb_firetemperature_area_202611051210-202611071200_kaldockhi');
});

test('each frame is stamped in UTC', () => {
  assert.equal(frameSaid({ time: '2026-11-06T14:30:00Z' }), '6 Nov 14:30 UTC');
  assert.equal(frameSaid(null), '');
});

test('a sensible pace and a sensible number of requests at once', () => {
  assert.ok(AT_ONCE >= 2 && AT_ONCE <= 8);
  assert.ok(SPEEDS.every((s) => s > 0 && s <= 30));
});
