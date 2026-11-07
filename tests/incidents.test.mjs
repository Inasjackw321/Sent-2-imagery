// Fire incidents: grouping detections, finding hot patches in a frame,
// and telling a detected fire from unconfirmed heat.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CLUSTER_KM, HOLD_HOURS, MATCH_KM, activeAt, clusterIncidents, confirming, hotspots,
  incidentSaid, kmBetween, marksFor, mwSaid, pointAt, sizeWord, toPicture,
} from '../frontend/js/incidents.js';

const fire = (lat, lon, frp, acquired, extra = {}) => ({
  lat, lon, frp, acquired, sensor: 'viirs-snpp', confidence_label: 'nominal',
  resolution_m: 375, ...extra,
});

test('distances are great-circle kilometres', () => {
  assert.ok(Math.abs(kmBetween({ lat: 0, lon: 0 }, { lat: 0, lon: 1 }) - 111.19) < 0.1);
  assert.equal(kmBetween({ lat: 50, lon: 30 }, { lat: 50, lon: 30 }), 0);
});

test('detections a fire front apart are one incident; far apart are two', () => {
  const near = 0.01;   // about a kilometre
  const fires = [
    fire(50, 30, 12, '2026-11-06T01:10:00Z'),
    fire(50 + near, 30, 87, '2026-11-06T11:40:00Z', { confidence_label: 'high' }),
    fire(50 + 2 * near, 30, 5, '2026-11-06T13:00:00Z'),
    fire(51, 31, 3, '2026-11-06T02:00:00Z', { sensor: 'modis' }),
  ];
  const got = clusterIncidents(fires);
  assert.equal(got.length, 2);
  assert.equal(got[0].count, 3);
  assert.equal(got[0].peakMw, 87);
  assert.equal(got[0].totalMw, 104);
  assert.equal(got[0].peakAt, '2026-11-06T11:40:00Z');
  assert.equal(got[0].first, '2026-11-06T01:10:00Z');
  assert.equal(got[0].last, '2026-11-06T13:00:00Z');
  assert.equal(got[0].confidence, 'high');
  assert.equal(got[1].sensors[0], 'modis');
  assert.ok(CLUSTER_KM >= 1);
});

test('no detections is no incidents', () => {
  assert.deepEqual(clusterIncidents([]), []);
  assert.deepEqual(clusterIncidents(null), []);
});

test('a detection is evidence for the hours after it, not before', () => {
  const f = fire(50, 30, 10, '2026-11-06T10:00:00Z');
  assert.equal(activeAt([f], '2026-11-06T09:00:00Z').length, 0);
  assert.equal(activeAt([f], '2026-11-06T10:00:00Z').length, 1);
  assert.equal(activeAt([f], `2026-11-06T${10 + HOLD_HOURS - 1}:00:00Z`).length, 1);
  assert.equal(activeAt([f], `2026-11-06T${10 + HOLD_HOURS + 1}:00:00Z`).length, 0);
  // A whole-day frame is a range; its start is the moment.
  assert.equal(activeAt([f], '2026-11-06T10:00:00Z/2026-11-07T00:00:00Z').length, 1);
});

/** A dark frame, with optional patches of [x, y, r, g, b]. */
function frame(w, h, patches = [], bg = [32, 10, 22]) {
  const data = new Uint8ClampedArray(w * h * 4);
  for (let p = 0; p < w * h; p += 1) data.set([...bg, 255], p * 4);
  for (const [x, y, r, g, b] of patches) data.set([r, g, b, 255], (y * w + x) * 4);
  return data;
}

test('a small red patch in a dark frame is found where it is', () => {
  const data = frame(100, 50, [[70, 20, 240, 60, 20], [71, 20, 200, 40, 20], [70, 21, 180, 30, 20]]);
  const spots = hotspots(data, 100, 50);
  assert.equal(spots.length, 1);
  assert.equal(spots[0].pixels, 3);
  assert.ok(Math.abs(spots[0].x - 70.5 / 100) < 1e-9 && Math.abs(spots[0].y - 20.5 / 50) < 1e-9);
  assert.ok(spots[0].strength > 0.9);
});

test('a white-hot fire counts; a cloud brighter in green and blue does not', () => {
  const white = hotspots(frame(60, 60, [[10, 10, 255, 255, 250]]), 60, 60);
  assert.equal(white.length, 1);
  const cloud = hotspots(frame(60, 60, [[10, 10, 120, 200, 230]]), 60, 60);
  assert.equal(cloud.length, 0);
});

test('a faint smudge a little above the background is not a hot patch', () => {
  // The kind of thing in a dim crop: background 32, brightest 50.
  const data = frame(60, 60, [[30, 30, 50, 24, 38], [31, 30, 48, 22, 36]]);
  assert.deepEqual(hotspots(data, 60, 60), []);
});

test('a frame warm all over is a warm surface, not a list of fires', () => {
  const patches = [];
  for (let y = 0; y < 30; y += 1) for (let x = 0; x < 60; x += 1) patches.push([x, y, 200, 40, 30]);
  assert.deepEqual(hotspots(frame(60, 60, patches), 60, 60), []);
});

test('separate patches stay separate, strongest first', () => {
  const data = frame(80, 80, [[5, 5, 150, 30, 20], [60, 60, 250, 90, 30]]);
  const spots = hotspots(data, 80, 80);
  assert.equal(spots.length, 2);
  assert.ok(spots[0].peak > spots[1].peak);
});

test('picture fractions and ground positions are inverses', () => {
  const box = { west: 30, south: 50, east: 31, north: 51 };
  const at = pointAt({ x: 0.25, y: 0.75 }, box);
  const back = toPicture(at, box);
  assert.ok(Math.abs(back.x - 0.25) < 1e-9 && Math.abs(back.y - 0.75) < 1e-9);
  assert.ok(at.lat > 50 && at.lat < 50.5);
});

test('heat near a recent detection is confirmed; elsewhere it is not', () => {
  const f = fire(50.5, 30.5, 40, '2026-11-06T08:00:00Z');
  const time = '2026-11-06T12:00:00Z';
  assert.equal(confirming({ lat: 50.51, lon: 30.5 }, [f], time), f);
  assert.equal(confirming({ lat: 50.5 + (MATCH_KM + 2) / 111, lon: 30.5 }, [f], time), null);
  assert.equal(confirming({ lat: 50.51, lon: 30.5 }, [f], '2026-11-07T12:00:00Z'), null);
});

test('a frame\'s marks: detections drawn with megawatts, the rest unconfirmed', () => {
  const box = { west: 30, south: 50, east: 31, north: 51 };
  const f = fire(50.5, 30.5, 40, '2026-11-06T02:00:00Z');   // aged past the hold
  const spotOn = { ...toPicture({ lat: 50.5, lon: 30.5 }, box), strength: 0.5 };
  const spotOff = { ...toPicture({ lat: 50.9, lon: 30.1 }, box), strength: 0.2 };
  const marks = marksFor([f], [spotOn, spotOff], '2026-11-06T12:00:00Z', box);
  // The confirmed patch brings its detection back onto the frame.
  assert.deepEqual(marks.detections, [f]);
  assert.equal(marks.unconfirmed.length, 1);
  assert.ok(Math.abs(marks.unconfirmed[0].lat - 50.9) < 1e-6);
});

test('megawatts and sizes are said plainly', () => {
  assert.equal(mwSaid(87), '87 MW');
  assert.equal(mwSaid(12.34), '12.3 MW');
  assert.equal(mwSaid(1530), '1.5 GW');
  assert.equal(mwSaid(0.4), 'under 1 MW');
  assert.equal(mwSaid(null), 'power not measured');
  assert.equal(sizeWord(600), 'Very large fire');
  assert.equal(sizeWord(150), 'Large fire');
  assert.equal(sizeWord(5), 'Small fire');
});

test('an incident in two lines', () => {
  const [inc] = clusterIncidents([
    fire(50, 30, 87, '2026-11-06T11:40:00Z', { confidence_label: 'high' }),
    fire(50.005, 30, 20, '2026-11-06T01:12:00Z', { sensor: 'modis' }),
  ]);
  const said = incidentSaid(inc);
  assert.equal(said.head, 'Fire · 87 MW peak');
  assert.match(said.sub, /2 detections · VIIRS \+ MODIS · high confidence · 6 Nov 01:12 UTC → 6 Nov 11:40 UTC/);
});

test('the marks draw onto a video frame where they are on the ground', async () => {
  const { drawMarksOn } = await import('../frontend/js/incidents.js');
  const arcs = [];
  const texts = [];
  const ctx = new Proxy({}, {
    get: (_, key) => {
      if (key === 'arc') return (x, y, r) => arcs.push([x, y, r]);
      if (key === 'fillText') return (t) => texts.push(t);
      if (key === 'measureText') return (t) => ({ width: t.length * 7 });
      return () => {};
    },
    set: () => true,
  });
  const box = { west: 30, south: 50, east: 31, north: 51 };
  const f = fire(50.5, 30.25, 87, '2026-11-06T02:00:00Z');
  const spot = { ...pointAt({ x: 0.8, y: 0.2 }, box), strength: 0.5 };
  drawMarksOn(ctx, 400, 300, { detections: [f], unconfirmed: [spot] }, box);
  assert.deepEqual(texts, ['? heat', '🔥 87 MW']);
  assert.ok(Math.abs(arcs[0][0] - 320) < 1e-6 && Math.abs(arcs[0][1] - 60) < 1e-6);
  assert.ok(Math.abs(arcs[1][0] - 100) < 1e-6);
});
