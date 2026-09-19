// Tests for which marks are drawn, and which may be moved to stay legible.
//
// Run with:  node --test tests/marks.test.mjs
//
// Both of these are rules about honesty rather than about drawing, which is
// why they are worth pinning. A mark that is hidden is a report a reader
// never sees; a mark that is moved is a position nobody stated.

import test from 'node:test';
import assert from 'node:assert/strict';

import { driftKm, floats, glyphParts, regionOnly }
  from '../frontend/js/tracker.js';

// A drone raised from a warning about drones. It sits at the centre of a
// province because that is where the warning sits -- see _drone_from_warning
// in the backend.
const raised = { kind: 'drone', from_warning: true, region_scope: null,
                 area_only: false };

test('an ordinary region-only track is not drawn', () => {
  // Its position is the arithmetic centre of a province. A dot there is an
  // object in a field outside Lutsk, which is "random drones".
  assert.equal(regionOnly({ kind: 'drone', area_only: true }), true);
  assert.equal(regionOnly({ kind: 'drone', region_scope: 'located' }), true);
});

test('but a drone raised from a warning is', () => {
  // Ukraine has NEPTUN's real tracks, so a vague mark there would sit beside
  // better ones; the Russian side has one channel posting region-level
  // warnings, and the choice there is between a vague mark and an empty map.
  //
  // It gets through on what it is NOT marked as, rather than on a clause
  // naming it: the backend sets neither area_only nor region_scope on it.
  // Asserted here as the shape the backend actually sends -- if that changes,
  // the mark silently stops being drawn, and this is what notices.
  assert.equal(regionOnly(raised), false);
  assert.equal(raised.area_only, false);
  assert.equal(raised.region_scope, null);
});

test('a warning is always drawn', () => {
  // A warning IS about the whole region, so the region is its true extent
  // rather than a stand-in for one.
  assert.equal(regionOnly({ kind: 'alert', area_only: true }), false);
  assert.equal(regionOnly({ kind: 'alert', region_scope: 'located' }), false);
});

test('a drone reported at a town is drawn', () => {
  assert.equal(regionOnly({ kind: 'drone' }), false);
});

test('a mark that claims a point may not be moved', () => {
  // A drone reported over Myrhorod is at Myrhorod. Move it and the map says
  // something nobody said.
  assert.equal(floats({ kind: 'drone' }), false);
  assert.equal(floats({ kind: 'missile', region_scope: 'covers' }), false);
});

test('and a mark that never claimed one may', () => {
  assert.equal(floats({ kind: 'alert' }), true);
  assert.equal(floats({ kind: 'drone', area_only: true }), true);
  assert.equal(floats({ kind: 'drone', region_scope: 'located' }), true);
});

test('a drone raised from a warning may be moved', () => {
  // It is created at its warning's own position, so without this the two
  // land on the same pixel every single time -- which is the exact picture
  // the rule was written for: a drone sitting on top of a warning triangle.
  assert.equal(floats(raised), true);
});

// A drone over Russia points east when nobody said which way.
//
// The request, and the reasoning behind it is sound: what these channels
// report is long-range drones that came from the west, so on that side the
// traffic runs broadly eastwards. It is still an assumption, so it is drawn
// as one -- hollow, like a course borrowed from the group, which is the
// weight this map has always given an inference.

test('an assumed course is drawn hollow, like a borrowed one', () => {
  const assumed = glyphParts({ kind: 'drone', course_from: 'assumed' },
    '#ffd400', 90);
  const borrowed = glyphParts({ kind: 'drone', course_from: 'group' },
    '#ffd400', 90);
  assert.equal(assumed.shape, borrowed.shape);
  assert.equal(assumed.turn, 90);
});

test('and a stated course is still solid', () => {
  const stated = glyphParts({ kind: 'drone', course_from: 'stated' },
    '#ffd400', 90);
  const assumed = glyphParts({ kind: 'drone', course_from: 'assumed' },
    '#ffd400', 90);
  assert.notEqual(stated.shape, assumed.shape);
  assert.notEqual(stated.body, assumed.body);
});

test('a mark with no course at all is still a ring', () => {
  // The assumption is put on by the backend, for one side of one border.
  // Where it was not, the ring still means what it always meant.
  assert.equal(glyphParts({ kind: 'drone' }, '#ffd400', null).shape, 'dot');
});

test('nothing is ever carried along an assumed course', () => {
  // Dead reckoning runs on a source's own speed. This is the check that an
  // assumed heading cannot be flown along even if a speed turns up beside
  // it -- which is how the version of this that flew marks at a speed from a
  // lookup table got in.
  // drift_minutes is the field the feed actually sends; an event without it
  // never drifts whatever else it carries, so a test built on the wrong
  // field name would pass however this behaved.
  const base = { kind: 'drone', speed_kmh: 180, heading: 90,
                 drift_minutes: 10 };
  assert.ok(driftKm({ ...base, course_from: 'stated' }) > 0,
    'the fixture produces no drift at all, so it proves nothing');
  assert.equal(driftKm({ ...base, course_from: 'assumed' }), 0);
});
