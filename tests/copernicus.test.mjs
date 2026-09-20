// Tests for the live-satellite panel: which day it draws, and which products
// it offers.
//
// Run with:  node --test tests/copernicus.test.mjs
//
// Both of these are about a panel that had grown two problems at once. It
// opened on a week of passes composited into one picture -- which is of no
// particular day, on a layer whose whole point is that it is live -- and it
// listed every product EUMETSAT serves, which is forty buttons for the four
// anybody opens.

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { dayIndex, productsShown, stepTo, wmsTime }
  from '../frontend/js/copernicus.js';

const WEEK = ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17',
              '2026-09-18', '2026-09-19', '2026-09-20'];

// ── Live, and staying live ─────────────────────────────────────

test('nothing chosen means the newest day', () => {
  assert.equal(dayIndex(WEEK, null), WEEK.length - 1);
});

test('and it stays the newest day when a fresher one arrives', () => {
  // The panel reloads itself every quarter of an hour. Holding an index would
  // pin the picture to whatever today was when the panel opened, so that a tab
  // left open overnight quietly shows yesterday while saying "live".
  const tomorrow = [...WEEK, '2026-09-21'];
  assert.equal(WEEK[dayIndex(WEEK, null)], '2026-09-20');
  assert.equal(tomorrow[dayIndex(tomorrow, null)], '2026-09-21');
});

test('a day scrubbed back to is the day that is drawn', () => {
  assert.equal(WEEK[dayIndex(WEEK, 2)], '2026-09-16');
});

test('an index off either end lands on a real day', () => {
  assert.equal(dayIndex(WEEK, -4), 0);
  assert.equal(dayIndex(WEEK, 99), WEEK.length - 1);
});

test('no days at all is not an error', () => {
  assert.equal(dayIndex([], null), 0);
  assert.equal(dayIndex(undefined, 3), 0);
});

// ── Scrubbing ──────────────────────────────────────────────────

test('scrolling back walks a day at a time', () => {
  assert.equal(stepTo(WEEK, null, -1), WEEK.length - 2);
  assert.equal(stepTo(WEEK, 3, -1), 2);
});

test('and stops at the oldest day rather than running off it', () => {
  assert.equal(stepTo(WEEK, 0, -1), 0);
});

test('scrolling forward off the end goes back to live, not to an index', () => {
  // This is the whole of what keeps it live. Landing on the last index instead
  // would look identical today and be a day stale tomorrow.
  assert.equal(stepTo(WEEK, WEEK.length - 2, 1), null);
  assert.equal(stepTo(WEEK, null, 1), null);
});

test('a layer with no days cannot be scrubbed', () => {
  assert.equal(stepTo([], null, -1), null);
});

// ── What the WMS is asked for ──────────────────────────────────

test('a day is asked for as the range that covers it', () => {
  // A bare date is midnight exactly on some servers, which is one instant --
  // and one instant from a polar orbiter is one orbit strip.
  assert.equal(wmsTime({ days: WEEK }, 2),
               '2026-09-16T00:00:00Z/2026-09-16T23:59:59Z');
});

test('live asks for today', () => {
  assert.match(wmsTime({ days: WEEK }, null), /^2026-09-20T00:00:00Z\//);
});

test('a geostationary layer keeps its own latest frame', () => {
  // It has no days: every frame is already the whole disc, so there is nothing
  // to composite and nothing to scrub.
  assert.equal(wmsTime({ days: [], time_default: '2026-09-20T11:40:00Z' }, null),
               '2026-09-20T11:40:00Z');
});

test('and a layer with neither asks for no time at all', () => {
  assert.equal(wmsTime({ days: [] }, null), null);
});

// ── The shortlist ──────────────────────────────────────────────

const LAYERS = [
  { id: 'a', everyday: true },
  { id: 'b', everyday: false },
  { id: 'c', everyday: true },
  { id: 'd', everyday: false },
];

test('only the everyday products are listed', () => {
  assert.deepEqual(productsShown(LAYERS, false, 'a').map((l) => l.id),
                   ['a', 'c']);
});

test('asking for all of them gives all of them', () => {
  assert.equal(productsShown(LAYERS, true, 'a').length, 4);
});

test('whatever is drawn is always listed, shortlisted or not', () => {
  // Otherwise choosing a product from the full list and then collapsing it
  // leaves the map drawing something with no button to press to get back to.
  assert.deepEqual(productsShown(LAYERS, false, 'd').map((l) => l.id),
                   ['a', 'c', 'd']);
});

test('a backend that says nothing about everyday shows everything', () => {
  // An older backend, or a cached one. Hiding every product because a field is
  // missing would be an empty panel with no way to tell why.
  const old = [{ id: 'x' }, { id: 'y' }];
  assert.equal(productsShown(old, false, null).length, 2);
});

test('no layers is not an error', () => {
  assert.deepEqual(productsShown(undefined, false, null), []);
});

// ── The panel itself ───────────────────────────────────────────

const SOURCE = readFileSync(
  new URL('../frontend/js/copernicus.js', import.meta.url), 'utf8');

test('the week composite is gone from the panel', () => {
  assert.equal(SOURCE.includes('Whole week'), false);
  assert.equal(SOURCE.includes('whole_week'), false);
});

test('the dates can be scrolled', () => {
  // Asked for in as many words. The slider covers dragging; this is the wheel.
  assert.match(SOURCE, /onwheel: onWheel/);
  assert.match(SOURCE, /function onWheel/);
});

test('and scrolling the dates does not scroll the panel instead', () => {
  const block = SOURCE.slice(SOURCE.indexOf('function onWheel'));
  assert.match(block.slice(0, 500), /e\.preventDefault\(\)/);
});

test('scrubbing retimes the layer rather than rebuilding it', () => {
  // Seven rebuilds across a week flashes the map empty seven times.
  assert.match(SOURCE, /drawn\.setParams/);
});

test('the panel ends at its own width', () => {
  // Measured, not guessed: adding the date row made #copBody 273px of content
  // in a 204px panel, and every product button stretched with it, because a
  // grid column of width auto is as wide as its widest item and a range input
  // will not shrink on its own. minmax(0, 1fr) is the column refusing.
  //
  // A browser is what actually proves this, and the suite has none. What is
  // pinned here is the declaration, so the fix cannot be tidied away by
  // someone who has not seen what it was for.
  const css = readFileSync(
    new URL('../frontend/css/app.css', import.meta.url), 'utf8');
  const from = css.indexOf('.cop-body {');
  // Comments stripped first. The comment above the declaration explains it by
  // quoting it, so a match against the raw block passes with the declaration
  // deleted -- which is a test that reads the reason and calls it the rule.
  const block = css.slice(from, css.indexOf('}', from))
    .replace(/\/\*[\s\S]*?\*\//g, '');
  assert.match(block, /grid-template-columns:\s*minmax\(0,\s*1fr\)/);
});
