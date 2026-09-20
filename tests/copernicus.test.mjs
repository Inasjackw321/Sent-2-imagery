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

// Set BEFORE anything constructs a Date. Every time on the bar is the
// satellite's, in UTC, and a test that runs in UTC cannot tell a label that
// says so from one that happens to agree -- so this suite runs fourteen hours
// ahead, where the two disagree about what day it is.
process.env.TZ = 'Pacific/Kiritimati';

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { clockLabel, dateLabel, frameIndex, framesPer, nextFrame,
         productsShown, stepFrame, wmsTime }
  from '../frontend/js/copernicus.js';

// A week of whole days, as a satellite that flies over offers them.
const WEEK = ['2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17',
              '2026-09-18', '2026-09-19', '2026-09-20'].map((day) => ({
  label: day, at: day, time: `${day}T00:00:00Z/${day}T23:59:59Z` }));

// And four frames ten minutes apart, as one that stares does.
const DISC = ['17:30', '17:40', '17:50', '18:00'].map((hm) => ({
  label: hm, at: '2026-09-20', time: `2026-09-20T${hm}:00Z` }));

// ── Live, and staying live ─────────────────────────────────────

test('nothing chosen means the newest frame', () => {
  assert.equal(frameIndex(WEEK, null), WEEK.length - 1);
});

test('and it stays the newest when a fresher one arrives', () => {
  // The panel reloads itself every quarter of an hour, and Meteosat publishes
  // every ten minutes. Holding an index would pin the picture to whatever was
  // newest when the panel opened, so a tab left open quietly falls behind
  // while the button still reads live.
  const later = [...DISC, { label: '18:10', at: '2026-09-20',
                            time: '2026-09-20T18:10:00Z' }];
  assert.equal(DISC[frameIndex(DISC, null)].label, '18:00');
  assert.equal(later[frameIndex(later, null)].label, '18:10');
});

test('a frame scrubbed back to is the frame that is drawn', () => {
  assert.equal(WEEK[frameIndex(WEEK, 2)].label, '2026-09-16');
});

test('an index off either end lands on a real frame', () => {
  assert.equal(frameIndex(WEEK, -4), 0);
  assert.equal(frameIndex(WEEK, 99), WEEK.length - 1);
});

test('no frames at all is not an error', () => {
  assert.equal(frameIndex([], null), 0);
  assert.equal(frameIndex(undefined, 3), 0);
});

// ── Scrubbing ──────────────────────────────────────────────────

test('scrolling back walks a frame at a time', () => {
  assert.equal(stepFrame(WEEK, null, -1), WEEK.length - 2);
  assert.equal(stepFrame(WEEK, 3, -1), 2);
});

test('and stops at the oldest frame rather than running off it', () => {
  assert.equal(stepFrame(WEEK, 0, -1), 0);
});

test('scrolling forward off the end goes back to live, not to an index', () => {
  // This is the whole of what keeps it live. Landing on the last index would
  // look identical now and be a frame stale ten minutes later.
  assert.equal(stepFrame(WEEK, WEEK.length - 2, 1), null);
  assert.equal(stepFrame(WEEK, null, 1), null);
});

test('a layer with no frames cannot be scrubbed', () => {
  assert.equal(stepFrame([], null, -1), null);
});

// ── The loop ───────────────────────────────────────────────────

test('playing on past the last frame comes round to the first', () => {
  assert.equal(nextFrame(DISC, DISC.length - 1), 0);
  assert.equal(nextFrame(DISC, 0), 1);
});

test('and it never resolves to live', () => {
  // The one thing the loop must not do. Live is a moving target, so a loop
  // that landed on it would stop replaying the same few hours and start
  // following the clock -- drifting a frame later on every refresh.
  for (let at = 0; at < DISC.length; at += 1) {
    assert.notEqual(nextFrame(DISC, at), null, `from ${at}`);
  }
  assert.equal(nextFrame(DISC, null), 0);
});

test('a single frame is not a loop', () => {
  assert.equal(nextFrame([], null), null);
});

test('the hour dial steps an hour whatever the cadence is', () => {
  assert.equal(framesPer(60, 10), 6);
  assert.equal(framesPer(60, 15), 4);
  assert.equal(framesPer(60, 60), 1);
});

test('and never stands still', () => {
  // A cadence of a day would otherwise round an hour to zero frames, and the
  // button would be there doing nothing.
  assert.equal(framesPer(60, 1440), 1);
  assert.equal(framesPer(60, 0), 1);
  assert.equal(framesPer(60, undefined), 1);
});

// ── What the WMS is asked for ──────────────────────────────────

test('a day is asked for as the range that covers it', () => {
  // A bare date is midnight exactly on some servers, which is one instant --
  // and one instant from a polar orbiter is one orbit strip.
  assert.equal(wmsTime({ frames: WEEK }, 2),
               '2026-09-16T00:00:00Z/2026-09-16T23:59:59Z');
});

test('and an instant as an instant', () => {
  assert.equal(wmsTime({ frames: DISC }, 1), '2026-09-20T17:40:00Z');
});

test('live asks for the newest frame', () => {
  assert.equal(wmsTime({ frames: DISC }, null), '2026-09-20T18:00:00Z');
});

test('a layer with no frames keeps its own latest', () => {
  assert.equal(wmsTime({ frames: [], time_default: '2026-09-20T11:40:00Z' }, null),
               '2026-09-20T11:40:00Z');
});

test('and one with neither asks for no time at all', () => {
  assert.equal(wmsTime({ frames: [] }, null), null);
});

// ── What the bar reads ─────────────────────────────────────────

test('the date is the day the frame falls on', () => {
  assert.equal(dateLabel(DISC[0]), '20 Sept');
});

test('and it is read in UTC, not in whatever zone the reader is in', () => {
  // Every time on this bar is the satellite's. A browser far enough east
  // reading a UTC date as local shows the wrong day -- and this suite is set
  // to UTC+14 above so that the two genuinely differ here.
  assert.notEqual(Intl.DateTimeFormat().resolvedOptions().timeZone, 'UTC');
  assert.equal(dateLabel({ at: '2026-01-01' }), '1 Jan');
});

test('a missing date is blank rather than "Invalid Date"', () => {
  assert.equal(dateLabel(undefined), '');
  assert.equal(dateLabel({ at: '' }), '');
});

test('a satellite that stares reads a clock', () => {
  assert.equal(clockLabel({ animates: true }, DISC[3]), '18:00');
});

test('and one that flies over says so instead of reading 00:00', () => {
  // The frame is a whole day of passes. 00:00 is not when it passed over --
  // it is where the range this app asked for happens to start, which is a
  // number nobody can act on.
  assert.equal(clockLabel({ animates: false }, WEEK[0]), 'all day');
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

test('the times can be scrolled', () => {
  // Asked for in as many words. Bound once on the bar rather than per render,
  // so it survives the bar being rebuilt on every frame of the loop.
  assert.match(SOURCE, /addEventListener\('wheel', onWheel/);
  assert.match(SOURCE, /function onWheel/);
});

test('and the wheel listener can actually stop the page scrolling', () => {
  // A wheel listener is passive by default in this position, and a passive
  // one calling preventDefault does nothing at all except warn.
  assert.match(SOURCE, /addEventListener\('wheel', onWheel, \{ passive: false \}\)/);
  const block = SOURCE.slice(SOURCE.indexOf('function onWheel'));
  assert.match(block.slice(0, 500), /e\.preventDefault\(\)/);
});

test('scrolling stops the loop rather than fighting it', () => {
  const block = SOURCE.slice(SOURCE.indexOf('function onWheel'));
  assert.match(block.slice(0, 500), /pause\(\)/);
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
