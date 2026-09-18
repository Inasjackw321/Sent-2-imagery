// Tests for what a mark is drawn as.
//
// Run with:  node --test tests/glyphs.test.mjs
//
// Two things are being held here at once, and they pull against each other.
//
// A drone should LOOK like a drone -- the reference maps draw a quadcopter,
// and a silhouette reads across a room where a hue does not, for a
// colour-blind viewer, on a washed-out phone, and over a shaded warning.
//
// And a mark must still say which way the thing is going. That is the most
// useful fact this layer holds and the hardest-won: a course is stated by a
// report, or borrowed from the group around it, or not known at all, and the
// three have been kept visually distinct through several redesigns. A
// quadcopter with no front would have thrown all of it away in exchange for
// looking right.
//
// So the nose is the course, and these are the tests that it stays that way.

import test from 'node:test';
import assert from 'node:assert/strict';

import { glyphParts, glyphSize, ROTARY_SCALE } from '../frontend/js/tracker.js';

const C = '#ffd400';
const parts = (event, facing) => glyphParts(event, C, facing);

const DRONES = ['drone', 'jet_drone', 'fpv', 'recon'];
const NOT_DRONES = ['missile', 'bomb', 'aircraft', 'unknown'];

test('every drone-ish kind is drawn with rotors', () => {
  for (const kind of DRONES) {
    assert.match(parts({ kind }, 40).shape, /^drone/, kind);
  }
});

test('and nothing else is', () => {
  // A missile keeps the arrow, so the two differ in silhouette as well as in
  // hue rather than in hue alone.
  for (const kind of NOT_DRONES) {
    assert.doesNotMatch(parts({ kind }, 40).shape, /^drone/, kind);
  }
  assert.equal(parts({ kind: 'alert' }, null).shape, 'warning');
});

test('a stated course points where it was stated', () => {
  const got = parts({ kind: 'drone' }, 137);
  assert.equal(got.shape, 'drone');
  assert.equal(got.turn, 137);
});

test('a borrowed course is drawn differently from a stated one', () => {
  // A solid arrow is what a report said; an outlined one is an inference
  // from its neighbours. The difference is on the map rather than only in
  // the popup because that is where it is read.
  const stated = parts({ kind: 'drone' }, 40);
  const borrowed = parts({ kind: 'drone', course_from: 'group' }, 40);
  assert.equal(borrowed.shape, 'drone-borrowed');
  assert.notEqual(stated.body, borrowed.body);
  assert.equal(borrowed.turn, 40);
});

// The nose and the airframe, told apart so each can be checked for itself.
// Everything after the last rotor circle is the nose.
const split = (body) => {
  const cut = body.lastIndexOf('<path d="M9 0');
  return cut < 0
    ? { frame: body, nose: '' }
    : { frame: body.slice(0, cut), nose: body.slice(cut) };
};

test('and it is the NOSE that differs, not the airframe', () => {
  // Both are the same aircraft. If the rotors differed too, the mark would
  // be saying something about what the thing is rather than about how well
  // its course is known.
  const stated = split(parts({ kind: 'drone' }, 40).body);
  const borrowed = split(parts({ kind: 'drone', course_from: 'group' }, 40).body);
  assert.ok(stated.frame.includes('<circle'));
  assert.equal(stated.frame, borrowed.frame);
});

test('a stated nose is filled and a borrowed one is hollow', () => {
  // The distinction itself, rather than "the two strings differ somewhere".
  // A version that moved it into the rotors' stroke width passed that
  // weaker test while drawing both noses solid.
  const stated = split(parts({ kind: 'drone' }, 40).body).nose;
  const borrowed = split(parts({ kind: 'drone', course_from: 'group' }, 40).body).nose;
  assert.ok(stated.includes(`fill="${C}"`), stated);
  assert.ok(!stated.includes('fill="none"'), stated);
  assert.ok(borrowed.includes('fill="none"'), borrowed);
  assert.ok(borrowed.includes('stroke='), borrowed);
});

test('no course means no nose and no rotation', () => {
  // The invention this whole layer exists to avoid. A nose here would point
  // north and mean it.
  const got = parts({ kind: 'drone' }, null);
  assert.equal(got.shape, 'drone-adrift');
  assert.equal(got.turn, null, 'a courseless mark must not be turned');
  assert.ok(!got.body.includes('M9 0'), 'a courseless drone has a nose');
});

test('a courseless drone is still recognisably the same aircraft', () => {
  // It loses its nose, not its rotors: "something is here and its course is
  // not known" rather than "something unidentified is here".
  const adrift = parts({ kind: 'drone' }, null).body;
  assert.equal((adrift.match(/<circle/g) ?? []).length, 4);
});

test('a drone is drawn bigger than a triangle, and by a stated ratio', () => {
  // Measured rather than judged by eye: rendered side by side at 24, 34 and
  // 64 pixels, only the larger two could tell a solid nose from an outlined
  // one from none. See ROTARY_SCALE.
  assert.ok(ROTARY_SCALE > 1);
  for (const kind of DRONES) {
    assert.equal(glyphSize({ kind }), Math.round(24 * ROTARY_SCALE), kind);
  }
  for (const kind of [...NOT_DRONES, 'alert']) {
    assert.equal(glyphSize({ kind }), 24, kind);
  }
});

test('an unknown kind is sized like the ordinary marks', () => {
  for (const event of [{}, null, undefined, { kind: 'nonsense' }]) {
    assert.equal(glyphSize(event), 24, JSON.stringify(event));
  }
});

test('the artwork carries the colour it was given', () => {
  // The picture export and the map share this function, so a colour baked in
  // here would be a colour the key could not explain.
  for (const kind of DRONES) {
    assert.ok(parts({ kind }, 10).body.includes(C), kind);
  }
});

test('every drawing is a closed set of parts the caller can use', () => {
  for (const kind of [...DRONES, ...NOT_DRONES, 'alert']) {
    for (const facing of [null, 0, 180]) {
      const got = parts({ kind }, facing);
      assert.equal(typeof got.shape, 'string');
      assert.ok(got.shape.length, kind);
      assert.ok(got.body.includes('<'), `${kind} ${facing}`);
      assert.ok(got.turn === null || Number.isFinite(got.turn),
        `${kind} ${facing}`);
    }
  }
});
