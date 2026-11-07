// The frame every saved video is laid out in: where things go, what the
// minimap shows, and what the panel says.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  FILM_H, FILM_W, PANEL_W, coordSaid, layout, mercY, minimapView, sizeSaid, spanAt,
  toMinimap, whenSaid,
} from '../frontend/js/filmtemplate.js';
import { spanHours } from '../frontend/js/arealoop.js';

test('the imagery fits beside the panel without being stretched', () => {
  for (const [w, h] of [[700, 500], [400, 900], [1600, 300]]) {
    const at = layout(w, h).image;
    assert.ok(at.x >= 0 && at.x + at.w <= FILM_W - PANEL_W, `${w}×${h} fits across`);
    assert.ok(at.y >= 0 && at.y + at.h <= FILM_H, `${w}×${h} fits down`);
    assert.ok(Math.abs(at.w / at.h - w / h) < 0.02, `${w}×${h} keeps its shape`);
  }
});

test('the panel is the right-hand strip, and the minimap is inside it', () => {
  const { panel, minimap } = layout(700, 500);
  assert.deepEqual([panel.x, panel.w, panel.h], [FILM_W - PANEL_W, PANEL_W, FILM_H]);
  assert.ok(minimap.x >= panel.x && minimap.x + minimap.w <= FILM_W);
  assert.ok(minimap.y > FILM_H / 2 && minimap.y + minimap.h < FILM_H);
});

test('the minimap shows the area with room around it to recognise the place', () => {
  const area = { west: 34.6, south: 30.6, east: 35.1, north: 31.0 };
  const view = minimapView(area, 320 / 230);
  assert.ok(view.x1 - view.x0 >= 12, 'at least twelve degrees across');
  assert.ok(view.x0 < area.west && view.x1 > area.east);
  assert.ok(view.y0 < mercY(area.south) && view.y1 > mercY(area.north));
  assert.ok(Math.abs((view.x1 - view.x0) / (view.y1 - view.y0) - 320 / 230) < 1e-9,
    'shaped to the minimap');
  // And a big area still fits, six times over.
  const big = minimapView({ west: 20, south: 40, east: 40, north: 55 }, 1.4);
  assert.ok(big.x1 - big.x0 >= 120);
});

test('the area lands in the middle of the minimap', () => {
  const area = { west: 34.6, south: 30.6, east: 35.1, north: 31.0 };
  const rect = { x: 0, y: 0, w: 320, h: 230 };
  const view = minimapView(area, rect.w / rect.h);
  const nw = toMinimap(view, rect, area.west, area.north);
  const se = toMinimap(view, rect, area.east, area.south);
  assert.ok(Math.abs((nw.x + se.x) / 2 - 160) < 1);
  assert.ok(Math.abs((nw.y + se.y) / 2 - 115) < 1);
  assert.ok(nw.y < se.y, 'north is up');
});

test('the timeline knows where a frame is in the span', () => {
  const frames = new Array(288).fill({});
  assert.equal(spanAt(frames, 0), 0);
  assert.equal(spanAt(frames, 287), 1);
  assert.ok(Math.abs(spanAt(frames, 143) - 0.498) < 0.01);
  assert.equal(spanAt([{}], 0), 1);
});

test('the panel says when, in UTC, and a whole-day frame says so', () => {
  assert.deepEqual(whenSaid('2026-11-06T21:30:00Z'), { day: '6 Nov 2026', clock: '21:30 UTC' });
  assert.equal(whenSaid('2026-11-06T00:00:00Z/2026-11-06T23:59:59Z').clock, 'whole day, UTC');
  assert.deepEqual(whenSaid(undefined), { day: '', clock: '' });
});

test('the panel says where and how big', () => {
  assert.equal(coordSaid(30.73, 35.03), '30.73° N, 35.03° E');
  assert.equal(coordSaid(-33.9, -70.65), '33.90° S, 70.65° W');
  assert.equal(sizeSaid({ west: 0, south: 0, east: 1, north: 1 }), '111 × 111 km');
});

test('the note gives the hours the frames really cover', () => {
  const frames = [{ time: '2026-11-05T12:10:00Z' }, { time: '2026-11-07T12:00:00Z' }];
  assert.equal(spanHours(frames), 48);
  assert.equal(spanHours([]), 0);
});
