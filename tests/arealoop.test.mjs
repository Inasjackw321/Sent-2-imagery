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

import {
  HEAT_DIM, HEAT_MARGIN, LOCAL_KM, flame, heatContrast, localRadius, localRed,
} from '../frontend/js/arealoop.js';

/** A `w` x `h` frame at one colour, with some pixels (by index) set apart. */
function frame(w, h, background, special = {}) {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i += 1) {
    const [r, g, b] = special[i] ?? background;
    data.set([r, g, b, 255], i * 4);
  }
  return data;
}
const px = (data, i) => Array.from(data.slice(i * 4, i * 4 + 3));
const at = (w, x, y) => y * w + x;

test('a fire stands out: its pixel goes to the flame scale, the scene goes dark', () => {
  const data = frame(40, 25, [80, 40, 60], { [at(40, 20, 12)]: [235, 120, 40] });
  const got = heatContrast(data, 40, 25, 6);
  assert.equal(got.hot, 1);
  const [r, g] = px(data, at(40, 20, 12));
  assert.ok(r >= 240 && g >= 180, `the fire is bright: ${px(data, at(40, 20, 12))}`);
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
  assert.equal(heatContrast(frame(40, 25, [80, 40, 60], special), 40, 25, 6).hot, 0);
});

test('heat is judged against the ground around it, not a fixed level', () => {
  // A sunlit desert at noon is warmer everywhere than a fire at night. The
  // same reading that is a fire against a cold night is background here.
  const fire = { [at(40, 20, 12)]: [150, 60, 20] };
  assert.equal(heatContrast(frame(40, 25, [60, 20, 20], fire), 40, 25, 6).hot, 1);
  assert.equal(heatContrast(frame(40, 25, [170, 120, 90], fire), 40, 25, 6).hot, 0);
});

test('only pixels a clear margin above their surroundings count', () => {
  const just = frame(40, 25, [100, 40, 40], { [at(40, 20, 12)]: [100 + HEAT_MARGIN, 40, 40] });
  assert.equal(heatContrast(just, 40, 25, 6).hot, 0);
  const clear = frame(40, 25, [100, 40, 40], { [at(40, 20, 12)]: [100 + HEAT_MARGIN + 25, 40, 40] });
  assert.equal(heatContrast(clear, 40, 25, 6).hot, 1);
});

test('a fire is not hidden by warm ground elsewhere in the box', () => {
  // The bug: half the box warm ground, and the fire on the cold half
  // disappeared because the whole box's 97th percentile rose past it. Judged
  // against its own surroundings it stays a fire, and the warm half, being
  // warm all the way across, is not.
  const w = 120;
  const h = 60;
  const special = {};
  for (let y = 0; y < h; y += 1) for (let x = 60; x < w; x += 1) special[at(w, x, y)] = [170, 40, 40];
  special[at(w, 20, 30)] = [160, 70, 30];
  const data = frame(w, h, [50, 15, 30], special);
  heatContrast(data, w, h, 8);
  const [r, g] = px(data, at(w, 20, 30));
  assert.ok(r >= 230 && g >= 60, `the fire is still a fire: ${px(data, at(w, 20, 30))}`);
  assert.ok(px(data, at(w, 100, 30))[0] <= 170 * HEAT_DIM + 1, 'the warm half is not heat');
});

test('the same fire is judged alike in a small box and a large one', () => {
  // The neighbourhood is kilometres, so the radius in pixels follows the scale.
  assert.equal(localRadius(2), LOCAL_KM * 2);
  assert.equal(localRadius(0.5), LOCAL_KM / 2);
  assert.ok(localRadius(1000) <= 96 && localRadius(0.001) >= 4);
});

test('the neighbourhood mean is the plain average, clipped at the edges', () => {
  const data = frame(3, 3, [0, 0, 0], { 4: [90, 0, 0] });
  const mean = localRed(data, 3, 3, 1);
  assert.equal(mean[4], 10);          // the middle: 90 over nine pixels
  assert.equal(mean[0], 90 / 4);      // a corner: 90 over its four
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
  assert.equal(frameSaid({ time: '2026-11-08' }), '8 Nov · whole day');
});

test('the thermal view starts on only for fire-heat products', () => {
  for (const hot of ['MTG FCI Fire Temperature RGB', 'SLSTR Level 2 FRP',
    'SLSTR Level 2 Fire Radiative Power (FRP) - Sentinel-3A', 'rgb_firetemperature']) {
    assert.equal(isHeat(hot), true, hot);
  }
  // Products with a colour scale of their own are left as drawn.
  for (const not of ['OLCI true colour', 'Sentinel-5P nitrogen dioxide', 'ASCAT winds',
    'Land Surface Temperature (Day, MODIS, Terra)', 'Fires and Thermal Anomalies (VIIRS)',
    'Carbon Dioxide (Total Column Average, OCO-2)']) {
    assert.equal(isHeat(not), false, not);
  }
});

test('the span is said the way the backend counted it', () => {
  assert.equal(spanSaid({ span: 'last 14 days' }), 'Last 14 days');
  assert.equal(spanSaid({ span: 'last 48 hours' }), 'Last 48 hours');
  assert.equal(spanSaid(null), 'Last 48 hours');
});

// ── An empty loop says why ───────────────────────────────────

import { drawnShare, emptySaid } from '../frontend/js/arealoop.js';

test('how much of a frame has anything drawn on it', () => {
  const clear = new Uint8ClampedArray(4 * 10);           // all transparent
  assert.equal(drawnShare(clear), 0);
  const some = new Uint8ClampedArray(4 * 10);
  some[3] = 255; some[7] = 255;
  assert.equal(drawnShare(some), 0.2);
});

test('an empty fire product is explained, not left looking broken', () => {
  assert.match(emptySaid('SLSTR Level 2 Fire', 14, true),
    /only the pixels where the satellite detected a fire.*none in this box over these 14 days/);
  assert.match(emptySaid('OLCI Level 2 CHL', 14, true), /nothing over this box in these 14 days/);
});

// ── Thermal view ─────────────────────────────────────────────

import {
  MOST_SPOTS, VIEWS, hotSpots, spotsSaid, thermalColour, thermalMap,
} from '../frontend/js/arealoop.js';

test('the thermal scale runs navy, red, orange, yellow, white', () => {
  const [r0, , b0] = thermalColour(0);
  assert.ok(b0 > r0, 'cold is blue');
  const [rMid, gMid] = thermalColour(0.6);
  assert.ok(rMid > 180 && gMid < 80, 'warm is red');
  const [r9, g9, b9] = thermalColour(0.9);
  assert.ok(r9 > 240 && g9 > 180 && b9 < 120, 'hot is yellow');
  assert.deepEqual(thermalColour(1), [255, 255, 225]);
  assert.deepEqual(thermalColour(2), thermalColour(1));
});

test('a frame becomes a heat map of its red channel; gaps stay clear', () => {
  const data = new Uint8ClampedArray([30, 10, 20, 255, 250, 90, 30, 255, 200, 0, 0, 0]);
  thermalMap(data);
  assert.deepEqual(Array.from(data.slice(0, 3)), thermalColour(30 / 255));
  assert.deepEqual(Array.from(data.slice(4, 7)), thermalColour(250 / 255));
  assert.deepEqual(Array.from(data.slice(8, 12)), [200, 0, 0, 0]);
});

test('a fire is a hot spot, found where it is', () => {
  const w = 60;
  const h = 40;
  const special = { [at(w, 30, 20)]: [240, 80, 30], [at(w, 31, 20)]: [200, 60, 30] };
  const spots = hotSpots(frame(w, h, [40, 12, 25], special), w, h, 6);
  assert.equal(spots.length, 1);
  assert.equal(spots[0].pixels, 2);
  assert.ok(Math.abs(spots[0].x - 30.5 / w) < 1e-9 && Math.abs(spots[0].y - 20.5 / h) < 1e-9);
  assert.ok(spots[0].above > 150);
});

test('warm ground, cloud and noise are not hot spots', () => {
  const w = 60;
  const h = 40;
  const warm = {};
  for (let y = 0; y < h; y += 1) for (let x = 0; x < 30; x += 1) warm[at(w, x, y)] = [180, 50, 40];
  assert.deepEqual(hotSpots(frame(w, h, [40, 12, 25], warm), w, h, 6), [],
    'a warm half is warm all across, not a spot');
  const cloud = { [at(w, 30, 20)]: [150, 220, 240] };
  assert.deepEqual(hotSpots(frame(w, h, [40, 12, 25], cloud), w, h, 6), []);
  const noise = {};
  for (let i = 0; i < w * h; i += 13) noise[i] = [40 + (i % 15), 12, 25];
  assert.deepEqual(hotSpots(frame(w, h, [40, 12, 25], noise), w, h, 6), []);
});

test('separate fires are separate spots, hottest first, and capped', () => {
  const w = 200;
  const h = 100;
  const special = {};
  for (let k = 0; k < MOST_SPOTS + 5; k += 1) special[at(w, 5 + k * 6, 50)] = [120 + k * 4, 30, 20];
  const spots = hotSpots(frame(w, h, [40, 12, 25], special), w, h, 4);
  assert.equal(spots.length, MOST_SPOTS);
  assert.ok(spots[0].peak >= spots[1].peak);
});

test('the bar says how many', () => {
  assert.equal(spotsSaid([]), 'No hot spots');
  assert.equal(spotsSaid([{}]), '🔥 1 hot spot');
  assert.equal(spotsSaid([{}, {}, {}]), '🔥 3 hot spots');
  assert.equal(spotsSaid(undefined), '');
  assert.deepEqual(VIEWS.map(([v]) => v), ['thermal', 'contrast', 'picture']);
});

test('the edge of warm ground does not glow as heat', () => {
  // A sunlit coast: warm land beside cold sea. Each side is even; the edge
  // between them is where a plain average would call the warm side hot.
  const w = 60;
  const h = 40;
  const warm = {};
  for (let y = 0; y < h; y += 1) for (let x = 0; x < 30; x += 1) warm[at(w, x, y)] = [180, 50, 40];
  assert.equal(heatContrast(frame(w, h, [40, 12, 25], warm), w, h, 6).hot, 0);
});

test('a big fire does not hide itself by raising its own surroundings', () => {
  // A fire front filling a quarter of the neighbourhood.
  const w = 40;
  const h = 40;
  const special = {};
  for (let y = 14; y < 26; y += 1) for (let x = 14; x < 26; x += 1) special[at(w, x, y)] = [245, 120, 40];
  const spots = hotSpots(frame(w, h, [40, 12, 25], special), w, h, 12);
  assert.equal(spots.length, 1);
  assert.ok(spots[0].pixels > 50);
});
