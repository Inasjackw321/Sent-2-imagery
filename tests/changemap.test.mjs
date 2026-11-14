// The change map panel's words and addresses.

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  SAVES, areaSaid, datePresets, daySaid, exportUrl, fileName, imagesSaid, isoDay, planSaid,
} from '../frontend/js/changemap.js';

test('the quick dates end today and reach back a month, a quarter and a year', () => {
  const p = datePresets(new Date('2026-11-14T15:00:00Z'));
  assert.deepEqual([p.month.start, p.month.end], ['2026-10-15', '2026-11-14']);
  assert.equal(p.quarter.start, '2026-08-15');
  assert.equal(p.year.start, '2025-11-14');
  assert.equal(isoDay('2026-01-02T23:00:00Z'), '2026-01-02');
});

test('images per side are explained', () => {
  assert.match(imagesSaid(1), /quickest/);
  assert.match(imagesSaid(3), /good balance/);
  assert.match(imagesSaid(6), /smaller changes/);
  assert.match(imagesSaid(20), /most sensitive/);
});

test('days, areas and the plan are said plainly', () => {
  assert.equal(daySaid('2026-07-04'), '4 Jul 2026');
  assert.equal(areaSaid([30.2, 50.5, 30.26, 50.54]), '4.2 × 4.4 km');
  const said = planSaid({ before: ['2026-06-22', '2026-07-04'], after: ['2026-08-03'],
    download_mb: 1234, saved: false });
  assert.equal(said.before, 'Before: 2 passes — 22 Jun 2026, 4 Jul 2026');
  assert.equal(said.after, 'After: 1 pass — 3 Aug 2026');
  assert.match(said.size, /1\.2 GB/);
  assert.match(planSaid({ before: [], after: [], download_mb: 5, saved: true }).size, /instantly/);
});

test('saving: every kind, its address and its filename', () => {
  assert.deepEqual(SAVES.map(([k]) => k), ['png', 'csv', 'geojson', 'tif', 'zip']);
  const url = new URL(exportUrl('0123456789abcdef', 'zip', { preset: 'Strict', watermark: '@me' }),
    'http://x');
  assert.equal(url.pathname, '/api/change/export/0123456789abcdef');
  assert.equal(url.searchParams.get('kind'), 'zip');
  assert.equal(url.searchParams.get('preset'), 'Strict');
  assert.equal(url.searchParams.get('watermark'), '@me');
  const result = { label: 'Irpin, Kyiv Oblast', params: { start: '2026-07-04', end: '2026-08-04' } };
  assert.equal(fileName(result, 'tif'), 'change_Irpin_Kyiv_Oblast_2026-07-04_2026-08-04.tif');
  assert.equal(fileName(result, 'jpeg').slice(-4), '.jpg');
});
