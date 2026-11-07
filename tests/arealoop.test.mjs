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

// ── Heat contrast ─────────────────────────────────────────────

import { HEAT_DIM, HEAT_MARGIN, flame, heatContrast, redPercentile } from '../frontend/js/arealoop.js';

/** A frame of `n` pixels at one colour, with some pixels set apart. */
function frame(n, background, special = {}) {
  const data = new Uint8ClampedArray(n * 4);
  for (let i = 0; i < n; i += 1) {
    const [r, g, b] = special[i] ?? background;
    data.set([r, g, b, 255], i * 4);
  }
  return data;
}
const px = (data, i) => Array.from(data.slice(i * 4, i * 4 + 3));

test('a fire stands out: its pixel goes to the flame scale, the scene goes dark', () => {
  const data = frame(1000, [80, 40, 60], { 7: [235, 120, 40] });
  const got = heatContrast(data);
  assert.equal(got.background, 80);
  assert.equal(got.hot, 1);
  const [r, g] = px(data, 7);
  assert.ok(r >= 240 && g >= 180, `the fire is bright: ${px(data, 7)}`);
  assert.deepEqual(px(data, 0), [80 * HEAT_DIM, 40 * HEAT_DIM, 60 * HEAT_DIM]);
});

test('hotter is whiter, the order the RGB itself uses for intensity', () => {
  const [, warmG, warmB] = flame(0.2);
  const [, hotG, hotB] = flame(0.9);
  assert.ok(hotG > warmG && hotB > warmB);
  assert.deepEqual(flame(1).map(Math.round), [255, 255, 235]);
});

test('a frame with no fire stays plain rather than inventing speckle', () => {
  // Noise a few levels either side of the background is not heat.
  const special = {};
  for (let i = 0; i < 1000; i += 37) special[i] = [80 + (i % 9), 40, 60];
  const data = frame(1000, [80, 40, 60], special);
  assert.equal(heatContrast(data).hot, 0);
});

test('heat is judged against the frame itself, not a fixed level', () => {
  // A sunlit desert at noon is warmer everywhere than a fire at night. The
  // same reading that is a fire against a cold night is background here.
  const night = frame(1000, [60, 20, 20], { 3: [150, 60, 20] });
  const noon = frame(1000, [170, 120, 90], { 3: [150, 60, 20] });
  assert.equal(heatContrast(night).hot, 1);
  assert.equal(heatContrast(noon).hot, 0);
});

test('only pixels a clear margin above the background count', () => {
  const just = frame(1000, [100, 40, 40], { 5: [100 + HEAT_MARGIN, 40, 40] });
  assert.equal(heatContrast(just).hot, 0);
  const clear = frame(1000, [100, 40, 40], { 5: [100 + HEAT_MARGIN + 20, 40, 40] });
  assert.equal(heatContrast(clear).hot, 1);
});

test('the background is the frame\'s own 97th percentile of red', () => {
  const special = {};
  for (let i = 0; i < 20; i += 1) special[i] = [250, 0, 0];
  assert.equal(redPercentile(frame(1000, [50, 0, 0], special), 0.97), 50);
});

// ── Every satellite ───────────────────────────────────────────

import { isHeat, spanSaid } from '../frontend/js/arealoop.js';

test('a day of orbit strips is asked for as a transparent PNG', () => {
  const q = Object.fromEntries(new URL(frameUrl('/w', 'x', [0, 0, 1, 1], { width: 1, height: 1 },
    '2026-11-06T00:00:00Z/2026-11-06T23:59:59Z', 'image/png'), 'http://h').searchParams);
  assert.equal(q.format, 'image/png');
  assert.equal(q.transparent, 'true');
  assert.equal(q.time, '2026-11-06T00:00:00Z/2026-11-06T23:59:59Z');
});

test('a whole-day frame says so rather than a clock time', () => {
  assert.equal(frameSaid({ time: '2026-11-06T00:00:00Z/2026-11-06T23:59:59Z' }),
    '6 Nov · whole day');
});

test('heat contrast starts on only for heat products', () => {
  for (const hot of ['MTG FCI Fire Temperature RGB', 'SLSTR Level 2 FRP', 'LST thermal']) {
    assert.equal(isHeat(hot), true, hot);
  }
  for (const not of ['OLCI true colour', 'Sentinel-5P nitrogen dioxide', 'ASCAT winds']) {
    assert.equal(isHeat(not), false, not);
  }
});

test('the span is said the way the backend counted it', () => {
  assert.equal(spanSaid({ span: 'last 14 days' }), 'Last 14 days');
  assert.equal(spanSaid({ span: 'last 48 hours' }), 'Last 48 hours');
  assert.equal(spanSaid(null), 'Last 48 hours');
});
